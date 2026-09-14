"""
Resume Database — bulk upload, search, and manage the central resume store.

This router is for the admin-facing "Resume Database" page. Resumes are
uploaded here independently of any job opening. Supports two parsing modes:
- "fast": Regex-based parser (no AI calls, < 1s per resume, ~85% accuracy)
- "ai":   Claude AI parser (LLM call per resume, ~3s each, ~99% accuracy)
"""

import asyncio
import concurrent.futures
import csv
import io
import json
import logging
import os
import shutil
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import StreamingResponse
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app.auth import get_current_user, get_org_id, require_roles
from app.authz import can_access_org
from app.database import get_db, SessionLocal
from app.media import UPLOAD_ROOT, resume_file_in_use, safe_unlink_upload, safe_upload_name, sign_media_url
from app.models.organization import Organization
from app.models.resume_metadata import ResumeMetadata
from app.models.user import User, UserRole
from app.schemas.resume_metadata import ResumeMetadataUpdate

logger = logging.getLogger(__name__)

router = APIRouter(dependencies=[Depends(get_current_user)])


def _check_resume_db_permission(user: User, db: Session) -> None:
    """Raise 403 if user's org doesn't have resume database access. Super admins always pass."""
    if user.role == UserRole.super_admin:
        return
    if not user.organization_id:
        raise HTTPException(status_code=403, detail="You do not have permission to perform this action. Please contact your admin.")
    org = db.query(Organization).filter(Organization.id == user.organization_id).first()
    if not org or not org.allow_resume_database:
        raise HTTPException(status_code=403, detail="You do not have permission to perform this action. Please contact your admin.")

# Resume-database writes and destructive actions: admins and super admins only
# (the page is admin-facing); destructive ones also call _check_resume_db_permission.
_require_resume_admin = require_roles(UserRole.admin, UserRole.super_admin)

RESUME_UPLOAD_DIR = f"{UPLOAD_ROOT}/resumes"
os.makedirs(RESUME_UPLOAD_DIR, exist_ok=True)

# Accepted upload content types -> extension used when the client file name
# carries none of the allowed ones.
ALLOWED_RESUME_TYPES = {
    "application/pdf": ".pdf",
    "application/msword": ".doc",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
}
RESUME_EXTENSIONS = {".pdf", ".doc", ".docx"}

# batch_id -> organization_id of the uploader (None for a global super admin)
_bulk_owner: dict = {}


def _stored_resume_name(filename: Optional[str], default_ext: Optional[str] = None) -> Optional[str]:
    """Server-side name for an uploaded resume: timestamp + sanitized stem + an
    allowlisted extension. None when the extension is not allowed."""
    safe = safe_upload_name(filename, RESUME_EXTENSIONS, default_ext=default_ext)
    if safe is None:
        return None
    stem, ext = safe
    return f"{datetime.utcnow().strftime('%Y%m%d%H%M%S%f')}_{stem}{ext}"


def _remove_resume_file_if_unused(db: Session, stored_path: Optional[str]) -> bool:
    """Delete a resume file once no Candidate / ResumeMetadata row references it.
    Only files inside the resumes upload folder are ever touched; never raises."""
    if not stored_path:
        return False
    try:
        if resume_file_in_use(db, stored_path):
            return False
    except Exception as exc:
        logger.warning("Resume file reference check failed for %s: %s", stored_path, exc)
        return False
    return safe_unlink_upload(stored_path, ("resumes",))


def _remove_unused_resume_files(paths: List[str]) -> None:
    """Background task: remove the files of resume rows that were just deleted,
    skipping any file another row still references."""
    unique = {p for p in paths if p}
    if not unique:
        return
    db = SessionLocal()
    try:
        for path in unique:
            _remove_resume_file_if_unused(db, path)
    finally:
        db.close()


def _parse_failure_message(exc: Exception) -> str:
    """parsing_error shown to users. The text extractors raise readable
    RuntimeError/ValueError messages; anything else is logged, not echoed."""
    if isinstance(exc, (RuntimeError, ValueError)):
        return str(exc)[:500]
    return "Resume could not be parsed due to an internal error."

# ── Background processing queue ──────────────────────────────────────────────
_bulk_queue: asyncio.Queue = asyncio.Queue()
_bulk_worker_started = False
_bulk_progress: dict = {}  # batch_id -> {total, completed, failed, status, parse_mode}

_NUM_WORKERS = 4
_executor = concurrent.futures.ThreadPoolExecutor(max_workers=_NUM_WORKERS)
# Limit concurrent AI API calls to avoid rate-limiting (Gemini free tier = ~15 RPM)
_ai_semaphore = asyncio.Semaphore(1)


def _update_batch_progress(batch_id: str, field: str):
    """Increment a batch progress field and mark completed if done."""
    if not batch_id or batch_id not in _bulk_progress:
        return
    _bulk_progress[batch_id][field] += 1
    p = _bulk_progress[batch_id]
    if (p["completed"] + p["failed"]) >= p["total"]:
        p["status"] = "completed"


def _parse_single_resume(meta_id: int, file_path: str, batch_id: str):
    """
    Parse a single resume using fast regex parser.
    Runs in a THREAD POOL so it doesn't block the event loop.
    """
    from app.services.resume_service import extract_text
    from app.services.fast_resume_parser import fast_parse_resume

    db = SessionLocal()
    try:
        meta = db.query(ResumeMetadata).filter(ResumeMetadata.id == meta_id).first()
        if not meta:
            _update_batch_progress(batch_id, "failed")
            return

        meta.parsing_status = "processing"
        db.commit()

        raw_text = extract_text(file_path)
        if not raw_text or len(raw_text.strip()) < 20:
            meta.parsing_status = "failed"
            meta.parsing_error = "Could not extract meaningful text from file"
            db.commit()
            _update_batch_progress(batch_id, "failed")
            return

        parsed = fast_parse_resume(raw_text)
        parsed_email = parsed.get("candidate_email")

        # Deduplication (org-scoped: never merges across organizations; rows
        # without an organization only dedupe against other org-less rows)
        target = meta
        replaced_file = None
        if parsed_email and meta.candidate_email != parsed_email:
            dedup_query = db.query(ResumeMetadata).filter(
                ResumeMetadata.candidate_email == parsed_email,
                ResumeMetadata.id != meta.id,
            )
            if meta.organization_id is not None:
                dedup_query = dedup_query.filter(ResumeMetadata.organization_id == meta.organization_id)
            else:
                dedup_query = dedup_query.filter(ResumeMetadata.organization_id.is_(None))
            existing = dedup_query.first()
            if existing:
                target = existing
                if existing.resume_file_path and existing.resume_file_path != file_path:
                    replaced_file = existing.resume_file_path
                db.delete(meta)
                db.flush()
            else:
                meta.candidate_email = parsed_email

        target.candidate_name = parsed.get("candidate_name") or target.candidate_name
        target.phone = parsed.get("phone")
        target.current_designation = parsed.get("current_designation")
        target.experience_years = parsed.get("experience_years")
        target.skills = json.dumps(parsed.get("skills", []))
        target.education = json.dumps(parsed.get("education", []))
        target.certifications = json.dumps(parsed.get("certifications", []))
        target.work_experience = json.dumps(parsed.get("work_experience", []))
        target.location = parsed.get("location")
        target.personal_details = json.dumps(parsed.get("personal_details") or {})
        target.keywords = json.dumps(parsed.get("keywords", []))
        target.summary = parsed.get("summary")
        target.raw_text = raw_text
        target.resume_file_path = file_path
        target.parsing_status = "completed"
        target.parsed_at = datetime.utcnow()
        db.commit()

        # The merged record now points at the new upload: drop its old file
        # unless another row (e.g. a hired candidate) still uses it.
        if replaced_file:
            _remove_resume_file_if_unused(db, replaced_file)

        _update_batch_progress(batch_id, "completed")

    except Exception as exc:
        logger.error("Bulk parse failed for meta %s: %s", meta_id, exc, exc_info=True)
        try:
            meta = db.query(ResumeMetadata).filter(ResumeMetadata.id == meta_id).first()
            if meta:
                meta.parsing_status = "failed"
                meta.parsing_error = _parse_failure_message(exc)
                db.commit()
        except Exception:
            pass
        _update_batch_progress(batch_id, "failed")
    finally:
        db.close()


async def _ai_parse_single_resume(meta_id: int, file_path: str, batch_id: str):
    """
    Parse a single resume using Claude AI — high accuracy.
    Runs as an async coroutine (AI API call is async).
    """
    from app.services.resume_service import extract_text, parse_resume
    from app.schemas.ai import ParseResumeRequest

    db = SessionLocal()
    try:
        meta = db.query(ResumeMetadata).filter(ResumeMetadata.id == meta_id).first()
        if not meta:
            _update_batch_progress(batch_id, "failed")
            return

        meta.parsing_status = "processing"
        db.commit()

        # Extract text in thread (sync I/O)
        raw_text = await asyncio.to_thread(extract_text, file_path)
        if not raw_text or len(raw_text.strip()) < 20:
            meta.parsing_status = "failed"
            meta.parsing_error = "Could not extract meaningful text from file"
            db.commit()
            _update_batch_progress(batch_id, "failed")
            return

        # AI parse (async LLM call)
        req = ParseResumeRequest(resume_text=raw_text)
        result = await parse_resume(req)
        data = result.parsed_data.model_dump()

        parsed_email = data.get("email")

        # Deduplication (org-scoped: never merges across organizations; rows
        # without an organization only dedupe against other org-less rows)
        target = meta
        replaced_file = None
        if parsed_email and meta.candidate_email != parsed_email:
            dedup_query = db.query(ResumeMetadata).filter(
                ResumeMetadata.candidate_email == parsed_email,
                ResumeMetadata.id != meta.id,
            )
            if meta.organization_id is not None:
                dedup_query = dedup_query.filter(ResumeMetadata.organization_id == meta.organization_id)
            else:
                dedup_query = dedup_query.filter(ResumeMetadata.organization_id.is_(None))
            existing = dedup_query.first()
            if existing:
                target = existing
                if existing.resume_file_path and existing.resume_file_path != file_path:
                    replaced_file = existing.resume_file_path
                db.delete(meta)
                db.flush()
            else:
                meta.candidate_email = parsed_email

        target.candidate_name = data.get("name") or target.candidate_name
        target.phone = data.get("phone")
        target.current_designation = data.get("current_designation")
        target.experience_years = data.get("experience_years")
        target.skills = json.dumps(data.get("skills") or [])
        target.education = json.dumps(data.get("education") or [])
        target.certifications = json.dumps(data.get("certifications") or [])
        target.work_experience = json.dumps(data.get("work_experience") or [])
        target.location = data.get("location")
        target.personal_details = json.dumps(data.get("personal_details") or {})
        target.keywords = json.dumps(data.get("keywords") or [])
        target.summary = data.get("summary")
        target.raw_text = raw_text
        target.resume_file_path = file_path
        target.parsing_status = "completed"
        target.parsed_at = datetime.utcnow()
        db.commit()

        # The merged record now points at the new upload: drop its old file
        # unless another row (e.g. a hired candidate) still uses it.
        if replaced_file:
            _remove_resume_file_if_unused(db, replaced_file)

        _update_batch_progress(batch_id, "completed")

        # Small delay between AI calls to respect rate limits
        await asyncio.sleep(1)

    except Exception as exc:
        logger.error("AI parse failed for meta %s: %s", meta_id, exc, exc_info=True)
        try:
            meta = db.query(ResumeMetadata).filter(ResumeMetadata.id == meta_id).first()
            if meta:
                meta.parsing_status = "failed"
                meta.parsing_error = _parse_failure_message(exc)
                db.commit()
        except Exception:
            pass
        _update_batch_progress(batch_id, "failed")
    finally:
        db.close()


async def _bulk_parse_worker():
    """
    Process bulk resume parse jobs from the queue.
    Dispatches to regex (thread pool) or AI (async) based on parse_mode.
    """
    loop = asyncio.get_event_loop()

    while True:
        item = await _bulk_queue.get()
        meta_id, file_path, batch_id = item[0], item[1], item[2]
        parse_mode = item[3] if len(item) > 3 else "fast"

        try:
            if parse_mode == "ai":
                # Serialize AI calls to avoid rate-limiting
                async with _ai_semaphore:
                    await _ai_parse_single_resume(meta_id, file_path, batch_id)
            else:
                await loop.run_in_executor(
                    _executor, _parse_single_resume, meta_id, file_path, batch_id
                )
        except Exception as exc:
            logger.error("Worker error for meta %s: %s", meta_id, exc)
        finally:
            _bulk_queue.task_done()


def _ensure_bulk_worker():
    """Start background bulk parse workers if not already running."""
    global _bulk_worker_started
    if not _bulk_worker_started:
        _bulk_worker_started = True
        loop = asyncio.get_event_loop()
        for _ in range(_NUM_WORKERS):
            loop.create_task(_bulk_parse_worker())


# ── Bulk Upload ──────────────────────────────────────────────────────────────

@router.get("/permissions")
def get_resume_db_permissions(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Check if the current user has resume database write access."""
    if user.role == UserRole.super_admin:
        return {"allowed": True}
    if user.role != UserRole.admin or not user.organization_id:
        return {"allowed": False}
    org = db.query(Organization).filter(Organization.id == user.organization_id).first()
    return {"allowed": bool(org and org.allow_resume_database)}


@router.post(
    "/upload",
    summary="Upload resumes to the central database (bulk)",
    description=(
        "Upload one or more resume files (PDF/DOC/DOCX). Files are saved and "
        "queued for fast regex-based parsing (no AI). Each resume takes < 1 second "
        "to parse. Returns a batch_id to track progress.\n\n"
        "Duplicate detection: if a file yields an email that already exists in "
        "the database, the existing record is updated with the new resume."
    ),
)
async def upload_resumes(
    request: Request,
    files: List[UploadFile] = File(...),
    parse_mode: str = Query("fast", description="'fast' (regex) or 'ai' (Claude AI)"),
    db: Session = Depends(get_db),
    user: User = Depends(_require_resume_admin),
):
    _check_resume_db_permission(user, db)
    _ensure_bulk_worker()

    if parse_mode not in ("fast", "ai"):
        parse_mode = "fast"

    if len(files) > 100:
        raise HTTPException(
            status_code=400,
            detail="Maximum 100 files per upload batch. Upload in smaller batches."
        )

    import uuid
    batch_id = uuid.uuid4().hex[:12]
    _bulk_progress[batch_id] = {
        "total": len(files),
        "completed": 0,
        "failed": 0,
        "status": "processing",
        "parse_mode": parse_mode,
    }
    _bulk_owner[batch_id] = user.organization_id

    created_ids = []

    for file in files:
        if file.content_type not in ALLOWED_RESUME_TYPES:
            _bulk_progress[batch_id]["total"] -= 1
            _bulk_progress[batch_id]["failed"] += 1
            continue

        # Save file to disk under a sanitized, server-chosen name
        safe_name = _stored_resume_name(file.filename, default_ext=ALLOWED_RESUME_TYPES[file.content_type])
        file_path = os.path.join(RESUME_UPLOAD_DIR, safe_name)
        with open(file_path, "wb") as f:
            shutil.copyfileobj(file.file, f)

        # Create a pending metadata record
        placeholder_email = f"noemail_{safe_name[:30]}@pending.local"
        meta = ResumeMetadata(
            candidate_email=placeholder_email,
            candidate_name=Path(file.filename).stem.replace("_", " ").replace("-", " ").title(),
            resume_file_path=file_path,
            parsing_status="pending",
            organization_id=user.organization_id,
        )
        db.add(meta)
        db.commit()
        db.refresh(meta)
        created_ids.append(meta.id)

        # Queue for background parsing
        await _bulk_queue.put((meta.id, file_path, batch_id, parse_mode))

    return {
        "batch_id": batch_id,
        "queued": len(created_ids),
        "total_files": len(files),
        "message": f"Queued {len(created_ids)} resume(s) for parsing. Use GET /resume-database/batch/{batch_id} to track progress.",
    }


# ── Batch Progress ───────────────────────────────────────────────────────────

@router.get(
    "/batch/{batch_id}",
    summary="Check bulk upload batch progress",
)
def get_batch_progress(batch_id: str, user: User = Depends(get_current_user)):
    # Only the uploader's organization (or a global super admin) may poll a batch
    if batch_id not in _bulk_progress or not can_access_org(user, _bulk_owner.get(batch_id)):
        raise HTTPException(status_code=404, detail="Batch not found")
    return _bulk_progress[batch_id]


# ── Failed Resumes ──────────────────────────────────────────────────────────

@router.get(
    "/failed",
    summary="List all failed resume records",
)
def list_failed_resumes(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Return all resumes that failed parsing, with error details."""
    org_id = get_org_id(user)
    query = db.query(ResumeMetadata).filter(ResumeMetadata.parsing_status == "failed")
    if org_id is not None:
        query = query.filter(ResumeMetadata.organization_id == org_id)
    failed = query.order_by(ResumeMetadata.created_at.desc()).all()

    return {
        "count": len(failed),
        "items": [
            {
                "id": m.id,
                "candidate_name": m.candidate_name,
                "candidate_email": m.candidate_email,
                "resume_file_path": m.resume_file_path,
                "resume_file_url": sign_media_url(m.resume_file_path),
                "parsing_error": m.parsing_error,
                "created_at": m.created_at.isoformat() if m.created_at else None,
            }
            for m in failed
        ],
    }


@router.get(
    "/failed/download",
    summary="Download CSV of failed resume records",
)
def download_failed_resumes(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Download a CSV file listing all resumes that failed parsing."""
    org_id = get_org_id(user)
    query = db.query(ResumeMetadata).filter(ResumeMetadata.parsing_status == "failed")
    if org_id is not None:
        query = query.filter(ResumeMetadata.organization_id == org_id)
    failed = query.order_by(ResumeMetadata.created_at.desc()).all()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["ID", "Filename", "Name", "Email", "Error", "Created At"])

    for m in failed:
        filename = os.path.basename(m.resume_file_path) if m.resume_file_path else ""
        writer.writerow([
            m.id,
            filename,
            m.candidate_name or "",
            m.candidate_email or "",
            m.parsing_error or "Unknown error",
            m.created_at.isoformat() if m.created_at else "",
        ])

    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=failed_resumes.csv"},
    )


# ── List / Search Resumes ────────────────────────────────────────────────────

@router.get(
    "/",
    summary="List and search resume metadata",
    description=(
        "Paginated list of all resume metadata records. Supports:\n"
        "- `search`: full-text search across name, email, skills, designation, location\n"
        "- `skills`: comma-separated skill filter\n"
        "- `min_experience` / `max_experience`: experience range filter\n"
        "- `location`: location filter\n"
        "- `status`: parsing status filter (completed/pending/failed)\n"
        "- `page` / `page_size`: pagination (default 1, 50)"
    ),
)
def list_resumes(
    search: Optional[str] = None,
    skills: Optional[str] = None,
    min_experience: Optional[float] = None,
    max_experience: Optional[float] = None,
    location: Optional[str] = None,
    status: Optional[str] = Query(None, description="completed|pending|processing|failed"),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    query = db.query(ResumeMetadata)

    # Organization scoping
    org_id = get_org_id(user)
    if org_id is not None:
        query = query.filter(ResumeMetadata.organization_id == org_id)

    # Exclude stale placeholder records (never parsed)
    query = query.filter(
        ~ResumeMetadata.candidate_email.like("%@pending.local")
    )

    # Filter by parsing status
    if status and status != "all":
        query = query.filter(ResumeMetadata.parsing_status == status)
    elif not status:
        # By default show only completed + processing
        query = query.filter(
            ResumeMetadata.parsing_status.in_(["completed", "processing"])
        )
    # status == "all" → no filter

    # Full-text search
    if search:
        search_term = f"%{search}%"
        query = query.filter(
            or_(
                ResumeMetadata.candidate_name.ilike(search_term),
                ResumeMetadata.candidate_email.ilike(search_term),
                ResumeMetadata.skills.ilike(search_term),
                ResumeMetadata.current_designation.ilike(search_term),
                ResumeMetadata.location.ilike(search_term),
                ResumeMetadata.keywords.ilike(search_term),
            )
        )

    # Skill filter
    if skills:
        for skill in skills.split(","):
            skill = skill.strip()
            if skill:
                query = query.filter(ResumeMetadata.skills.ilike(f"%{skill}%"))

    # Experience filter
    if min_experience is not None:
        query = query.filter(ResumeMetadata.experience_years >= min_experience)
    if max_experience is not None:
        query = query.filter(ResumeMetadata.experience_years <= max_experience)

    # Location filter
    if location:
        query = query.filter(ResumeMetadata.location.ilike(f"%{location}%"))

    total = query.count()
    items = (
        query
        .order_by(ResumeMetadata.created_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )

    return {
        "items": [_serialize_meta(m) for m in items],
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": (total + page_size - 1) // page_size,
    }


# ── Stats ────────────────────────────────────────────────────────────────────

@router.get(
    "/stats",
    summary="Get resume database statistics",
)
def get_stats(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    org_id = get_org_id(user)

    # Base filter: exclude stale placeholder records
    base_filters = [~ResumeMetadata.candidate_email.like("%@pending.local")]
    if org_id is not None:
        base_filters.append(ResumeMetadata.organization_id == org_id)

    total = db.query(func.count(ResumeMetadata.id)).filter(*base_filters).scalar() or 0
    completed = db.query(func.count(ResumeMetadata.id)).filter(
        *base_filters, ResumeMetadata.parsing_status == "completed",
    ).scalar() or 0
    pending = db.query(func.count(ResumeMetadata.id)).filter(
        *base_filters, ResumeMetadata.parsing_status.in_(["pending", "processing"]),
    ).scalar() or 0
    failed = db.query(func.count(ResumeMetadata.id)).filter(
        *base_filters, ResumeMetadata.parsing_status == "failed",
    ).scalar() or 0

    return {
        "total": total,
        "completed": completed,
        "pending": pending,
        "failed": failed,
    }


# ── Single Resume Detail ─────────────────────────────────────────────────────

@router.get(
    "/{meta_id}",
    summary="Get single resume metadata detail",
)
def get_resume_detail(meta_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    org_id = get_org_id(user)
    query = db.query(ResumeMetadata).filter(ResumeMetadata.id == meta_id)
    if org_id is not None:
        query = query.filter(ResumeMetadata.organization_id == org_id)
    meta = query.first()
    if not meta:
        raise HTTPException(status_code=404, detail="Resume not found")
    return _serialize_meta(meta, include_text=True)


# ── Update Resume Metadata ────────────────────────────────────────────────

@router.patch(
    "/{meta_id}",
    summary="Update resume metadata fields",
    description="Update editable fields on a resume metadata record. Only provided (non-null) fields are updated.",
)
def update_resume_metadata(
    meta_id: int,
    body: ResumeMetadataUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(_require_resume_admin),
):
    org_id = get_org_id(user)
    query = db.query(ResumeMetadata).filter(ResumeMetadata.id == meta_id)
    if org_id is not None:
        query = query.filter(ResumeMetadata.organization_id == org_id)
    meta = query.first()
    if not meta:
        raise HTTPException(status_code=404, detail="Resume not found")

    update_data = body.model_dump(exclude_none=True)

    # skills is stored as a JSON string in the DB
    if "skills" in update_data:
        update_data["skills"] = json.dumps(update_data["skills"])

    for field, value in update_data.items():
        setattr(meta, field, value)

    meta.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(meta)

    return _serialize_meta(meta)


# ── Clear All ────────────────────────────────────────────────────────────────

@router.delete(
    "/clear-all",
    summary="Delete ALL resume metadata records and files",
    status_code=200,
)
def clear_all_resumes(
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    user: User = Depends(_require_resume_admin),
):
    """Wipe the resume database for the current organization (or all for a global super admin)."""
    _check_resume_db_permission(user, db)
    from app.models.candidate import Candidate

    org_id = get_org_id(user)

    # Scope to organization
    resume_query = db.query(ResumeMetadata)
    if org_id is not None:
        resume_query = resume_query.filter(ResumeMetadata.organization_id == org_id)

    all_resumes = resume_query.all()
    resume_ids = [m.id for m in all_resumes]
    count = len(all_resumes)

    # Unlink candidates referencing these resumes
    if resume_ids:
        db.query(Candidate).filter(Candidate.resume_metadata_id.in_(resume_ids)).update(
            {"resume_metadata_id": None}, synchronize_session="fetch"
        )

    file_paths = [m.resume_file_path for m in all_resumes if m.resume_file_path]
    for meta in all_resumes:
        db.delete(meta)

    db.commit()

    # Remove only the files of the rows deleted above, and only those no other
    # row (e.g. a candidate) still references. The resumes folder also holds
    # candidate uploads and other tenants' files, so it is never wiped wholesale.
    # Runs after the response: the reference check is a query per file.
    background_tasks.add_task(_remove_unused_resume_files, file_paths)

    return {"deleted": count, "message": f"Deleted all {count} resume records and files."}


# ── Delete Single ────────────────────────────────────────────────────────────

@router.delete(
    "/{meta_id}",
    summary="Delete a resume metadata record",
    status_code=204,
)
def delete_resume(meta_id: int, db: Session = Depends(get_db), user: User = Depends(_require_resume_admin)):
    from app.models.candidate import Candidate

    _check_resume_db_permission(user, db)
    org_id = get_org_id(user)
    query = db.query(ResumeMetadata).filter(ResumeMetadata.id == meta_id)
    if org_id is not None:
        query = query.filter(ResumeMetadata.organization_id == org_id)
    meta = query.first()
    if not meta:
        raise HTTPException(status_code=404, detail="Resume not found")

    # Unlink any candidates referencing this metadata
    db.query(Candidate).filter(Candidate.resume_metadata_id == meta_id).update(
        {"resume_metadata_id": None}, synchronize_session="fetch"
    )

    file_path = meta.resume_file_path
    db.delete(meta)
    db.commit()

    # Delete the file from disk unless another row still references it
    _remove_resume_file_if_unused(db, file_path)


# ── Cleanup Duplicates ──────────────────────────────────────────────────────

@router.post(
    "/cleanup",
    summary="Remove stale placeholder records and deduplicate",
    description=(
        "Removes placeholder records (`@pending.local`) that failed or never parsed. "
        "Returns count of deleted records."
    ),
)
def cleanup_stale_records(
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    user: User = Depends(_require_resume_admin),
):
    """Remove placeholder records that were never parsed or failed."""
    from app.models.candidate import Candidate

    _check_resume_db_permission(user, db)
    org_id = get_org_id(user)

    # Delete placeholder records that are still pending/failed (never parsed)
    query = db.query(ResumeMetadata).filter(
        ResumeMetadata.candidate_email.like("%@pending.local"),
        ResumeMetadata.parsing_status.in_(["pending", "failed"]),
    )
    if org_id is not None:
        query = query.filter(ResumeMetadata.organization_id == org_id)
    stale = query.all()

    deleted_count = 0
    stale_ids = [m.id for m in stale]

    # Unlink any candidates referencing these metadata records
    if stale_ids:
        db.query(Candidate).filter(Candidate.resume_metadata_id.in_(stale_ids)).update(
            {"resume_metadata_id": None}, synchronize_session="fetch"
        )

    file_paths = [m.resume_file_path for m in stale if m.resume_file_path]
    for meta in stale:
        db.delete(meta)
        deleted_count += 1

    db.commit()
    # Delete their files (unless still referenced elsewhere) after the response
    background_tasks.add_task(_remove_unused_resume_files, file_paths)
    return {"deleted": deleted_count, "message": f"Removed {deleted_count} stale placeholder record(s)."}


# ── Re-parse single resume (hybrid: regex + LLM correction) ──────────────────

@router.post(
    "/{meta_id}/reparse",
    summary="Re-parse a single resume with improved regex + LLM correction",
)
async def reparse_single_resume(meta_id: int, use_llm: bool = True, db: Session = Depends(get_db), user: User = Depends(_require_resume_admin)):
    from app.services.fast_resume_parser import fast_parse_resume
    from app.services.llm_resume_parser import llm_parse_resume, needs_llm_correction

    _check_resume_db_permission(user, db)
    org_id = get_org_id(user)
    query = db.query(ResumeMetadata).filter(ResumeMetadata.id == meta_id)
    if org_id is not None:
        query = query.filter(ResumeMetadata.organization_id == org_id)
    meta = query.first()
    if not meta:
        raise HTTPException(status_code=404, detail="Resume not found")
    if not meta.raw_text:
        raise HTTPException(status_code=400, detail="No raw text available for re-parsing")

    try:
        # Step 1: Fast regex parse
        result = fast_parse_resume(meta.raw_text)

        # Step 2: LLM correction if regex output looks bad
        llm_used = False
        if use_llm and needs_llm_correction(result):
            try:
                llm_result = await llm_parse_resume(meta.raw_text, result)
                if llm_result:
                    # Merge LLM corrections into regex result
                    for key in ["candidate_name", "current_designation", "experience_years",
                                "location", "skills", "education", "certifications"]:
                        if llm_result.get(key):
                            result[key] = llm_result[key]
                    llm_used = True
            except Exception as e:
                logger.warning("LLM correction failed for %s: %s", meta_id, e)

        # Step 3: Apply to DB
        if result.get("candidate_name"):
            meta.candidate_name = result["candidate_name"]
        if result.get("skills"):
            meta.skills = json.dumps(result["skills"] if isinstance(result["skills"], list) else result["skills"])
        if result.get("education"):
            meta.education = json.dumps(result["education"] if isinstance(result["education"], list) else result["education"])
        if result.get("certifications"):
            meta.certifications = json.dumps(result["certifications"] if isinstance(result["certifications"], list) else result["certifications"])
        if result.get("work_experience"):
            meta.work_experience = json.dumps(result["work_experience"])
        if result.get("keywords"):
            meta.keywords = json.dumps(result["keywords"] if isinstance(result["keywords"], list) else result["keywords"])
        if result.get("phone"):
            meta.phone = result["phone"]
        if result.get("location"):
            meta.location = result["location"]
        if result.get("current_designation"):
            meta.current_designation = result["current_designation"]
        if result.get("experience_years"):
            meta.experience_years = result["experience_years"]
        if result.get("personal_details"):
            meta.personal_details = json.dumps(result["personal_details"])
        if result.get("summary"):
            meta.summary = result["summary"]

        meta.updated_at = datetime.utcnow()
        db.commit()
        db.refresh(meta)

        serialized = _serialize_meta(meta)
        serialized["llm_used"] = llm_used
        return serialized
    except Exception as e:
        logger.error("Re-parse failed for %s: %s", meta_id, e, exc_info=True)
        raise HTTPException(status_code=500, detail="Re-parse failed. Please try again.")


# ── Bulk re-parse with LLM correction (for bad entries) ──────────────────────

@router.post(
    "/reparse-bad",
    summary="Re-parse resumes that have bad data (auto-detected) using regex + LLM",
)
async def reparse_bad_resumes(db: Session = Depends(get_db), user: User = Depends(_require_resume_admin)):
    """Find resumes with garbage data and re-parse them with LLM correction."""
    from app.services.fast_resume_parser import fast_parse_resume
    from app.services.llm_resume_parser import llm_parse_resume, needs_llm_correction

    _check_resume_db_permission(user, db)
    org_id = get_org_id(user)
    query = db.query(ResumeMetadata).filter(
        ResumeMetadata.parsing_status == "completed",
        ResumeMetadata.raw_text.isnot(None),
    )
    if org_id is not None:
        query = query.filter(ResumeMetadata.organization_id == org_id)
    metas = query.all()

    needs_fix = []
    for meta in metas:
        data = {
            "candidate_name": meta.candidate_name,
            "current_designation": meta.current_designation,
            "experience_years": meta.experience_years,
            "location": meta.location,
            "skills": json.loads(meta.skills) if meta.skills else [],
        }
        if needs_llm_correction(data):
            needs_fix.append(meta)

    fixed = 0
    errors = 0
    for meta in needs_fix:
        try:
            regex_result = fast_parse_resume(meta.raw_text)
            llm_result = await llm_parse_resume(meta.raw_text, regex_result)

            if llm_result:
                for key in ["candidate_name", "current_designation", "experience_years", "location"]:
                    if llm_result.get(key):
                        setattr(meta, key, llm_result[key])
                if llm_result.get("skills"):
                    meta.skills = json.dumps(llm_result["skills"])
                if llm_result.get("education"):
                    meta.education = json.dumps(llm_result["education"])
                if llm_result.get("certifications"):
                    meta.certifications = json.dumps(llm_result["certifications"])
            else:
                # At least apply improved regex
                if regex_result.get("candidate_name"):
                    meta.candidate_name = regex_result["candidate_name"]
                if regex_result.get("skills"):
                    meta.skills = json.dumps(regex_result["skills"])
                if regex_result.get("current_designation"):
                    meta.current_designation = regex_result["current_designation"]

            meta.updated_at = datetime.utcnow()
            fixed += 1
        except Exception as e:
            logger.warning("Fix failed for %s: %s", meta.id, e)
            errors += 1

    db.commit()
    return {
        "total_checked": len(metas),
        "needed_fix": len(needs_fix),
        "fixed": fixed,
        "errors": errors,
    }


# ── Re-parse all resumes with improved parser ────────────────────────────────

@router.post(
    "/reparse-all",
    summary="Re-parse all completed resumes with the improved parser",
    description=(
        "Re-runs the fast regex parser on all resumes that have raw_text stored. "
        "Updates skills, education, certifications, work experience, and other metadata. "
        "Does NOT change candidate_email or parsing_status. Useful after parser improvements."
    ),
)
def reparse_all_resumes(db: Session = Depends(get_db), user: User = Depends(_require_resume_admin)):
    """Re-parse all completed resumes using the improved fast parser."""
    from app.services.fast_resume_parser import fast_parse_resume, _is_valid_name
    import json
    from datetime import datetime

    _check_resume_db_permission(user, db)
    org_id = get_org_id(user)
    query = db.query(ResumeMetadata).filter(
        ResumeMetadata.parsing_status == "completed",
        ResumeMetadata.raw_text.isnot(None),
    )
    if org_id is not None:
        query = query.filter(ResumeMetadata.organization_id == org_id)
    metas = query.all()

    updated = 0
    errors = 0

    for meta in metas:
        try:
            result = fast_parse_resume(meta.raw_text)

            # Update name if: new name is valid, OR old name is invalid/missing
            old_name_valid = _is_valid_name(meta.candidate_name) if meta.candidate_name else False
            if result["candidate_name"] and (not meta.candidate_name or not old_name_valid):
                meta.candidate_name = result["candidate_name"]

            # Always update these — parser is better now
            if result["skills"]:
                meta.skills = json.dumps(result["skills"])
            if result["education"]:
                meta.education = json.dumps(result["education"])
            if result["certifications"]:
                meta.certifications = json.dumps(result["certifications"])
            if result["work_experience"]:
                meta.work_experience = json.dumps(result["work_experience"])
            if result["keywords"]:
                meta.keywords = json.dumps(result["keywords"])

            # Update phone/location/designation if missing
            if not meta.phone and result["phone"]:
                meta.phone = result["phone"]
            if not meta.location and result["location"]:
                meta.location = result["location"]
            if not meta.current_designation and result["current_designation"]:
                meta.current_designation = result["current_designation"]
            if not meta.experience_years and result["experience_years"]:
                meta.experience_years = result["experience_years"]
            if result.get("personal_details"):
                meta.personal_details = json.dumps(result["personal_details"])

            # Update summary
            if result["summary"]:
                meta.summary = result["summary"]

            meta.updated_at = datetime.utcnow()
            updated += 1
        except Exception as e:
            logger.warning("Re-parse failed for %s: %s", meta.candidate_email, e)
            errors += 1

    db.commit()
    return {
        "total_processed": len(metas),
        "updated": updated,
        "errors": errors,
        "message": f"Re-parsed {updated} resume(s) with improved parser.",
    }


# ── Import (transfer from another DB) ────────────────────────────────────────

@router.post(
    "/import",
    summary="Import resume metadata records from JSON",
    description=(
        "Accepts a JSON array of resume records (exported from another instance). "
        "Skips duplicates (by email). Returns count of imported and skipped records."
    ),
)
async def import_resumes(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(_require_resume_admin),
):
    _check_resume_db_permission(user, db)
    try:
        body = await request.json()
    except ValueError:
        raise HTTPException(status_code=400, detail="Expected a JSON array of resume records")
    if not isinstance(body, list):
        raise HTTPException(status_code=400, detail="Expected a JSON array of resume records")

    org_id = get_org_id(user)
    imported = 0
    skipped = 0
    errors = 0

    for record in body:
        if not isinstance(record, dict):
            errors += 1
            continue
        email = record.get("candidate_email")
        if not isinstance(email, str) or not email or "@pending.local" in email:
            skipped += 1
            continue

        # Skip if already exists in this org
        if _resume_exists_in_org(db, email, org_id):
            skipped += 1
            continue

        try:
            meta = ResumeMetadata(
                candidate_email=email,
                candidate_name=record.get("candidate_name"),
                phone=record.get("phone"),
                current_designation=record.get("current_designation"),
                experience_years=record.get("experience_years"),
                skills=record.get("skills") if isinstance(record.get("skills"), str) else json.dumps(record.get("skills", [])),
                education=record.get("education") if isinstance(record.get("education"), str) else json.dumps(record.get("education", [])),
                certifications=record.get("certifications") if isinstance(record.get("certifications"), str) else json.dumps(record.get("certifications", [])),
                work_experience=record.get("work_experience") if isinstance(record.get("work_experience"), str) else json.dumps(record.get("work_experience", [])),
                location=record.get("location"),
                personal_details=record.get("personal_details") if isinstance(record.get("personal_details"), str) else json.dumps(record.get("personal_details") or {}),
                keywords=record.get("keywords") if isinstance(record.get("keywords"), str) else json.dumps(record.get("keywords", [])),
                summary=record.get("summary"),
                raw_text=record.get("raw_text"),
                parsing_status=_import_parsing_status(record),
                parsed_at=datetime.fromisoformat(record["parsed_at"]) if record.get("parsed_at") else None,
                organization_id=user.organization_id,
            )
            db.add(meta)
            imported += 1

            # Commit in batches of 100
            if imported % 100 == 0:
                db.commit()
        except Exception as exc:
            logger.error("Import failed for %s: %s", email, exc)
            errors += 1

    db.commit()
    return {
        "imported": imported,
        "skipped": skipped,
        "errors": errors,
        "total_received": len(body),
    }


# ── Import with File ─────────────────────────────────────────────────────────

@router.post(
    "/import-with-file",
    summary="Import a single resume record with its file",
    description="Upload a resume file along with parsed metadata JSON. Used for local-to-server transfer.",
)
async def import_with_file(
    metadata: str = Form(...),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    user: User = Depends(_require_resume_admin),
):
    """Import a single resume with its file from local to server."""
    _check_resume_db_permission(user, db)
    try:
        record = json.loads(metadata)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid metadata JSON")
    if not isinstance(record, dict):
        raise HTTPException(status_code=400, detail="Invalid metadata JSON")

    email = record.get("candidate_email")
    if not isinstance(email, str) or not email or "@pending.local" in email:
        return {"status": "skipped", "reason": "invalid email"}

    # Skip if already exists in this org
    if _resume_exists_in_org(db, email, get_org_id(user)):
        return {"status": "skipped", "reason": "already exists"}

    # Save file under a sanitized, server-chosen name (PDF/DOC/DOCX only)
    safe_name = _stored_resume_name(file.filename)
    if safe_name is None:
        return {"status": "skipped", "reason": "unsupported file type"}
    file_path = os.path.join(RESUME_UPLOAD_DIR, safe_name)
    with open(file_path, "wb") as f:
        shutil.copyfileobj(file.file, f)

    try:
        meta = ResumeMetadata(
            candidate_email=email,
            candidate_name=record.get("candidate_name"),
            phone=record.get("phone"),
            current_designation=record.get("current_designation"),
            experience_years=record.get("experience_years"),
            skills=record.get("skills") if isinstance(record.get("skills"), str) else json.dumps(record.get("skills", [])),
            education=record.get("education") if isinstance(record.get("education"), str) else json.dumps(record.get("education", [])),
            certifications=record.get("certifications") if isinstance(record.get("certifications"), str) else json.dumps(record.get("certifications", [])),
            work_experience=record.get("work_experience") if isinstance(record.get("work_experience"), str) else json.dumps(record.get("work_experience", [])),
            location=record.get("location"),
            personal_details=record.get("personal_details") if isinstance(record.get("personal_details"), str) else json.dumps(record.get("personal_details") or {}),
            keywords=record.get("keywords") if isinstance(record.get("keywords"), str) else json.dumps(record.get("keywords", [])),
            summary=record.get("summary"),
            raw_text=record.get("raw_text"),
            resume_file_path=file_path,
            parsing_status=_import_parsing_status(record),
            parsed_at=datetime.fromisoformat(record["parsed_at"]) if record.get("parsed_at") else None,
            organization_id=user.organization_id,
        )
        db.add(meta)
        db.commit()
    except Exception as exc:
        logger.error("Import with file failed for %s: %s", email, exc)
        db.rollback()
        # The row was not stored, so its file must not linger
        safe_unlink_upload(file_path, ("resumes",))
        raise HTTPException(status_code=500, detail="Import failed. Please try again.")
    return {"status": "imported", "id": meta.id}


# ── Helpers ──────────────────────────────────────────────────────────────────

def _resume_exists_in_org(db: Session, email: str, org_id: Optional[int]) -> bool:
    """Duplicate check within one organization (org-less rows only for a global
    super admin, whose get_org_id is None) — never across tenants."""
    q = db.query(ResumeMetadata.id).filter(ResumeMetadata.candidate_email == email)
    if org_id is not None:
        q = q.filter(ResumeMetadata.organization_id == org_id)
    else:
        q = q.filter(ResumeMetadata.organization_id.is_(None))
    return q.first() is not None


def _import_parsing_status(record: dict) -> str:
    status = record.get("parsing_status")
    return status if status in ("pending", "processing", "completed", "failed") else "completed"


def _parse_json(val: Optional[str]) -> list | dict:
    if not val:
        return []
    try:
        return json.loads(val)
    except (json.JSONDecodeError, TypeError):
        return []


def _serialize_meta(meta: ResumeMetadata, include_text: bool = False) -> dict:
    data = {
        "id": meta.id,
        "candidate_email": meta.candidate_email,
        "candidate_name": meta.candidate_name,
        "phone": meta.phone,
        "current_designation": meta.current_designation,
        "experience_years": meta.experience_years,
        "skills": _parse_json(meta.skills),
        "education": _parse_json(meta.education),
        "location": meta.location,
        "annual_package": meta.annual_package,
        "keywords": _parse_json(meta.keywords),
        "summary": meta.summary,
        "parsing_status": meta.parsing_status,
        "resume_file_path": meta.resume_file_path,
        "resume_file_url": sign_media_url(meta.resume_file_path),
        "parsed_at": meta.parsed_at.isoformat() if meta.parsed_at else None,
        "created_at": meta.created_at.isoformat() if meta.created_at else None,
    }
    if include_text:
        data["raw_text"] = meta.raw_text
        data["certifications"] = _parse_json(meta.certifications)
        data["work_experience"] = _parse_json(meta.work_experience)
        data["personal_details"] = _parse_json(meta.personal_details)
        data["parsing_error"] = meta.parsing_error
    return data
