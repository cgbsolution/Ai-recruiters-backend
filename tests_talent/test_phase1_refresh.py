"""Phase 1 — Loop A refresh: consent gate, idempotency, opt-out, caps,
deliverability auto-pause, the refresh page, the pilot and the funnel."""
import hashlib
import hmac
import json
import uuid
from datetime import datetime, timedelta

import inngest
from inngest.experimental import mocked

from app.models import ResumeMetadata
from app.models.talent import (
    AccessToken, AuditLog, Campaign, CampaignMember, ContactConsent, CvVersion, SendCounter, SuppressionEntry, Touch,
)
from app.talent import caps, campaigns, consent, deliverability, outbox, refresh, tokens
from app.talent.transports import MemoryTransport, PermanentSendError, TransientSendError, UnknownSendError, \
    _memory_transport
from tests_talent.conftest import CapturingInngest


def _campaign(db, org, kind="refresh", cycle="test"):
    return campaigns.get_or_create_campaign(db, org.id, kind, cycle, "Test campaign")


def _send(db, campaign, profile, touch_no=1, kind="refresh_touch_1", **kw):
    return outbox.send_touch(db, campaign_id=campaign.id, profile_id=profile.id, touch_no=touch_no, kind=kind, **kw)


def _old(days=400):
    return datetime.utcnow() - timedelta(days=days)


# ── The consent gate ────────────────────────────────────────────────────────

def test_no_consent_means_nothing_is_ever_sent(world, db, outbox):
    org = world.org()
    world.enable(org, "refresh")
    p = world.profile(org, email="asha@acme.com", email_status="valid")
    c = _campaign(db, org)
    result = _send(db, c, p)
    assert result.status == "skipped" and result.reason == "no_consent"
    assert outbox == []

    world.consent(p, "jobs")  # consent for a DIFFERENT purpose doesn't count
    assert _send(db, c, p, touch_no=2, kind="refresh_touch_2").reason == "no_consent"
    world.consent(p, "refresh", status="withdrawn")
    assert _send(db, c, p, touch_no=3, kind="refresh_touch_3").reason == "consent_withdrawn"
    assert outbox == []
    assert db.query(AuditLog).filter_by(action="message.skipped").count() == 3


def test_every_blocker_is_checked_at_send_time(world, db, outbox):
    org = world.org()
    world.enable(org, "refresh")
    c = _campaign(db, org)
    suppressed = world.profile(org, email="sup@acme.com", email_status="valid")
    unreachable = world.profile(org, email="gone@acme.com", email_status="valid", verification_status="unreachable")
    placeholder = world.profile(org, email="noemail_1@pending.local")
    for p in (suppressed, unreachable, placeholder):
        world.consent(p)
    consent.suppress(db, email="SUP@acme.com", reason="unsubscribe")
    db.commit()
    assert _send(db, c, suppressed).reason == "suppressed"
    assert _send(db, c, unreachable).reason == "unreachable"
    assert _send(db, c, placeholder).reason == "email_placeholder"
    assert outbox == []


def test_loop_switch_and_master_switch(world, db, outbox, monkeypatch):
    org = world.org()
    p = world.profile(org, email="a@acme.com", email_status="valid")
    world.consent(p)
    c = _campaign(db, org)
    assert _send(db, c, p).reason == "loop_off:refresh"  # loop not switched on
    world.enable(org, "refresh")
    from app.talent import outbox as outbox_module

    monkeypatch.setattr(outbox_module.talent_config, "TALENT_AGENT_ENABLED", False)
    assert _send(db, c, p, touch_no=2, kind="refresh_touch_2").reason == "loop_off:refresh"
    assert outbox == []


def test_permits_cannot_be_forged_or_skipped():
    import pytest

    from app.talent.consent import ContactPermit
    from app.talent.transports import OutgoingEmail

    forged = ContactPermit(1, "email", "refresh", "x@y.com", datetime.utcnow(), object())
    msg = OutgoingEmail(subject="s", text="t", html="h", from_name="f", message_id_hint="m")
    with pytest.raises(PermissionError):
        MemoryTransport().send(forged, msg)
    with pytest.raises(AttributeError):
        MemoryTransport().send(None, msg)


def test_only_the_outbox_talks_to_transports():
    """Fails if any Talent Agent module other than the outbox (and the
    transports themselves) sends mail or WhatsApp — i.e. bypasses can_contact."""
    import ast
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[1] / "app"
    allowed = {"app/talent/outbox.py", "app/talent/transports.py"}
    banned_imports = {"smtplib"}
    banned_names = {"email_transport", "whatsapp_transport", "SmtpTransport", "WhatsAppTransport", "MemoryTransport",
                    "send_whatsapp_message", "_send_email_sync", "send_schedule_email"}
    offenders = []
    for path in list((root / "talent").rglob("*.py")) + list((root / "agent").rglob("*.py")) + \
            [root / "routers" / "talent_admin.py", root / "routers" / "talent_public.py"] + \
            list((root / "routers").glob("talent_*.py")):
        rel = path.relative_to(root.parent).as_posix()
        if rel in allowed:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom):
                names = {a.name for a in node.names} | {(node.module or "").split(".")[0]}
                if (node.module or "") == "app.services.interview_service":
                    offenders.append(f"{rel}: imports the legacy senders")
            elif isinstance(node, (ast.Name, ast.Attribute)):
                names = {getattr(node, "id", None) or getattr(node, "attr", None)}
            else:
                continue
            hits = names & (banned_imports | banned_names)
            if hits:
                offenders.append(f"{rel}: {sorted(hits)}")
    assert offenders == [], offenders


# ── Idempotency ─────────────────────────────────────────────────────────────

def test_a_touch_is_sent_once_however_often_it_is_attempted(world, db, outbox):
    org = world.org()
    world.enable(org, "refresh")
    p = world.profile(org, email="asha@acme.com", email_status="valid", candidate_name="Asha Rao")
    world.consent(p)
    c = _campaign(db, org)
    results = [_send(db, c, p) for _ in range(3)]
    assert [r.status for r in results] == ["sent", "sent", "sent"]
    assert [r.reason for r in results] == [None, "already_sent", "already_sent"]
    assert len(outbox) == 1 and outbox[0]["to"] == "asha@acme.com"
    assert db.query(Touch).count() == 1
    headers = outbox[0]["headers"]
    assert headers["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"
    assert "/t/refresh/" in outbox[0]["text"] and "Hi Asha" in outbox[0]["text"]
    assert db.query(AuditLog).filter_by(action="message.sent").count() == 1


def test_transient_failures_retry_but_unknown_outcomes_never_resend(world, db, outbox):
    org = world.org()
    world.enable(org, "refresh")
    p, q = world.profile(org, email="t@acme.com", email_status="valid"), world.profile(org, email="u@acme.com", email_status="valid")
    world.consent(p)
    world.consent(q)
    c = _campaign(db, org)
    _memory_transport.fail_next = [TransientSendError("smtp down", retry_after_seconds=60)]
    first = _send(db, c, p)
    assert first.status == "deferred" and first.reason == "transient_error" and outbox == []
    assert _send(db, c, p).status == "sent" and len(outbox) == 1

    _memory_transport.fail_next = [UnknownSendError("connection lost during DATA")]
    assert _send(db, c, q).status == "unknown"
    assert _send(db, c, q).reason == "already_unknown"  # never retried
    assert len(outbox) == 1
    # The day's counter was given back for the transient failure only.
    assert db.query(SendCounter).one().total == 2


def test_hard_bounce_at_send_time_dead_letters(world, db, outbox, events):
    org = world.org()
    world.enable(org, "refresh")
    p = world.profile(org, email="bad@acme.com", email_status="valid")
    world.consent(p)
    c = _campaign(db, org)
    _memory_transport.fail_next = [PermanentSendError("550 no such user", hard_bounce=True)]
    assert _send(db, c, p).status == "failed"
    db.expire_all()
    assert db.get(ResumeMetadata, p.id).verification_status == "unreachable"
    assert consent.is_suppressed(db, email="bad@acme.com")
    assert db.query(Touch).one().status == "bounced"
    assert ("talent/member.stopped", {"profile_id": p.id, "campaign_id": None, "reason": "bounced"}) in events


# ── Caps & quiet hours ──────────────────────────────────────────────────────

def test_domain_cap_and_bulk_share(world, db, monkeypatch):
    from app.talent import caps as caps_module

    monkeypatch.setattr(caps_module.talent_config, "TALENT_BULK_SHARE", 0.5)
    dom = caps.get_domain(db, "agent.test")
    dom.ramp = [{"from_day": 0, "cap": 4}, {"from_day": 7, "cap": 10}]
    db.commit()
    granted_bulk = [caps.reserve(db, "agent.test", caps.BULK) for _ in range(4)]
    assert granted_bulk == [True, True, False, False]  # bulk may take half
    granted_tx = [caps.reserve(db, "agent.test", caps.TRANSACTIONAL) for _ in range(3)]
    assert granted_tx == [True, True, False]  # the rest of the cap, then nothing
    db.commit()
    assert caps.cap_for(dom, dom.ramp_started_on + timedelta(days=8)) == 10


def test_sends_wait_for_the_cap_and_the_quiet_hours(world, db, outbox):
    org = world.org()
    world.enable(org, "refresh")
    c = _campaign(db, org)
    dom = caps.get_domain(db, "agent.test")
    dom.ramp = [{"from_day": 0, "cap": 2}]
    db.commit()
    profiles = [world.profile(org, email=f"p{i}@acme.com", email_status="valid") for i in range(3)]
    for p in profiles:
        world.consent(p)
    results = [_send(db, c, p, priority=caps.TRANSACTIONAL) for p in profiles]
    assert [r.status for r in results] == ["sent", "sent", "deferred"]
    assert results[2].reason == "domain_cap" and results[2].retry_at > datetime.utcnow()
    assert db.query(Touch).filter_by(profile_id=profiles[2].id).one().status == "queued"

    world.enable(org, "refresh", quiet_hours=(0, 23))  # quiet 00:00-23:00 local: almost always
    late = world.profile(org, email="late@acme.com", email_status="valid")
    world.consent(late)
    from zoneinfo import ZoneInfo

    local_now = datetime.utcnow().replace(tzinfo=ZoneInfo("UTC")).astimezone(ZoneInfo("Asia/Kolkata"))
    if local_now.hour < 23:
        r = _send(db, c, late)
        assert r.status == "deferred" and r.reason == "quiet_hours"


# ── Deliverability ──────────────────────────────────────────────────────────

def _webhook(client, payload, secret="whsec-test"):
    body = json.dumps(payload).encode()
    sig = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return client.post("/talent/public/webhooks/deliverability", content=body,
                       headers={"X-Talent-Signature": sig, "Content-Type": "application/json"})


def test_bounce_rate_auto_pauses_the_campaign(client, world, db, outbox):
    org = world.org()
    world.enable(org, "refresh")
    c = _campaign(db, org)
    profiles = [world.profile(org, email=f"b{i}@acme.com", email_status="valid") for i in range(8)]
    for p in profiles:
        world.consent(p)
    for p in profiles[:5]:
        assert _send(db, c, p).status == "sent"
    ids = {m["to"]: m["message_id"] for m in outbox}
    assert _webhook(client, {"events": []}, secret="wrong").status_code == 401

    # 3 hard bounces / max(5 sent, 100 min sample) = 3% — not above 3%: still active
    for i in range(3):
        r = _webhook(client, {"events": [{"id": f"e{i}", "type": "bounce", "bounce_type": "hard",
                                          "message_id": ids[f"b{i}@acme.com"]}]})
        assert r.json() == {"recorded": 1}
    assert _webhook(client, {"events": [{"id": "e0", "type": "bounce", "message_id": ids["b0@acme.com"]}]}).json() \
        == {"recorded": 0}  # replayed event id: ignored
    db.expire_all()
    assert db.get(Campaign, c.id).status == "active"

    _webhook(client, {"events": [{"id": "e3", "type": "bounce", "bounce_type": "hard", "message_id": ids["b3@acme.com"]}]})
    db.expire_all()
    paused = db.get(Campaign, c.id)
    assert paused.status == "paused" and paused.paused_reason.startswith("auto: bounce rate")
    assert db.query(AuditLog).filter_by(action="campaign.auto_paused").count() == 1
    r = _send(db, c, profiles[6])
    assert r.status == "paused" and len(outbox) == 5
    for i in range(4):
        assert db.get(ResumeMetadata, profiles[i].id).verification_status == "unreachable"


def test_a_complaint_pauses_and_suppresses(client, world, db, outbox):
    org = world.org()
    world.enable(org, "refresh")
    c = _campaign(db, org)
    p = world.profile(org, email="angry@acme.com", email_status="valid")
    world.consent(p)
    _send(db, c, p)
    _webhook(client, {"events": [{"id": "c1", "type": "complaint", "message_id": outbox[0]["message_id"]}]})
    db.expire_all()
    assert db.get(Campaign, c.id).status == "paused"  # 1 / 100 = 1% > 0.1%
    assert consent.is_suppressed(db, email="angry@acme.com")
    assert db.get(ContactConsent, (p.id, "email", "refresh")).status == "withdrawn"


# ── Opt-out ─────────────────────────────────────────────────────────────────

def test_unsubscribe_takes_effect_immediately(client, world, db, outbox, events):
    org = world.org()
    world.enable(org, "refresh")
    c = _campaign(db, org)
    p = world.profile(org, email="leave@acme.com", email_status="valid", phone="9876543210")
    world.consent(p)
    db.add(CampaignMember(campaign_id=c.id, profile_id=p.id, status="in_progress"))
    db.commit()
    _send(db, c, p)
    unsub = outbox[0]["headers"]["List-Unsubscribe"].strip("<>").rsplit("/", 1)[-1]
    r = client.post(f"/talent/public/unsubscribe/{unsub}", data={"List-Unsubscribe": "One-Click"})
    assert r.status_code == 200 and r.json()["unsubscribed"] is True

    assert _send(db, c, p, touch_no=2, kind="refresh_touch_2").reason in ("suppressed", "consent_withdrawn")
    assert len(outbox) == 1
    db.expire_all()
    assert db.get(CampaignMember, (c.id, p.id)).status == "opted_out"
    assert consent.is_suppressed(db, phone="+91 98765 43210")
    assert any(n == "talent/member.stopped" and d["reason"] == "opted_out" for n, d in events)
    # The refresh sequence step now stops instead of sending.
    assert refresh.send_step(db, str(c.id), p.id, 2)["status"] == "stopped"


# ── The refresh page ────────────────────────────────────────────────────────

def test_refresh_page_confirm_flow(client, world, db, outbox, events):
    org = world.org()
    world.enable(org, "refresh")
    c = _campaign(db, org)
    p = world.profile(org, email="asha@acme.com", email_status="valid", current_designation="Engineer",
                      raw_text="Current CTC: 6 LPA", created_at=_old())
    world.consent(p, "refresh")
    db.add(CampaignMember(campaign_id=c.id, profile_id=p.id, status="in_progress"))
    db.commit()
    _send(db, c, p)
    link = [w for w in outbox[0]["text"].split() if "/t/refresh/" in w][0]
    token = link.rsplit("/", 1)[-1]

    page = client.get(f"/talent/public/refresh/{token}").json()
    assert page["fields"]["current_designation"] == "Engineer" and page["fields"]["current_ctc"] == 6
    assert db.query(Touch).one().opened_at is not None

    pdf = b"%PDF-1.4 new cv"
    r = client.post(f"/talent/public/refresh/{token}/cv", files={"file": ("cv.pdf", pdf, "application/pdf")})
    assert r.status_code == 200
    staged = db.query(CvVersion).filter_by(source="refresh_upload").one()
    assert staged.parse_status == "staged"
    bad = client.post(f"/talent/public/refresh/{token}/cv", files={"file": ("x.pdf", b"MZ evil", "application/pdf")})
    assert bad.status_code == 400

    body = {"fields": {"current_designation": "Senior Engineer", "current_company": "Newco", "current_ctc": 9,
                       "expected_ctc": 14, "notice_period_days": 30, "job_search_status": "active",
                       "open_to_relocate": True, "location": "Pune"},
            "consents": {"jobs": True, "interview": True, "refresh": True}}
    r = client.post(f"/talent/public/refresh/{token}", json=body)
    assert r.status_code == 200, r.text
    db.expire_all()
    fresh = db.get(ResumeMetadata, p.id)
    assert fresh.verification_status == "fresh" and fresh.last_verified_at is not None
    assert (fresh.current_designation, fresh.current_company, fresh.current_ctc) == ("Senior Engineer", "Newco", 9)
    assert set(json.loads(fresh.candidate_fields)) >= {"current_ctc", "current_company", "current_designation"}
    assert db.get(ContactConsent, (p.id, "email", "jobs")).status == "granted"
    assert db.get(CampaignMember, (c.id, p.id)).status == "responded"
    assert db.query(CvVersion).filter_by(source="refresh_upload").one().parse_status == "pending"
    assert db.query(Touch).one().responded_at is not None
    names = [n for n, _ in events]
    assert "talent/refresh.responded" in names and "talent/member.stopped" in names
    # Single use: the link is spent.
    assert client.post(f"/talent/public/refresh/{token}", json=body).status_code == 404
    assert client.get(f"/talent/public/refresh/{token}").status_code == 404
    # Event payloads carry ids only.
    for _, data in events:
        assert "asha" not in json.dumps(data).lower()


def test_refresh_page_rejects_bad_values_and_unknown_links(client, world, db):
    org = world.org()
    p = world.profile(org, email="x@acme.com")
    raw = tokens.issue(db, p.id, "refresh")
    db.commit()
    assert client.get("/talent/public/refresh/not-a-token").status_code == 404
    r = client.post(f"/talent/public/refresh/{raw}", json={"fields": {"notice_period_days": 900}})
    assert r.status_code == 400
    assert client.get(f"/talent/public/refresh/{raw}").status_code == 200  # a bad submit doesn't spend the link
    expired = db.query(AccessToken).filter_by(profile_id=p.id).one()
    expired.expires_at = datetime.utcnow() - timedelta(minutes=1)
    db.commit()
    assert client.get(f"/talent/public/refresh/{raw}").status_code == 404


# ── Pilot, sweep and the funnel ─────────────────────────────────────────────

def test_pilot_is_stratified_by_cv_age(world, db):
    org = world.org()
    world.enable(org, "refresh")
    ages = [100] * 30 + [500] * 30 + [900] * 4  # days: <12, 12-24, 24+ months
    for i, age in enumerate(ages):
        p = world.profile(org, email=f"s{i}@acme.com", email_status="valid", created_at=_old(age))
        world.consent(p, "refresh")
    no_consent = world.profile(org, email="nc@acme.com", email_status="valid", created_at=_old(100))
    pilot = campaigns.create_pilot(db, org.id, size=30, actor="user:1", seed=7)
    strata = {s["label"]: s for s in pilot.segment["strata"]}
    assert strata["24+ months"]["selected"] == 4  # all of the smallest stratum
    assert strata["0-12 months"]["selected"] + strata["12-24 months"]["selected"] == 26
    assert abs(strata["0-12 months"]["selected"] - strata["12-24 months"]["selected"]) <= 1
    members = {m.profile_id for m in db.query(CampaignMember).filter_by(campaign_id=pilot.id)}
    assert len(members) == 30 and no_consent.id not in members


def test_sweep_enrolls_due_profiles_skills_first_within_budget(world, db):
    org = world.org()
    world.enable(org, "refresh", "refresh_rolling")
    world.job(org, skills=("Kafka",))
    plain = [world.profile(org, email=f"plain{i}@acme.com", email_status="valid", created_at=_old(300),
                           skills='["Excel"]') for i in range(5)]
    matching = world.profile(org, email="kafka@acme.com", email_status="valid", created_at=_old(250),
                             skills='["Kafka", "Java"]')
    recent = world.profile(org, email="new@acme.com", email_status="valid", created_at=_old(10))
    for p in plain + [matching, recent]:
        world.consent(p, "refresh")
    dom = caps.get_domain(db, "agent.test")
    dom.ramp = [{"from_day": 0, "cap": 10}]
    db.commit()
    # bulk budget 10 * 0.7 = 7, enrolment share 40% -> 2 per day
    result = campaigns.sweep_org(db, org.id, rolling=True)
    assert result["budget"] == 2
    dispatched = [pid for _, pid in result["dispatch"]]
    assert dispatched[0] == matching.id and len(dispatched) == 2
    assert recent.id not in dispatched
    db.expire_all()
    assert db.get(ResumeMetadata, matching.id).verification_status == "refreshing"
    again = campaigns.sweep_org(db, org.id, rolling=True)
    assert set(pid for _, pid in again["dispatch"]).isdisjoint(dispatched)


def test_funnel_matches_the_raw_rows(client, world, db, outbox):
    org = world.org()
    world.enable(org, "refresh")
    c = _campaign(db, org)
    ps = [world.profile(org, email=f"f{i}@acme.com", email_status="valid") for i in range(6)]
    for p in ps[:5]:
        world.consent(p)
        db.add(CampaignMember(campaign_id=c.id, profile_id=p.id, status="in_progress"))
    db.add(CampaignMember(campaign_id=c.id, profile_id=ps[5].id, status="in_progress"))
    db.commit()
    for p in ps:
        _send(db, c, p)  # the last one has no consent -> skipped
    for p in ps[:2]:
        _send(db, c, p, touch_no=2, kind="refresh_touch_2")
    db.query(Touch).filter(Touch.profile_id == ps[0].id, Touch.touch_no == 1).update({"opened_at": datetime.utcnow()})
    refresh.end_sequence(db, str(c.id), ps[4].id)
    db.commit()
    f = campaigns.funnel(db, c.id)

    rows = db.query(Touch).filter_by(campaign_id=c.id).all()
    assert f["messages_sent"] == sum(1 for t in rows if t.status == "sent") == 7
    assert f["sent"] == len({t.profile_id for t in rows if t.status == "sent"}) == 5
    assert f["opened"] == len({t.profile_id for t in rows if t.opened_at}) == 1
    assert f["skipped_reasons"] == {"no_consent": 1}
    assert f["no_response"] == 1 and f["members"] == 6
    api = client.get(f"/talent/campaigns/{c.id}", headers=world.headers(world.user(org))).json()
    assert api["funnel"]["messages_sent"] == 7


# ── The Inngest functions ───────────────────────────────────────────────────

def test_refresh_touch_function_runs_three_touches_then_no_response(world, db, outbox):
    from app.agent.refresh_touch import refresh_touch

    org = world.org()
    world.enable(org, "refresh")
    c = _campaign(db, org)
    p = world.profile(org, email="seq@acme.com", email_status="valid")
    world.consent(p)
    db.add(CampaignMember(campaign_id=c.id, profile_id=p.id, status="in_progress"))
    db.commit()
    event = inngest.Event(name="talent/refresh.member.dispatched",
                          data={"campaign_id": str(c.id), "profile_id": p.id, "key": f"{c.id}:{p.id}"})
    stubs = {"wait-after-1": None, "wait-after-2": None, "wait-after-3": None}
    res = mocked.trigger(refresh_touch, event, mocked.Inngest(app_id="t", is_production=False), step_stubs=stubs)
    assert res.status is mocked.Status.COMPLETED, res.error
    assert res.output == {"result": "no_response"}
    assert sorted(t.touch_no for t in db.query(Touch).all()) == [1, 2, 3] and len(outbox) == 3
    db.expire_all()
    assert db.get(CampaignMember, (c.id, p.id)).status == "no_response"

    # Re-running the whole function never sends again.
    db.get(CampaignMember, (c.id, p.id)).status = "in_progress"
    db.commit()
    mocked.trigger(refresh_touch, event, mocked.Inngest(app_id="t", is_production=False), step_stubs=stubs)
    assert len(outbox) == 3


def test_refresh_touch_stops_on_a_stop_event(world, db, outbox):
    from app.agent.refresh_touch import refresh_touch

    org = world.org()
    world.enable(org, "refresh")
    c = _campaign(db, org)
    p = world.profile(org, email="stop@acme.com", email_status="valid")
    world.consent(p)
    db.add(CampaignMember(campaign_id=c.id, profile_id=p.id, status="in_progress"))
    db.commit()
    event = inngest.Event(name="talent/refresh.member.dispatched",
                          data={"campaign_id": str(c.id), "profile_id": p.id, "key": "k"})
    stop = {"name": "talent/member.stopped", "data": {"profile_id": p.id, "reason": "responded"}}
    res = mocked.trigger(refresh_touch, event, mocked.Inngest(app_id="t", is_production=False),
                         step_stubs={"wait-after-1": stop})
    assert res.output == {"stopped_after": 1, "reason": "responded"}
    assert len(outbox) == 1


def test_sweep_function_dispatches_ids_only(world, db):
    from app.agent.refresh_sweep import refresh_sweep

    org = world.org()
    world.enable(org, "refresh")
    p = world.profile(org, email="sw@acme.com", email_status="valid", created_at=_old())
    world.consent(p, "refresh")
    campaigns.create_pilot(db, org.id, size=5, actor="user:1", seed=1)
    client = CapturingInngest()
    res = mocked.trigger(refresh_sweep, inngest.Event(name="talent/refresh.sweep.requested",
                                                      data={"organization_id": org.id}), client)
    assert res.status is mocked.Status.COMPLETED, res.error
    dispatched = [e for e in client.sent if e.name == "talent/refresh.member.dispatched"]
    assert len(dispatched) == 1 and dispatched[0].data["profile_id"] == p.id
    assert set(dispatched[0].data) == {"campaign_id", "profile_id", "key"}


# ── My data ─────────────────────────────────────────────────────────────────

def test_my_data_export_withdraw_and_delete(client, world, db, outbox, tmp_path):
    import os

    from app.media import UPLOAD_ROOT

    org = world.org()
    world.enable(org, "refresh")
    os.makedirs(os.path.join(UPLOAD_ROOT, "resumes"), exist_ok=True)
    cv_file = os.path.join(UPLOAD_ROOT, "resumes", "asha_cv.pdf")
    with open(cv_file, "wb") as fh:
        fh.write(b"%PDF-1.4")
    p = world.profile(org, email="asha@acme.com", email_status="valid", phone="9876543210",
                      resume_file_path=cv_file.replace("\\", "/"))
    pid = p.id
    world.consent(p)
    c = _campaign(db, org)
    _send(db, c, p)
    my_data = [w for w in outbox[0]["text"].split() if "/t/my-data/" in w][0].rsplit("/", 1)[-1]

    summary = client.get(f"/talent/public/my-data/{my_data}").json()
    assert summary["profile"]["email"] == "asha@acme.com" and summary["messages"][0]["kind"] == "refresh_touch_1"
    export = client.get(f"/talent/public/my-data/{my_data}/export")
    assert export.status_code == 200 and "attachment" in export.headers["content-disposition"]
    assert export.json()["profile_record"]["candidate_email"] == "asha@acme.com"

    r = client.post(f"/talent/public/my-data/{my_data}/consent", json={"channel": "email", "purpose": "jobs",
                                                                       "granted": False})
    assert r.status_code == 200
    assert client.post(f"/talent/public/my-data/{my_data}/delete", json={"confirm": "nope"}).status_code == 400
    r = client.post(f"/talent/public/my-data/{my_data}/delete", json={"confirm": "DELETE"})
    assert r.status_code == 200, r.text
    db.expunge_all()
    assert db.query(ResumeMetadata).filter_by(id=pid).first() is None
    assert not os.path.exists(cv_file)
    assert db.query(Touch).count() == 0 and db.query(ContactConsent).count() == 0
    assert consent.is_suppressed(db, email="asha@acme.com")
    erased = db.query(AuditLog).filter_by(action="profile.erased").one()
    assert erased.actor == "candidate" and "asha" not in json.dumps(erased.detail)
    assert db.query(AuditLog).filter_by(action="profile.deleted", subject_id=str(pid)).one().actor == "candidate"
    assert client.get(f"/talent/public/my-data/{my_data}").status_code == 404


def test_audit_never_holds_contact_details(world, db, outbox):
    org = world.org()
    world.enable(org, "refresh")
    p = world.profile(org, email="private@acme.com", email_status="valid", phone="9876543210")
    world.consent(p)
    _send(db, _campaign(db, org), p)
    from app.talent.audit import audit

    audit(db, actor="agent", action="test", subject_type="x", detail={"note": "mail private@acme.com or 98765 43210"},
          commit=True)
    dump = json.dumps([r.detail for r in db.query(AuditLog).all()])
    assert "private@acme.com" not in dump and "98765" not in dump


def test_candidate_can_opt_back_in_but_complaints_stay_suppressed(client, world, db):
    org = world.org()
    p = world.profile(org, email="back@acme.com", email_status="valid")
    q = world.profile(org, email="spam@acme.com", email_status="valid")
    raw_p, raw_q = tokens.issue(db, p.id, "my_data"), tokens.issue(db, q.id, "my_data")
    db.commit()
    assert client.post(f"/talent/public/my-data/{raw_p}/withdraw-all").status_code == 200
    assert consent.is_suppressed(db, email="back@acme.com")
    client.post(f"/talent/public/my-data/{raw_p}/consent", json={"channel": "email", "purpose": "jobs", "granted": True})
    db.expire_all()
    assert not consent.is_suppressed(db, email="back@acme.com")
    assert consent.can_contact(db, p.id, "email", "jobs").allowed

    consent.suppress(db, email="spam@acme.com", reason="complaint")
    db.commit()
    client.post(f"/talent/public/my-data/{raw_q}/consent", json={"channel": "email", "purpose": "jobs", "granted": True})
    db.expire_all()
    assert consent.is_suppressed(db, email="spam@acme.com")
    assert consent.can_contact(db, q.id, "email", "jobs").reason == "suppressed"
