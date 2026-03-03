import uuid
from datetime import datetime
from typing import Any, Dict, List

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.models.interview import InterviewSession, InterviewToken, MeetingSlot
from app.schemas.interview import (
    InterviewSessionSummary,
    InterviewUrlResponse,
    MeetingSlotCreate,
    MeetingSlotRead,
)
from app.schemas.job import JobCreate, JobListItem, JobRead, JobUpdate
from app.services import job_service

router = APIRouter()


@router.get("/stats", response_model=List[Dict[str, Any]])
def get_stats(db: Session = Depends(get_db)):
    return job_service.get_stats(db)


def _get_active_token(db: Session, job_id: int) -> str | None:
    """Return the active interview token string for a job, or None."""
    tok = db.query(InterviewToken).filter(
        InterviewToken.job_id == job_id,
        InterviewToken.is_active == True,
    ).first()
    return tok.token if tok else None


def _create_token(db: Session, job_id: int) -> str:
    """Create a new interview token for a job and return the token string."""
    token_str = uuid.uuid4().hex
    tok = InterviewToken(job_id=job_id, token=token_str, is_active=True)
    db.add(tok)
    db.commit()
    db.refresh(tok)
    return tok.token


@router.get("/", response_model=List[JobListItem])
def list_jobs(db: Session = Depends(get_db)):
    jobs = job_service.get_jobs_list(db)
    result = []
    for job in jobs:
        item = JobListItem.model_validate(job)
        result.append(item.model_copy(update={"interview_token": _get_active_token(db, job.id)}))
    return result


@router.post("/", response_model=JobRead, status_code=status.HTTP_201_CREATED)
def create_job(payload: JobCreate, db: Session = Depends(get_db)):
    job = job_service.create_job(db, payload)
    # Auto-generate a unique interview token for this job
    token_str = _create_token(db, job.id)
    result = JobRead.model_validate(job)
    return result.model_copy(update={"interview_token": token_str})


@router.get("/{job_id}", response_model=JobRead)
def get_job(job_id: int, db: Session = Depends(get_db)):
    job = job_service.get_job_by_id(db, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    result = JobRead.model_validate(job)
    return result.model_copy(update={"interview_token": _get_active_token(db, job_id)})


@router.put("/{job_id}", response_model=JobRead)
def update_job(job_id: int, payload: JobUpdate, db: Session = Depends(get_db)):
    job = job_service.update_job(db, job_id, payload)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    result = JobRead.model_validate(job)
    return result.model_copy(update={"interview_token": _get_active_token(db, job_id)})


@router.delete("/{job_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_job(job_id: int, db: Session = Depends(get_db)):
    ok = job_service.delete_job(db, job_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Job not found")


# ── Interview URL & Session Management ───────────────────────────────────────

@router.post(
    "/{job_id}/interview-url",
    response_model=InterviewUrlResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Generate (or retrieve) the interview URL for a job",
    description=(
        "Generates a unique, secure interview URL for the given job opening. "
        "The URL contains a UUID token stored in `interview_tokens`.\n\n"
        "**Idempotent:** If an active token already exists for this job, the same token and URL "
        "are returned (no duplicate tokens are created).\n\n"
        "**Frontend URL format:** `{FRONTEND_BASE}/interview/{token}`\n\n"
        "Share this URL with candidates. Each candidate who visits the link registers independently "
        "and gets their own `InterviewSession`."
    ),
    tags=["Jobs"],
)
def generate_interview_url(job_id: int, request: Request, db: Session = Depends(get_db)):
    job = job_service.get_job_by_id(db, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    # Return existing active token if present
    existing = db.query(InterviewToken).filter(
        InterviewToken.job_id == job_id,
        InterviewToken.is_active == True,
    ).first()
    if existing:
        base_url = str(request.base_url).rstrip("/")
        return InterviewUrlResponse(
            token=existing.token,
            url=f"{base_url}/interview/{existing.token}",
            job_id=job_id,
            is_active=existing.is_active,
            created_at=existing.created_at,
        )

    token_str = uuid.uuid4().hex
    tok = InterviewToken(
        job_id=job_id,
        token=token_str,
        is_active=True,
    )
    db.add(tok)
    db.commit()
    db.refresh(tok)

    base_url = str(request.base_url).rstrip("/")
    return InterviewUrlResponse(
        token=tok.token,
        url=f"{base_url}/interview/{tok.token}",
        job_id=job_id,
        is_active=tok.is_active,
        created_at=tok.created_at,
    )


@router.get(
    "/{job_id}/interview-sessions",
    response_model=List[InterviewSessionSummary],
    summary="List all interview sessions for a job",
    description=(
        "Returns a summary list of every `InterviewSession` for a given job, ordered by most recent first.\n\n"
        "**Each summary includes:** session status, candidate name/email, start/end timestamps, "
        "violation count, overall score, and recommendation (if evaluation has been generated).\n\n"
        "Session statuses: `pending` · `in_progress` · `completed` · `terminated`"
    ),
    tags=["Jobs"],
)
def list_interview_sessions(job_id: int, db: Session = Depends(get_db)):
    job = job_service.get_job_by_id(db, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    sessions = (
        db.query(InterviewSession)
        .filter(InterviewSession.job_id == job_id)
        .order_by(InterviewSession.created_at.desc())
        .all()
    )

    result = []
    for s in sessions:
        candidate = s.candidate
        evaluation = s.evaluation
        result.append(InterviewSessionSummary(
            id=s.id,
            status=s.status.value,
            candidate_id=s.candidate_id,
            candidate_name=candidate.name if candidate else None,
            candidate_email=candidate.email if candidate else None,
            started_at=s.started_at,
            completed_at=s.completed_at,
            violation_count=s.violation_count or 0,
            overall_score=evaluation.overall_score if evaluation else None,
            recommendation=evaluation.recommendation.value if evaluation and evaluation.recommendation else None,
            created_at=s.created_at,
        ))
    return result


@router.post(
    "/{job_id}/meeting-slots",
    response_model=MeetingSlotRead,
    status_code=status.HTTP_201_CREATED,
    summary="Add an available meeting slot",
    description=(
        "Creates a new available meeting slot for candidates who pass the interview threshold.\n\n"
        "**Fields:** `start_time` and `end_time` (ISO 8601 datetime strings, UTC recommended).\n\n"
        "Once a candidate books this slot via `POST /interview/sessions/{id}/schedule-meeting`, "
        "`is_booked` is set to `true` and the slot no longer appears in the candidate-facing list."
    ),
    tags=["Jobs"],
)
def add_meeting_slot(job_id: int, payload: MeetingSlotCreate, db: Session = Depends(get_db)):
    job = job_service.get_job_by_id(db, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    slot = MeetingSlot(
        job_id=job_id,
        start_time=payload.start_time,
        end_time=payload.end_time,
        is_booked=False,
    )
    db.add(slot)
    db.commit()
    db.refresh(slot)
    return slot
