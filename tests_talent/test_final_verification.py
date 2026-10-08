"""Final verification — re-running any Inngest function never double-sends or
double-scores, and every outbound message and AI score is in the audit log."""
import asyncio
import json
from datetime import datetime, timedelta

import inngest
from inngest.experimental import mocked

from app.models.talent import AuditLog, InterviewBooking, JobMatch, TalentInterview, TalentJobSettings, Touch
from app.talent import interviews, scheduling
from tests_talent import fakes
from tests_talent.conftest import CapturingInngest
from tests_talent.test_phase4_interviews import responder


def run_twice(fn, name, data, stubs=None):
    outputs = []
    for _ in range(2):
        res = mocked.trigger(fn, inngest.Event(name=name, data=data), CapturingInngest(), step_stubs=stubs or {})
        assert res.status is mocked.Status.COMPLETED, res.error
        outputs.append(res.output)
    return outputs


def _setup(world, db, autonomy="autopilot"):
    org = world.org()
    world.enable(org, "refresh", "match", "invite", "interview")
    job = world.job(org, skills=("Kafka", "Python"))
    db.merge(TalentJobSettings(job_id=job.id, autonomy_mode=autonomy, top_k=5, interview_mode="async_text",
                               question_count=8, time_limit_minutes=30, slot_minutes=45))
    p = world.profile(org, email="twice@acme.com", email_status="valid", verification_status="fresh",
                      skills='["Kafka", "Python"]', candidate_name="Neha Das",
                      work_experience=json.dumps([{"title": "Data Engineer", "company": "Northwind",
                                                   "description": "Kafka pipelines in Python"}]))
    world.consent(p)
    db.commit()
    return org, job, p


def test_rerunning_every_sending_and_scoring_function_is_safe(world, db, monkeypatch, outbox):
    from app.agent.interview_conduct import interview_prepare
    from app.agent.interview_reminders import interview_reminders
    from app.agent.interview_schedule import interview_schedule
    from app.agent.interview_score import interview_score
    from app.agent.invite_send import invite_send
    from app.agent.match_job import match_job

    fake = fakes.install(monkeypatch, lambda params: fakes.rerank_responder(params)
                         if "recruitment screener" in params["system"][0]["text"] else responder(params))
    org, job, p = _setup(world, db)

    run_twice(match_job, "talent/match.requested", {"job_id": job.id})
    rerank_calls = [c for c in fake.calls if "recruitment screener" in c["system"][0]["text"]]
    assert len(rerank_calls) == 1  # scored once
    m = db.query(JobMatch).filter_by(job_id=job.id).one()
    assert db.query(AuditLog).filter_by(action="match.scored").count() == 1

    run_twice(invite_send, "talent/invite.send", {"match_id": str(m.id)}, {"wait-answer": None, "wait-answer-2": None})
    assert [t.touch_no for t in db.query(Touch).filter_by(template="invite")] == [1]
    assert db.query(Touch).filter_by(template="invite_reminder").count() == 1

    m.status, m.response = "invited", "interested"
    db.commit()
    iv, _ = scheduling.offer_interview(db, m.id)
    data = {"interview_id": str(iv.id), "attempt": 0, "key": "k"}
    run_twice(interview_schedule, "talent/interview.offered", data,
              {"wait-booking": {"name": "talent/interview.booked", "data": {"interview_id": str(iv.id)}}})
    assert db.query(Touch).filter_by(template="interview_offer").count() == 1
    slot = db.query(InterviewBooking).filter_by(interview_id=iv.id).first()
    scheduling.book(db, iv.id, slot.id)
    run_twice(interview_reminders, "talent/interview.booked", data)
    for kind in ("interview_confirmed", "interview_reminder_24h", "interview_reminder_1h"):
        assert db.query(Touch).filter_by(template=kind).count() == 1, kind

    run_twice(interview_prepare, "talent/interview.booked", data)
    question_calls = [c for c in fake.calls if "prepare a structured first-round interview" in c["system"][0]["text"]]
    assert len(question_calls) == 1
    db.expire_all()
    iv = db.get(TalentInterview, iv.id)
    now = datetime.utcnow()
    iv.scheduled_at, iv.slot_ends_at = now - timedelta(minutes=1), now + timedelta(minutes=44)
    db.commit()
    interviews.start(db, iv, disclosure_acknowledged=True, consent_to_record=True, answer_mode="text")
    for q in iv.questions:
        asyncio.run(interviews.answer(str(iv.id), q["id"], f"I ran Kafka in production with Python, answer {q['id']}."))
    run_twice(interview_score, "talent/interview.completed", {"interview_id": str(iv.id)})
    scorer_calls = [c for c in fake.calls if "Score the interview transcript" in c["system"][0]["text"]]
    assert len(scorer_calls) == 1
    assert db.query(AuditLog).filter_by(action="interview.scored").count() == 1

    # Every message that left is in the audit log, once.
    sent = db.query(Touch).filter(Touch.status == "sent").count()
    assert sent == len(outbox) == db.query(AuditLog).filter_by(action="message.sent").count()


def test_refresh_respond_twice_parses_and_embeds_once(world, db, monkeypatch):
    from app.agent.refresh_respond import refresh_respond
    from app.models.talent import CvVersion
    from app.talent import cv

    org, job, p = _setup(world, db)
    version = cv.new_version(db, p, source="refresh_confirm", file_path=None)
    db.commit()
    run_twice(refresh_respond, "talent/refresh.responded", {"profile_id": p.id, "cv_version_id": str(version.id)})
    db.expire_all()
    v = db.get(CvVersion, version.id)
    assert v.parse_status == "parsed" and v.is_current and v.embedding_model
    assert db.query(AuditLog).filter_by(action="cv.parsed", subject_id=str(version.id)).count() == 1


def test_retention_sweep_reports_unless_switched_on(world, db):
    from app.agent.retention_sweep import retention_sweep
    from app.models import ResumeMetadata

    org = world.org()
    world.enable(org, "refresh")
    expired = world.profile(org, email="old@acme.com", retention_until=datetime.utcnow() - timedelta(days=1))
    kept = world.profile(org, email="new@acme.com", retention_until=datetime.utcnow() + timedelta(days=100))
    expired_id, kept_id = expired.id, kept.id
    res = mocked.trigger(retention_sweep, inngest.Event(name="inngest/scheduled.timer", data={}), CapturingInngest())
    assert res.output[str(org.id)] == {"due": 1, "erased": 0}
    db.expunge_all()
    assert db.query(ResumeMetadata).filter_by(id=expired_id).first() is not None
    world.enable(org, "retention")
    res = mocked.trigger(retention_sweep, inngest.Event(name="inngest/scheduled.timer", data={}), CapturingInngest())
    assert res.output[str(org.id)] == {"due": 1, "erased": 1}
    db.expunge_all()
    assert db.query(ResumeMetadata).filter_by(id=expired_id).first() is None
    assert db.query(ResumeMetadata).filter_by(id=kept_id).first() is not None
    assert db.query(AuditLog).filter_by(action="profile.erased").one().detail["reason"] == "retention_expired"
