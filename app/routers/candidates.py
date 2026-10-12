import asyncio
import json
import logging
import os
import shutil
from datetime import datetime, timezone as _tz
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, Request, UploadFile, status
from sqlalchemy.orm import Session

from slowapi import Limiter
from slowapi.util import get_remote_address

from app.auth import get_current_user, get_org_id
from app.authz import authorize_candidate, authorize_job, job_org_id
from app.database import get_db, SessionLocal
from app.invites import build_invite_url, ensure_invite_token, get_or_create_job_token, rotate_invite_token
from app.media import (
    UPLOAD_ROOT,
    resolve_upload_file,
    resume_file_in_use,
    safe_unlink_upload,
    safe_upload_name,
    sign_media_url,
    to_upload_relpath,
)
from app.models.candidate import Candidate
from app.models.organization import Organization
from app.models.resume_metadata import ResumeMetadata
from app.models.user import User
from app.ratelimit import per_user_limit, user_or_ip_key
from app.refs import CandidateId, JobId, encode_ref

# Keyed on the signed-in user, not the (spoofable) forwarded client IP.
limiter = Limiter(key_func=user_or_ip_key)


def _get_candidate_with_org_check(db: Session, candidate_id: int, user: User) -> Candidate:
    """Fetch a candidate the caller may access, or 404 (fails closed — see app.authz)."""
    return authorize_candidate(db, candidate_id, user)


def _require_whatsapp_allowed(db: Session, user: User) -> None:
    """403 unless WhatsApp notifications are enabled for the caller's organization.

    get_org_id() returns None only for a global super admin (who isn't gated);
    an admin/recruiter without an organization is refused by get_org_id itself
    instead of silently skipping the check."""
    org_id = get_org_id(user)
    if org_id is None:
        return
    org = db.query(Organization).filter(Organization.id == org_id).first()
    if not org or not getattr(org, "allow_whatsapp", False):
        raise HTTPException(
            status_code=403,
            detail="WhatsApp notifications are not enabled for your organization. Contact your super admin.",
        )


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
    HireCandidateRequest,
    PromoteStageRequest,
    ScheduleInterviewRequest,
    SendWhatsAppRequest,
)
from app.schemas.resume_metadata import DuplicateCheckResponse
from app.services import candidate_service
from app.services.file_compression import schedule_compression
from app.services.interview_text import feedback_and_question, question_text
from app.services.file_safety import UnsafeFile, store_resume_upload
from app.services.resume_links import find_profile

router = APIRouter(dependencies=[Depends(get_current_user)])

RESUME_UPLOAD_DIR = f"{UPLOAD_ROOT}/resumes"
os.makedirs(RESUME_UPLOAD_DIR, exist_ok=True)

ALLOWED_RESUME_TYPES = {
    "application/pdf",
    "application/msword",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}
_RESUME_EXT_FOR_TYPE = {
    "application/pdf": ".pdf",
    "application/msword": ".doc",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
}


def _download_stem(name: Optional[str]) -> str:
    """File-name stem for a Content-Disposition header. The raw candidate name
    (settable by anyone through the public /apply form) was pasted into the
    header: a quote let it inject its own filename*= (downloading the
    transcript as .html), and any non-Latin-1 name made the download 500."""
    import re as _re

    stem = _re.sub(r"[^A-Za-z0-9_-]+", "_", name or "").strip("_")[:80]
    return stem or "candidate"


MAX_RESUME_FILES_PER_UPLOAD = 200


def _resume_upload_problem(file: UploadFile) -> Optional[str]:
    from app.media import resume_upload_problem

    return resume_upload_problem(file)


def _stored_resume_name(file: UploadFile) -> str:
    """Server-chosen disk name for an uploaded resume: timestamp + sanitized stem +
    an allowed extension (the client file name used to be joined in verbatim)."""
    stem, ext = safe_upload_name(
        file.filename,
        _RESUME_EXT_FOR_TYPE.values(),
        _RESUME_EXT_FOR_TYPE.get(file.content_type, ".pdf"),
    )
    return f"{datetime.utcnow().strftime('%Y%m%d%H%M%S%f')}_{stem}{ext}"

# ── Concurrent background parse pool ─────────────────────────────────────────
_parse_semaphore: asyncio.Semaphore | None = None


def _get_parse_semaphore() -> asyncio.Semaphore:
    """Lazy-init semaphore inside the running event loop."""
    global _parse_semaphore
    if _parse_semaphore is None:
        _parse_semaphore = asyncio.Semaphore(3)
    return _parse_semaphore


def _sync_extract_text(file_path: str) -> str:
    """Extract text from resume file — runs in thread pool.

    Strips NUL bytes (0x00) that pdfplumber can emit for certain bullet
    glyphs (e.g. ➢) or malformed font encodings. PostgreSQL TEXT fields
    reject NUL bytes and raise DataError if they reach the DB.
    """
    from app.services.fast_resume_parser import extract_text_from_file

    text = extract_text_from_file(file_path)
    if not text:
        raise RuntimeError(f"Could not extract text from {file_path}")
    # Strip null bytes — PostgreSQL TEXT columns reject \x00 bytes.
    return text.replace("\x00", "")


async def _parse_one(candidate_id: int, file_path: str, job_id: int | None, org_id: int | None = None):
    """
    Parse a single resume concurrently (guarded by semaphore).
    Uses Haiku (~1-2s) for parsing + smart scoring (~50ms) for fit score.
    Total: ~3-5s per resume vs ~30-40s with old Sonnet approach.

    `org_id` is the uploader's organization (get_org_id), used for the resume
    metadata when no job is given; with a job, the job's organization is used.
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

            # Determine organization_id from the job (else the uploader's). Every
            # ResumeMetadata lookup below is scoped to it — never across tenants.
            _org_id = org_id
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

            # Extract email from resume text if parser didn't find a usable one.
            # Normalize whitespace — PDF extractors sometimes insert spaces around
            # `@` or `.` for styled headers (e.g. "foo @gmail.com"), which would
            # otherwise be persisted verbatim from Haiku or missed by the regex.
            import re as _re
            from app.services.fast_resume_parser import extract_email as _extract_email
            _haiku_email_raw = (parsed.get("candidate_email") or "").strip()
            _haiku_email = _re.sub(r"\s+", "", _haiku_email_raw).lower() if _haiku_email_raw else ""
            if (
                not _haiku_email
                or "@" not in _haiku_email
                or _haiku_email.endswith("@resume.local")
                or not _re.search(r"\.[a-zA-Z]{2,}$", _haiku_email)
            ):
                fallback_email = _extract_email(resume_text or "")
                if fallback_email:
                    parsed["candidate_email"] = fallback_email
                elif _haiku_email and "@" in _haiku_email and not _haiku_email.endswith("@resume.local"):
                    parsed["candidate_email"] = _haiku_email
                else:
                    parsed["candidate_email"] = None
            else:
                parsed["candidate_email"] = _haiku_email

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
                    ex_meta = find_profile(db, _org_id, real_email, include_shared_inboxes=True)
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
            if real_email:
                candidate.email = real_email
            elif candidate.email and candidate.email.startswith("pending_") and candidate.email.endswith("@resume.local"):
                # Parser could not extract a real email. Rewrite the placeholder so the
                # candidate is not silently hidden by the list-view filter at
                # candidate_service.get_candidates (which excludes pending_%@resume.local).
                # Use a stable per-candidate stub tied to the DB id so duplicate detection
                # still has something to key on.
                candidate.email = f"no-email-{candidate.id}@unknown.local"
            candidate.phone = parsed.get("phone") or candidate.phone
            candidate.location = parsed.get("location") or candidate.location
            candidate.role = parsed.get("current_designation") or candidate.role

            # ── Upsert ResumeMetadata (org-scoped) ────────────────────────────
            meta = None
            if parsed_email and not parsed_email.endswith("@resume.local"):
                meta = find_profile(db, _org_id, parsed_email, include_shared_inboxes=True)

            if not meta:
                # Prefer the real parsed email; otherwise use the (possibly rewritten)
                # candidate.email — never persist a pending_%@resume.local placeholder
                # into ResumeMetadata, since the list-view filter hides those.
                meta_email = real_email or candidate.email
                meta = ResumeMetadata(
                    candidate_email=meta_email,
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

            # ── Commit all candidate + metadata BEFORE scoring ────────────────
            # Scoring can fail (AI timeout, API error, DB constraint on fit-score
            # fields). Without this early commit, a scoring failure leaves the
            # session needing rollback, causing the final db.commit() below to
            # throw PendingRollbackError — which deletes the candidate entirely.
            # By committing here first, scoring failure can only undo fit-score
            # columns, not the parsed resume data we just wrote.
            candidate.parsing_status = "processing"
            db.commit()

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
                    # Keep the candidate with "failed" status so the upload response
                    # can return it and the UI can show the actual error — deleting it
                    # caused the frontend to show a generic "Failed" with no context.
                    candidate.parsing_status = "failed"
                    candidate.parsing_error = str(exc)[:500]
                    db.commit()
                    logger.info("Marked candidate %s as failed: %s", candidate_id, str(exc)[:200])
            except Exception as cleanup_exc:
                logger.warning("Cleanup after parse failure: %s", cleanup_exc)
        finally:
            db.close()


async def _compute_fit_score_with_fallback(
    candidate: Candidate, db: Session, job_id: int, resume_text: str | None = None
):
    """
    Run AI scoring directly (awaited). Falls back to smart scoring if AI fails.
    This runs inside _parse_one which is already concurrent, so the upload
    endpoint shows "Parsing..." while this completes (~30-40s per candidate).

    IMPORTANT: Any exception raised inside _ai_fit_score_for_candidate (including
    a failed db.commit()) leaves the SQLAlchemy session needing rollback.  If we
    swallow the exception without calling db.rollback(), the shared session is in
    an invalid state — the caller's subsequent db.commit() then raises
    PendingRollbackError, which triggers _parse_one's outer except and DELETES
    the candidate.  Always rollback after a scoring failure to return the session
    to a clean state for the caller.
    """
    # Try AI scoring — produces accurate score with reasoning
    try:
        score_data = await asyncio.wait_for(
            _ai_fit_score_for_candidate(candidate, db, resume_text=resume_text),
            timeout=90,
        )
        if score_data:
            logger.info("AI fit score for candidate %s: %s%% (%s)",
                        candidate.id, score_data.get("fit_percentage"), score_data.get("fit_level"))
            return
    except asyncio.TimeoutError:
        logger.warning("AI scoring timed out for candidate %s", candidate.id)
        # Rollback only if the session is in an invalid state from a failed
        # db.commit() inside _ai_fit_score_for_candidate. If the session is
        # clean (AI call failed before any commit), rollback is a no-op.
        try:
            db.rollback()
        except Exception:
            pass
    except Exception as e:
        logger.warning("AI scoring failed for candidate %s: %s", candidate.id, e)
        try:
            db.rollback()
        except Exception:
            pass

    # Fallback: smart scoring (instant)
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
            logger.info("Smart scoring fallback for candidate %s: %s%%", candidate.id, result.fit_percentage)
    except Exception as e:
        logger.warning("Smart scoring also failed for candidate %s: %s", candidate.id, e)
        try:
            db.rollback()
        except Exception:
            pass


_background_tasks: list[asyncio.Task] = []  # prevent GC of fire-and-forget tasks


def _launch_parse_task(candidate_id: int, file_path: str, job_id: int | None, org_id: int | None = None):
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

    task = loop.create_task(_parse_one(candidate_id, file_path, job_id, org_id))
    _background_tasks.append(task)  # prevent garbage collection
    task.add_done_callback(_task_done)
    logger.info("Parse task created for candidate %s: %s", candidate_id, task)


# ── Candidate Quota Pre-check ──────────────────────────────────────────────────

@router.get("/can-add")
def can_add_candidate(
    job_id: JobId,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Pre-check whether another candidate can be added for this job."""
    job = authorize_job(db, job_id, user)
    if not job.organization_id:
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


# ── Duplicate Check ──────────────────────────────────────────────────────────
# Declared before GET /{candidate_id}, which would otherwise capture
# "/check-duplicate" and answer 422.

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
    job_id: Optional[JobId] = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    from app.models.job import Job

    org_id = get_org_id(user)  # None only for a global super admin
    query = db.query(ResumeMetadata).filter(ResumeMetadata.candidate_email == email)
    if org_id is not None:
        query = query.filter(ResumeMetadata.organization_id == org_id)
    meta = query.first()

    if not meta:
        return DuplicateCheckResponse(exists=False)

    # Linked jobs within the caller's organization only (this used to list the
    # email's job ids across every tenant).
    linked_q = (
        db.query(Candidate.job_id)
        .join(Job, Candidate.job_id == Job.id)
        .filter(Candidate.email == email)
    )
    if org_id is not None:
        linked_q = linked_q.filter(Job.organization_id == org_id)
    linked_jobs = [c.job_id for c in linked_q.all()]

    return DuplicateCheckResponse(
        exists=True,
        candidate_email=meta.candidate_email,
        candidate_name=meta.candidate_name,
        current_designation=meta.current_designation,
        resume_file_path=meta.resume_file_path,
        resume_file_url=sign_media_url(meta.resume_file_path),
        parsed_at=meta.parsed_at,
        linked_job_ids=linked_jobs,
    )


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
    job_id: Optional[JobId] = None,
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
    if payload.job_id is not None:
        # The body job must belong to the caller's organization (404 otherwise) —
        # this used to insert into, or overwrite rows of, any tenant's job.
        authorize_job(db, payload.job_id, user)
        # Enforce org candidate quota using lifetime counter — only when a row
        # is actually inserted (an existing email on this job is updated in place).
        existing = (
            candidate_service.find_candidate_for_job(db, payload.email, payload.job_id)
            if payload.email
            else None
        )
        if existing is None:
            _check_and_increment_candidate_quota(db, payload.job_id)
    candidate = candidate_service.create_candidate(db, payload)
    candidate.added_by = user.email
    db.commit()
    db.refresh(candidate)

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
def get_candidate(candidate_id: CandidateId, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    candidate = _get_candidate_with_org_check(db, candidate_id, user)
    return candidate


@router.put("/{candidate_id}", response_model=CandidateRead, summary="Update candidate")
def update_candidate(
    candidate_id: CandidateId, payload: CandidateUpdate, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    candidate = _get_candidate_with_org_check(db, candidate_id, user)
    # Moving the candidate to another job: the target job must be the caller's
    # too (otherwise a candidate could be pushed into another tenant's job, and
    # the duplicate-email 409 below became a cross-tenant membership oracle).
    if payload.job_id is not None and payload.job_id != candidate.job_id:
        authorize_job(db, payload.job_id, user)
    try:
        candidate = candidate_service.update_candidate(db, candidate_id, payload)
    except candidate_service.DuplicateCandidateEmailError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        import traceback
        import uuid as _uuid
        # Details go to the log only: the exception text carried the failing
        # SQL statement and its bound parameters back to the browser.
        error_id = _uuid.uuid4().hex
        logger.error(
            "update_candidate(%s) failed [%s]: %s\n%s",
            candidate_id, error_id, exc, traceback.format_exc()
        )
        raise HTTPException(
            status_code=500,
            detail=f"Failed to update candidate. Please check the values and try again (error id {error_id}).",
        ) from exc
    if not candidate:
        raise HTTPException(status_code=404, detail="Candidate not found")
    # Serialize to dict here so any Pydantic/SQLAlchemy serialization errors
    # are surfaced with a clear message rather than a blank 500.
    try:
        return CandidateRead.model_validate(candidate, from_attributes=True)
    except Exception as exc:
        import traceback
        logger.error(
            "update_candidate(%s) serialization failed: %s\n%s",
            candidate_id, exc, traceback.format_exc()
        )
        raise HTTPException(
            status_code=500,
            detail="Candidate updated, but the updated record could not be returned. Please refresh.",
        ) from exc


@router.delete("/{candidate_id}", status_code=status.HTTP_204_NO_CONTENT, summary="Delete candidate")
def delete_candidate(candidate_id: CandidateId, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
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
    candidate_id: CandidateId,
    payload: Optional[HireCandidateRequest] = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    from app.models.job import Job

    candidate = _get_candidate_with_org_check(db, candidate_id, user)

    job_id = payload.job_id if payload is not None else None
    # The job being hired into must belong to the caller's organization (404
    # otherwise) — this used to accept any tenant's job and charge its quota.
    target_job = authorize_job(db, job_id, user) if job_id is not None else None

    # Find all job associations for this candidate (by email) — within ONE
    # tenant. Matching the email across every organization pushed other
    # companies' rows to Offer-Made. A global super admin is scoped to the
    # tenant of the job being hired into (or of the candidate's own job).
    email = candidate.email
    scope_org_id = get_org_id(user)
    if scope_org_id is None:
        scope_org_id = (
            target_job.organization_id if target_job is not None else job_org_id(db, candidate.job_id)
        )
    entries_q = (
        db.query(Candidate)
        .join(Job, Candidate.job_id == Job.id)
        .filter(Candidate.email == email)
    )
    if scope_org_id is not None:
        entries_q = entries_q.filter(Job.organization_id == scope_org_id)
    else:
        entries_q = entries_q.filter(Job.organization_id.is_(None))
    all_entries = entries_q.all()

    job_ids_linked = [c.job_id for c in all_entries]

    # If job_id provided and candidate not in that job, add them
    if target_job is not None and target_job.id not in job_ids_linked:
        job = target_job
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
def get_job_associations(candidate_id: CandidateId, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    from app.models.job import Job

    candidate = _get_candidate_with_org_check(db, candidate_id, user)

    # Same email within the caller's organization only — this used to return
    # every tenant's job ids/titles/statuses for the email, and the UI then
    # hired into the first one.
    org_id = get_org_id(user)  # None only for a global super admin
    entries_q = (
        db.query(Candidate.job_id, Candidate.status, Candidate.candidate_stage, Job.title)
        .join(Job, Candidate.job_id == Job.id)
        .filter(Candidate.email == candidate.email)
    )
    if org_id is not None:
        entries_q = entries_q.filter(Job.organization_id == org_id)
    entries = entries_q.all()

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
def get_parsing_status(candidate_id: CandidateId, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    candidate = _get_candidate_with_org_check(db, candidate_id, user)
    return {
        "candidate_id": candidate.id,
        "parsing_status": candidate.parsing_status,
        "parsing_error": candidate.parsing_error,
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
    job_id: Optional[JobId] = Form(None),
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
    # Verify user owns the job (fails closed — see app.authz)
    _job = authorize_job(db, job_id, user) if job_id is not None else None
    # Tenant for the parsed resume metadata: the job's, else the uploader's.
    org_id = _job.organization_id if _job is not None else get_org_id(user)

    created_candidates = []
    parse_tasks = []

    # Pre-check quota availability (don't increment yet — only on successful parse)
    if _job is not None:
        if _job.organization_id:
            _org = db.query(Organization).filter(Organization.id == _job.organization_id).first()
            if _org and (_org.max_candidates_per_job or 0) > 0:
                remaining = _org.max_candidates_per_job - (_org.total_candidates_used or 0)
                if remaining < len(files):
                    raise HTTPException(
                        status_code=403,
                        detail=f"Candidate quota exceeded. You can add {max(0, remaining)} more candidate(s).",
                    )

    skipped_files: list[str] = []

    if len(files) > MAX_RESUME_FILES_PER_UPLOAD:
        raise HTTPException(
            status_code=400,
            detail=f"Upload at most {MAX_RESUME_FILES_PER_UPLOAD} resumes at a time.",
        )

    for file in files:
        if file.content_type not in ALLOWED_RESUME_TYPES:
            ext = Path(file.filename).suffix.upper() if file.filename else "Unknown"
            skipped_files.append(f"{file.filename} ({ext})")
            logger.warning("Skipped unsupported file: %s (type: %s)", file.filename, file.content_type)
            continue
        problem = _resume_upload_problem(file)
        if problem:
            skipped_files.append(f"{file.filename} ({problem})")
            logger.warning("Skipped resume upload %s: %s", file.filename, problem)
            continue

        safe_name = _stored_resume_name(file)
        file_path = os.path.join(RESUME_UPLOAD_DIR, safe_name)
        try:
            await asyncio.to_thread(store_resume_upload, file, file_path)
        except UnsafeFile as exc:
            skipped_files.append(f"{file.filename} ({exc.reason})")
            continue
        schedule_compression(file_path)  # background worker; the stored copy shrinks, its text doesn't change

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
            added_by=user.email,
        )
        db.add(candidate)
        db.commit()
        db.refresh(candidate)
        created_candidates.append(candidate)

        # Queue for background parse
        parse_tasks.append((candidate.id, file_path, job_id))

    # If ALL files were unsupported, return error
    if not parse_tasks and skipped_files:
        raise HTTPException(
            status_code=400,
            detail=f"No valid resumes to upload. Unsupported files: {', '.join(skipped_files)}. Only PDF, DOC, and DOCX are accepted.",
        )

    # Parse all resumes concurrently (semaphore limits to 3 at a time)
    # Awaiting here ensures tasks complete reliably (no fire-and-forget issues)
    if parse_tasks:
        await asyncio.gather(
            *[_parse_one(cid, fp, jid, org_id) for cid, fp, jid in parse_tasks],
            return_exceptions=True,
        )

    # Save IDs and paths BEFORE expire_all (avoids ObjectDeletedError)
    candidate_refs = [(c.id, c.resume_path) for c in created_candidates]

    # Re-fetch candidates to return final parsed state
    db.expire_all()
    result = []
    seen_ids: set[int] = set()
    for cid, cpath in candidate_refs:
        try:
            refreshed = db.query(Candidate).filter(Candidate.id == cid).first()
            if refreshed and refreshed.id not in seen_ids:
                result.append(refreshed)
                seen_ids.add(refreshed.id)
            elif cpath:
                # Placeholder was deleted (dedup) — find surviving candidate
                surviving = db.query(Candidate).filter(
                    Candidate.resume_path == cpath,
                    Candidate.job_id == job_id,
                ).first()
                if surviving and surviving.id not in seen_ids:
                    result.append(surviving)
                    seen_ids.add(surviving.id)
        except Exception:
            pass  # candidate was deleted during parse — skip

    return result


# ── Replace Resume for Existing Candidate ─────────────────────────────────────

async def _reparse_for_existing_candidate(candidate_id: int, file_path: str) -> None:
    """
    Re-parse a newly uploaded resume for an EXISTING candidate.

    Unlike `_parse_one`, this preserves the candidate's identity (email + DB row)
    instead of deleting / merging on email collision. Used by the per-row
    "upload resume" action when a candidate has no resume file.
    """
    async with _get_parse_semaphore():
        db = SessionLocal()
        try:
            candidate = db.query(Candidate).filter(Candidate.id == candidate_id).first()
            if not candidate:
                return

            candidate.parsing_status = "processing"
            db.commit()

            resume_text = await asyncio.to_thread(_sync_extract_text, file_path)

            parsed = None
            try:
                from app.services.llm_resume_parser import llm_parse_resume
                parsed = await llm_parse_resume(resume_text)
                if not (parsed and parsed.get("candidate_name")):
                    parsed = None
            except Exception as haiku_exc:
                logger.warning("Haiku parse failed for re-upload candidate %s: %s", candidate_id, haiku_exc)

            if not parsed:
                try:
                    from app.services.fast_resume_parser import fast_parse_resume
                    parsed = await asyncio.to_thread(fast_parse_resume, resume_text)
                except Exception as regex_exc:
                    logger.error("Regex parse failed for re-upload candidate %s: %s", candidate_id, regex_exc)
                    parsed = {}

            # Update candidate fields — preserve identity (email + id)
            candidate.name = parsed.get("candidate_name") or candidate.name
            candidate.phone = parsed.get("phone") or candidate.phone
            candidate.location = parsed.get("location") or candidate.location
            candidate.role = parsed.get("current_designation") or candidate.role
            candidate.resume_path = file_path

            # Tenant = the candidate's job's organization. The lookup below is
            # always scoped to it: a job-less candidate (organization unknown)
            # used to overwrite whichever tenant's metadata matched the email.
            _org_id = job_org_id(db, candidate.job_id)

            # Upsert ResumeMetadata keyed by the candidate's existing email
            meta = None
            if candidate.email and not candidate.email.endswith("@resume.local"):
                meta = find_profile(db, _org_id, candidate.email, include_shared_inboxes=True)
            if not meta:
                meta = ResumeMetadata(
                    candidate_email=candidate.email,
                    organization_id=_org_id,
                )
                db.add(meta)

            meta.candidate_name = parsed.get("candidate_name") or candidate.name
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

            if candidate.job_id:
                await _compute_fit_score_with_fallback(candidate, db, candidate.job_id, resume_text)

            candidate.parsing_status = "completed"
            db.commit()
        except Exception as exc:
            logger.error("Re-parse for candidate %s failed: %s", candidate_id, exc, exc_info=True)
            try:
                db.rollback()
                c = db.query(Candidate).filter(Candidate.id == candidate_id).first()
                if c:
                    c.parsing_status = "failed"
                    db.commit()
            except Exception:
                pass
        finally:
            db.close()


@router.post(
    "/{candidate_id}/replace-resume",
    response_model=CandidateRead,
    summary="Upload or replace resume for an existing candidate",
    description=(
        "Upload a PDF/DOC/DOCX resume for an existing candidate. The new file "
        "replaces any previously uploaded resume, the resume is re-parsed, and "
        "the fit score is recomputed.\n\n"
        "Preserves the candidate's identity (email, DB row, interview state) — "
        "only `resume_path`, parsed metadata, and `resume_fit_score` are updated.\n\n"
        "**Content-Type:** `multipart/form-data`"
    ),
)
@limiter.limit("20/minute")
async def replace_resume(
    request: Request,
    candidate_id: CandidateId,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    candidate = _get_candidate_with_org_check(db, candidate_id, user)

    if file.content_type not in ALLOWED_RESUME_TYPES:
        ext = Path(file.filename).suffix.upper() if file.filename else "Unknown"
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported file type ({ext}). Only PDF, DOC, and DOCX are accepted.",
        )
    problem = _resume_upload_problem(file)
    if problem:
        raise HTTPException(status_code=400, detail=f"Resume rejected: {problem}.")

    safe_name = _stored_resume_name(file)
    new_path = os.path.join(RESUME_UPLOAD_DIR, safe_name)
    try:
        await asyncio.to_thread(store_resume_upload, file, new_path)
    except UnsafeFile as exc:
        raise HTTPException(status_code=exc.status_code, detail=f"Resume rejected: {exc.reason}")
    schedule_compression(new_path)

    old_path = candidate.resume_path

    # Parse synchronously so the caller receives the updated candidate
    await _reparse_for_existing_candidate(candidate.id, new_path)

    # Best-effort cleanup of the previous file: only inside the resumes folder
    # (resume_path used to be client-writable and reached os.remove as-is), and
    # only when no row still points at it — this candidate after a failed
    # re-parse, a hired copy, or a resume-database entry.
    if old_path and old_path != new_path and not resume_file_in_use(db, old_path):
        safe_unlink_upload(old_path, ["resumes"])

    db.expire_all()
    refreshed = db.query(Candidate).filter(Candidate.id == candidate_id).first()
    if not refreshed:
        raise HTTPException(status_code=500, detail="Failed to reload candidate after resume replace")
    return refreshed


# ── Fit Score Recompute ───────────────────────────────────────────────────────

@router.post(
    "/{candidate_id}/compute-fit-score",
    dependencies=[Depends(per_user_limit("compute-fit-score", 20))],
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
async def compute_fit_score(candidate_id: CandidateId, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
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
    # One call can queue an LLM scoring run per candidate in the job.
    dependencies=[Depends(per_user_limit("batch-recompute", 3, 10 * 60))],
    summary="Batch recompute fit scores for all candidates in a job",
    description=(
        "Re-runs smart scoring instantly for all candidates, then triggers "
        "AI semantic scoring in the background for accurate results.\n\n"
        "Smart scores appear immediately. AI scores update within ~40s per candidate."
    ),
)
async def batch_recompute_scores(
    job_id: JobId,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Batch recompute fit scores: instant smart scoring + background AI scoring."""
    from app.services.smart_scoring_service import compute_fit_score_for_candidate as smart_score

    job = authorize_job(db, job_id, user)

    # Also rebuild job metadata with normalized skills (LLM-enhanced)
    try:
        from app.services.jd_metadata_service import build_job_metadata
        await build_job_metadata(db, job_id)
        logger.info("Rebuilt job metadata for job %s before batch scoring", job_id)
    except Exception as meta_exc:
        logger.warning("Job metadata rebuild failed for job %s: %s", job_id, meta_exc)

    candidates = db.query(Candidate).filter(Candidate.job_id == job_id).all()
    if not candidates:
        return {"total": 0, "updated": 0, "failed": 0, "message": "No candidates found"}

    updated = 0
    failed = 0
    results = []

    # Run AI scoring sequentially but with a per-candidate timeout
    # to avoid the entire request timing out
    for candidate in candidates:
        try:
            score_data = await asyncio.wait_for(
                _ai_fit_score_for_candidate(candidate, db),
                timeout=60,  # 60s max per candidate
            )
            if score_data:
                updated += 1
                results.append({
                    "id": candidate.id,
                    "name": candidate.name,
                    "fit_score": score_data.get("fit_percentage"),
                    "fit_level": score_data.get("fit_level"),
                })
            else:
                failed += 1
        except asyncio.TimeoutError:
            logger.warning("AI scoring timed out for candidate %s, skipping", candidate.id)
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
    from app.services.smart_scoring_service import resume_metadata_for_candidate
    from app.schemas.ai import FitScoreRequest

    # Get job data first: it decides which tenant's resume data may be used.
    job = db.query(Job).filter(Job.id == candidate.job_id).first() if candidate.job_id else None
    if not job:
        return None

    # Get resume text if not provided
    if not resume_text:
        # Parsed resume from the job's own organization only — matching on email
        # alone used to send another tenant's resume text to the LLM.
        meta = resume_metadata_for_candidate(db, candidate, job.organization_id)
        if meta and meta.raw_text:
            resume_text = meta.raw_text
        if not resume_text:
            # Only a real file inside the resumes folder (resume_path used to be
            # client-writable, so any .pdf/.docx on the server could be read).
            resume_file = resolve_upload_file(candidate.resume_path, ["resumes"])
            if resume_file:
                try:
                    resume_text = extract_text(resume_file)
                except Exception as e:
                    logger.warning("Failed to extract text from %s: %s", candidate.resume_path, e)

    if not resume_text:
        logger.warning("No resume text found for candidate %s (email=%s, resume_path=%s)", candidate.id, candidate.email, candidate.resume_path)
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
    if job.upload_jd:
        try:
            # upload_jd is a client-supplied string: read it only when it resolves
            # to a file inside the JD upload folder.
            from app.services.jd_metadata_service import resolve_jd_file

            jd_file = resolve_jd_file(job.upload_jd)
            if jd_file:
                jd_text = extract_text(jd_file)
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

    # ── Blend the LLM's semantic composite with the continuous feature score ──
    # On its own the LLM collapses similar candidates onto identical values
    # (every plausible applicant to one job came back 82%). The feature score is
    # continuous and effectively never ties, so blending restores the ranking
    # while keeping the LLM as the dominant signal. See the weights and the full
    # rationale in smart_scoring_service.
    from app.services.smart_scoring_service import (
        blend_with_features,
        feature_score_for_candidate,
        level_for_percentage,
    )

    feature_result = feature_score_for_candidate(db, candidate, job, resume_text)
    final_percentage = blend_with_features(result.fit_percentage, feature_result)
    final_level = level_for_percentage(final_percentage)

    logger.info(
        "Fit score candidate=%s job=%s: llm=%s%% feature=%s%% -> blended=%s%% (%s)",
        candidate.id, job.id, result.fit_percentage,
        feature_result.fit_percentage if feature_result else "n/a",
        final_percentage, final_level,
    )

    # Save to candidate. `breakdown` carries the per-dimension feature detail in
    # the SAME shape the deterministic path writes, so the hover tooltip is
    # populated no matter which path scored the candidate — previously the LLM
    # path left it empty and the tooltip showed an all-zero breakdown.
    score_data = {
        "fit_level": final_level,
        "fit_percentage": final_percentage,
        "summary": result.summary,
        "key_matching_skills": result.key_matching_skills,
        "relevant_experience_summary": result.relevant_experience_summary,
        "missing_weak_areas": result.missing_weak_areas,
        "reasoning_points": result.reasoning_points,
        "verdict": result.verdict,
        # Unified fields also emitted by smart_scoring_service.ScoringResult
        "missing_skills": result.missing_weak_areas,
        "breakdown": feature_result.breakdown if feature_result else {},
        # Provenance so a score can always be traced back to how it was made.
        "score_source": "llm+features" if feature_result else "llm",
        "llm_percentage": result.fit_percentage,
        "feature_percentage": feature_result.fit_percentage if feature_result else None,
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

    candidate.resume_fit_score = f"{final_percentage}%"
    candidate.fit_score_reasoning = json.dumps(score_data)
    candidate.fit_score_details = json.dumps(details_data)
    db.commit()
    db.refresh(candidate)

    return score_data


# ── Interview Scheduling ──────────────────────────────────────────────────────

@router.post(
    "/{candidate_id}/schedule-interview",
    # Each call emails the candidate (plus CCs) from the platform's domain.
    dependencies=[Depends(per_user_limit("schedule-interview", 30))],
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
    candidate_id: CandidateId,
    payload: ScheduleInterviewRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Schedule (or re-schedule) an interview for a candidate and send notifications."""
    from app.services.interview_service import send_schedule_email, send_whatsapp_message
    from app.models.interview import InterviewStatusLog

    candidate = _get_candidate_with_org_check(db, candidate_id, user)

    # The interview link is per job; a candidate without one can't take an interview.
    if not candidate.job_id:
        raise HTTPException(status_code=400, detail="Candidate is not linked to a job opening.")

    # Validate WhatsApp permission
    if payload.send_whatsapp:
        _require_whatsapp_allowed(db, user)

    old_status = candidate.interview_status
    is_reschedule = old_status == "Scheduled"

    # Terminal statuses represent a completed (or otherwise concluded) AI round.
    # Rescheduling must not silently undo that — it would zero out the "AI Round"
    # tab count and the "Interview Completed" card, and overwrite the candidate's
    # real interview_date with the new schedule.
    TERMINAL_STATUSES = {
        "Completed",
        "Terminated – Cheating Detected",
        "Ended by Candidate",
        "Candidate Terminated Prematurely",
        "Terminated Prematurely",
        "Disconnected – Network Issue",
        "Candidate Dropped Interview",
    }
    if old_status in TERMINAL_STATUSES:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Candidate's interview is already '{old_status}'. "
                "Move them back to a pre-interview stage before rescheduling."
            ),
        )

    # Log status change
    log = InterviewStatusLog(
        candidate_id=candidate.id,
        previous_status=old_status,
        new_status="Scheduled",
        changed_by="admin",
        reason="Rescheduled" if is_reschedule else "Scheduled",
    )
    db.add(log)

    # Normalize the scheduled moment to a NAIVE UTC datetime before storing.
    # Pydantic parses "...Z" into an aware UTC datetime, but the column is
    # `TIMESTAMP WITHOUT TIME ZONE`; depending on psycopg / server session
    # timezone, an aware value can land in the DB as the server-local wall
    # clock instead of UTC, which then gets a second +offset applied when
    # _format_interview_date converts "naive-as-UTC" → IST. Forcing UTC-naive
    # here removes all ambiguity so both email and dashboard render the
    # original recruiter-selected moment correctly.
    _sched_dt = payload.interview_date
    if _sched_dt is not None and _sched_dt.tzinfo is not None:
        _sched_dt = _sched_dt.astimezone(_tz.utc).replace(tzinfo=None)
    candidate.interview_date = _sched_dt
    candidate.interview_timezone = payload.interview_timezone
    candidate.joining_flexibility = payload.joining_flexibility
    candidate.allow_candidate_reschedule = payload.allow_candidate_reschedule
    candidate.allow_joining_early = payload.allow_joining_early
    candidate.interview_status = "Scheduled"

    # Personal invite link (app.invites): the candidate's own secret on the job's
    # interview link. The email, WhatsApp and calendar all use this stored link;
    # it is committed below before anything is sent. The URL the recruiter's page
    # sends is only an origin hint. A reschedule keeps the same secret, so an
    # earlier email's link keeps working; cancel/retake rotate it.
    job_token = get_or_create_job_token(db, candidate.job_id)
    ensure_invite_token(candidate)
    candidate.interview_link = build_invite_url(
        job_token.token, candidate.invite_token, origin_hint=payload.interview_url
    )

    # Update token expiry based on recruiter's choice.
    if candidate.job_id:
        from datetime import timedelta
        token_row = job_token
        if token_row:
            if payload.link_expiry:
                # Recruiter chose "Expires after X hours/days"
                if payload.link_expiry.unit == "hours":
                    new_expiry = datetime.utcnow() + timedelta(hours=payload.link_expiry.value)
                else:
                    new_expiry = datetime.utcnow() + timedelta(days=payload.link_expiry.value)
                # Ensure token lives at least 24h past the scheduled interview
                # (naive UTC, like new_expiry — the aware payload value raised TypeError)
                if _sched_dt:
                    min_expiry = _sched_dt + timedelta(hours=24)
                    if new_expiry < min_expiry:
                        new_expiry = min_expiry
                token_row.expires_at = new_expiry
            else:
                # Recruiter chose "No expiry (link stays active)" —
                # CLEAR any existing expires_at so the link never expires
                token_row.expires_at = None

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

    # ── Observer link (live read-only watch URL for recruiter) ──────────────
    observer_link = None
    observer_email_sent = False
    try:
        from app.services.observer_service import (
            get_or_create_observer_token,
            send_observer_link_email,
            _build_observer_url,
        )
        from app.models.job import Job as _JobModel

        _exp = None
        if candidate.interview_date:
            from datetime import timedelta as _td
            _exp = candidate.interview_date + _td(days=2)

        obs_tok = get_or_create_observer_token(
            db,
            candidate=candidate,
            creator_user_id=user.id,
            creator_email=user.email,
            expires_at=_exp,
        )
        observer_link = _build_observer_url(obs_tok.token)

        _job = db.query(_JobModel).filter(_JobModel.id == candidate.job_id).first()
        _title = _job.title if _job else "Interview"
        observer_email_sent = await send_observer_link_email(
            db,
            candidate=candidate,
            creator_email=user.email,
            observer_token=obs_tok,
            job_title=_title,
            is_reschedule=is_reschedule,
        )
    except Exception:
        logger.warning(
            "Failed to mint or email observer link for candidate %s",
            candidate.id,
            exc_info=True,
        )

    # Return candidate data + notification status
    result = CandidateRead.model_validate(candidate)
    response_dict = result.model_dump()
    response_dict["email_sent"] = email_sent
    response_dict["whatsapp_sent"] = whatsapp_sent
    if google_meet_link:
        response_dict["google_meet_link"] = google_meet_link
    if observer_link:
        response_dict["observer_link"] = observer_link
        response_dict["observer_email_sent"] = observer_email_sent
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
    candidate_id: CandidateId,
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

    # Same organization gate as schedule-interview (this branch used to skip it).
    if payload.send_whatsapp:
        _require_whatsapp_allowed(db, user)

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
    # The emailed personal link must stop working; scheduling again issues a new one.
    rotate_invite_token(candidate)
    candidate.interview_link = None
    # Likewise any observer link shared for this interview.
    from app.services.observer_service import revoke_observer_tokens
    revoke_observer_tokens(db, candidate.id)
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


# ── Retake Interview: archive (download) then reset ───────────────────────────

def _recordings_dir() -> str:
    """Disk directory where interview recordings live (mirrors interview.py)."""
    return os.path.join(UPLOAD_ROOT, "recordings")


def _completed_or_terminated_sessions(db: Session, candidate_id: int):
    from app.models.interview import InterviewSession, SessionStatus
    return (
        db.query(InterviewSession)
        .filter(
            InterviewSession.candidate_id == candidate_id,
            InterviewSession.status.in_([SessionStatus.completed, SessionStatus.terminated]),
        )
        .order_by(InterviewSession.id.desc())
        .all()
    )


@router.get(
    "/{candidate_id}/interview-archive",
    summary="Download a ZIP backup of the candidate's interview (video + transcript + evaluation)",
    description=(
        "Bundles the completed interview into a single ZIP — `recording.webm`, "
        "`transcript.txt`, and `evaluation.json` — so the recruiter can keep a "
        "copy BEFORE resetting the interview for a retake."
    ),
)
def download_interview_archive(
    candidate_id: CandidateId,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    import json as _json
    import tempfile
    import zipfile
    from fastapi.responses import FileResponse
    from starlette.background import BackgroundTask
    from app.models.interview import InterviewMessage, Evaluation, Violation

    candidate = _get_candidate_with_org_check(db, candidate_id, user)

    sessions = _completed_or_terminated_sessions(db, candidate_id)
    if not sessions:
        raise HTTPException(
            status_code=404,
            detail="No completed interview found to archive for this candidate.",
        )
    session = sessions[0]  # most recent

    def _loads(s):
        if not s:
            return None
        try:
            return _json.loads(s)
        except Exception:
            return s

    # ── Transcript ────────────────────────────────────────────────────────────
    msgs = (
        db.query(InterviewMessage)
        .filter(InterviewMessage.session_id == session.id)
        .order_by(InterviewMessage.created_at)
        .all()
    )
    lines = [
        f"Interview Transcript — {candidate.name or 'Candidate'} <{candidate.email or 'no-email'}>",
        f"Session {session.id} | status={getattr(session.status, 'value', session.status)} "
        f"| completed_at={session.completed_at}",
        "=" * 70,
        "",
    ]
    for m in msgs:
        speaker = "AI Interviewer" if m.role == "ai" else (candidate.name or "Candidate")
        if m.feedback:
            lines.append(f"{speaker}:\n{m.feedback}\n")
        lines.append(f"{speaker}:\n{m.content}\n")
    transcript_txt = "\n".join(lines)

    # ── Evaluation JSON ─────────────────────────────────────────────────────────
    ev = db.query(Evaluation).filter(Evaluation.session_id == session.id).first()
    violations = db.query(Violation).filter(Violation.session_id == session.id).all()
    eval_obj = {
        "candidate": {
            "id": candidate.id,
            "name": candidate.name,
            "email": candidate.email,
            "role": candidate.role,
        },
        "session_id": session.id,
        "status": getattr(session.status, "value", str(session.status)),
        "completed_at": session.completed_at.isoformat() if session.completed_at else None,
        "video_duration": candidate.video_duration,
        "scores": {
            "overall": (ev.overall_score if ev else None) if ev else candidate.score,
            "technical": ev.technical_score if ev else None,
            "communication": ev.communication_score if ev else None,
            "problem_solving": ev.problem_solving_score if ev else None,
            "confidence": ev.confidence_score if ev else None,
            "coding": ev.coding_score if ev else None,
            "trust_score": candidate.trust_score,
            "ai_cheating_score": candidate.ai_cheating_score,
        },
        "recommendation": (ev.recommendation if ev else None),
        "summary": ev.summary if ev else None,
        "strengths": _loads(ev.strengths) if ev else None,
        "weaknesses": _loads(ev.weaknesses) if ev else None,
        "detailed_feedback": ev.detailed_feedback if ev else None,
        "per_question_evaluations": _loads(ev.per_question_evaluations) if ev else None,
        "skills_ratings": _loads(ev.skills_ratings) if ev else None,
        "violations": [
            {
                "type": getattr(v.violation_type, "value", str(v.violation_type)),
                "details": v.details,
                "at": v.occurred_at.isoformat() if v.occurred_at else None,
            }
            for v in violations
        ],
    }
    eval_json = _json.dumps(eval_obj, indent=2, default=str)

    # ── Recording file on disk ──────────────────────────────────────────────────
    # Resolved through app.media, and the stored link is used only when it names
    # one of THIS candidate's sessions (recordings are {session_id}.webm).
    # recording_link used to be client-writable, and a basename lookup could pull
    # another candidate's video into the ZIP.
    from app.models.interview import InterviewSession

    rec_path = None
    rec_ext = ".webm"
    rec_rel = to_upload_relpath(candidate.recording_link)
    if rec_rel and rec_rel.startswith("recordings/"):
        rec_stem = rec_rel.rsplit("/", 1)[-1].split(".", 1)[0]
        own_session_ids = {
            sid for (sid,) in
            db.query(InterviewSession.id).filter(InterviewSession.candidate_id == candidate.id).all()
        }
        if rec_stem.isdigit() and int(rec_stem) in own_session_ids:
            rec_path = resolve_upload_file(candidate.recording_link, ["recordings"])
            if rec_path:
                rec_ext = os.path.splitext(rec_path)[1] or ".webm"
    if not rec_path:
        # fall back to the conventional name {session_id}.webm
        for ext in (".webm", ".mp4"):
            p = resolve_upload_file(f"uploads/recordings/{session.id}{ext}", ["recordings"])
            if p:
                rec_path, rec_ext = p, ext
                break

    # ── Build the ZIP to a temp file, stream it, then clean up ──────────────────
    fd, zip_path = tempfile.mkstemp(suffix=".zip", prefix=f"interview_{candidate_id}_")
    os.close(fd)
    try:
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            # Evaluation + transcript as a single readable PDF report. Fall back
            # to the raw text/JSON only if PDF generation is unavailable.
            try:
                zf.writestr("evaluation_report.pdf", _build_evaluation_pdf_bytes(db, candidate))
            except Exception as pdf_exc:
                logger.warning(
                    "archive: evaluation PDF build failed for candidate %s: %s — including text fallback",
                    candidate_id, pdf_exc,
                )
                zf.writestr("transcript.txt", transcript_txt)
                zf.writestr("evaluation.json", eval_json)
            if rec_path:
                zf.write(rec_path, arcname=f"recording{rec_ext}")
            else:
                zf.writestr(
                    "recording_MISSING.txt",
                    "No recording file was found on the server for this interview.",
                )
    except Exception as exc:
        try:
            os.remove(zip_path)
        except OSError:
            pass
        logger.error("Failed to build interview archive for candidate %s: %s", candidate_id, exc)
        raise HTTPException(status_code=500, detail="Failed to build interview archive.")

    def _cleanup(path: str):
        try:
            if os.path.isfile(path):
                os.remove(path)
        except OSError:
            pass

    safe_name = (candidate.name or "candidate").strip().replace(" ", "_") or "candidate"
    return FileResponse(
        zip_path,
        media_type="application/zip",
        filename=f"interview_{safe_name}_{candidate_id}.zip",
        background=BackgroundTask(_cleanup, zip_path),
    )


@router.post(
    "/{candidate_id}/retake-interview",
    response_model=CandidateRead,
    summary="Reset a completed interview so the candidate can retake it (keeps resume & identity)",
    description=(
        "Permanently deletes the candidate's interview session(s) — transcript, "
        "evaluation, violations, screenshots, and the recording file — and resets "
        "the interview-state fields so the candidate can be scheduled again and start "
        "fresh. Their previous personal interview link stops working; scheduling "
        "again sends a new one. The candidate row, resume, and resume fit score are "
        "KEPT. Intended to be called AFTER the recruiter has downloaded the archive."
    ),
)
def retake_interview(
    candidate_id: CandidateId,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    from app.models.interview import InterviewSession, InterviewStatusLog

    candidate = _get_candidate_with_org_check(db, candidate_id, user)

    completed = _completed_or_terminated_sessions(db, candidate_id)
    if not completed:
        raise HTTPException(
            status_code=400,
            detail="This candidate has no completed interview to reset.",
        )

    # Reset means a clean slate: delete ALL of this candidate's sessions (so
    # verify-access creates a brand-new one), not just the completed ones.
    all_sessions = (
        db.query(InterviewSession)
        .filter(InterviewSession.candidate_id == candidate_id)
        .all()
    )
    session_ids = [s.id for s in all_sessions]

    # Delete recording files + leftover live-upload parts on disk (named by
    # session id). DB rows cascade-delete when the session is removed.
    rec_dir = _recordings_dir()
    for sid in session_ids:
        for ext in (".webm", ".mp4", ".webm.fixed.webm"):
            p = os.path.join(rec_dir, f"{sid}{ext}")
            try:
                if os.path.isfile(p):
                    os.remove(p)
            except OSError as exc:
                logger.warning("retake: could not remove recording %s: %s", p, exc)
        shutil.rmtree(os.path.join(rec_dir, ".parts", str(sid)), ignore_errors=True)

    # Deleting the session cascades to messages, evaluation, violations,
    # feedback, meeting_schedule, screenshots, and coding_submissions.
    for s in all_sessions:
        db.delete(s)

    # Reset the candidate's INTERVIEW state — keep identity, resume, fit score.
    log = InterviewStatusLog(
        candidate_id=candidate.id,
        previous_status=candidate.interview_status,
        new_status="New",
        changed_by="admin",
        reason="Interview reset for retake (previous interview archived & removed)",
    )
    db.add(log)

    candidate.interview_status = "New"
    # Move back to the initial pipeline stage so they no longer count as
    # "AI Round" (completed) — they're waiting to take the AI round again.
    candidate.candidate_stage = "Candidates"
    candidate.interview_date = None
    candidate.interview_timezone = None
    candidate.interview_link = None
    # Old personal links stop working; the candidate gets a new link when the
    # interview is scheduled again (schedule-interview also sets the job link's
    # expiry then). The job-wide link expiry is no longer pushed out here — that
    # re-opened expired links for every candidate on the job.
    rotate_invite_token(candidate)
    candidate.recording_link = None
    candidate.video_duration = None
    # Post-interview scores — clear all of them.
    candidate.score = None
    candidate.trust_score = None
    candidate.grammar_score = None
    candidate.confidence_score = None
    candidate.clarity_score = None
    candidate.pronunciation_score = None
    candidate.enthusiasm_score = None
    candidate.conciseness_score = None
    candidate.ai_cheating_score = None
    # An observer link shared for the old interview must not open the retake.
    from app.services.observer_service import revoke_observer_tokens
    revoke_observer_tokens(db, candidate.id)

    db.commit()
    db.refresh(candidate)
    logger.info(
        "[Retake] Reset interview for candidate %s (%s) — deleted sessions %s",
        candidate.id, candidate.email, session_ids,
    )
    return candidate


# ── Stage Promotion ───────────────────────────────────────────────────────────

@router.post(
    "/{candidate_id}/promote",
    response_model=CandidateRead,
    summary="Promote candidate to a hiring stage",
    description="Move candidate between stages: `Candidates` → `AI Round` → `Panel Round` → `Offer-Made` / `Rejected`. Also updates `status` to Offered/Rejected when appropriate.",
)
def promote_candidate(
    candidate_id: CandidateId,
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
    dependencies=[Depends(per_user_limit("send-whatsapp", 20))],
    summary="Send WhatsApp message",
    description="Send a custom free-form text message to the candidate via Meta WhatsApp Business Cloud API. Requires `WHATSAPP_TOKEN` and `WHATSAPP_PHONE_NUMBER_ID` in `.env`.",
)
async def send_whatsapp(
    candidate_id: CandidateId,
    payload: SendWhatsAppRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Send a custom WhatsApp message to a candidate."""
    import re as _re
    from app.services.interview_service import send_whatsapp_message

    candidate = _get_candidate_with_org_check(db, candidate_id, user)
    _require_whatsapp_allowed(db, user)

    # Messages go only to the candidate's number on file: the platform's business
    # number must not be usable to text arbitrary phones.
    phone = candidate.phone
    if not phone:
        raise HTTPException(status_code=400, detail="No phone number available")
    given = _re.sub(r"\D", "", payload.phone or "")
    on_file = _re.sub(r"\D", "", phone)
    if given and not (
        given == on_file
        or (min(len(given), len(on_file)) >= 7 and (given.endswith(on_file) or on_file.endswith(given)))
    ):
        raise HTTPException(
            status_code=400,
            detail="Messages can only be sent to the candidate's phone number on file.",
        )

    try:
        result = await send_whatsapp_message(phone, payload.message)
        return {"success": True, "detail": result}
    except Exception as exc:
        logger.warning("WhatsApp send failed for candidate %s: %s", candidate.id, exc)
        raise HTTPException(status_code=500, detail="WhatsApp send failed")


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
def get_evaluation(candidate_id: CandidateId, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
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
    # Existence is checked inside the resumes folder only (resume_path used to be
    # client-writable, so os.path.exists answered for any server path).
    if candidate.resume_path and resolve_upload_file(candidate.resume_path, ["resumes"]):
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
                "ref": encode_ref("job", job.id),
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
        # Surface the evaluation pipeline's own state. Without this the UI can
        # only guess from the stub row's summary text, so a recruiter clicking
        # "Re-generate" saw a generic "couldn't be generated" and had no way to
        # tell a transient LLM timeout from a real, permanent failure.
        try:
            _meta = json.loads(latest_session.session_metadata or "{}")
        except Exception:
            _meta = {}

        session_info = {
            "id": latest_session.id,
            "started_at": (latest_session.started_at.isoformat() + "Z") if latest_session.started_at else None,
            "completed_at": (latest_session.completed_at.isoformat() + "Z") if latest_session.completed_at else None,
            "violation_count": latest_session.violation_count or 0,
            "status": latest_session.status.value,
            "evaluation_state": _meta.get("evaluation_state", "idle"),
            "evaluation_error": _meta.get("evaluation_error"),
            "evaluation_state_at": _meta.get("evaluation_state_at"),
        }

        messages = (
            db.query(InterviewMessage)
            .filter(InterviewMessage.session_id == latest_session.id)
            .order_by(InterviewMessage.created_at)
            .all()
        )

        # Full transcript for chat-style view. Nira's feedback on the previous
        # answer is its own bubble, ahead of the next question — the same way
        # the candidate heard it (feedback, pause, question).
        for msg in messages:
            timestamp = (msg.created_at.isoformat() + "Z") if msg.created_at else None
            content = msg.content
            if msg.role == "ai":
                feedback, content = feedback_and_question(msg)
                if feedback:
                    full_transcript.append({
                        "role": msg.role,
                        "content": feedback,
                        "question_index": None,
                        "timestamp": timestamp,
                    })
            full_transcript.append({
                "role": msg.role,
                "content": content,
                "question_index": msg.question_index,
                "timestamp": timestamp,
            })

        # Build Q&A pairs from CHRONOLOGICAL message order.
        # We cannot trust question_index from the AI model — it sometimes
        # returns the same index for multiple different questions, collapsing
        # them into one entry. Instead, pair each AI message (after the
        # intro) with the next candidate message in sequence.

        def _is_closing_message(text: str) -> bool:
            """Detect AI closing messages that are not real questions."""
            lower = text.lower()
            closing_phrases = [
                "that concludes our interview",
                "concludes our interview",
                "thank you for your time",
                "we'll be in touch",
                "we will be in touch",
                "interview is now complete",
            ]
            return any(p in lower for p in closing_phrases) and "?" not in text

        # Step 1: Build pairs from chronological order
        # Skip the first AI message (intro, qi=0) and any closing messages
        pairs: list = []
        current_ai_msg = None
        is_first_ai = True
        is_first_candidate = True

        for msg in messages:
            if msg.role == "ai":
                if is_first_ai:
                    is_first_ai = False
                    # First AI message is the intro — skip it
                    continue
                if _is_closing_message(msg.content):
                    continue
                # This is a new question from AI
                # If there's a pending AI question without a candidate answer,
                # save it as unanswered
                if current_ai_msg is not None:
                    pairs.append({"question": current_ai_msg, "answer": None})
                # Question only — Nira's feedback on the previous answer is
                # stored apart (or split off, for older interviews).
                current_ai_msg = question_text(msg)
            elif msg.role == "candidate":
                if is_first_candidate:
                    is_first_candidate = False
                    # First candidate message is self-intro — skip it
                    continue
                if current_ai_msg is not None:
                    pairs.append({"question": current_ai_msg, "answer": msg.content})
                    current_ai_msg = None
                # else: candidate message without a preceding AI question — ignore

        # Don't forget the last pending AI question
        if current_ai_msg is not None:
            pairs.append({"question": current_ai_msg, "answer": None})

        # Step 2: Fallback to question_index grouping if chronological
        # pairing produced fewer results (e.g., edge cases)
        if not pairs:
            qa_map: dict = {}
            for msg in messages:
                qi = msg.question_index
                if qi is None or qi == 0:
                    continue
                if qi not in qa_map:
                    qa_map[qi] = {"question": None, "answer": None}
                if msg.role == "ai" and qa_map[qi]["question"] is None:
                    qa_map[qi]["question"] = question_text(msg)
                elif msg.role == "candidate" and qa_map[qi]["answer"] is None:
                    qa_map[qi]["answer"] = msg.content
            pairs = [v for _, v in sorted(qa_map.items()) if v["question"]]

        transcript = []
        for seq_idx, pair in enumerate(pairs, start=1):
            transcript.append({
                "question": pair["question"],
                "answer": pair["answer"] or "[No answer provided — skipped]",
                "index": seq_idx,
            })

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
                    "url": sign_media_url(ss.file_path),
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
                "coding_score": getattr(ev, "coding_score", None),
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
            # Signed media URLs (app.media) — the caller was authorized for this
            # candidate above.
            "image_url": sign_media_url(candidate.image),
            "ref": encode_ref("candidate", candidate.id),
        },
        "job": job_info,
        "resume": {
            "path": candidate.resume_path,
            "url": resume_url,
            "signed_url": sign_media_url(candidate.resume_path),
            "download_url": sign_media_url(candidate.resume_path, download=True),
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
            "url": sign_media_url(candidate.recording_link),
            "download_url": sign_media_url(candidate.recording_link, download=True),
            "mp4_download_url": sign_media_url(candidate.recording_link, download=True, fmt="mp4"),
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
    dependencies=[Depends(per_user_limit("regenerate-evaluation", 10))],
    summary="Force-regenerate AI evaluation for a completed session",
)
def regenerate_evaluation(
    candidate_id: CandidateId,
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


def _build_evaluation_pdf_bytes(db: Session, candidate) -> bytes:
    """Full evaluation report PDF (candidate info + scores + AI evaluation +
    transcript) as bytes. Shared by the download-pdf endpoint and the
    interview-archive ZIP so both produce the same readable report.
    Raises ImportError if fpdf2 is not installed."""
    from io import BytesIO
    from fpdf import FPDF
    from app.models.interview import InterviewSession, InterviewMessage, SessionStatus, Evaluation

    candidate_id = candidate.id
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

    # Candidate Photo + Info (side by side)
    photo_rendered = False
    if candidate.image:
        # candidate.image is like "/uploads/photos/123.jpg" — resolved inside the
        # photos folder only (the column used to be client-writable and was
        # opened as any local path).
        photo_path = resolve_upload_file(candidate.image, ["photos"])
        if photo_path:
            try:
                # Place photo on the right, text on the left
                x_before = pdf.get_x()
                y_before = pdf.get_y()
                pdf.image(photo_path, x=155, y=y_before, w=35, h=43)
                pdf.set_xy(x_before, y_before)
                photo_rendered = True
            except Exception:
                pass

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

    # Transcript — use chronological message order (not question_index which
    # the AI model does not reliably increment, causing questions to collapse)
    if latest_session:
        messages = (
            db.query(InterviewMessage)
            .filter(InterviewMessage.session_id == latest_session.id)
            .order_by(InterviewMessage.created_at)
            .all()
        )

        def _is_closing(text: str) -> bool:
            lower = text.lower()
            phrases = ["that concludes our interview", "concludes our interview",
                       "thank you for your time", "we'll be in touch", "we will be in touch"]
            return any(p in lower for p in phrases) and "?" not in text

        pdf_pairs: list = []
        current_q = None
        is_first_ai = True
        is_first_candidate = True
        for msg in messages:
            if msg.role == "ai":
                if is_first_ai:
                    is_first_ai = False
                    continue
                if _is_closing(msg.content):
                    continue
                if current_q is not None:
                    pdf_pairs.append({"question": current_q, "answer": None})
                current_q = question_text(msg)
            elif msg.role == "candidate":
                if is_first_candidate:
                    is_first_candidate = False
                    continue
                if current_q is not None:
                    pdf_pairs.append({"question": current_q, "answer": msg.content})
                    current_q = None
        if current_q is not None:
            pdf_pairs.append({"question": current_q, "answer": None})

        if pdf_pairs:
            pdf.add_page()
            pdf.set_font("Helvetica", "B", 13)
            pdf.cell(0, 8, "Interview Transcript", new_x="LMARGIN", new_y="NEXT")
            pdf.ln(3)
            for seq_idx, pair in enumerate(pdf_pairs, start=1):
                pdf.set_font("Helvetica", "B", 10)
                pdf.multi_cell(0, 5, f"Q{seq_idx}: {pair['question']}")
                pdf.set_font("Helvetica", "", 10)
                answer_text = pair['answer'] or "[No answer provided - skipped]"
                pdf.multi_cell(0, 5, f"A: {answer_text}")
                pdf.ln(3)

    buf = BytesIO()
    pdf.output(buf)
    return buf.getvalue()


@router.get(
    "/{candidate_id}/evaluation/download-pdf",
    summary="Download evaluation report as PDF",
)
def download_evaluation_pdf(candidate_id: CandidateId, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Generate and return a PDF evaluation report for the candidate."""
    from io import BytesIO
    from fastapi.responses import StreamingResponse

    candidate = _get_candidate_with_org_check(db, candidate_id, user)
    try:
        pdf_bytes = _build_evaluation_pdf_bytes(db, candidate)
    except ImportError:
        raise HTTPException(status_code=500, detail="fpdf2 package not installed")
    safe_name = _download_stem(candidate.name)
    return StreamingResponse(
        BytesIO(pdf_bytes),
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{safe_name}_Evaluation.pdf"'},
    )


@router.get(
    "/{candidate_id}/evaluation/download-docx",
    summary="Download evaluation report as DOCX",
)
def download_evaluation_docx(candidate_id: CandidateId, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
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

    # Transcript — chronological pairing (same approach as PDF and evaluation API)
    if latest_session:
        messages = (
            db.query(InterviewMessage)
            .filter(InterviewMessage.session_id == latest_session.id)
            .order_by(InterviewMessage.created_at)
            .all()
        )

        def _is_closing_docx(text: str) -> bool:
            lower = text.lower()
            phrases = ["that concludes our interview", "concludes our interview",
                       "thank you for your time", "we'll be in touch", "we will be in touch"]
            return any(p in lower for p in phrases) and "?" not in text

        docx_pairs: list = []
        current_q = None
        is_first_ai = True
        is_first_candidate = True
        for msg in messages:
            if msg.role == "ai":
                if is_first_ai:
                    is_first_ai = False
                    continue
                if _is_closing_docx(msg.content):
                    continue
                if current_q is not None:
                    docx_pairs.append({"question": current_q, "answer": None})
                current_q = question_text(msg)
            elif msg.role == "candidate":
                if is_first_candidate:
                    is_first_candidate = False
                    continue
                if current_q is not None:
                    docx_pairs.append({"question": current_q, "answer": msg.content})
                    current_q = None
        if current_q is not None:
            docx_pairs.append({"question": current_q, "answer": None})

        if docx_pairs:
            doc.add_page_break()
            doc.add_heading("Interview Transcript", level=2)
            for seq_idx, pair in enumerate(docx_pairs, start=1):
                p = doc.add_paragraph()
                run = p.add_run(f"Q{seq_idx}: {pair['question']}")
                run.bold = True
                answer_text = pair['answer'] or "[No answer provided - skipped]"
                doc.add_paragraph(f"A: {answer_text}")
                doc.add_paragraph("")  # spacer

    buf = BytesIO()
    doc.save(buf)
    buf.seek(0)
    safe_name = _download_stem(candidate.name)
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={"Content-Disposition": f'attachment; filename="{safe_name}_Evaluation.docx"'},
    )


@router.get(
    "/{candidate_id}/evaluation/download-transcript",
    summary="Download interview transcript as text file",
)
def download_transcript(candidate_id: CandidateId, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
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
        if msg.feedback:
            lines.append(f"[{timestamp}] {role_label}:")
            lines.append(msg.feedback)
            lines.append("")
        lines.append(f"[{timestamp}] {role_label}:")
        lines.append(msg.content)
        lines.append("")

    content = "\n".join(lines)
    buf = BytesIO(content.encode("utf-8"))
    safe_name = _download_stem(candidate.name)
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


# ── Email Contact History ────────────────────────────────────────────────────

@router.get(
    "/{candidate_id}/email-history",
    summary="Get email contact history for a candidate",
    tags=["Candidates"],
)
def get_email_history(
    candidate_id: CandidateId,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    from app.models.candidate import EmailLog
    # Org check — this used to load any tenant's candidate by id.
    candidate = _get_candidate_with_org_check(db, candidate_id, user)
    logs = (
        db.query(EmailLog)
        .filter(EmailLog.candidate_id == candidate_id)
        .order_by(EmailLog.sent_at.desc())
        .all()
    )
    return {
        "candidate_name": candidate.name,
        "emails": [
            {
                "id": log.id,
                "subject": log.subject,
                "triggered_by": log.triggered_by,
                "trigger_event": log.trigger_event,
                "sent_at": log.sent_at.isoformat() if log.sent_at else None,
                "status": log.status,
            }
            for log in logs
        ],
    }


# ── Observer Link (admin actions) ────────────────────────────────────────────

@router.get(
    "/{candidate_id}/observer-link",
    summary="Get the live observer link for this candidate",
)
def get_observer_link(
    candidate_id: CandidateId,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Returns the latest active observer URL for this candidate, or null."""
    from app.models.observer import ObserverToken
    from app.services.observer_service import _build_observer_url

    candidate = _get_candidate_with_org_check(db, candidate_id, user)
    obs = (
        db.query(ObserverToken)
        .filter(
            ObserverToken.candidate_id == candidate.id,
            ObserverToken.revoked_at.is_(None),
        )
        .order_by(ObserverToken.created_at.desc())
        .first()
    )
    if obs is None:
        return {"link": None}
    return {
        "link": _build_observer_url(obs.token),
        "token": obs.token,
        "expires_at": obs.expires_at.isoformat() if obs.expires_at else None,
        "view_count": obs.view_count or 0,
        "created_by_email": obs.created_by_email,
    }


@router.post(
    "/{candidate_id}/observer-link/revoke",
    summary="Revoke the observer link for this candidate",
)
def revoke_observer_link(
    candidate_id: CandidateId,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    from app.models.observer import ObserverToken

    candidate = _get_candidate_with_org_check(db, candidate_id, user)
    obs = (
        db.query(ObserverToken)
        .filter(
            ObserverToken.candidate_id == candidate.id,
            ObserverToken.revoked_at.is_(None),
        )
        .order_by(ObserverToken.created_at.desc())
        .first()
    )
    if obs is None:
        raise HTTPException(status_code=404, detail="No active observer link to revoke")
    obs.revoked_at = datetime.utcnow()
    db.commit()
    return {"ok": True, "revoked_at": obs.revoked_at.isoformat()}


@router.post(
    "/{candidate_id}/observer-link/resend",
    dependencies=[Depends(per_user_limit("observer-link-resend", 20))],
    summary="Email the observer link to the current user",
)
async def resend_observer_link(
    candidate_id: CandidateId,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    from app.services.observer_service import (
        get_or_create_observer_token,
        send_observer_link_email,
        _build_observer_url,
    )
    from app.models.job import Job as _JobModel

    candidate = _get_candidate_with_org_check(db, candidate_id, user)

    _exp = None
    if candidate.interview_date:
        from datetime import timedelta as _td
        _exp = candidate.interview_date + _td(days=2)

    obs = get_or_create_observer_token(
        db,
        candidate=candidate,
        creator_user_id=user.id,
        creator_email=user.email,
        expires_at=_exp,
    )
    job = db.query(_JobModel).filter(_JobModel.id == candidate.job_id).first()
    sent = await send_observer_link_email(
        db,
        candidate=candidate,
        creator_email=user.email,
        observer_token=obs,
        job_title=(job.title if job else "Interview"),
        is_reschedule=False,
    )
    return {
        "ok": True,
        "email_sent": sent,
        "link": _build_observer_url(obs.token),
    }
