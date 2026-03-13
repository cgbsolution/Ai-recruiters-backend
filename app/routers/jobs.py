import asyncio
import logging
import uuid
from datetime import datetime
from typing import Any, Dict, List

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db, SessionLocal
from app.models.interview import InterviewSession, InterviewToken, MeetingSlot

logger = logging.getLogger(__name__)
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


def _bg_build_job_metadata(job_id: int):
    """Background task: extract JD metadata using Claude (non-blocking)."""
    async def _run():
        db = SessionLocal()
        try:
            from app.services.jd_metadata_service import build_job_metadata
            await build_job_metadata(db, job_id)
            logger.info("JD metadata built for job %s", job_id)
        except Exception:
            logger.warning("Failed to build JD metadata for job %s", job_id, exc_info=True)
        finally:
            db.close()
    try:
        loop = asyncio.get_event_loop()
        loop.create_task(_run())
    except RuntimeError:
        asyncio.run(_run())


@router.post("/", response_model=JobRead, status_code=status.HTTP_201_CREATED)
def create_job(payload: JobCreate, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    job = job_service.create_job(db, payload)
    # Auto-generate a unique interview token for this job
    token_str = _create_token(db, job.id)
    # Extract JD metadata in background
    background_tasks.add_task(_bg_build_job_metadata, job.id)
    result = JobRead.model_validate(job)
    return result.model_copy(update={"interview_token": token_str})


@router.get("/{job_id}", response_model=JobRead)
def get_job(job_id: int, db: Session = Depends(get_db)):
    from app.models.job_metadata import JobMetadata
    job = job_service.get_job_by_id(db, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    result = JobRead.model_validate(job)
    # Enrich with summary from job metadata
    jd_meta = db.query(JobMetadata).filter(JobMetadata.job_id == job_id).first()
    summary = jd_meta.responsibilities_summary if jd_meta else None
    return result.model_copy(update={
        "interview_token": _get_active_token(db, job_id),
        "summary": summary,
    })


@router.put("/{job_id}", response_model=JobRead)
def update_job(job_id: int, payload: JobUpdate, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    job = job_service.update_job(db, job_id, payload)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    # Re-extract JD metadata in background
    background_tasks.add_task(_bg_build_job_metadata, job.id)
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


# ── Download JD as Word Document ─────────────────────────────────────────────

@router.get(
    "/{job_id}/download-jd",
    summary="Download a generated Word document JD for a job",
    tags=["Jobs"],
)
def download_jd(job_id: int, db: Session = Depends(get_db)):
    import io
    import os
    from docx import Document
    from docx.shared import Pt, Inches, RGBColor
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from fastapi.responses import StreamingResponse
    from app.models.job import Job, JobSkill, JobResponsibility

    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    from app.models.job_metadata import JobMetadata

    skills = db.query(JobSkill).filter(JobSkill.job_id == job_id).all()
    responsibilities = db.query(JobResponsibility).filter(JobResponsibility.job_id == job_id).all()
    jd_meta = db.query(JobMetadata).filter(JobMetadata.job_id == job_id).first()

    doc = Document()

    # ── Styles ────────────────────────────────────────────────────────────
    style = doc.styles["Normal"]
    style.font.name = "Calibri"
    style.font.size = Pt(11)
    style.font.color.rgb = RGBColor(0x33, 0x33, 0x33)

    # ── Title ─────────────────────────────────────────────────────────────
    title_para = doc.add_heading(job.title or "Job Description", level=1)
    title_para.alignment = WD_ALIGN_PARAGRAPH.LEFT

    # ── Meta info ─────────────────────────────────────────────────────────
    meta_lines = []
    if job.department:
        meta_lines.append(f"Department: {job.department}")
    if job.location:
        meta_lines.append(f"Location: {job.location}")
    if job.job_type:
        meta_lines.append(f"Job Type: {job.job_type}")
    exp_min = job.experience_min or 0
    exp_max = job.experience_max or 0
    if exp_min or exp_max:
        meta_lines.append(f"Experience: {exp_min}–{exp_max} years")
    if job.openings:
        meta_lines.append(f"Openings: {job.openings}")

    if meta_lines:
        for line in meta_lines:
            p = doc.add_paragraph(line)
            p.paragraph_format.space_after = Pt(2)
            for run in p.runs:
                run.font.size = Pt(10)
                run.font.color.rgb = RGBColor(0x66, 0x66, 0x66)
        doc.add_paragraph("")  # spacer

    # ── Job Summary ───────────────────────────────────────────────────────
    summary_text = jd_meta.responsibilities_summary if jd_meta else None
    if summary_text:
        doc.add_heading("Job Summary", level=2)
        doc.add_paragraph(summary_text)

    # ── Required Skills ───────────────────────────────────────────────────
    if skills:
        doc.add_heading("Required Skills", level=2)

        for s in skills:
            label = str(s.skill_criteria.value) if s.skill_criteria else ""
            text = f"• {s.skill}"
            if label and label != "None":
                text += f"  ({label})"
            doc.add_paragraph(text)

    # ── Responsibilities ──────────────────────────────────────────────────
    if responsibilities:
        doc.add_heading("Responsibilities", level=2)
        for r in responsibilities:
            doc.add_paragraph(f"• {r.description}")

    # ── Hiring Manager ────────────────────────────────────────────────────
    if job.manager_name or job.manager_email or job.manager_phone:
        doc.add_heading("Hiring Manager", level=2)
        if job.manager_name:
            doc.add_paragraph(f"Name: {job.manager_name}")
        if job.manager_email:
            doc.add_paragraph(f"Email: {job.manager_email}")
        if job.manager_phone:
            doc.add_paragraph(f"Phone: {job.manager_phone}")

    # ── Save to stream ────────────────────────────────────────────────────
    buffer = io.BytesIO()
    doc.save(buffer)
    buffer.seek(0)

    safe_title = "".join(c for c in (job.title or "JD") if c.isalnum() or c in " -_").strip()
    filename = f"{safe_title} - Job Description.docx"

    return StreamingResponse(
        buffer,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ── Match Candidates From Database ───────────────────────────────────────────

@router.post(
    "/{job_id}/match-candidates",
    summary="Match candidates from resume database against this job",
    description=(
        "Compares all parsed resumes in the central resume_metadata table "
        "against this job's skills, keywords, title, and experience range.\n\n"
        "Pure keyword/skill matching — NO AI calls — instant even with 50k+ resumes.\n\n"
        "Query param `threshold` controls minimum fit score (default 60)."
    ),
    tags=["Jobs"],
)
def match_candidates(
    job_id: int,
    threshold: int = 60,
    db: Session = Depends(get_db),
):
    from app.services.matching_service import match_candidates_for_job

    job = job_service.get_job_by_id(db, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    result = match_candidates_for_job(db, job_id, threshold=threshold)

    return {
        "job_id": job_id,
        "total_in_database": result["total_scanned"],
        "matched_count": result["matched_count"],
        "candidates": result["candidates"],
    }


@router.post(
    "/{job_id}/add-matched-candidates",
    summary="Add selected matched candidates to this job",
    description=(
        "Takes a list of candidate emails (from the match-candidates results) "
        "and creates Candidate records linked to this job.\n\n"
        "Skips candidates who are already added to this job. "
        "Copies resume data from resume_metadata and queues fit-score computation."
    ),
    tags=["Jobs"],
)
async def add_matched_candidates(
    job_id: int,
    payload: dict,
    db: Session = Depends(get_db),
):
    from app.models.candidate import Candidate
    from app.models.resume_metadata import ResumeMetadata

    job = job_service.get_job_by_id(db, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    candidate_emails = payload.get("candidate_emails", [])
    if not candidate_emails:
        raise HTTPException(status_code=400, detail="No candidate emails provided")

    added = 0
    skipped = 0
    details = []

    for email in candidate_emails:
        # Check if already exists for this job
        existing = db.query(Candidate).filter(
            Candidate.email == email,
            Candidate.job_id == job_id,
        ).first()

        if existing:
            skipped += 1
            details.append({"email": email, "status": "skipped", "reason": "already exists"})
            continue

        # Get metadata
        meta = db.query(ResumeMetadata).filter(
            ResumeMetadata.candidate_email == email
        ).first()

        if not meta:
            skipped += 1
            details.append({"email": email, "status": "skipped", "reason": "no metadata found"})
            continue

        candidate = Candidate(
            name=meta.candidate_name or email.split("@")[0],
            email=meta.candidate_email,
            phone=meta.phone or "",
            location=meta.location or "",
            role=meta.current_designation or "",
            resume_path=meta.resume_file_path,
            job_id=job_id,
            interview_status="New",
            candidate_stage="Candidates",
            resume_metadata_id=meta.id,
            parsing_status="completed",
        )
        db.add(candidate)
        db.commit()
        db.refresh(candidate)
        added += 1
        details.append({"email": email, "status": "added", "candidate_id": candidate.id})

        # Queue background fit-score computation
        from app.routers.candidates import _parse_queue, _ensure_worker
        _ensure_worker()
        # Re-use the parse worker — it will skip re-parsing since metadata exists
        # but will compute fit score for this job
        if meta.resume_file_path:
            import asyncio
            await _parse_queue.put((candidate.id, meta.resume_file_path, job_id))

    return {
        "added": added,
        "skipped": skipped,
        "details": details,
    }
