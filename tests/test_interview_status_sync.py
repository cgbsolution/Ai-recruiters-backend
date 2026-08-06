"""Regression tests for candidate interview_status staying stuck on "Scheduled".

Two independent defects kept a candidate who had actually finished the
interview looking un-interviewed in the recruiter list:

1. The conversation engine marks the *session* completed in three places but
   never touched the *candidate* row, so the status only advanced if the client
   also reached POST /complete.
2. The self-healing pass that was supposed to repair such rows required a
   non-null score, so a candidate whose AI evaluation failed (stub row, null
   score) was skipped forever — exactly the case it existed for.
"""
from datetime import datetime

import pytest

from app.models.candidate import Candidate
from app.models.interview import (
    InterviewSession,
    InterviewStatusLog,
    InterviewToken,
    SessionStatus,
)
from app.models.job import Job
from app.services.avatar_interview_service import _mark_candidate_interview_completed
from app.services.candidate_service import _reconcile_stale_completed_candidates


def _make(db, interview_status, score=None, stage="Candidates"):
    stamp = datetime.utcnow().timestamp()
    job = Job(
        title="Test Role",
        department="Engineering",
        location="Bangalore",
        job_type="Full-time",
        experience_level="Mid",
        emp_type="Full-time",
    )
    db.add(job)
    db.flush()
    token = InterviewToken(job_id=job.id, token=f"tok-{stamp}")
    db.add(token)
    db.flush()

    cand = Candidate(
        name="Test Candidate",
        email=f"c{stamp}@example.com",
        interview_status=interview_status,
        score=score,
        candidate_stage=stage,
    )
    db.add(cand)
    db.flush()
    session = InterviewSession(
        token_id=token.id,
        candidate_id=cand.id,
        job_id=job.id,
        status=SessionStatus.completed,
        started_at=datetime.utcnow(),
        completed_at=datetime.utcnow(),
    )
    db.add(session)
    db.flush()
    return cand, session


def test_engine_completion_advances_scheduled_candidate(db):
    cand, session = _make(db, "Scheduled")

    _mark_candidate_interview_completed(db, session, reason="engine closed interview")
    db.flush()

    assert cand.interview_status == "Completed"
    assert cand.candidate_stage == "AI Round"
    assert cand.interview_date is not None
    logs = db.query(InterviewStatusLog).filter_by(candidate_id=cand.id).all()
    assert [(l.previous_status, l.new_status) for l in logs] == [("Scheduled", "Completed")]
    db.rollback()


def test_engine_completion_never_clobbers_a_terminal_verdict(db):
    cand, session = _make(db, "Terminated – Cheating Detected")

    _mark_candidate_interview_completed(db, session, reason="engine closed interview")
    db.flush()

    assert cand.interview_status == "Terminated – Cheating Detected"
    assert db.query(InterviewStatusLog).filter_by(candidate_id=cand.id).count() == 0
    db.rollback()


def test_reconciler_heals_candidate_whose_evaluation_failed(db):
    """The stuck row in the field: terminal session, but score is null because
    the evaluation errored. This used to be skipped and stay "Scheduled"."""
    cand, _ = _make(db, "Scheduled", score=None)
    db.flush()

    _reconcile_stale_completed_candidates(db, [cand])

    assert cand.interview_status == "Completed"
    assert cand.candidate_stage == "AI Round"
    db.rollback()


def test_reconciler_leaves_candidates_without_a_terminal_session_alone(db):
    cand = Candidate(
        name="Not Yet Interviewed",
        email=f"n{datetime.utcnow().timestamp()}@example.com",
        interview_status="Scheduled",
        candidate_stage="Candidates",
    )
    db.add(cand)
    db.flush()

    _reconcile_stale_completed_candidates(db, [cand])

    assert cand.interview_status == "Scheduled"
    db.rollback()
