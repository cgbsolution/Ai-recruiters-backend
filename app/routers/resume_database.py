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

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import StreamingResponse
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app.database import get_db, SessionLocal
from app.models.resume_metadata import ResumeMetadata
from app.schemas.resume_metadata import ResumeMetadataUpdate

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

        # Deduplication
        target = meta
        if parsed_email and meta.candidate_email != parsed_email:
            existing = db.query(ResumeMetadata).filter(
                ResumeMetadata.candidate_email == parsed_email,
                ResumeMetadata.id != meta.id,
            ).first()
            if existing:
                target = existing
                if existing.resume_file_path and os.path.exists(existing.resume_file_path) and existing.resume_file_path != file_path:
                    try:
                        os.remove(existing.resume_file_path)
                    except Exception:
                        pass
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

        _update_batch_progress(batch_id, "completed")

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

        # Deduplication
        target = meta
        if parsed_email and meta.candidate_email != parsed_email:
            existing = db.query(ResumeMetadata).filter(
                ResumeMetadata.candidate_email == parsed_email,
                ResumeMetadata.id != meta.id,
            ).first()
            if existing:
                target = existing
                if existing.resume_file_path and os.path.exists(existing.resume_file_path) and existing.resume_file_path != file_path:
                    try:
                        os.remove(existing.resume_file_path)
                    except Exception:
                        pass
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

        _update_batch_progress(batch_id, "completed")

        # Small delay between AI calls to respect rate limits
        await asyncio.sleep(1)

    except Exception as exc:
        logger.error("AI parse failed for meta %s: %s", meta_id, exc, exc_info=True)
        try:
            meta = db.query(ResumeMetadata).filter(ResumeMetadata.id == meta_id).first()
            if meta:
                meta.parsing_status = "failed"
                meta.parsing_error = str(exc)[:500]
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
):
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
def get_batch_progress(batch_id: str):
    if batch_id not in _bulk_progress:
        raise HTTPException(status_code=404, detail="Batch not found")
    return _bulk_progress[batch_id]


# ── Failed Resumes ──────────────────────────────────────────────────────────

@router.get(
    "/failed",
    summary="List all failed resume records",
)
def list_failed_resumes(db: Session = Depends(get_db)):
    """Return all resumes that failed parsing, with error details."""
    failed = db.query(ResumeMetadata).filter(
        ResumeMetadata.parsing_status == "failed",
    ).order_by(ResumeMetadata.created_at.desc()).all()

    return {
        "count": len(failed),
        "items": [
            {
                "id": m.id,
                "candidate_name": m.candidate_name,
                "candidate_email": m.candidate_email,
                "resume_file_path": m.resume_file_path,
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
def download_failed_resumes(db: Session = Depends(get_db)):
    """Download a CSV file listing all resumes that failed parsing."""
    failed = db.query(ResumeMetadata).filter(
        ResumeMetadata.parsing_status == "failed",
    ).order_by(ResumeMetadata.created_at.desc()).all()

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
):
    meta = db.query(ResumeMetadata).filter(ResumeMetadata.id == meta_id).first()
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
def clear_all_resumes(db: Session = Depends(get_db)):
    """Wipe the entire resume database. Use with caution."""
    from app.models.candidate import Candidate

    # Unlink all candidates
    db.query(Candidate).filter(Candidate.resume_metadata_id.isnot(None)).update(
        {"resume_metadata_id": None}, synchronize_session="fetch"
    )

    all_resumes = db.query(ResumeMetadata).all()
    count = len(all_resumes)

    for meta in all_resumes:
        if meta.resume_file_path and os.path.exists(meta.resume_file_path):
            try:
                os.remove(meta.resume_file_path)
            except Exception:
                pass
        db.delete(meta)

    db.commit()

    # Also clean up the uploads/resumes folder
    if os.path.exists(RESUME_UPLOAD_DIR):
        for f in os.listdir(RESUME_UPLOAD_DIR):
            try:
                os.remove(os.path.join(RESUME_UPLOAD_DIR, f))
            except Exception:
                pass

    return {"deleted": count, "message": f"Deleted all {count} resume records and files."}


# ── Delete Single ────────────────────────────────────────────────────────────

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


# ── Re-parse single resume (hybrid: regex + LLM correction) ──────────────────

@router.post(
    "/{meta_id}/reparse",
    summary="Re-parse a single resume with improved regex + LLM correction",
)
async def reparse_single_resume(meta_id: int, use_llm: bool = True, db: Session = Depends(get_db)):
    from app.services.fast_resume_parser import fast_parse_resume
    from app.services.llm_resume_parser import llm_parse_resume, needs_llm_correction

    meta = db.query(ResumeMetadata).filter(ResumeMetadata.id == meta_id).first()
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
        raise HTTPException(status_code=500, detail=f"Re-parse failed: {str(e)}")


# ── Bulk re-parse with LLM correction (for bad entries) ──────────────────────

@router.post(
    "/reparse-bad",
    summary="Re-parse resumes that have bad data (auto-detected) using regex + LLM",
)
async def reparse_bad_resumes(db: Session = Depends(get_db)):
    """Find resumes with garbage data and re-parse them with LLM correction."""
    from app.services.fast_resume_parser import fast_parse_resume
    from app.services.llm_resume_parser import llm_parse_resume, needs_llm_correction

    metas = db.query(ResumeMetadata).filter(
        ResumeMetadata.parsing_status == "completed",
        ResumeMetadata.raw_text.isnot(None),
    ).all()

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
def reparse_all_resumes(db: Session = Depends(get_db)):
    """Re-parse all completed resumes using the improved fast parser."""
    from app.services.fast_resume_parser import fast_parse_resume, _is_valid_name
    import json
    from datetime import datetime

    metas = db.query(ResumeMetadata).filter(
        ResumeMetadata.parsing_status == "completed",
        ResumeMetadata.raw_text.isnot(None),
    ).all()

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
):
    body = await request.json()
    if not isinstance(body, list):
        raise HTTPException(status_code=400, detail="Expected a JSON array of resume records")

    imported = 0
    skipped = 0
    errors = 0

    for record in body:
        email = record.get("candidate_email")
        if not email or "@pending.local" in email:
            skipped += 1
            continue

        # Skip if already exists
        existing = db.query(ResumeMetadata).filter(
            ResumeMetadata.candidate_email == email
        ).first()
        if existing:
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
                parsing_status=record.get("parsing_status", "completed"),
                parsed_at=datetime.fromisoformat(record["parsed_at"]) if record.get("parsed_at") else None,
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
):
    """Import a single resume with its file from local to server."""
    try:
        record = json.loads(metadata)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid metadata JSON")

    email = record.get("candidate_email")
    if not email or "@pending.local" in email:
        return {"status": "skipped", "reason": "invalid email"}

    # Skip if already exists
    existing = db.query(ResumeMetadata).filter(
        ResumeMetadata.candidate_email == email
    ).first()
    if existing:
        return {"status": "skipped", "reason": "already exists"}

    # Save file
    safe_name = f"{datetime.utcnow().strftime('%Y%m%d%H%M%S%f')}_{file.filename}"
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
            parsing_status=record.get("parsing_status", "completed"),
            parsed_at=datetime.fromisoformat(record["parsed_at"]) if record.get("parsed_at") else None,
        )
        db.add(meta)
        db.commit()
        return {"status": "imported", "id": meta.id}
    except Exception as exc:
        logger.error("Import with file failed for %s: %s", email, exc)
        raise HTTPException(status_code=500, detail=str(exc))


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
        "annual_package": meta.annual_package,
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
