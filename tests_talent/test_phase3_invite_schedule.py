"""Phase 3 — Loop C invites (copilot approvals) and Loop D scheduling."""
import threading
import uuid
from datetime import datetime, timedelta

import inngest
import pytest
from inngest.experimental import mocked

from app.database import SessionLocal
from app.models.talent import (
    AuditLog, InterviewBooking, JobMatch, TalentApproval, TalentInterview, TalentJobSettings, Touch,
)
from app.models.user import UserRole
from app.talent import approvals, invites, scheduling, tokens
from tests_talent.conftest import CapturingInngest


def _ranked(world, db, org, *, autonomy="copilot", n=2, panel=None, **job_settings):
    world.enable(org, "match", "invite", "interview")
    job = world.job(org)
    db.merge(TalentJobSettings(job_id=job.id, autonomy_mode=autonomy, top_k=10, panel_user_ids=panel,
                               interview_mode="async_text", slot_minutes=45, **job_settings))
    db.commit()
    matches = []
    for i in range(n):
        p = world.profile(org, email=f"c{i}-{uuid.uuid4().hex[:4]}@acme.com", email_status="valid",
                          verification_status="fresh", candidate_name=f"Cand {i}")
        world.consent(p)
        m = JobMatch(job_id=job.id, profile_id=p.id, organization_id=org.id, ai_score=90 - i, status="ranked")
        db.add(m)
        matches.append(m)
    db.commit()
    return job, matches


def _trigger(fn, name, data, stubs=None, client=None):
    res = mocked.trigger(fn, inngest.Event(name=name, data=data), client or CapturingInngest(), step_stubs=stubs or {})
    assert res.status is mocked.Status.COMPLETED, res.error
    return res.output


# ── Loop C ──────────────────────────────────────────────────────────────────

def test_copilot_never_invites_before_a_recruiter_approves(client, world, db, outbox):
    from app.agent.invite_plan import invite_plan
    from app.agent.invite_send import invite_send

    org = world.org()
    recruiter = world.user(org, UserRole.recruiter)
    job, (m1, m2) = _ranked(world, db, org)
    sent_client = CapturingInngest()
    out = _trigger(invite_plan, "talent/match.completed", {"job_id": job.id}, client=sent_client)
    assert out["approvals"] == 2 and out["invite"] == 0 and sent_client.sent == []
    db.expire_all()
    assert {m.status for m in db.query(JobMatch)} == {"approval_pending"}

    # Even if something asks the sequence to run, nothing is sent without an approval.
    res = _trigger(invite_send, "talent/invite.send", {"match_id": str(m1.id)})
    assert res == {"stopped": "not_approved"} and outbox == []
    assert db.query(AuditLog).filter_by(action="invite.blocked").count() == 1

    pending = client.get(f"/talent/approvals?job_id={job.id}", headers=world.headers(recruiter)).json()
    assert len(pending) == 2 and pending[0]["match"]["profile"]["name"]
    r = client.post("/talent/approvals/decide", json={"approval_ids": [pending[0]["id"]], "approve": True},
                    headers=world.headers(recruiter))
    assert r.json() == {"decided": 1, "invites_started": 1}
    approved_match = pending[0]["match"]["id"]
    res = _trigger(invite_send, "talent/invite.send", {"match_id": approved_match},
                   stubs={"wait-answer": None, "wait-answer-2": None})
    assert res == {"closed": True}
    assert [m["subject"].split(" — ")[0] for m in outbox] == [f"{job.title} at {org.name}",
                                                              f"Reminder: are you interested in the {job.title} role?"]
    client.post("/talent/approvals/decide", json={"approval_ids": [pending[1]["id"]], "approve": False},
                headers=world.headers(recruiter))
    db.expire_all()
    declined = db.get(JobMatch, uuid.UUID(pending[1]["match"]["id"]))
    assert declined.status == "closed" and len(outbox) == 2  # declining sends nothing
    decided = db.query(TalentApproval).filter(TalentApproval.status != "pending").all()
    assert all(a.decided_by == recruiter.id for a in decided)


def test_autopilot_invites_and_the_candidate_answers(client, world, db, outbox, events):
    from app.agent.invite_plan import invite_plan
    from app.agent.invite_send import invite_send

    org = world.org()
    job, (m1, m2) = _ranked(world, db, org, autonomy="autopilot")
    capture = CapturingInngest()
    out = _trigger(invite_plan, "talent/match.completed", {"job_id": job.id}, client=capture)
    assert out["invite"] == 2 and len(capture.sent) == 2
    stop = {"name": "talent/invite.responded", "data": {"match_id": str(m1.id), "interested": True}}
    assert _trigger(invite_send, "talent/invite.send", {"match_id": str(m1.id)}, stubs={"wait-answer": stop}) == {"answered": True}
    _trigger(invite_send, "talent/invite.send", {"match_id": str(m2.id)}, stubs={"wait-answer": stop})
    assert len(outbox) == 2

    invite_token = [w for w in outbox[0]["text"].split() if "/t/invite/" in w][0].rsplit("/", 1)[-1]
    page = client.get(f"/talent/public/invite/{invite_token}").json()
    assert page["job"]["title"] == job.title and page["open"]
    r = client.post(f"/talent/public/invite/{invite_token}",
                    json={"interested": True, "current_ctc": 12, "expected_ctc": 18, "notice_period_days": 30})
    assert r.status_code == 200 and "/t/interview/" in r.json()["interview_url"]
    db.expire_all()
    m = db.get(JobMatch, m1.id)
    assert m.response == "interested" and m.confirmed["expected_ctc"] == 18
    iv = db.query(TalentInterview).filter_by(match_id=m1.id).one()
    assert iv.status == "offered" and db.query(InterviewBooking).filter_by(interview_id=iv.id).count() == 3
    assert ("talent/invite.responded", {"match_id": str(m1.id), "interested": True}) in events
    assert client.post(f"/talent/public/invite/{invite_token}", json={"interested": False}).status_code == 404

    other_token = [w for w in outbox[1]["text"].split() if "/t/invite/" in w][0].rsplit("/", 1)[-1]
    client.post(f"/talent/public/invite/{other_token}", json={"interested": False})
    db.expire_all()
    assert db.get(JobMatch, m2.id).status == "declined"
    assert db.query(TalentInterview).filter_by(match_id=m2.id).count() == 0


def test_contactless_matches_are_never_queued(world, db):
    org = world.org()
    job, (m1, m2) = _ranked(world, db, org)
    from app.models.talent import ContactConsent

    db.query(ContactConsent).filter_by(profile_id=m2.profile_id, purpose="jobs").update({"status": "withdrawn"})
    db.commit()
    plan = invites.plan(db, job.id)
    assert plan["approvals"] == 1
    assert db.get(JobMatch, m2.id).status == "ranked"


# ── Loop D ──────────────────────────────────────────────────────────────────

def _interested(db, match):
    match.response = "interested"
    db.commit()
    interview, url = scheduling.offer_interview(db, match.id)
    return interview, url.rsplit("/", 1)[-1]


def test_double_booking_is_impossible(world, db, monkeypatch):
    monkeypatch.setattr(scheduling, "google_busy", lambda user, s, e: [])
    org = world.org()
    panelist = world.user(org, UserRole.recruiter)
    job, (m1, m2) = _ranked(world, db, org, panel=[panelist.id])
    iv1, _ = _interested(db, m1)
    iv2, _ = _interested(db, m2)
    slots1 = db.query(InterviewBooking).filter_by(interview_id=iv1.id).order_by(InterviewBooking.starts_at).all()
    slots2 = db.query(InterviewBooking).filter_by(interview_id=iv2.id).order_by(InterviewBooking.starts_at).all()
    assert slots1[0].starts_at == slots2[0].starts_at and slots1[0].owner_user_id == panelist.id  # same free time offered

    scheduling.book(db, iv1.id, slots1[0].id)
    with pytest.raises(scheduling.SlotTaken):
        scheduling.book(db, iv2.id, slots2[0].id)
    db.expire_all()
    assert db.get(TalentInterview, iv2.id).status == "offered"
    scheduling.book(db, iv2.id, slots2[1].id)  # a different time is fine


def test_concurrent_bookings_for_one_panelist_cannot_both_win(world, db, monkeypatch):
    monkeypatch.setattr(scheduling, "google_busy", lambda user, s, e: [])
    org = world.org()
    panelist = world.user(org, UserRole.recruiter)
    job, matches = _ranked(world, db, org, panel=[panelist.id], n=4)
    interviews = [_interested(db, m)[0] for m in matches]
    targets = [db.query(InterviewBooking).filter_by(interview_id=iv.id).order_by(InterviewBooking.starts_at).first()
               for iv in interviews]
    assert len({t.starts_at for t in targets}) == 1
    results, barrier = [], threading.Barrier(len(targets))

    def attempt(iv_id, slot_id):
        s = SessionLocal()
        try:
            barrier.wait()
            scheduling.book(s, iv_id, slot_id)
            results.append("ok")
        except scheduling.SlotTaken:
            results.append("taken")
        finally:
            s.close()

    threads = [threading.Thread(target=attempt, args=(iv.id, t.id)) for iv, t in zip(interviews, targets)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results) == ["ok", "taken", "taken", "taken"]
    assert db.query(InterviewBooking).filter_by(owner_user_id=panelist.id, status="booked").count() == 1


def test_busy_calendars_and_unreadable_calendars(world, db, monkeypatch):
    org = world.org()
    panelist = world.user(org, UserRole.recruiter)
    job, (m1, m2) = _ranked(world, db, org, panel=[panelist.id])
    monkeypatch.setattr(scheduling, "google_busy", lambda user, s, e: None)  # can't read it
    iv, _ = _interested(db, m1)
    assert db.query(InterviewBooking).filter_by(interview_id=iv.id).count() == 0
    db.expire_all()
    assert db.get(TalentInterview, iv.id).needs_recruiter is True

    free = scheduling.candidate_slots(db, job)
    assert free == []
    first = None
    monkeypatch.setattr(scheduling, "google_busy", lambda user, s, e: [])
    first = scheduling.candidate_slots(db, job)[0]
    monkeypatch.setattr(scheduling, "google_busy", lambda user, s, e: [(first[0] - timedelta(minutes=5), first[1])])
    assert scheduling.candidate_slots(db, job)[0][0] != first[0]  # the busy time is never offered


def test_calendar_event_never_invites_the_candidate(world, db, monkeypatch):
    calls = []
    monkeypatch.setattr(scheduling, "google_busy", lambda user, s, e: [])
    import app.services.google_calendar_service as gcal

    monkeypatch.setattr(gcal, "create_meet_event", lambda *a, **kw: calls.append(kw) or {"event_id": "ev1",
                                                                                         "meet_link": "https://meet"})
    org = world.org()
    panelist = world.user(org, UserRole.recruiter)
    panelist.google_access_token, panelist.google_refresh_token = "a", "r"
    db.commit()
    job, (m1, _) = _ranked(world, db, org, panel=[panelist.id])
    iv, _ = _interested(db, m1)
    slot = db.query(InterviewBooking).filter_by(interview_id=iv.id).first()
    scheduling.book(db, iv.id, slot.id)
    assert calls and calls[0]["attendee_email"] == ""
    db.expire_all()
    assert db.get(TalentInterview, iv.id).calendar_event_id == "ev1"


def test_reschedule_once_then_a_recruiter_steps_in(client, world, db, events):
    org = world.org()
    recruiter = world.user(org, UserRole.recruiter)
    job, (m1, _) = _ranked(world, db, org)
    iv, token = _interested(db, m1)
    state = client.get(f"/talent/public/interview/{token}").json()
    assert len(state["slots"]) == 3 and state["can_reschedule"] and "AI interviewer" in state["disclosure"]
    state = client.post(f"/talent/public/interview/{token}/book", json={"slot_id": state["slots"][0]["id"]}).json()
    assert state["status"] == "scheduled" and state["scheduled_label"]
    state = client.post(f"/talent/public/interview/{token}/reschedule").json()
    assert state["status"] == "offered" and len(state["slots"]) == 3 and not state["can_reschedule"]
    client.post(f"/talent/public/interview/{token}/book", json={"slot_id": state["slots"][1]["id"]})
    r = client.post(f"/talent/public/interview/{token}/reschedule")
    assert r.status_code == 409 and "recruiter" in r.json()["detail"]
    db.expire_all()
    assert db.get(TalentInterview, iv.id).needs_recruiter is True
    flagged = client.get("/talent/interviews?needs_recruiter=true", headers=world.headers(recruiter)).json()
    assert [f["id"] for f in flagged] == [str(iv.id)]
    r = client.post(f"/talent/interviews/{iv.id}/reschedule", headers=world.headers(recruiter))
    assert r.status_code == 200 and r.json()["status"] == "offered" and not r.json()["needs_recruiter"]
    assert sum(1 for n, _ in events if n == "talent/interview.rescheduled") == 2


def test_no_show_reoffers_once_then_flags(world, db, outbox):
    org = world.org()
    job, (m1, _) = _ranked(world, db, org)
    iv, _ = _interested(db, m1)
    slot = db.query(InterviewBooking).filter_by(interview_id=iv.id).first()
    scheduling.book(db, iv.id, slot.id)
    later = slot.ends_at + timedelta(hours=1)
    assert scheduling.no_show_check(db, str(iv.id), 0, now=slot.ends_at)["status"] == "too_early"
    assert scheduling.no_show_check(db, str(iv.id), 0, now=later) == {"status": "reoffered"}
    db.expire_all()
    iv_db = db.get(TalentInterview, iv.id)
    assert iv_db.status == "offered" and iv_db.reschedule_count == 1 and iv_db.no_show_count == 1
    assert scheduling.send_interview_message(db, str(iv.id), "interview_missed", 1)["status"] == "sent"
    assert "We missed you" in outbox[-1]["subject"]
    slot2 = db.query(InterviewBooking).filter_by(interview_id=iv.id, status="offered").first()
    scheduling.book(db, iv.id, slot2.id)
    assert scheduling.no_show_check(db, str(iv.id), 1, now=slot2.ends_at + timedelta(hours=1)) == {"status": "flagged"}
    db.expire_all()
    iv_db = db.get(TalentInterview, iv.id)
    assert iv_db.status == "no_show" and iv_db.needs_recruiter
    assert db.query(InterviewBooking).filter_by(interview_id=iv.id, status="booked").count() == 0


def test_reminders_function_sends_confirmation_and_reminders(world, db, outbox):
    from app.agent.interview_reminders import interview_reminders
    from app.agent.interview_schedule import interview_schedule

    org = world.org()
    job, (m1, m2) = _ranked(world, db, org)
    iv, _ = _interested(db, m1)
    out = _trigger(interview_schedule, "talent/interview.offered", {"interview_id": str(iv.id), "attempt": 0, "key": "k"},
                   stubs={"wait-booking": {"name": "talent/interview.booked", "data": {"interview_id": str(iv.id)}}})
    assert out == {"booked": True} and outbox[-1]["subject"].startswith("Choose a time")
    slot = db.query(InterviewBooking).filter_by(interview_id=iv.id).order_by(InterviewBooking.starts_at).first()
    scheduling.book(db, iv.id, slot.id)
    out = _trigger(interview_reminders, "talent/interview.booked", {"interview_id": str(iv.id), "attempt": 0, "key": "k"})
    subjects = [m["subject"] for m in outbox]
    assert any(s.startswith("Confirmed:") for s in subjects)
    assert any("tomorrow" in s for s in subjects) and any("in one hour" in s for s in subjects)
    assert out["status"] in ("too_early", "moved_on", "started", "reoffered", "flagged")

    # A slot two hours away: the 24-hour reminder would be wrong, so it is skipped.
    iv2, _ = _interested(db, m2)
    slot2 = db.query(InterviewBooking).filter_by(interview_id=iv2.id).first()
    soon = datetime.utcnow() + timedelta(hours=2)
    slot2.starts_at, slot2.ends_at = soon, soon + timedelta(minutes=45)
    db.commit()
    scheduling.book(db, iv2.id, slot2.id)
    assert scheduling.send_interview_message(db, str(iv2.id), "interview_reminder_24h", 0,
                                             db.get(TalentInterview, iv2.id).scheduled_at.isoformat()) == {
        "status": "stopped", "reason": "too_late"}
    assert scheduling.send_interview_message(db, str(iv2.id), "interview_reminder_1h", 0)["status"] == "sent"


def test_talk_to_a_person_is_always_available(client, world, db):
    org = world.org()
    job, (m1, _) = _ranked(world, db, org)
    iv, token = _interested(db, m1)
    state = client.post(f"/talent/public/interview/{token}/human").json()
    assert state["status"] == "human_requested" and state["needs_recruiter"]
    assert db.query(AuditLog).filter_by(action="interview.human_requested").count() == 1
