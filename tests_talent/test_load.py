"""Load test (opt-in: TALENT_LOAD_TEST=1) — a 40,000-profile refresh backfill
completes within the daily send caps, with no queue starvation.

Real code paths: campaigns.sweep_org (rolling enrolment by priority + dispatch
within the day's budget), caps.reserve (the atomic per-day reservation every
send makes), campaigns.finish_member. Simulated: the clock (one sweep a day
on the default 500 → 2,000 → 5,000 ramp), candidate responses (30% answer
touch 1, 10% touch 2), and the transport. Each day 150 transactional sends
(invites, reminders) must also get through — the bulk refresh must never
starve them — and every due follow-up is sent before new people are started,
so no cohort waits behind newer ones.

Also times the embedding backfill over the same 40,000 profiles.
"""
import os
import random
import secrets
import time
from collections import defaultdict
from datetime import datetime, timedelta

import pytest
import sqlalchemy as sa

from app.models import ResumeMetadata
from app.models.talent import CampaignMember, ContactConsent, SendCounter
from app.talent import backfill, caps, campaigns

pytestmark = pytest.mark.skipif(os.environ.get("TALENT_LOAD_TEST") != "1", reason="set TALENT_LOAD_TEST=1")

N = 40_000
TRANSACTIONAL_PER_DAY = 150
GAPS = (3, 4)  # days between touches
FINAL_WAIT = 4


def _bulk_profiles(db, org, now):
    rng = random.Random(1)
    t = ResumeMetadata.__table__
    for start in range(0, N, 5000):
        rows = []
        for i in range(start, min(N, start + 5000)):
            skills = ["Kafka", "Python"] if i % 5 == 0 else ["Excel", "SQL"]
            rows.append({
                "organization_id": org.id, "candidate_email": f"p{i}-{secrets.token_hex(3)}@load.test",
                "candidate_name": f"Person {i}", "parsing_status": "completed", "verification_status": "stale",
                "email_status": "valid", "skills": str(skills).replace("'", '"'),
                "skills_index": "|" + "|".join(skills) + "|", "pipeline_status": "new",
                "created_at": now - timedelta(days=rng.randint(200, 1500)), "current_designation": "Engineer",
            })
        db.execute(t.insert(), rows)
    db.commit()
    ids = [i for (i,) in db.query(ResumeMetadata.id).filter(ResumeMetadata.organization_id == org.id)]
    ct = ContactConsent.__table__
    for start in range(0, len(ids), 5000):
        db.execute(ct.insert(), [{"profile_id": pid, "channel": "email", "purpose": "refresh", "status": "granted",
                                  "source": "load", "granted_at": now} for pid in ids[start:start + 5000]])
    db.commit()
    return ids


def test_40k_refresh_backfill_within_caps_without_starvation(world, db):
    base = datetime(2026, 11, 2, 4, 0)  # 09:30 IST, inside the sending window
    org = world.org("LoadOrg")
    world.enable(org, "refresh", "refresh_rolling")
    world.job(org, skills=("Kafka",))
    t0 = time.time()
    ids = _bulk_profiles(db, org, base)
    assert len(ids) == N
    domain = "agent.test"
    dom = caps.get_domain(db, domain, base.date())
    dom.ramp_started_on = base.date()
    db.commit()

    rng = random.Random(7)
    state = {}  # profile_id -> {"campaign": id, "sent": n, "due": day, "first_due": day}
    worst_delay = 0
    daily = []
    finished = 0
    day = 0
    while finished < N and day < 120:
        now = base + timedelta(days=day)
        # 1. Transactional mail always gets through.
        granted = caps.reserve_many(db, domain, caps.TRANSACTIONAL, TRANSACTIONAL_PER_DAY, now)
        assert granted == TRANSACTIONAL_PER_DAY, f"transactional starved on day {day}: {granted}"
        # 2. Follow-ups that are due, oldest first, before anyone new is started.
        due = sorted((s["due"], pid) for pid, s in state.items() if s["due"] is not None and s["due"] <= day)
        to_send = []
        for _, pid in due:
            s = state[pid]
            if s["sent"] == 3:  # final wait over: no response
                campaigns.finish_member(db, s["campaign"], pid, "no_response")
                s["due"] = None
                finished += 1
            else:
                to_send.append(pid)
        granted = caps.reserve_many(db, domain, caps.BULK, len(to_send), now)
        for pid in to_send[:granted]:  # the rest wait for tomorrow
            s = state[pid]
            worst_delay = max(worst_delay, day - s["due"])
            s["sent"] += 1
            if s["sent"] == 2 and rng.random() < 0.10:
                campaigns.finish_member(db, s["campaign"], pid, "responded")
                s["due"] = None
                finished += 1
            else:
                s["due"] = day + (GAPS[1] if s["sent"] == 2 else FINAL_WAIT)
        db.commit()
        # 3. Today's sweep: enrol + dispatch within what is left of the budget.
        result = campaigns.sweep_org(db, org.id, rolling=True, now=now)
        granted = caps.reserve_many(db, domain, caps.BULK, len(result["dispatch"]), now)
        assert granted == len(result["dispatch"]), "a dispatched touch 1 found no capacity"
        for campaign_id, pid in result["dispatch"]:
            if rng.random() < 0.30:
                campaigns.finish_member(db, campaign_id, pid, "responded")
                finished += 1
                state[pid] = {"campaign": campaign_id, "sent": 1, "due": None}
            else:
                state[pid] = {"campaign": campaign_id, "sent": 1, "due": day + GAPS[0]}
        db.commit()
        counter = db.get(SendCounter, (domain, now.date()))
        cap, bulk_cap = caps.cap_for(dom, now.date()), caps.bulk_cap_for(dom, now.date())
        assert counter.total <= cap and counter.bulk <= bulk_cap, f"day {day}: {counter.total}/{cap}, bulk {counter.bulk}/{bulk_cap}"
        daily.append((day, cap, counter.total, counter.bulk, len(result["dispatch"])))
        day += 1

    assert finished == N, f"only {finished} of {N} finished in {day} days"
    assert len(state) == N
    open_members = db.query(sa.func.count()).select_from(CampaignMember).filter(
        CampaignMember.status.in_(("queued", "in_progress"))).scalar()
    assert open_members == 0
    assert worst_delay <= 1, f"a follow-up waited {worst_delay} days past its due date"
    print(f"\n40,000 profiles refreshed in {day} simulated days ({time.time() - t0:.0f}s wall clock); "
          f"worst follow-up delay {worst_delay} day(s)")
    for d, cap, total, bulk, started in daily[::7]:
        print(f"  day {d:>3}: cap {cap:>5}, sent {total:>5} (bulk {bulk:>5}), new sequences {started:>5}")


def test_40k_embedding_backfill(world, db):
    base = datetime(2026, 11, 2, 4, 0)
    org = world.org("EmbedOrg")
    _bulk_profiles(db, org, base)
    t0 = time.time()
    made = 0
    while True:
        n = backfill.ensure_baselines(db, org.id, limit=2000)
        made += n
        if n == 0:
            break
    t1 = time.time()
    embedded = 0
    while True:
        n = backfill.embed_chunk(db, org.id, size=500)
        embedded += n
        if n == 0:
            break
    t2 = time.time()
    assert made == N and embedded == N
    print(f"\nbaselines: {made} in {t1 - t0:.0f}s; embeddings: {embedded} in {t2 - t1:.0f}s "
          f"({N // 500} steps of 500 — within Inngest's step cap)")
