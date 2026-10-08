"""Phase 5 — live voice behind its switch: same consent flow, the existing live
interviewer, the same scoring pipeline."""
import asyncio
from datetime import datetime, timedelta
from urllib.parse import parse_qs, urlsplit

from app.models.candidate import Candidate
from app.models.interview import InterviewMessage, InterviewSession, InterviewToken, SessionStatus
from app.models.talent import AuditLog, InterviewBooking, JobMatch, TalentInterview, TalentJobSettings
from app.talent import live_voice, scheduling, scoring
from tests_talent import fakes
from tests_talent.test_phase4_interviews import responder


def _live_match(world, db, *, live_on: bool):
    org = world.org()
    loops = ["match", "invite", "interview"] + (["live_voice"] if live_on else [])
    world.enable(org, *loops)
    job = world.job(org, skills=("Kafka",))
    db.merge(TalentJobSettings(job_id=job.id, interview_mode="live_voice", slot_minutes=45, question_count=8,
                               time_limit_minutes=30))
    p = world.profile(org, email="live@acme.com", email_status="valid", candidate_name="Ravi Kumar")
    world.consent(p)
    m = JobMatch(job_id=job.id, profile_id=p.id, organization_id=org.id, ai_score=80, status="invited",
                 response="interested")
    db.add(m)
    db.commit()
    return org, job, p, m


def test_live_voice_stays_behind_its_switch(world, db):
    org, job, p, m = _live_match(world, db, live_on=False)
    iv, _ = scheduling.offer_interview(db, m.id)
    assert iv.mode == "async_voice"


def test_live_interview_end_to_end(client, world, db, monkeypatch, events):
    fakes.install(monkeypatch, responder)
    org, job, p, m = _live_match(world, db, live_on=True)
    iv, url = scheduling.offer_interview(db, m.id)
    token = url.rsplit("/", 1)[-1]
    assert iv.mode == "live_voice"
    slot = db.query(InterviewBooking).filter_by(interview_id=iv.id).first()
    scheduling.book(db, iv.id, slot.id)
    db.expire_all()
    app_row = db.get(Candidate, db.get(JobMatch, m.id).application_id)
    assert app_row.job_id == job.id and app_row.resume_metadata_id == p.id and app_row.invite_token
    assert app_row.interview_date == slot.starts_at and app_row.interview_status == "Scheduled"

    iv = db.get(TalentInterview, iv.id)
    now = datetime.utcnow()
    iv.scheduled_at, iv.slot_ends_at = now - timedelta(minutes=1), now + timedelta(minutes=44)
    db.commit()
    state = client.get(f"/talent/public/interview/{token}").json()
    assert state["can_start"] and state["mode"] == "live_voice"
    r = client.post(f"/talent/public/interview/{token}/start",
                    json={"disclosure_acknowledged": True, "consent_to_record": False})
    assert r.status_code == 409 and "live_url" not in r.text  # no consent, no link
    r = client.post(f"/talent/public/interview/{token}/start",
                    json={"disclosure_acknowledged": True, "consent_to_record": True})
    live = r.json()["live_url"]
    parts = urlsplit(live)
    job_token = parts.path.rsplit("/", 1)[-1]
    invite = parse_qs(parts.query)["invite"][0]
    assert invite == app_row.invite_token
    assert db.query(AuditLog).filter_by(action="interview.consent_recorded").one().detail["answer_mode"] == "live_voice"

    # The existing live interviewer admits the candidate with that link.
    r = client.post(f"/interview/{job_token}/verify-access", json={"invite": invite})
    assert r.status_code == 200 and r.json()["access"] == "granted", r.text

    # It runs; when the session completes, its transcript feeds the same scoring pipeline.
    tok = db.query(InterviewToken).filter_by(job_id=job.id, is_active=True).first()
    session = db.query(InterviewSession).filter_by(candidate_id=app_row.id).first()
    if session is None:
        session = InterviewSession(token_id=tok.id, candidate_id=app_row.id, job_id=job.id)
        db.add(session)
        db.flush()
    session.status, session.completed_at = SessionStatus.completed, datetime.utcnow()
    for role, text in [("ai", "Tell me about running Kafka in production."),
                       ("candidate", "I ran a twelve broker Kafka cluster and cut consumer lag to seconds."),
                       ("ai", "How do you test your Python services?"),
                       ("candidate", "Pytest suites that replay recorded topics, plus contract tests in CI.")]:
        db.add(InterviewMessage(session_id=session.id, role=role, content=text))
    db.commit()
    assert live_voice.sync(db, str(iv.id))["status"] == "imported"
    db.expire_all()
    iv = db.get(TalentInterview, iv.id)
    assert iv.status == "completed" and iv.legacy_session_id == session.id
    assert [t["turn"] for t in iv.transcript] == ["question", "answer", "question", "answer"]
    assert ("talent/interview.completed", {"interview_id": str(iv.id)}) in events
    result = asyncio.run(scoring.score_interview(str(iv.id)))
    assert result["status"] == "scored"
    db.expire_all()
    scored = db.get(TalentInterview, iv.id)
    assert scored.human_review_status == "pending" and any(e["verified"] for s in scored.scores for e in s["evidence"])
