import asyncio
import json
import logging
import os
import shutil
from datetime import datetime
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, Request, UploadFile, status
from sqlalchemy.orm import Session

from slowapi import Limiter
from slowapi.util import get_remote_address

from app.database import get_db, SessionLocal
from app.models.candidate import Candidate
from app.models.resume_metadata import ResumeMetadata

limiter = Limiter(key_func=get_remote_address)
from app.schemas.candidate import (
    CancelInterviewRequest,
    CandidateCreate,
    CandidateRead,
    CandidateUpdate,
    PromoteStageRequest,
    ScheduleInterviewRequest,
    SendWhatsAppRequest,
)
from app.schemas.resume_metadata import DuplicateCheckResponse
from app.services import candidate_service

router = APIRouter()

IS_SERVERLESS = bool(os.environ.get("VERCEL") or os.environ.get("AWS_LAMBDA_FUNCTION_NAME"))
UPLOAD_ROOT = "/tmp/uploads" if IS_SERVERLESS else "uploads"
RESUME_UPLOAD_DIR = f"{UPLOAD_ROOT}/resumes"
os.makedirs(RESUME_UPLOAD_DIR, exist_ok=True)

ALLOWED_RESUME_TYPES = {
    "application/pdf",
    "application/msword",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}

# ── Sequential background parse queue ────────────────────────────────────────
_parse_queue: asyncio.Queue = asyncio.Queue()
_worker_started = False


def _sync_extract_text(file_path: str) -> str:
    """Extract text from resume file — runs in thread pool."""
    from app.services.fast_resume_parser import extract_text_from_file

    text = extract_text_from_file(file_path)
    if not text:
        raise RuntimeError(f"Could not extract text from {file_path}")
    return text


async def _parse_worker():
    """
    Process resume parse jobs one at a time.
    Uses Claude AI for parsing (accurate names/details) + LLM fit score.
    """
    while True:
        candidate_id, file_path, job_id = await _parse_queue.get()
        db = SessionLocal()
        try:
            candidate = db.query(Candidate).filter(Candidate.id == candidate_id).first()
            if not candidate:
                continue

            candidate.parsing_status = "processing"
            db.commit()

            from app.models.job import Job, JobSkill, JobResponsibility

            # ── Step 1: Extract text in thread pool ───────────────────────────
            resume_text = await asyncio.to_thread(_sync_extract_text, file_path)

            # ── Step 2: AI parse for accurate name/details extraction ─────────
            parsed = None
            try:
                from app.services.resume_service import parse_resume
                from app.schemas.ai import ParseResumeRequest

                req = ParseResumeRequest(resume_text=resume_text)
                result = await parse_resume(req)
                ai_data = result.parsed_data.model_dump()

                # Map AI parser output to our expected format
                parsed = {
                    "candidate_name": ai_data.get("name"),
                    "candidate_email": ai_data.get("email"),
                    "phone": ai_data.get("phone"),
                    "current_designation": ai_data.get("current_designation") or ai_data.get("current_role"),
                    "experience_years": ai_data.get("experience_years"),
                    "skills": ai_data.get("skills") or [],
                    "education": ai_data.get("education") or [],
                    "certifications": ai_data.get("certifications") or [],
                    "work_experience": ai_data.get("work_experience") or [],
                    "location": ai_data.get("location"),
                    "personal_details": ai_data.get("personal_details") or {},
                    "keywords": ai_data.get("keywords") or [],
                    "summary": ai_data.get("summary"),
                }
                logger.info("AI parsed candidate %s: name=%s, email=%s", candidate_id, parsed.get("candidate_name"), parsed.get("candidate_email"))
            except Exception as ai_exc:
                logger.warning("AI parse failed for candidate %s, falling back to regex: %s", candidate_id, ai_exc)
                # Fallback to fast regex parser
                from app.services.fast_resume_parser import fast_parse_resume
                parsed = await asyncio.to_thread(fast_parse_resume, resume_text)

            # Update candidate with parsed data
            parsed_email = parsed.get("candidate_email") or candidate.email
            real_email = parsed_email if (parsed_email and not parsed_email.endswith("@resume.local")) else None

            # If we found a real email, check for duplicates for this job
            if real_email and real_email != candidate.email and job_id:
                existing = db.query(Candidate).filter(
                    Candidate.email == real_email,
                    Candidate.job_id == job_id,
                    Candidate.id != candidate.id,
                ).first()
                if existing:
                    # Duplicate — update existing, delete this placeholder
                    existing.resume_path = file_path
                    existing.parsing_status = "completed"
                    db.delete(candidate)
                    db.commit()
                    _parse_queue.task_done()
                    continue

            candidate.name = parsed.get("candidate_name") or candidate.name
            candidate.email = real_email or candidate.email
            candidate.phone = parsed.get("phone") or candidate.phone
            candidate.location = parsed.get("location") or candidate.location
            candidate.role = parsed.get("current_designation") or candidate.role

            # ── Upsert ResumeMetadata ─────────────────────────────────────────
            meta = None
            if parsed_email and not parsed_email.endswith("@resume.local"):
                meta = db.query(ResumeMetadata).filter(
                    ResumeMetadata.candidate_email == parsed_email
                ).first()

            if not meta:
                meta = ResumeMetadata(candidate_email=parsed_email or candidate.email)
                db.add(meta)

            meta.candidate_name = parsed.get("candidate_name")
            meta.phone = parsed.get("phone")
            meta.current_designation = parsed.get("current_designation")
            meta.experience_years = parsed.get("experience_years")
            meta.skills = json.dumps(parsed.get("skills", []))
            meta.education = json.dumps(parsed.get("education", []))
            meta.certifications = json.dumps(parsed.get("certifications", []))
            meta.work_experience = json.dumps(parsed.get("work_experience", []))
            meta.location = parsed.get("location")
            meta.personal_details = json.dumps(parsed.get("personal_details") or {})
            meta.keywords = json.dumps(parsed.get("keywords", []))
            meta.summary = parsed.get("summary")
            meta.resume_file_path = file_path
            meta.raw_text = resume_text
            meta.parsing_status = "completed"
            meta.parsed_at = datetime.utcnow()
            db.flush()

            candidate.resume_metadata_id = meta.id

            # ── Step 3: Compute fit score from metadata comparison ─────────
            if job_id:
                try:
                    from app.services.smart_scoring_service import compute_fit_score_for_candidate

                    logger.info("Computing fit score for candidate %s against job %s", candidate_id, job_id)
                    score_result = compute_fit_score_for_candidate(db, candidate_id, job_id)

                    if score_result:
                        candidate.resume_fit_score = score_result.fit_level
                        candidate.fit_score_reasoning = json.dumps(score_result.to_dict())
                        logger.info("Fit score for candidate %s: %s (%s%%)", candidate_id, score_result.fit_level, score_result.fit_percentage)
                    else:
                        logger.warning("Could not compute fit score for candidate %s — missing metadata", candidate_id)
                except Exception as fit_exc:
                    logger.error("Fit score computation FAILED for candidate %s (job %s): %s", candidate_id, job_id, fit_exc, exc_info=True)

            candidate.parsing_status = "completed"
            db.commit()

        except Exception as exc:
            logger.error("Background parse failed for candidate %s: %s", candidate_id, exc, exc_info=True)
            try:
                candidate = db.query(Candidate).filter(Candidate.id == candidate_id).first()
                if candidate:
                    candidate.parsing_status = "failed"
                    db.commit()
            except Exception:
                pass
        finally:
            db.close()
            _parse_queue.task_done()


def _ensure_worker():
    """Start the background parse worker if not already running."""
    global _worker_started
    if not _worker_started:
        _worker_started = True
        asyncio.get_event_loop().create_task(_parse_worker())


# ── Standard CRUD ─────────────────────────────────────────────────────────────

@router.get(
    "/",
    response_model=List[CandidateRead],
    summary="List candidates (deduplicated)",
    description=(
        "Return candidates with optional filters. Deduplicates by email.\n\n"
        "- `status`: Applied | Interview | Offered | Selected | Hired | Rejected\n"
        "- `job_id`: filter by opening\n"
        "- `stage`: Candidates | AI Round | Panel Round | Offer-Made | Rejected"
    ),
)
def list_candidates(
    status: Optional[str] = None,
    job_id: Optional[int] = None,
    stage: Optional[str] = None,
    db: Session = Depends(get_db),
):
    return candidate_service.get_candidates(db, status=status, job_id=job_id, stage=stage)


@router.post(
    "/",
    response_model=CandidateRead,
    status_code=status.HTTP_201_CREATED,
    summary="Create candidate manually",
)
def create_candidate(payload: CandidateCreate, db: Session = Depends(get_db)):
    candidate = candidate_service.create_candidate(db, payload)

    # Auto-compute fit score if candidate is linked to a job
    if candidate.job_id:
        try:
            from app.services.smart_scoring_service import compute_fit_score_for_candidate
            result = compute_fit_score_for_candidate(db, candidate.id, candidate.job_id)
            if result:
                candidate.resume_fit_score = result.fit_level
                candidate.fit_score_reasoning = json.dumps(result.to_dict())
                db.commit()
                db.refresh(candidate)
        except Exception:
            pass  # fit score is best-effort on manual add

    return candidate


@router.get(
    "/{candidate_id}",
    response_model=CandidateRead,
    summary="Get candidate by ID",
)
def get_candidate(candidate_id: int, db: Session = Depends(get_db)):
    candidate = candidate_service.get_candidate_by_id(db, candidate_id)
    if not candidate:
        raise HTTPException(status_code=404, detail="Candidate not found")
    return candidate


@router.put("/{candidate_id}", response_model=CandidateRead, summary="Update candidate")
def update_candidate(
    candidate_id: int, payload: CandidateUpdate, db: Session = Depends(get_db)
):
    candidate = candidate_service.update_candidate(db, candidate_id, payload)
    if not candidate:
        raise HTTPException(status_code=404, detail="Candidate not found")
    return candidate


@router.delete("/{candidate_id}", status_code=status.HTTP_204_NO_CONTENT, summary="Delete candidate")
def delete_candidate(candidate_id: int, db: Session = Depends(get_db)):
    ok = candidate_service.delete_candidate(db, candidate_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Candidate not found")


# ── Hire Candidate ──────────────────────────────────────────────────────────

@router.post(
    "/{candidate_id}/hire",
    summary="Hire a candidate — updates status across all job associations",
    description=(
        "Marks the candidate as Hired. If `job_id` is provided and the candidate "
        "is not already in that job, adds them. Updates the candidate's status to "
        "'Offer-Made' in the linked job as well."
    ),
)
def hire_candidate(
    candidate_id: int,
    payload: dict = None,
    db: Session = Depends(get_db),
):
    from app.models.job import Job

    candidate = db.query(Candidate).filter(Candidate.id == candidate_id).first()
    if not candidate:
        raise HTTPException(status_code=404, detail="Candidate not found")

    job_id = (payload or {}).get("job_id")

    # Find all job associations for this candidate (by email)
    email = candidate.email
    all_entries = db.query(Candidate).filter(
        Candidate.email == email,
        Candidate.job_id.isnot(None),
    ).all()

    job_ids_linked = [c.job_id for c in all_entries]

    # If job_id provided and candidate not in that job, add them
    if job_id and job_id not in job_ids_linked:
        job = db.query(Job).filter(Job.id == job_id).first()
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")
        new_entry = Candidate(
            name=candidate.name,
            role=candidate.role or job.title,
            email=candidate.email,
            phone=candidate.phone,
            location=candidate.location,
            status="Hired",
            image=candidate.image,
            job_id=job_id,
            resume_path=candidate.resume_path,
            candidate_stage="Offer-Made",
            interview_status=candidate.interview_status,
        )
        db.add(new_entry)
        job_ids_linked.append(job_id)

    # Update status of all entries for this email to reflect hire
    for entry in all_entries:
        entry.candidate_stage = "Offer-Made"
        if entry.id == candidate_id or (job_id and entry.job_id == job_id):
            entry.status = "Hired"

    # Update the main candidate record
    candidate.status = "Hired"
    db.commit()
    db.refresh(candidate)

    # Return enriched data
    hired_job_title = None
    if job_id:
        job = db.query(Job).filter(Job.id == job_id).first()
        if job:
            hired_job_title = job.title
    elif candidate.job_id:
        job = db.query(Job).filter(Job.id == candidate.job_id).first()
        if job:
            hired_job_title = job.title

    return {
        "id": candidate.id,
        "name": candidate.name,
        "email": candidate.email,
        "status": "Hired",
        "job_id": job_id or candidate.job_id,
        "job_title": hired_job_title,
        "linked_jobs": job_ids_linked,
    }


@router.get(
    "/{candidate_id}/job-associations",
    summary="Get all jobs this candidate is linked to",
)
def get_job_associations(candidate_id: int, db: Session = Depends(get_db)):
    from app.models.job import Job

    candidate = db.query(Candidate).filter(Candidate.id == candidate_id).first()
    if not candidate:
        raise HTTPException(status_code=404, detail="Candidate not found")

    entries = (
        db.query(Candidate.job_id, Candidate.status, Candidate.candidate_stage, Job.title)
        .outerjoin(Job, Candidate.job_id == Job.id)
        .filter(Candidate.email == candidate.email, Candidate.job_id.isnot(None))
        .all()
    )

    return {
        "candidate_id": candidate.id,
        "email": candidate.email,
        "jobs": [
            {
                "job_id": job_id,
                "job_title": title,
                "status": s,
                "stage": stage,
            }
            for job_id, s, stage, title in entries
        ],
    }


# ── Duplicate Check ──────────────────────────────────────────────────────────

@router.get(
    "/check-duplicate",
    response_model=DuplicateCheckResponse,
    summary="Check if a resume/candidate already exists by email",
    description=(
        "Checks the central resume_metadata table to see if a candidate with "
        "this email already has a parsed resume. Also returns which job_ids "
        "the candidate is already linked to."
    ),
)
def check_duplicate(
    email: str,
    job_id: Optional[int] = None,
    db: Session = Depends(get_db),
):
    meta = db.query(ResumeMetadata).filter(
        ResumeMetadata.candidate_email == email
    ).first()

    if not meta:
        return DuplicateCheckResponse(exists=False)

    linked_jobs = [
        c.job_id for c in db.query(Candidate.job_id).filter(
            Candidate.email == email, Candidate.job_id.isnot(None)
        ).all()
    ]

    return DuplicateCheckResponse(
        exists=True,
        candidate_email=meta.candidate_email,
        candidate_name=meta.candidate_name,
        current_designation=meta.current_designation,
        resume_file_path=meta.resume_file_path,
        parsed_at=meta.parsed_at,
        linked_job_ids=linked_jobs,
    )


# ── Parsing Status ───────────────────────────────────────────────────────────

@router.get(
    "/{candidate_id}/parsing-status",
    summary="Get resume parsing status for a candidate",
)
def get_parsing_status(candidate_id: int, db: Session = Depends(get_db)):
    candidate = db.query(Candidate).filter(Candidate.id == candidate_id).first()
    if not candidate:
        raise HTTPException(status_code=404, detail="Candidate not found")
    return {
        "candidate_id": candidate.id,
        "parsing_status": candidate.parsing_status,
        "name": candidate.name,
        "email": candidate.email,
        "resume_fit_score": candidate.resume_fit_score,
        "fit_score_reasoning": candidate.fit_score_reasoning,
    }


# ── Resume Upload & Async Background Parse ──────────────────────────────────

@router.post(
    "/upload-resume",
    response_model=List[CandidateRead],
    status_code=status.HTTP_201_CREATED,
    summary="Upload resumes (batch)",
    description=(
        "Upload one or more PDF/DOC/DOCX resume files.\n\n"
        "Files are saved immediately and candidate records created with "
        "`parsing_status='pending'`. Parsing runs **one at a time** in the "
        "background to avoid overloading the AI. Poll "
        "`GET /candidates/{id}/parsing-status` to track progress.\n\n"
        "If `replace_existing=true` and a resume for the same email already "
        "exists in metadata, the old metadata is replaced.\n\n"
        "**Content-Type:** `multipart/form-data`"
    ),
)
@limiter.limit("10/minute")
async def upload_resumes(
    request: Request,
    job_id: Optional[int] = Form(None),
    replace_existing: Optional[bool] = Form(False),
    files: List[UploadFile] = File(...),
    db: Session = Depends(get_db),
):
    """
    Upload one or more resumes. Files are saved to disk, candidate records
    created with parsing_status='pending', and parsing queued in background
    (one-at-a-time to conserve AI resources).
    """
    _ensure_worker()
    created_candidates = []

    for file in files:
        if file.content_type not in ALLOWED_RESUME_TYPES:
            raise HTTPException(
                status_code=400,
                detail=f"Unsupported file type for {file.filename}. Allowed: PDF, DOC, DOCX",
            )

        safe_name = f"{datetime.utcnow().strftime('%Y%m%d%H%M%S%f')}_{file.filename}"
        file_path = os.path.join(RESUME_UPLOAD_DIR, safe_name)
        with open(file_path, "wb") as f:
            shutil.copyfileobj(file.file, f)

        # Use filename stem as placeholder name until parse completes
        placeholder_name = Path(file.filename).stem
        placeholder_email = f"pending_{safe_name[:20]}@resume.local"

        # If same email+job candidate exists, update their resume file & re-queue
        # (We won't know the real email until parse, but for re-uploads from the
        # duplicate-check flow the frontend can pass replace_existing=true)

        candidate = Candidate(
            name=placeholder_name,
            email=placeholder_email,
            phone="",
            location="",
            role="",
            resume_path=file_path,
            job_id=job_id,
            interview_status="New",
            candidate_stage="Candidates",
            parsing_status="pending",
        )
        db.add(candidate)
        db.commit()
        db.refresh(candidate)
        created_candidates.append(candidate)

        # Queue for background processing (one-at-a-time)
        await _parse_queue.put((candidate.id, file_path, job_id))

    return created_candidates


# ── Fit Score Recompute ───────────────────────────────────────────────────────

@router.post(
    "/{candidate_id}/compute-fit-score",
    response_model=CandidateRead,
    summary="Recompute resume fit score",
    description="Re-runs Claude LLM fit-score calculation for an existing candidate against their linked job. Requires resume file on disk and a linked `job_id`.",
)
async def compute_fit_score(candidate_id: int, db: Session = Depends(get_db)):
    """Re-compute fit score using smart scoring (ML → Algorithm). No AI cost."""
    candidate = db.query(Candidate).filter(Candidate.id == candidate_id).first()
    if not candidate:
        raise HTTPException(status_code=404, detail="Candidate not found")
    if not candidate.job_id:
        raise HTTPException(status_code=400, detail="Candidate is not linked to a job")

    from app.services.smart_scoring_service import compute_fit_score_for_candidate
    result = compute_fit_score_for_candidate(db, candidate_id, candidate.job_id)

    if not result:
        raise HTTPException(status_code=400, detail="Could not compute score — missing resume metadata")

    candidate.resume_fit_score = result.fit_level
    candidate.fit_score_reasoning = json.dumps(result.to_dict())
    db.commit()
    db.refresh(candidate)
    return candidate


# ── Interview Scheduling ──────────────────────────────────────────────────────

@router.post(
    "/{candidate_id}/schedule-interview",
    summary="Schedule / reschedule interview",
    description=(
        "Set the interview date/time for a candidate and trigger notifications.\n\n"
        "- Saves `interview_date`, `interview_timezone`, `joining_flexibility`, `allow_candidate_reschedule`\n"
        "- Sets `interview_status` to **Scheduled**\n"
        "- Sends HTML email via SMTP (requires `SMTP_*` config in `.env`)\n"
        "- Optionally sends WhatsApp message via Meta Cloud API (`send_whatsapp: true`)\n"
        "- Saves custom email template overrides in `schedule_email_template` (JSON)"
    ),
)
async def schedule_interview(
    candidate_id: int,
    payload: ScheduleInterviewRequest,
    db: Session = Depends(get_db),
):
    """Schedule (or re-schedule) an interview for a candidate and send notifications."""
    from app.services.interview_service import send_schedule_email, send_whatsapp_message
    from app.models.interview import InterviewStatusLog

    candidate = db.query(Candidate).filter(Candidate.id == candidate_id).first()
    if not candidate:
        raise HTTPException(status_code=404, detail="Candidate not found")

    old_status = candidate.interview_status
    is_reschedule = old_status == "Scheduled"

    # Log status change
    log = InterviewStatusLog(
        candidate_id=candidate.id,
        previous_status=old_status,
        new_status="Scheduled",
        changed_by="admin",
        reason="Rescheduled" if is_reschedule else "Scheduled",
    )
    db.add(log)

    candidate.interview_date = payload.interview_date
    candidate.interview_timezone = payload.interview_timezone
    candidate.joining_flexibility = payload.joining_flexibility
    candidate.allow_candidate_reschedule = payload.allow_candidate_reschedule
    candidate.allow_joining_early = payload.allow_joining_early
    candidate.interview_status = "Scheduled"

    # Save the frontend-provided interview URL for email links
    if payload.interview_url:
        candidate.interview_link = payload.interview_url

    # Update token expiry if set
    if payload.link_expiry and candidate.job_id:
        from app.models.interview import InterviewToken
        from datetime import timedelta
        token_row = (
            db.query(InterviewToken)
            .filter(InterviewToken.job_id == candidate.job_id, InterviewToken.is_active == True)
            .first()
        )
        if token_row:
            if payload.link_expiry.unit == "hours":
                token_row.expires_at = datetime.utcnow() + timedelta(hours=payload.link_expiry.value)
            else:
                token_row.expires_at = datetime.utcnow() + timedelta(days=payload.link_expiry.value)

    template_data = {
        "subject": payload.email_subject,
        "intro_text": payload.email_intro_text,
        "extra_heading": payload.email_extra_heading,
        "extra_body": payload.email_extra_body,
        "cc_emails": payload.cc_emails or [],
    }
    candidate.schedule_email_template = json.dumps(template_data)
    db.commit()
    db.refresh(candidate)

    email_sent = False
    whatsapp_sent = False

    try:
        email_sent = await send_schedule_email(candidate, db, is_reschedule=is_reschedule)
        if not email_sent:
            logger.warning("Email send returned False for candidate %s", candidate.id)
    except Exception:
        logger.warning("Failed to send schedule email for candidate %s", candidate.id, exc_info=True)

    if payload.send_whatsapp and candidate.phone:
        try:
            from app.models.job import Job
            job = db.query(Job).filter(Job.id == candidate.job_id).first()
            job_title = job.title if job else "the role"
            msg = _build_whatsapp_message(candidate, job_title, is_reschedule=is_reschedule)
            await send_whatsapp_message(candidate.phone, msg)
            whatsapp_sent = True
        except Exception:
            logger.warning("Failed to send WhatsApp to candidate %s", candidate.id, exc_info=True)

    # Return candidate data + notification status
    result = CandidateRead.model_validate(candidate)
    response_dict = result.model_dump()
    response_dict["email_sent"] = email_sent
    response_dict["whatsapp_sent"] = whatsapp_sent
    return response_dict


# ── Cancel Interview ─────────────────────────────────────────────────────────

@router.post(
    "/{candidate_id}/cancel-interview",
    response_model=CandidateRead,
    summary="Cancel a scheduled interview",
    description=(
        "Cancels a scheduled interview for a candidate.\n\n"
        "- Sets `interview_status` to **Cancelled**\n"
        "- Clears `interview_date`\n"
        "- Logs the status change to `interview_status_logs`\n"
        "- Sends cancellation email + optional WhatsApp\n\n"
        "Only works when current status is 'Scheduled'."
    ),
)
async def cancel_interview(
    candidate_id: int,
    payload: CancelInterviewRequest,
    db: Session = Depends(get_db),
):
    """Cancel a scheduled interview and notify the candidate."""
    from app.services.interview_service import send_cancel_email, send_whatsapp_message
    from app.models.interview import InterviewStatusLog

    candidate = db.query(Candidate).filter(Candidate.id == candidate_id).first()
    if not candidate:
        raise HTTPException(status_code=404, detail="Candidate not found")

    if candidate.interview_status != "Scheduled":
        raise HTTPException(
            status_code=400,
            detail=f"Cannot cancel: current status is '{candidate.interview_status}', expected 'Scheduled'",
        )

    # Log status change
    log = InterviewStatusLog(
        candidate_id=candidate.id,
        previous_status=candidate.interview_status,
        new_status="Cancelled",
        changed_by="admin",
        reason=payload.reason or "Cancelled by admin",
    )
    db.add(log)

    candidate.interview_status = "Cancelled"
    candidate.interview_date = None
    db.commit()
    db.refresh(candidate)

    # Send cancellation notifications
    try:
        await send_cancel_email(candidate, db)
    except Exception:
        logger.warning("Failed to send cancel email for candidate %s", candidate.id, exc_info=True)

    if payload.send_whatsapp and candidate.phone:
        try:
            from app.models.job import Job
            job = db.query(Job).filter(Job.id == candidate.job_id).first()
            job_title = job.title if job else "the role"
            msg = (
                f"Hi {candidate.name},\n"
                f"Your interview for the role of {job_title} at CGBiz Solutions Private Limited "
                f"has been cancelled. Please contact HR for further details."
            )
            await send_whatsapp_message(candidate.phone, msg)
        except Exception:
            logger.warning("Failed to send WhatsApp cancellation to candidate %s", candidate.id, exc_info=True)

    return candidate


# ── Stage Promotion ───────────────────────────────────────────────────────────

@router.post(
    "/{candidate_id}/promote",
    response_model=CandidateRead,
    summary="Promote candidate to a hiring stage",
    description="Move candidate between stages: `Candidates` → `AI Round` → `Panel Round` → `Offer-Made` / `Rejected`. Also updates `status` to Offered/Rejected when appropriate.",
)
def promote_candidate(
    candidate_id: int,
    payload: PromoteStageRequest,
    db: Session = Depends(get_db),
):
    """Move candidate to a new hiring stage."""
    valid_stages = {"AI Round", "Panel Round", "Offer-Made", "Rejected", "Candidates"}
    if payload.stage not in valid_stages:
        raise HTTPException(status_code=400, detail=f"Invalid stage. Must be one of: {valid_stages}")

    candidate = db.query(Candidate).filter(Candidate.id == candidate_id).first()
    if not candidate:
        raise HTTPException(status_code=404, detail="Candidate not found")

    candidate.candidate_stage = payload.stage
    if payload.stage == "Offer-Made":
        candidate.status = "Offered"
    elif payload.stage == "Rejected":
        candidate.status = "Rejected"

    db.commit()
    db.refresh(candidate)
    return candidate


# ── Send WhatsApp ─────────────────────────────────────────────────────────────

@router.post(
    "/{candidate_id}/send-whatsapp",
    summary="Send WhatsApp message",
    description="Send a custom free-form text message to the candidate via Meta WhatsApp Business Cloud API. Requires `WHATSAPP_TOKEN` and `WHATSAPP_PHONE_NUMBER_ID` in `.env`.",
)
async def send_whatsapp(
    candidate_id: int,
    payload: SendWhatsAppRequest,
    db: Session = Depends(get_db),
):
    """Send a custom WhatsApp message to a candidate."""
    from app.services.interview_service import send_whatsapp_message

    candidate = db.query(Candidate).filter(Candidate.id == candidate_id).first()
    if not candidate:
        raise HTTPException(status_code=404, detail="Candidate not found")

    phone = payload.phone or candidate.phone
    if not phone:
        raise HTTPException(status_code=400, detail="No phone number available")

    try:
        result = await send_whatsapp_message(phone, payload.message)
        return {"success": True, "detail": result}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"WhatsApp send failed: {str(exc)}")


# ── Evaluation Report ─────────────────────────────────────────────────────────

@router.get(
    "/{candidate_id}/evaluation",
    summary="Get full candidate evaluation report",
    description=(
        "Returns a consolidated report for the `/evaluation/[candidateId]` page.\n\n"
        "Includes:\n"
        "- Candidate profile and stage\n"
        "- Resume file URL\n"
        "- Fit score level + reasoning breakdown (from Claude LLM)\n"
        "- Post-interview scores (grammar, confidence, clarity, trust, pronunciation, enthusiasm, conciseness, ai_cheating)\n"
        "- Recording link and duration\n\n"
        "Score fields are `null` until the interview is completed."
    ),
)
def get_evaluation(candidate_id: int, db: Session = Depends(get_db)):
    """Return full evaluation data: resume analysis + scores + interview Q&A transcript."""
    from app.models.interview import InterviewSession, InterviewMessage, SessionStatus, Evaluation

    candidate = db.query(Candidate).filter(Candidate.id == candidate_id).first()
    if not candidate:
        raise HTTPException(status_code=404, detail="Candidate not found")

    fit_reasoning = None
    fit_details = None
    if candidate.fit_score_reasoning:
        try:
            fit_reasoning = json.loads(candidate.fit_score_reasoning)
        except Exception:
            fit_reasoning = candidate.fit_score_reasoning
    if candidate.fit_score_details:
        try:
            fit_details = json.loads(candidate.fit_score_details)
        except Exception:
            fit_details = candidate.fit_score_details

    resume_url = None
    if candidate.resume_path and os.path.exists(candidate.resume_path):
        resume_url = f"/{candidate.resume_path.replace(os.sep, '/')}"

    # ── Q&A transcript from latest completed session ──────────────────────────
    transcript = []
    evaluation_detail = None

    latest_session = (
        db.query(InterviewSession)
        .filter(
            InterviewSession.candidate_id == candidate_id,
            InterviewSession.status == SessionStatus.completed,
        )
        .order_by(InterviewSession.completed_at.desc())
        .first()
    )

    if latest_session:
        messages = (
            db.query(InterviewMessage)
            .filter(InterviewMessage.session_id == latest_session.id)
            .order_by(InterviewMessage.created_at)
            .all()
        )
        # Group by question_index into Q&A pairs
        qa_map: dict = {}
        for msg in messages:
            qi = msg.question_index or 0
            if qi == 0:
                continue  # skip opening/closing system turns without index
            if qi not in qa_map:
                qa_map[qi] = {"question": None, "answer": None, "index": qi}
            if msg.role == "ai" and qa_map[qi]["question"] is None:
                qa_map[qi]["question"] = msg.content
            elif msg.role == "candidate" and qa_map[qi]["answer"] is None:
                qa_map[qi]["answer"] = msg.content
        transcript = [
            v for _, v in sorted(qa_map.items())
            if v["question"] and v["answer"]
        ]

        # ── Latest AI evaluation for this session ─────────────────────────────
        ev = (
            db.query(Evaluation)
            .filter(Evaluation.session_id == latest_session.id)
            .first()
        )
        if ev:
            try:
                strengths = json.loads(ev.strengths) if ev.strengths else []
            except Exception:
                strengths = []
            try:
                weaknesses = json.loads(ev.weaknesses) if ev.weaknesses else []
            except Exception:
                weaknesses = []
            evaluation_detail = {
                "recommendation": ev.recommendation.value if ev.recommendation else None,
                "summary": ev.summary,
                "strengths": strengths,
                "weaknesses": weaknesses,
                "detailed_feedback": ev.detailed_feedback,
                "meets_threshold": ev.meets_threshold or False,
            }

    return {
        "candidate": {
            "id": candidate.id,
            "name": candidate.name,
            "email": candidate.email,
            "phone": candidate.phone,
            "location": candidate.location,
            "role": candidate.role,
            "candidate_stage": candidate.candidate_stage,
            "interview_status": candidate.interview_status,
            "interview_date": candidate.interview_date.isoformat() if candidate.interview_date else None,
            "interview_timezone": candidate.interview_timezone,
        },
        "resume": {
            "path": candidate.resume_path,
            "url": resume_url,
        },
        "fit_score": {
            "level": candidate.resume_fit_score,
            "reasoning": fit_reasoning,
            "details": fit_details,
        },
        "scores": {
            "overall": candidate.score,
            "trust": candidate.trust_score,
            "grammar": candidate.grammar_score,
            "confidence": candidate.confidence_score,
            "clarity": candidate.clarity_score,
            "pronunciation": candidate.pronunciation_score,
            "enthusiasm": candidate.enthusiasm_score,
            "conciseness": candidate.conciseness_score,
            "ai_cheating": candidate.ai_cheating_score,
        },
        "recording": {
            "link": candidate.recording_link,
            "duration": candidate.video_duration,
        },
        "transcript": transcript,
        "evaluation": evaluation_detail,
    }


# ── Helpers ───────────────────────────────────────────────────────────────────

def _build_whatsapp_message(candidate: Candidate, job_title: str, is_reschedule: bool = False) -> str:
    action = "rescheduled" if is_reschedule else "scheduled"
    date_str = ""
    if candidate.interview_date:
        date_str = candidate.interview_date.strftime("%B %d, %Y, %I:%M %p")
        if candidate.interview_timezone:
            date_str += f" {candidate.interview_timezone}"
    link = candidate.interview_link or ""
    return (
        f"Hi {candidate.name},\n"
        f"Your Interview for the role of {job_title} by CGBiz Solutions Private limited "
        f"has been {action} on {date_str}. "
        f"Please ensure you're ready.\n\n"
        f"Interview Link: {link}"
    )
