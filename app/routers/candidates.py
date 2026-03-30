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

from app.auth import get_current_user, get_org_id
from app.database import get_db, SessionLocal
from app.models.candidate import Candidate
from app.models.organization import Organization
from app.models.resume_metadata import ResumeMetadata
from app.models.user import User

limiter = Limiter(key_func=get_remote_address)


def _get_candidate_with_org_check(db: Session, candidate_id: int, user: User) -> Candidate:
    """Fetch a candidate and verify it belongs to the user's organization."""
    from app.models.job import Job as JobModel
    candidate = db.query(Candidate).filter(Candidate.id == candidate_id).first()
    if not candidate:
        raise HTTPException(status_code=404, detail="Candidate not found")
    org_id = get_org_id(user)
    if org_id is not None and candidate.job_id:
        job = db.query(JobModel).filter(JobModel.id == candidate.job_id).first()
        if not job or job.organization_id != org_id:
            raise HTTPException(status_code=404, detail="Candidate not found")
    return candidate


def _check_and_increment_candidate_quota(db: Session, job_id: int) -> None:
    """Check lifetime candidate quota and increment counter. Raises 403 if exceeded."""
    from app.models.job import Job as JobModel
    job = db.query(JobModel).filter(JobModel.id == job_id).first()
    if not job or not job.organization_id:
        return
    org = db.query(Organization).filter(Organization.id == job.organization_id).first()
    if not org or (org.max_candidates_per_job or 0) == 0:
        return
    if (org.total_candidates_used or 0) >= org.max_candidates_per_job:
        raise HTTPException(
            status_code=403,
            detail="You have exceeded the candidate limit. Please upgrade your plan.",
        )
    org.total_candidates_used = (org.total_candidates_used or 0) + 1
    db.flush()
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

router = APIRouter(dependencies=[Depends(get_current_user)])

IS_SERVERLESS = bool(os.environ.get("VERCEL") or os.environ.get("AWS_LAMBDA_FUNCTION_NAME"))
UPLOAD_ROOT = "/tmp/uploads" if IS_SERVERLESS else "uploads"
RESUME_UPLOAD_DIR = f"{UPLOAD_ROOT}/resumes"
os.makedirs(RESUME_UPLOAD_DIR, exist_ok=True)

ALLOWED_RESUME_TYPES = {
    "application/pdf",
    "application/msword",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}

# ── Concurrent background parse pool ─────────────────────────────────────────
_parse_semaphore: asyncio.Semaphore | None = None


def _get_parse_semaphore() -> asyncio.Semaphore:
    """Lazy-init semaphore inside the running event loop."""
    global _parse_semaphore
    if _parse_semaphore is None:
        _parse_semaphore = asyncio.Semaphore(3)
    return _parse_semaphore


def _sync_extract_text(file_path: str) -> str:
    """Extract text from resume file — runs in thread pool."""
    from app.services.fast_resume_parser import extract_text_from_file

    text = extract_text_from_file(file_path)
    if not text:
        raise RuntimeError(f"Could not extract text from {file_path}")
    return text


async def _parse_one(candidate_id: int, file_path: str, job_id: int | None):
    """
    Parse a single resume concurrently (guarded by semaphore).
    Uses Haiku (~1-2s) for parsing + smart scoring (~50ms) for fit score.
    Total: ~3-5s per resume vs ~30-40s with old Sonnet approach.
    """
    async with _get_parse_semaphore():
        db = SessionLocal()
        try:
            candidate = db.query(Candidate).filter(Candidate.id == candidate_id).first()
            if not candidate:
                return

            candidate.parsing_status = "processing"
            db.commit()

            from app.models.job import Job, JobSkill, JobResponsibility

            # Determine organization_id from the job
            _org_id = None
            if job_id:
                _job = db.query(Job).filter(Job.id == job_id).first()
                if _job:
                    _org_id = _job.organization_id

            # ── Step 1: Extract text in thread pool ───────────────────────────
            resume_text = await asyncio.to_thread(_sync_extract_text, file_path)

            # ── Step 2: Parse resume using Haiku (fast, ~1-2s) ─────────────
            parsed = None

            # Primary: Haiku LLM parser (fast + accurate)
            try:
                from app.services.llm_resume_parser import llm_parse_resume
                parsed = await llm_parse_resume(resume_text)
                if parsed and parsed.get("candidate_name"):
                    logger.info("Haiku parsed candidate %s: name=%s", candidate_id, parsed.get("candidate_name"))
                else:
                    parsed = None  # Haiku returned empty — fall through
            except Exception as haiku_exc:
                logger.warning("Haiku parse failed for candidate %s: %s", candidate_id, haiku_exc)

            # Fallback: fast regex parser (instant, no AI cost)
            if not parsed:
                try:
                    from app.services.fast_resume_parser import fast_parse_resume
                    parsed = await asyncio.to_thread(fast_parse_resume, resume_text)
                    logger.info("Regex parsed candidate %s: name=%s", candidate_id, parsed.get("candidate_name"))
                except Exception as regex_exc:
                    logger.error("Regex parse also failed for candidate %s: %s", candidate_id, regex_exc)
                    parsed = {}

            # Extract email from resume text if parser didn't find it
            if not parsed.get("candidate_email"):
                import re as _re
                email_match = _re.search(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}', resume_text or "")
                if email_match:
                    parsed["candidate_email"] = email_match.group(0)

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
                    # Duplicate — update existing candidate with new resume data, delete placeholder
                    existing.resume_path = file_path
                    existing.name = parsed.get("candidate_name") or existing.name
                    existing.phone = parsed.get("phone") or existing.phone
                    existing.location = parsed.get("location") or existing.location
                    existing.role = parsed.get("current_designation") or existing.role
                    existing.parsing_status = "processing"
                    # Reset interview state — this is a fresh submission for this job
                    existing.interview_status = "New"
                    existing.interview_date = None
                    existing.interview_timezone = None
                    existing.candidate_stage = "Candidates"

                    # Update ResumeMetadata for existing candidate (org-scoped)
                    ex_meta_q = db.query(ResumeMetadata).filter(
                        ResumeMetadata.candidate_email == real_email
                    )
                    if _org_id is not None:
                        ex_meta_q = ex_meta_q.filter(ResumeMetadata.organization_id == _org_id)
                    ex_meta = ex_meta_q.first()
                    if not ex_meta:
                        ex_meta = ResumeMetadata(candidate_email=real_email, organization_id=_org_id)
                        db.add(ex_meta)
                    ex_meta.candidate_name = parsed.get("candidate_name")
                    ex_meta.phone = parsed.get("phone")
                    ex_meta.current_designation = parsed.get("current_designation")
                    ex_meta.experience_years = parsed.get("experience_years")
                    ex_meta.skills = json.dumps(parsed.get("skills", []))
                    ex_meta.education = json.dumps(parsed.get("education", []))
                    ex_meta.certifications = json.dumps(parsed.get("certifications", []))
                    ex_meta.work_experience = json.dumps(parsed.get("work_experience", []))
                    ex_meta.location = parsed.get("location")
                    ex_meta.personal_details = json.dumps(parsed.get("personal_details") or {})
                    ex_meta.keywords = json.dumps(parsed.get("keywords", []))
                    ex_meta.summary = parsed.get("summary")
                    ex_meta.resume_file_path = file_path
                    ex_meta.raw_text = resume_text
                    ex_meta.parsing_status = "completed"
                    ex_meta.parsed_at = datetime.utcnow()
                    db.flush()

                    existing.resume_metadata_id = ex_meta.id

                    # Compute fit score for existing candidate
                    if job_id:
                        await _compute_fit_score_with_fallback(existing, db, job_id, resume_text)

                    existing.parsing_status = "completed"
                    db.delete(candidate)
                    db.commit()
                    return

            candidate.name = parsed.get("candidate_name") or candidate.name
            candidate.email = real_email or candidate.email
            candidate.phone = parsed.get("phone") or candidate.phone
            candidate.location = parsed.get("location") or candidate.location
            candidate.role = parsed.get("current_designation") or candidate.role

            # ── Upsert ResumeMetadata (org-scoped) ────────────────────────────
            meta = None
            if parsed_email and not parsed_email.endswith("@resume.local"):
                meta_q = db.query(ResumeMetadata).filter(
                    ResumeMetadata.candidate_email == parsed_email
                )
                if _org_id is not None:
                    meta_q = meta_q.filter(ResumeMetadata.organization_id == _org_id)
                meta = meta_q.first()

            if not meta:
                meta = ResumeMetadata(
                    candidate_email=parsed_email or candidate.email,
                    organization_id=_org_id,
                )
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

            # ── Step 3: Compute fit score (smart scoring, instant) ────────────
            if job_id:
                await _compute_fit_score_with_fallback(candidate, db, job_id, resume_text)

            candidate.parsing_status = "completed"

            # ── Increment quota only on successful parse ─────────────────────
            if job_id:
                from app.models.job import Job as _QJob
                _qjob = db.query(_QJob).filter(_QJob.id == job_id).first()
                if _qjob and _qjob.organization_id:
                    _qorg = db.query(Organization).filter(
                        Organization.id == _qjob.organization_id
                    ).first()
                    if _qorg:
                        _qorg.total_candidates_used = (_qorg.total_candidates_used or 0) + 1

            db.commit()

        except Exception as exc:
            logger.error("Background parse failed for candidate %s: %s", candidate_id, exc, exc_info=True)
            try:
                db.rollback()
                candidate = db.query(Candidate).filter(Candidate.id == candidate_id).first()
                if candidate:
                    db.delete(candidate)
                    db.commit()
                    logger.info("Deleted failed candidate %s", candidate_id)
                # Clean up the uploaded file
                if file_path and os.path.isfile(file_path):
                    os.remove(file_path)
                    logger.info("Deleted failed resume file: %s", file_path)
            except Exception as cleanup_exc:
                logger.warning("Cleanup after parse failure: %s", cleanup_exc)
        finally:
            db.close()


async def _compute_fit_score_with_fallback(
    candidate: Candidate, db: Session, job_id: int, resume_text: str | None = None
):
    """
    Compute fit score using smart scoring (instant, same as database matches).
    Falls back to AI semantic scoring only if smart scoring returns no result.
    """
    # Primary: Smart scoring — instant (<50ms), same engine as database matches
    try:
        from app.services.smart_scoring_service import compute_fit_score_for_candidate as smart_score
        result = smart_score(db, candidate.id, job_id)
        if result:
            candidate.resume_fit_score = f"{result.fit_percentage}%"
            candidate.fit_score_reasoning = json.dumps(result.to_dict())
            candidate.fit_score_details = json.dumps({
                "score_breakdown": {k: v["score"] for k, v in result.breakdown.items()} if result.breakdown else {},
                "source": "smart_scoring",
            })
            db.commit()
            db.refresh(candidate)
            logger.info("Smart scoring for candidate %s: %s%% (%s)", candidate.id, result.fit_percentage, result.fit_level)
            return
    except Exception as smart_exc:
        logger.warning("Smart scoring failed for candidate %s, trying AI fallback: %s", candidate.id, smart_exc)

    # Fallback: AI semantic scoring (slower, ~15-20s, but works without metadata)
    try:
        logger.info("Computing AI fit score for candidate %s against job %s", candidate.id, job_id)
        score_data = await _ai_fit_score_for_candidate(candidate, db, resume_text=resume_text)
        if score_data:
            logger.info("AI fit score for candidate %s: %s%% (%s)", candidate.id, score_data.get("fit_percentage"), score_data.get("fit_level"))
        else:
            logger.warning("Could not compute fit score for candidate %s — missing data", candidate.id)
    except Exception as fit_exc:
        logger.error("AI fit score also FAILED for candidate %s: %s", candidate.id, fit_exc, exc_info=True)


_background_tasks: list[asyncio.Task] = []  # prevent GC of fire-and-forget tasks


def _launch_parse_task(candidate_id: int, file_path: str, job_id: int | None):
    """Fire-and-forget a concurrent parse task (guarded by semaphore)."""
    def _task_done(t: asyncio.Task):
        _background_tasks.discard(t) if hasattr(_background_tasks, 'discard') else None
        try:
            _background_tasks.remove(t)
        except ValueError:
            pass
        exc = t.exception() if not t.cancelled() else None
        if exc:
            logger.error("Parse task for candidate %s crashed: %s", candidate_id, exc, exc_info=exc)

    try:
        loop = asyncio.get_running_loop()
        logger.info("Launching parse task for candidate %s on loop %s", candidate_id, id(loop))
    except RuntimeError:
        logger.error("No running event loop for parse task (candidate %s)", candidate_id)
        return

    task = loop.create_task(_parse_one(candidate_id, file_path, job_id))
    _background_tasks.append(task)  # prevent garbage collection
    task.add_done_callback(_task_done)
    logger.info("Parse task created for candidate %s: %s", candidate_id, task)


# ── Candidate Quota Pre-check ──────────────────────────────────────────────────

@router.get("/can-add")
def can_add_candidate(
    job_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Pre-check whether another candidate can be added for this job."""
    from app.models.job import Job as JobModel
    job = db.query(JobModel).filter(JobModel.id == job_id).first()
    if not job or not job.organization_id:
        return {"allowed": True}
    org = db.query(Organization).filter(Organization.id == job.organization_id).first()
    if not org or (org.max_candidates_per_job or 0) == 0:
        return {"allowed": True}
    if (org.total_candidates_used or 0) >= org.max_candidates_per_job:
        return {
            "allowed": False,
            "message": "You have exceeded the candidate limit. Please upgrade your plan.",
            "used": org.total_candidates_used or 0,
            "limit": org.max_candidates_per_job,
        }
    return {"allowed": True}


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
    user: User = Depends(get_current_user),
):
    return candidate_service.get_candidates(db, status=status, job_id=job_id, stage=stage, org_id=get_org_id(user))


@router.post(
    "/",
    response_model=CandidateRead,
    status_code=status.HTTP_201_CREATED,
    summary="Create candidate manually",
)
def create_candidate(payload: CandidateCreate, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    # Enforce org candidate quota using lifetime counter
    if payload.job_id:
        _check_and_increment_candidate_quota(db, payload.job_id)
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
def get_candidate(candidate_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    candidate = _get_candidate_with_org_check(db, candidate_id, user)
    return candidate


@router.put("/{candidate_id}", response_model=CandidateRead, summary="Update candidate")
def update_candidate(
    candidate_id: int, payload: CandidateUpdate, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    _get_candidate_with_org_check(db, candidate_id, user)
    candidate = candidate_service.update_candidate(db, candidate_id, payload)
    if not candidate:
        raise HTTPException(status_code=404, detail="Candidate not found")
    return candidate


@router.delete("/{candidate_id}", status_code=status.HTTP_204_NO_CONTENT, summary="Delete candidate")
def delete_candidate(candidate_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    _get_candidate_with_org_check(db, candidate_id, user)
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
    user: User = Depends(get_current_user),
):
    from app.models.job import Job

    candidate = _get_candidate_with_org_check(db, candidate_id, user)

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
        _check_and_increment_candidate_quota(db, job_id)
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
            interview_status="New",
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
def get_job_associations(candidate_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    from app.models.job import Job

    candidate = _get_candidate_with_org_check(db, candidate_id, user)

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
    user: User = Depends(get_current_user),
):
    org_id = get_org_id(user)
    query = db.query(ResumeMetadata).filter(ResumeMetadata.candidate_email == email)
    if org_id is not None:
        query = query.filter(ResumeMetadata.organization_id == org_id)
    meta = query.first()

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
    summary="Poll resume parsing status",
    description=(
        "Returns the current parsing status, name, email, and fit score for a candidate.\n\n"
        "**Statuses:** `pending` → `processing` → `completed` | `failed`\n\n"
        "Poll this endpoint every 2-3 seconds after uploading resumes to track progress. "
        "Once `parsing_status` is `completed`, the `resume_fit_score` field will contain the fit percentage."
    ),
)
def get_parsing_status(candidate_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    candidate = _get_candidate_with_org_check(db, candidate_id, user)
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
    summary="Upload resumes (batch, parallel)",
    description=(
        "Upload one or more PDF/DOC/DOCX resume files.\n\n"
        "Files are saved immediately and candidate records created with "
        "`parsing_status='pending'`. Parsing runs **concurrently** in the "
        "background (up to 3 at a time) using Claude Haiku for fast extraction (~1-2s per resume).\n\n"
        "**Fit Score** is computed automatically using the smart scoring engine "
        "(same 12-dimension weighted model used by database matching). "
        "If smart scoring fails, falls back to AI semantic scoring.\n\n"
        "Poll `GET /candidates/{id}/parsing-status` to track progress per candidate.\n\n"
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
    user: User = Depends(get_current_user),
):
    """
    Upload one or more resumes. Files are saved to disk, candidate records
    created with parsing_status='pending', and parsing launched concurrently
    in background (up to 3 at a time via semaphore).
    """
    # Verify user owns the job
    if job_id:
        from app.models.job import Job as JobModel
        org_id = get_org_id(user)
        job = db.query(JobModel).filter(JobModel.id == job_id).first()
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")
        if org_id is not None and job.organization_id != org_id:
            raise HTTPException(status_code=404, detail="Job not found")

    created_candidates = []
    parse_tasks = []

    # Pre-check quota availability (don't increment yet — only on successful parse)
    if job_id:
        from app.models.job import Job as _JobModel
        _job = db.query(_JobModel).filter(_JobModel.id == job_id).first()
        if _job and _job.organization_id:
            _org = db.query(Organization).filter(Organization.id == _job.organization_id).first()
            if _org and (_org.max_candidates_per_job or 0) > 0:
                remaining = _org.max_candidates_per_job - (_org.total_candidates_used or 0)
                if remaining < len(files):
                    raise HTTPException(
                        status_code=403,
                        detail=f"Candidate quota exceeded. You can add {max(0, remaining)} more candidate(s).",
                    )

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

        # Queue for background parse
        parse_tasks.append((candidate.id, file_path, job_id))

    # Parse all resumes concurrently (semaphore limits to 3 at a time)
    # Awaiting here ensures tasks complete reliably (no fire-and-forget issues)
    if parse_tasks:
        await asyncio.gather(
            *[_parse_one(cid, fp, jid) for cid, fp, jid in parse_tasks],
            return_exceptions=True,
        )

    # Re-fetch candidates to return final parsed state
    # Some placeholders may have been deleted during dedup (merged into existing),
    # so also look up by the resume file path to find the surviving candidate.
    db.expire_all()  # clear stale cache from the outer session
    result = []
    seen_ids = set()
    for c in created_candidates:
        refreshed = db.query(Candidate).filter(Candidate.id == c.id).first()
        if refreshed and refreshed.id not in seen_ids:
            result.append(refreshed)
            seen_ids.add(refreshed.id)
        elif c.resume_path:
            # Placeholder was deleted (dedup) — find the surviving candidate by resume path
            surviving = db.query(Candidate).filter(
                Candidate.resume_path == c.resume_path,
                Candidate.job_id == job_id,
            ).first()
            if surviving and surviving.id not in seen_ids:
                result.append(surviving)
                seen_ids.add(surviving.id)

    return result


# ── Fit Score Recompute ───────────────────────────────────────────────────────

@router.post(
    "/{candidate_id}/compute-fit-score",
    response_model=CandidateRead,
    summary="Recompute resume fit score using AI (deep analysis)",
    description=(
        "Re-runs the **full AI LLM fit-score** with 6-dimension semantic scoring "
        "for an existing candidate against their linked job.\n\n"
        "This uses Claude Sonnet for deep analysis (~15-20s) and is more accurate "
        "than the fast smart scoring used during upload. Use this for individual "
        "candidates when higher accuracy is needed."
    ),
)
async def compute_fit_score(candidate_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Re-compute fit score using AI semantic analysis (6-dimension model)."""
    candidate = _get_candidate_with_org_check(db, candidate_id, user)
    if not candidate.job_id:
        raise HTTPException(status_code=400, detail="Candidate is not linked to a job")

    result = await _ai_fit_score_for_candidate(candidate, db)
    if not result:
        raise HTTPException(status_code=400, detail="Could not compute score — missing resume text or job data")

    return candidate


@router.post(
    "/batch-recompute-scores",
    summary="Batch recompute fit scores for all candidates in a job",
    description=(
        "Re-runs the full AI semantic fit-score for **all** candidates linked to the given `job_id`.\n\n"
        "Returns a progress summary with per-candidate scores. "
        "This is a slow operation (~15-20s per candidate) and should be used for "
        "re-scoring after job requirements change."
    ),
)
async def batch_recompute_scores(
    job_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Batch recompute AI fit scores for all candidates in a job opening."""
    from app.models.job import Job
    org_id = get_org_id(user)
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if org_id is not None and job.organization_id != org_id:
        raise HTTPException(status_code=404, detail="Job not found")

    candidates = db.query(Candidate).filter(Candidate.job_id == job_id).all()
    if not candidates:
        return {"total": 0, "updated": 0, "failed": 0, "message": "No candidates found"}

    updated = 0
    failed = 0
    results = []

    for candidate in candidates:
        try:
            result = await _ai_fit_score_for_candidate(candidate, db)
            if result:
                updated += 1
                results.append({
                    "id": candidate.id,
                    "name": candidate.name,
                    "fit_score": result.get("fit_percentage"),
                    "fit_level": result.get("fit_level"),
                })
            else:
                failed += 1
        except Exception as e:
            logger.warning("Batch recompute failed for candidate %s: %s", candidate.id, e)
            failed += 1

    return {
        "total": len(candidates),
        "updated": updated,
        "failed": failed,
        "results": results,
    }


async def _ai_fit_score_for_candidate(candidate: Candidate, db: Session, resume_text: str | None = None) -> dict | None:
    """
    Run AI semantic fit-score for a single candidate.
    Returns the score dict or None if missing data.
    Accepts optional resume_text to skip re-extraction.
    """
    from app.models.job import Job
    from app.services.resume_service import calculate_fit_score, extract_text
    from app.schemas.ai import FitScoreRequest

    # Get resume text if not provided
    if not resume_text:
        meta = None
        if candidate.email:
            meta = db.query(ResumeMetadata).filter(
                ResumeMetadata.candidate_email == candidate.email
            ).first()
        if meta and meta.raw_text:
            resume_text = meta.raw_text
        if not resume_text and candidate.resume_path and os.path.exists(candidate.resume_path):
            try:
                resume_text = extract_text(candidate.resume_path)
            except Exception as e:
                logger.warning("Failed to extract text from %s: %s", candidate.resume_path, e)

    if not resume_text:
        logger.warning("No resume text found for candidate %s (email=%s, resume_path=%s)", candidate.id, candidate.email, candidate.resume_path)
        return None

    # Get job data
    job = db.query(Job).filter(Job.id == candidate.job_id).first()
    if not job:
        return None

    # Build JD text from responsibilities
    responsibilities = [r.description for r in job.responsibilities] if job.responsibilities else []
    required_skills = []
    if job.skills:
        for s in job.skills:
            if s.skill:
                required_skills.append(s.skill)
            elif s.name:
                required_skills.append(s.name)

    # Build full JD text from uploaded JD file or structured data
    jd_text = None
    if job.upload_jd and os.path.exists(job.upload_jd):
        try:
            jd_text = extract_text(job.upload_jd)
        except Exception:
            pass

    # If no uploaded JD, synthesize from structured data so AI has full context
    if not jd_text and (required_skills or responsibilities):
        parts = [f"Job Title: {job.title}"]
        if job.department:
            parts.append(f"Department: {job.department}")
        if job.experience_min is not None:
            parts.append(f"Experience Required: {job.experience_min}-{job.experience_max} years")
        if required_skills:
            parts.append(f"Required Skills: {', '.join(required_skills)}")
        if responsibilities:
            parts.append("Key Responsibilities:")
            parts.extend(f"- {r}" for r in responsibilities)
        jd_text = "\n".join(parts)

    req = FitScoreRequest(
        resume_text=resume_text,
        job_title=job.title,
        job_description=jd_text,
        required_experience_min=job.experience_min,
        required_experience_max=job.experience_max,
        required_skills=required_skills,
        responsibilities=responsibilities,
    )

    result = await calculate_fit_score(req)

    # Save to candidate
    score_data = {
        "fit_level": result.fit_level,
        "fit_percentage": result.fit_percentage,
        "summary": result.summary,
        "key_matching_skills": result.key_matching_skills,
        "relevant_experience_summary": result.relevant_experience_summary,
        "missing_weak_areas": result.missing_weak_areas,
        "reasoning_points": result.reasoning_points,
        "verdict": result.verdict,
    }
    details_data = {
        "score_breakdown": result.score_breakdown,
        "strength_mapping": result.strength_mapping,
        "breakdown": {
            "experience_match": result.breakdown.experience_match,
            "skills_match": result.breakdown.skills_match,
            "tech_stack_alignment": result.breakdown.tech_stack_alignment,
            "role_alignment": result.breakdown.role_alignment,
        },
    }

    candidate.resume_fit_score = f"{result.fit_percentage}%"
    candidate.fit_score_reasoning = json.dumps(score_data)
    candidate.fit_score_details = json.dumps(details_data)
    db.commit()
    db.refresh(candidate)

    return score_data


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
    user: User = Depends(get_current_user),
):
    """Schedule (or re-schedule) an interview for a candidate and send notifications."""
    from app.services.interview_service import send_schedule_email, send_whatsapp_message
    from app.models.interview import InterviewStatusLog

    candidate = _get_candidate_with_org_check(db, candidate_id, user)

    # Validate WhatsApp permission
    if payload.send_whatsapp:
        org_id = user.organization_id
        if org_id is not None:
            from app.models.organization import Organization as OrgModel
            org = db.query(OrgModel).filter(OrgModel.id == org_id).first()
            if org and not getattr(org, "allow_whatsapp", False):
                raise HTTPException(
                    status_code=403,
                    detail="WhatsApp notifications are not enabled for your organization. Contact your super admin.",
                )

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

    # ── Google Meet creation (if requested) ─────────────────────────────────
    google_meet_link = None
    if payload.create_google_meet:
        if not getattr(user, "google_refresh_token", None):
            raise HTTPException(
                status_code=400,
                detail="Google account not connected. Please connect your Google account in Profile settings first.",
            )
        try:
            from app.services.google_calendar_service import create_meet_event
            from app.models.job import Job as JobModel
            job = db.query(JobModel).filter(JobModel.id == candidate.job_id).first()
            job_title = job.title if job else "Interview"

            meet_result = create_meet_event(
                access_token=user.google_access_token,
                refresh_token=user.google_refresh_token,
                summary=f"Interview - {candidate.name} for {job_title}",
                description=f"Interview with {candidate.name} for the role of {job_title}",
                start_dt=payload.interview_date,
                attendee_email=candidate.email,
                timezone=payload.interview_timezone.replace("Calcutta", "Kolkata"),
            )
            google_meet_link = meet_result.get("meet_link")

            # Update token if it was refreshed
            if meet_result.get("new_access_token"):
                user.google_access_token = meet_result["new_access_token"]
                db.commit()

            logger.info("Google Meet created for candidate %s: %s", candidate.id, google_meet_link)
        except Exception:
            logger.warning("Failed to create Google Meet for candidate %s", candidate.id, exc_info=True)

    email_sent = False
    whatsapp_sent = False

    try:
        email_sent = await send_schedule_email(candidate, db, is_reschedule=is_reschedule, google_meet_link=google_meet_link)
        if not email_sent:
            logger.warning("Email send returned False for candidate %s", candidate.id)
    except Exception:
        logger.warning("Failed to send schedule email for candidate %s", candidate.id, exc_info=True)

    if payload.send_whatsapp and candidate.phone:
        try:
            from app.models.job import Job
            from app.models.organization import Organization
            job = db.query(Job).filter(Job.id == candidate.job_id).first()
            job_title = job.title if job else "the role"
            org_name = "Our Company"
            if job and job.organization_id:
                org = db.query(Organization).filter(Organization.id == job.organization_id).first()
                if org:
                    org_name = org.name
            msg = _build_whatsapp_message(candidate, job_title, org_name=org_name, is_reschedule=is_reschedule)
            await send_whatsapp_message(candidate.phone, msg)
            whatsapp_sent = True
        except Exception:
            logger.warning("Failed to send WhatsApp to candidate %s", candidate.id, exc_info=True)

    # Return candidate data + notification status
    result = CandidateRead.model_validate(candidate)
    response_dict = result.model_dump()
    response_dict["email_sent"] = email_sent
    response_dict["whatsapp_sent"] = whatsapp_sent
    if google_meet_link:
        response_dict["google_meet_link"] = google_meet_link
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
    user: User = Depends(get_current_user),
):
    """Cancel a scheduled interview and notify the candidate."""
    from app.services.interview_service import send_cancel_email, send_whatsapp_message
    from app.models.interview import InterviewStatusLog

    candidate = _get_candidate_with_org_check(db, candidate_id, user)

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
            from app.models.organization import Organization
            job = db.query(Job).filter(Job.id == candidate.job_id).first()
            job_title = job.title if job else "the role"
            org_name = "Our Company"
            if job and job.organization_id:
                org = db.query(Organization).filter(Organization.id == job.organization_id).first()
                if org:
                    org_name = org.name
            msg = (
                f"Hi {candidate.name},\n"
                f"Your interview for the role of {job_title} at {org_name} "
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
    user: User = Depends(get_current_user),
):
    """Move candidate to a new hiring stage."""
    valid_stages = {"AI Round", "Panel Round", "Offer-Made", "Rejected", "Candidates"}
    if payload.stage not in valid_stages:
        raise HTTPException(status_code=400, detail=f"Invalid stage. Must be one of: {valid_stages}")

    candidate = _get_candidate_with_org_check(db, candidate_id, user)

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
    user: User = Depends(get_current_user),
):
    """Send a custom WhatsApp message to a candidate."""
    from app.services.interview_service import send_whatsapp_message

    candidate = _get_candidate_with_org_check(db, candidate_id, user)

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
def get_evaluation(candidate_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Return full evaluation data: resume analysis + scores + interview Q&A transcript."""
    from app.models.interview import InterviewSession, InterviewMessage, SessionStatus, Evaluation, Violation
    from app.models.job import Job

    candidate = _get_candidate_with_org_check(db, candidate_id, user)

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

    # ── Job details (experience range) ────────────────────────────────────────
    job_info = None
    if candidate.job_id:
        job = db.query(Job).filter(Job.id == candidate.job_id).first()
        if job:
            job_info = {
                "title": job.title,
                "experience_min": job.experience_min,
                "experience_max": job.experience_max,
            }

    # ── Q&A transcript from latest completed session ──────────────────────────
    transcript = []
    full_transcript = []  # All messages in order for chat-style view
    evaluation_detail = None
    violations_list = []
    session_info = None

    latest_session = (
        db.query(InterviewSession)
        .filter(
            InterviewSession.candidate_id == candidate_id,
            InterviewSession.status.in_([SessionStatus.completed, SessionStatus.terminated]),
        )
        .order_by(InterviewSession.completed_at.desc())
        .first()
    )

    screenshots_list = []

    if latest_session:
        session_info = {
            "id": latest_session.id,
            "started_at": (latest_session.started_at.isoformat() + "Z") if latest_session.started_at else None,
            "completed_at": (latest_session.completed_at.isoformat() + "Z") if latest_session.completed_at else None,
            "violation_count": latest_session.violation_count or 0,
            "status": latest_session.status.value,
        }

        messages = (
            db.query(InterviewMessage)
            .filter(InterviewMessage.session_id == latest_session.id)
            .order_by(InterviewMessage.created_at)
            .all()
        )

        # Full transcript for chat-style view
        for msg in messages:
            full_transcript.append({
                "role": msg.role,
                "content": msg.content,
                "question_index": msg.question_index,
                "timestamp": (msg.created_at.isoformat() + "Z") if msg.created_at else None,
            })

        # Group by question_index into Q&A pairs
        qa_map: dict = {}

        def _extract_question(text: str) -> str:
            """Strip conversational preamble to isolate the actual question."""
            import re as _re
            # Common AI transition patterns before the question
            # e.g., "Great, thanks! So let's get started. Can you explain..."
            # Split on patterns like "Let's get started.", "Moving on,", "Next question —"
            separators = [
                r"(?:So |Now )?[Ll]et(?:'s| us) (?:get started|begin|move on|dive in)[.!]?\s*",
                r"(?:Great|Good|Thanks|Thank you|Interesting|That's (?:great|good|fair|interesting))[^.?!]*[.!]\s*",
                r"Moving on[,.]?\s*",
                r"Next question[.:\s—–-]+",
                r"Let me ask you[.:\s—–-]+",
                r"Here's (?:the next|another|my next) question[.:\s—–-]+",
            ]
            # Try each separator — take the part AFTER the last preamble match
            best = text
            for sep in separators:
                parts = _re.split(sep, text, maxsplit=1)
                if len(parts) > 1 and len(parts[-1].strip()) > 20:
                    best = parts[-1].strip()
                    text = best  # chain — strip multiple preambles
            return best

        for msg in messages:
            qi = msg.question_index or 0
            if qi == 0:
                continue  # skip opening/closing system turns without index
            if qi not in qa_map:
                qa_map[qi] = {"question": None, "answer": None, "index": qi}
            if msg.role == "ai" and qa_map[qi]["question"] is None:
                qa_map[qi]["question"] = _extract_question(msg.content)
            elif msg.role == "candidate" and qa_map[qi]["answer"] is None:
                qa_map[qi]["answer"] = msg.content
        transcript = [
            {**v, "answer": v["answer"] or "[No answer provided — skipped]"}
            for _, v in sorted(qa_map.items())
            if v["question"]
        ]

        # ── Violations for this session ───────────────────────────────────────
        violations = (
            db.query(Violation)
            .filter(Violation.session_id == latest_session.id)
            .order_by(Violation.occurred_at)
            .all()
        )
        for v in violations:
            violations_list.append({
                "type": v.violation_type.value,
                "details": v.details,
                "occurred_at": (v.occurred_at.isoformat() + "Z") if v.occurred_at else None,
            })

        # ── Screenshots (face detection frames) for this session ──────────────
        try:
            from app.models.interview import InterviewScreenshot
            screenshots = (
                db.query(InterviewScreenshot)
                .filter(InterviewScreenshot.session_id == latest_session.id)
                .order_by(InterviewScreenshot.captured_at)
                .all()
            )
            for ss in screenshots:
                fp = ss.file_path.replace(os.sep, "/") if ss.file_path else ""
                if fp and not fp.startswith("/"):
                    fp = f"/{fp}"
                screenshots_list.append({
                    "file_path": fp,
                    "captured_at": (ss.captured_at.isoformat() + "Z") if ss.captured_at else None,
                    "face_count": getattr(ss, "face_count", None),
                })
        except Exception as _ss_err:
            import logging as _log
            _log.warning(f"Could not load screenshots for session {latest_session.id}: {_ss_err}")

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
            try:
                per_question = json.loads(ev.per_question_evaluations) if ev.per_question_evaluations else []
            except Exception:
                per_question = []
            try:
                skills_ratings = json.loads(ev.skills_ratings) if ev.skills_ratings else []
            except Exception:
                skills_ratings = []
            evaluation_detail = {
                "recommendation": ev.recommendation.value if ev.recommendation else None,
                "summary": ev.summary,
                "strengths": strengths,
                "weaknesses": weaknesses,
                "detailed_feedback": ev.detailed_feedback,
                "meets_threshold": ev.meets_threshold or False,
                "technical_score": ev.technical_score,
                "communication_score": ev.communication_score,
                "problem_solving_score": ev.problem_solving_score,
                "confidence_score": ev.confidence_score,
                "overall_score": ev.overall_score,
                "per_question_evaluations": per_question,
                "skills_ratings": skills_ratings,
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
            "interview_date": candidate.interview_date.isoformat() + "Z" if candidate.interview_date else None,
            "interview_timezone": candidate.interview_timezone,
            "image": candidate.image,
        },
        "job": job_info,
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
        "full_transcript": full_transcript,
        "evaluation": evaluation_detail,
        "violations": violations_list,
        "screenshots": screenshots_list,
        "session": session_info,
    }


# ── Regenerate Evaluation ─────────────────────────────────────────────────────


@router.post(
    "/{candidate_id}/regenerate-evaluation",
    summary="Force-regenerate AI evaluation for a completed session",
)
def regenerate_evaluation(
    candidate_id: int,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """
    Find the latest completed/terminated session for this candidate and
    re-trigger the AI evaluation background task. Safe to call even if an
    evaluation already exists — it will re-run and overwrite.
    """
    from app.models.interview import InterviewSession, Evaluation, SessionStatus
    from app.routers.interview import _run_evaluation_background

    _get_candidate_with_org_check(db, candidate_id, user)

    session = (
        db.query(InterviewSession)
        .filter(
            InterviewSession.candidate_id == candidate_id,
            InterviewSession.status.in_([SessionStatus.completed, SessionStatus.terminated]),
        )
        .order_by(InterviewSession.completed_at.desc())
        .first()
    )
    if not session:
        raise HTTPException(status_code=404, detail="No completed interview session found for this candidate")

    # Delete existing evaluation so background task will regenerate it
    existing = db.query(Evaluation).filter(Evaluation.session_id == session.id).first()
    if existing:
        db.delete(existing)
        db.commit()

    background_tasks.add_task(_run_evaluation_background, session.id)
    return {"status": "processing", "session_id": session.id, "message": "Evaluation is being regenerated in the background"}


# ── Download Endpoints ────────────────────────────────────────────────────────


@router.get(
    "/{candidate_id}/evaluation/download-pdf",
    summary="Download evaluation report as PDF",
)
def download_evaluation_pdf(candidate_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Generate and return a PDF evaluation report for the candidate."""
    from io import BytesIO
    from fastapi.responses import StreamingResponse
    from app.models.interview import InterviewSession, InterviewMessage, SessionStatus, Evaluation

    candidate = _get_candidate_with_org_check(db, candidate_id, user)

    try:
        from fpdf import FPDF
    except ImportError:
        raise HTTPException(status_code=500, detail="fpdf2 package not installed")

    latest_session = (
        db.query(InterviewSession)
        .filter(
            InterviewSession.candidate_id == candidate_id,
            InterviewSession.status.in_([SessionStatus.completed, SessionStatus.terminated]),
        )
        .order_by(InterviewSession.completed_at.desc())
        .first()
    )

    ev = None
    if latest_session:
        ev = db.query(Evaluation).filter(Evaluation.session_id == latest_session.id).first()

    pdf = FPDF()
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_page()

    # Title
    pdf.set_font("Helvetica", "B", 18)
    pdf.cell(0, 12, "Interview Evaluation Report", new_x="LMARGIN", new_y="NEXT", align="C")
    pdf.ln(5)

    # Candidate Info
    pdf.set_font("Helvetica", "B", 14)
    pdf.cell(0, 8, candidate.name or "Unknown", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 10)
    pdf.cell(0, 6, f"Role: {candidate.role or 'N/A'}  |  Email: {candidate.email}  |  Phone: {candidate.phone or 'N/A'}", new_x="LMARGIN", new_y="NEXT")
    if candidate.interview_date:
        pdf.cell(0, 6, f"Interview Date: {candidate.interview_date.strftime('%B %d, %Y, %I:%M %p')}", new_x="LMARGIN", new_y="NEXT")
    pdf.cell(0, 6, f"Status: {candidate.interview_status or 'N/A'}  |  Stage: {candidate.candidate_stage or 'N/A'}", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(5)

    # Scores
    pdf.set_font("Helvetica", "B", 13)
    pdf.cell(0, 8, "Interview Scores", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 10)
    scores = [
        ("Overall Score", candidate.score),
        ("Confidence", candidate.confidence_score),
        ("Clarity", candidate.clarity_score),
        ("Grammar", candidate.grammar_score),
        ("Enthusiasm", candidate.enthusiasm_score),
        ("Pronunciation", candidate.pronunciation_score),
        ("Conciseness", candidate.conciseness_score),
        ("Trust Score", candidate.trust_score),
        ("AI Cheating Score", candidate.ai_cheating_score),
    ]
    for label, val in scores:
        pdf.cell(60, 6, label, new_x="RIGHT")
        pdf.cell(0, 6, f"{val:.1f}" if val is not None else "N/A", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(5)

    # Evaluation
    if ev:
        pdf.set_font("Helvetica", "B", 13)
        pdf.cell(0, 8, "AI Evaluation", new_x="LMARGIN", new_y="NEXT")

        if ev.recommendation:
            pdf.set_font("Helvetica", "B", 11)
            pdf.cell(0, 7, f"Recommendation: {ev.recommendation.value}", new_x="LMARGIN", new_y="NEXT")

        if ev.summary:
            pdf.set_font("Helvetica", "", 10)
            pdf.multi_cell(0, 5, ev.summary)
            pdf.ln(3)

        try:
            strengths = json.loads(ev.strengths) if ev.strengths else []
        except Exception:
            strengths = []
        if strengths:
            pdf.set_font("Helvetica", "B", 11)
            pdf.cell(0, 7, "Strengths:", new_x="LMARGIN", new_y="NEXT")
            pdf.set_font("Helvetica", "", 10)
            for s in strengths:
                pdf.multi_cell(0, 5, f"  - {s}")

        try:
            weaknesses = json.loads(ev.weaknesses) if ev.weaknesses else []
        except Exception:
            weaknesses = []
        if weaknesses:
            pdf.set_font("Helvetica", "B", 11)
            pdf.cell(0, 7, "Areas to Improve:", new_x="LMARGIN", new_y="NEXT")
            pdf.set_font("Helvetica", "", 10)
            for w in weaknesses:
                pdf.multi_cell(0, 5, f"  - {w}")

        if ev.detailed_feedback:
            pdf.ln(3)
            pdf.set_font("Helvetica", "B", 11)
            pdf.cell(0, 7, "Detailed Feedback:", new_x="LMARGIN", new_y="NEXT")
            pdf.set_font("Helvetica", "", 10)
            pdf.multi_cell(0, 5, ev.detailed_feedback)

    # Transcript
    if latest_session:
        messages = (
            db.query(InterviewMessage)
            .filter(InterviewMessage.session_id == latest_session.id)
            .order_by(InterviewMessage.created_at)
            .all()
        )
        qa_map: dict = {}
        for msg in messages:
            qi = msg.question_index or 0
            if qi == 0:
                continue
            if qi not in qa_map:
                qa_map[qi] = {"question": None, "answer": None, "index": qi}
            if msg.role == "ai" and qa_map[qi]["question"] is None:
                qa_map[qi]["question"] = msg.content
            elif msg.role == "candidate" and qa_map[qi]["answer"] is None:
                qa_map[qi]["answer"] = msg.content
        transcript_pairs = [v for _, v in sorted(qa_map.items()) if v["question"] and v["answer"]]

        if transcript_pairs:
            pdf.add_page()
            pdf.set_font("Helvetica", "B", 13)
            pdf.cell(0, 8, "Interview Transcript", new_x="LMARGIN", new_y="NEXT")
            pdf.ln(3)
            for pair in transcript_pairs:
                pdf.set_font("Helvetica", "B", 10)
                pdf.multi_cell(0, 5, f"Q{pair['index']}: {pair['question']}")
                pdf.set_font("Helvetica", "", 10)
                pdf.multi_cell(0, 5, f"A: {pair['answer']}")
                pdf.ln(3)

    buf = BytesIO()
    pdf.output(buf)
    buf.seek(0)
    safe_name = (candidate.name or "candidate").replace(" ", "_")
    return StreamingResponse(
        buf,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{safe_name}_Evaluation.pdf"'},
    )


@router.get(
    "/{candidate_id}/evaluation/download-docx",
    summary="Download evaluation report as DOCX",
)
def download_evaluation_docx(candidate_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Generate and return a DOCX evaluation report for the candidate."""
    from io import BytesIO
    from fastapi.responses import StreamingResponse
    from docx import Document
    from docx.shared import Pt, RGBColor
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from app.models.interview import InterviewSession, InterviewMessage, SessionStatus, Evaluation

    candidate = _get_candidate_with_org_check(db, candidate_id, user)

    latest_session = (
        db.query(InterviewSession)
        .filter(
            InterviewSession.candidate_id == candidate_id,
            InterviewSession.status.in_([SessionStatus.completed, SessionStatus.terminated]),
        )
        .order_by(InterviewSession.completed_at.desc())
        .first()
    )

    ev = None
    if latest_session:
        ev = db.query(Evaluation).filter(Evaluation.session_id == latest_session.id).first()

    doc = Document()

    # Title
    title = doc.add_heading("Interview Evaluation Report", level=0)
    title.alignment = WD_ALIGN_PARAGRAPH.CENTER

    # Candidate Info
    doc.add_heading(candidate.name or "Unknown", level=1)
    info_lines = [
        f"Role: {candidate.role or 'N/A'}",
        f"Email: {candidate.email}",
        f"Phone: {candidate.phone or 'N/A'}",
        f"Status: {candidate.interview_status or 'N/A'}  |  Stage: {candidate.candidate_stage or 'N/A'}",
    ]
    if candidate.interview_date:
        info_lines.insert(3, f"Interview Date: {candidate.interview_date.strftime('%B %d, %Y, %I:%M %p')}")
    for line in info_lines:
        p = doc.add_paragraph(line)
        p.style.font.size = Pt(10)

    # Scores Table
    doc.add_heading("Interview Scores", level=2)
    score_items = [
        ("Overall Score", candidate.score),
        ("Confidence", candidate.confidence_score),
        ("Clarity", candidate.clarity_score),
        ("Grammar", candidate.grammar_score),
        ("Enthusiasm", candidate.enthusiasm_score),
        ("Pronunciation", candidate.pronunciation_score),
        ("Conciseness", candidate.conciseness_score),
        ("Trust Score", candidate.trust_score),
        ("AI Cheating Score", candidate.ai_cheating_score),
    ]
    table = doc.add_table(rows=1, cols=2)
    table.style = "Light Grid Accent 1"
    hdr_cells = table.rows[0].cells
    hdr_cells[0].text = "Metric"
    hdr_cells[1].text = "Score"
    for label, val in score_items:
        row = table.add_row().cells
        row[0].text = label
        row[1].text = f"{val:.1f}" if val is not None else "N/A"

    # Evaluation
    if ev:
        doc.add_heading("AI Evaluation", level=2)
        if ev.recommendation:
            p = doc.add_paragraph()
            run = p.add_run(f"Recommendation: {ev.recommendation.value}")
            run.bold = True
            run.font.size = Pt(12)
        if ev.summary:
            doc.add_paragraph(ev.summary)

        try:
            strengths = json.loads(ev.strengths) if ev.strengths else []
        except Exception:
            strengths = []
        if strengths:
            doc.add_heading("Strengths", level=3)
            for s in strengths:
                doc.add_paragraph(s, style="List Bullet")

        try:
            weaknesses = json.loads(ev.weaknesses) if ev.weaknesses else []
        except Exception:
            weaknesses = []
        if weaknesses:
            doc.add_heading("Areas to Improve", level=3)
            for w in weaknesses:
                doc.add_paragraph(w, style="List Bullet")

        if ev.detailed_feedback:
            doc.add_heading("Detailed Feedback", level=3)
            doc.add_paragraph(ev.detailed_feedback)

    # Transcript
    if latest_session:
        messages = (
            db.query(InterviewMessage)
            .filter(InterviewMessage.session_id == latest_session.id)
            .order_by(InterviewMessage.created_at)
            .all()
        )
        qa_map: dict = {}
        for msg in messages:
            qi = msg.question_index or 0
            if qi == 0:
                continue
            if qi not in qa_map:
                qa_map[qi] = {"question": None, "answer": None, "index": qi}
            if msg.role == "ai" and qa_map[qi]["question"] is None:
                qa_map[qi]["question"] = msg.content
            elif msg.role == "candidate" and qa_map[qi]["answer"] is None:
                qa_map[qi]["answer"] = msg.content
        transcript_pairs = [v for _, v in sorted(qa_map.items()) if v["question"] and v["answer"]]

        if transcript_pairs:
            doc.add_page_break()
            doc.add_heading("Interview Transcript", level=2)
            for pair in transcript_pairs:
                p = doc.add_paragraph()
                run = p.add_run(f"Q{pair['index']}: {pair['question']}")
                run.bold = True
                doc.add_paragraph(f"A: {pair['answer']}")
                doc.add_paragraph("")  # spacer

    buf = BytesIO()
    doc.save(buf)
    buf.seek(0)
    safe_name = (candidate.name or "candidate").replace(" ", "_")
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={"Content-Disposition": f'attachment; filename="{safe_name}_Evaluation.docx"'},
    )


@router.get(
    "/{candidate_id}/evaluation/download-transcript",
    summary="Download interview transcript as text file",
)
def download_transcript(candidate_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Generate and return a plain text interview transcript."""
    from io import BytesIO
    from fastapi.responses import StreamingResponse
    from app.models.interview import InterviewSession, InterviewMessage, SessionStatus

    candidate = _get_candidate_with_org_check(db, candidate_id, user)

    latest_session = (
        db.query(InterviewSession)
        .filter(
            InterviewSession.candidate_id == candidate_id,
            InterviewSession.status.in_([SessionStatus.completed, SessionStatus.terminated]),
        )
        .order_by(InterviewSession.completed_at.desc())
        .first()
    )

    if not latest_session:
        raise HTTPException(status_code=404, detail="No completed interview found")

    messages = (
        db.query(InterviewMessage)
        .filter(InterviewMessage.session_id == latest_session.id)
        .order_by(InterviewMessage.created_at)
        .all()
    )

    lines = [
        f"Interview Transcript - {candidate.name}",
        f"Role: {candidate.role or 'N/A'}",
        f"Date: {candidate.interview_date.strftime('%B %d, %Y, %I:%M %p') if candidate.interview_date else 'N/A'}",
        "=" * 60,
        "",
    ]

    for msg in messages:
        role_label = "AI Interviewer" if msg.role == "ai" else "Candidate"
        timestamp = msg.created_at.strftime("%H:%M:%S") if msg.created_at else ""
        lines.append(f"[{timestamp}] {role_label}:")
        lines.append(msg.content)
        lines.append("")

    content = "\n".join(lines)
    buf = BytesIO(content.encode("utf-8"))
    safe_name = (candidate.name or "candidate").replace(" ", "_")
    return StreamingResponse(
        buf,
        media_type="text/plain; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{safe_name}_Transcript.txt"'},
    )


# ── Helpers ───────────────────────────────────────────────────────────────────

def _build_whatsapp_message(candidate: Candidate, job_title: str, org_name: str = "Our Company", is_reschedule: bool = False) -> str:
    action = "rescheduled" if is_reschedule else "scheduled"
    date_str = ""
    if candidate.interview_date:
        date_str = candidate.interview_date.strftime("%B %d, %Y, %I:%M %p")
        if candidate.interview_timezone:
            date_str += f" {candidate.interview_timezone}"
    link = candidate.interview_link or ""
    return (
        f"Hi {candidate.name},\n"
        f"Your Interview for the role of {job_title} at {org_name} "
        f"has been {action} on {date_str}. "
        f"Please ensure you're ready.\n\n"
        f"Interview Link: {link}"
    )
