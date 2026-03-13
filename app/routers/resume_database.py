"""
Resume Database — bulk upload, search, and manage the central resume store.

This router is for the admin-facing "Resume Database" page. Resumes are
uploaded here independently of any job opening. Parsing uses the fast
regex-based parser (no AI calls) so 50k+ resumes can be processed quickly.
"""

import asyncio
import json
import logging
import os
import shutil
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app.database import get_db, SessionLocal
from app.models.resume_metadata import ResumeMetadata

logger = logging.getLogger(__name__)

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

# ── Background processing queue ──────────────────────────────────────────────
_bulk_queue: asyncio.Queue = asyncio.Queue()
_bulk_worker_started = False
_bulk_progress: dict = {}  # batch_id -> {total, completed, failed, status}


async def _bulk_parse_worker():
    """
    Process bulk resume parse jobs sequentially.
    Uses fast regex parser — no AI calls — so each resume takes < 1 second.

    Deduplication: if parsing extracts an email that already exists in the DB,
    the existing record is UPDATED (with new resume) and the placeholder is deleted.
    """
    from app.services.resume_service import extract_text
    from app.services.fast_resume_parser import fast_parse_resume

    while True:
        meta_id, file_path, batch_id = await _bulk_queue.get()
        db = SessionLocal()
        try:
            meta = db.query(ResumeMetadata).filter(ResumeMetadata.id == meta_id).first()
            if not meta:
                continue

            meta.parsing_status = "processing"
            db.commit()

            # Step 1: Extract raw text from PDF/DOCX
            raw_text = extract_text(file_path)
            if not raw_text or len(raw_text.strip()) < 20:
                meta.parsing_status = "failed"
                meta.parsing_error = "Could not extract meaningful text from file"
                db.commit()
                if batch_id and batch_id in _bulk_progress:
                    _bulk_progress[batch_id]["failed"] += 1
                continue

            # Step 2: Fast regex-based parsing (no AI)
            parsed = fast_parse_resume(raw_text)
            parsed_email = parsed.get("candidate_email")

            # Step 3: Deduplication — if email already exists, merge into existing
            target = meta  # default: update the current record
            if parsed_email and meta.candidate_email != parsed_email:
                existing = db.query(ResumeMetadata).filter(
                    ResumeMetadata.candidate_email == parsed_email,
                    ResumeMetadata.id != meta.id,
                ).first()
                if existing:
                    # Merge: update existing record, delete old file, remove placeholder
                    target = existing
                    # Delete old resume file from the existing record
                    if existing.resume_file_path and os.path.exists(existing.resume_file_path) and existing.resume_file_path != file_path:
                        try:
                            os.remove(existing.resume_file_path)
                        except Exception:
                            pass
                    # Delete the placeholder record
                    db.delete(meta)
                    db.flush()
                else:
                    # No duplicate — just update email on current record
                    meta.candidate_email = parsed_email

            # Step 4: Update the target record with parsed data
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

            if batch_id and batch_id in _bulk_progress:
                _bulk_progress[batch_id]["completed"] += 1

        except Exception as exc:
            logger.error("Bulk parse failed for meta %s: %s", meta_id, exc, exc_info=True)
            try:
                meta = db.query(ResumeMetadata).filter(ResumeMetadata.id == meta_id).first()
                if meta:
                    meta.parsing_status = "failed"
                    meta.parsing_error = str(exc)[:500]
                    db.commit()
            except Exception:
                pass
            if batch_id and batch_id in _bulk_progress:
                _bulk_progress[batch_id]["failed"] += 1
        finally:
            db.close()
            _bulk_queue.task_done()

            # Update batch status
            if batch_id and batch_id in _bulk_progress:
                p = _bulk_progress[batch_id]
                if (p["completed"] + p["failed"]) >= p["total"]:
                    p["status"] = "completed"


def _ensure_bulk_worker():
    """Start the background bulk parse worker if not already running."""
    global _bulk_worker_started
    if not _bulk_worker_started:
        _bulk_worker_started = True
        asyncio.get_event_loop().create_task(_bulk_parse_worker())


# ── Bulk Upload ──────────────────────────────────────────────────────────────

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
    db: Session = Depends(get_db),
):
    _ensure_bulk_worker()

    if len(files) > 200:
        raise HTTPException(
            status_code=400,
            detail="Maximum 200 files per upload batch. Upload in smaller batches."
        )

    import uuid
    batch_id = uuid.uuid4().hex[:12]
    _bulk_progress[batch_id] = {
        "total": len(files),
        "completed": 0,
        "failed": 0,
        "status": "processing",
    }

    created_ids = []

    for file in files:
        if file.content_type not in ALLOWED_RESUME_TYPES:
            _bulk_progress[batch_id]["total"] -= 1
            _bulk_progress[batch_id]["failed"] += 1
            continue

        # Save file to disk
        safe_name = f"{datetime.utcnow().strftime('%Y%m%d%H%M%S%f')}_{file.filename}"
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
        )
        db.add(meta)
        db.commit()
        db.refresh(meta)
        created_ids.append(meta.id)

        # Queue for background parsing
        await _bulk_queue.put((meta.id, file_path, batch_id))

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
def get_batch_progress(batch_id: str):
    if batch_id not in _bulk_progress:
        raise HTTPException(status_code=404, detail="Batch not found")
    return _bulk_progress[batch_id]


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
):
    query = db.query(ResumeMetadata)

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
def get_stats(db: Session = Depends(get_db)):
    # Exclude stale placeholder records from stats
    base = db.query(func.count(ResumeMetadata.id)).filter(
        ~ResumeMetadata.candidate_email.like("%@pending.local")
    )
    total = base.scalar() or 0
    completed = db.query(func.count(ResumeMetadata.id)).filter(
        ResumeMetadata.parsing_status == "completed",
        ~ResumeMetadata.candidate_email.like("%@pending.local"),
    ).scalar() or 0
    pending = db.query(func.count(ResumeMetadata.id)).filter(
        ResumeMetadata.parsing_status.in_(["pending", "processing"]),
        ~ResumeMetadata.candidate_email.like("%@pending.local"),
    ).scalar() or 0
    failed = db.query(func.count(ResumeMetadata.id)).filter(
        ResumeMetadata.parsing_status == "failed",
        ~ResumeMetadata.candidate_email.like("%@pending.local"),
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
def get_resume_detail(meta_id: int, db: Session = Depends(get_db)):
    meta = db.query(ResumeMetadata).filter(ResumeMetadata.id == meta_id).first()
    if not meta:
        raise HTTPException(status_code=404, detail="Resume not found")
    return _serialize_meta(meta, include_text=True)


# ── Delete ───────────────────────────────────────────────────────────────────

@router.delete(
    "/{meta_id}",
    summary="Delete a resume metadata record",
    status_code=204,
)
def delete_resume(meta_id: int, db: Session = Depends(get_db)):
    from app.models.candidate import Candidate

    meta = db.query(ResumeMetadata).filter(ResumeMetadata.id == meta_id).first()
    if not meta:
        raise HTTPException(status_code=404, detail="Resume not found")

    # Unlink any candidates referencing this metadata
    db.query(Candidate).filter(Candidate.resume_metadata_id == meta_id).update(
        {"resume_metadata_id": None}, synchronize_session="fetch"
    )

    # Delete file from disk
    if meta.resume_file_path and os.path.exists(meta.resume_file_path):
        try:
            os.remove(meta.resume_file_path)
        except Exception:
            pass
    db.delete(meta)
    db.commit()


# ── Cleanup Duplicates ──────────────────────────────────────────────────────

@router.post(
    "/cleanup",
    summary="Remove stale placeholder records and deduplicate",
    description=(
        "Removes placeholder records (`@pending.local`) that failed or never parsed. "
        "Returns count of deleted records."
    ),
)
def cleanup_stale_records(db: Session = Depends(get_db)):
    """Remove placeholder records that were never parsed or failed."""
    from app.models.candidate import Candidate

    # Delete placeholder records that are still pending/failed (never parsed)
    stale = db.query(ResumeMetadata).filter(
        ResumeMetadata.candidate_email.like("%@pending.local"),
        ResumeMetadata.parsing_status.in_(["pending", "failed"]),
    ).all()

    deleted_count = 0
    stale_ids = [m.id for m in stale]

    # Unlink any candidates referencing these metadata records
    if stale_ids:
        db.query(Candidate).filter(Candidate.resume_metadata_id.in_(stale_ids)).update(
            {"resume_metadata_id": None}, synchronize_session="fetch"
        )

    for meta in stale:
        # Delete the file if it exists
        if meta.resume_file_path and os.path.exists(meta.resume_file_path):
            try:
                os.remove(meta.resume_file_path)
            except Exception:
                pass
        db.delete(meta)
        deleted_count += 1

    db.commit()
    return {"deleted": deleted_count, "message": f"Removed {deleted_count} stale placeholder record(s)."}


# ── Helpers ──────────────────────────────────────────────────────────────────

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
        "keywords": _parse_json(meta.keywords),
        "summary": meta.summary,
        "parsing_status": meta.parsing_status,
        "resume_file_path": meta.resume_file_path,
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
