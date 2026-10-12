"""
Resume Database — bulk upload, search, and manage the central resume store.

This router is for the admin-facing "Resume Database" page. Resumes are
uploaded here independently of any job opening. Supports two parsing modes:
- "fast": Regex-based parser (no AI calls, < 1s per resume, ~85% accuracy)
- "ai":   Claude AI parser (one LLM call per resume, several seconds each,
          RESUME_AI_CONCURRENCY at a time, ~99% accuracy)
"""

import asyncio
import csv
import hashlib
import io
import json
import logging
import os
import re
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Tuple

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth import get_current_user, get_org_id, require_roles
from app.authz import can_access_org
from app.config import settings
from app.database import get_db, SessionLocal
from app.media import (
    UPLOAD_ROOT,
    resolve_upload_file,
    resume_file_in_use,
    resume_upload_problem,
    safe_unlink_upload,
    safe_upload_name,
    sign_media_url,
)
from app.services.file_safety import UnsafeFile, store_resume_upload
from app.models.organization import Organization
from app.models.resume_metadata import ResumeMetadata, mark_recruiter_edit
from app.models.user import User, UserRole
from app.schemas.resume_metadata import ResumeMetadataUpdate
from app.services import resume_search
from app.services.fast_resume_parser import FAST_PARSER_VERSION
from app.services.file_compression import schedule_compression
from app.services.resume_search import NOT_UNPARSED_PLACEHOLDER as _NOT_UNPARSED_PLACEHOLDER

logger = logging.getLogger(__name__)

# Bounds for POST /import (a JSON export from another instance).
_MAX_IMPORT_BODY_BYTES = 200 * 1024 * 1024
_MAX_IMPORT_RECORDS = 20_000
_MAX_IMPORT_RAW_TEXT = 100_000

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
#
# Every uploaded file becomes a placeholder row plus a queue item. A worker
# reads the file in a separate process (app.services.extraction_pool), parses
# it (regex, or the LLM in AI mode) and saves the result under one lock.
# Saving is where duplicates are merged; serializing it means two copies of
# the same resume in flight can never both survive.
#
# The queue and the per-batch counters live in memory (the app runs a single
# uvicorn process). Rows also carry batch_id + parse_mode, so after a restart
# requeue_unfinished_bulk_rows() queues them again and GET /batch/{id}
# reports from the database.

_bulk_queue: asyncio.Queue = asyncio.Queue()
_bulk_workers: list = []  # strong refs: the loop only keeps weak ones to tasks
_bulk_progress: dict = {}  # batch_id -> {total, completed, failed, replaced, status, parse_mode}

# Concurrent LLM calls. This was 1 (sized for the Gemini free tier) and the
# 1 s rate-limit pause sat inside the slot, so AI mode parsed one resume at a
# time — an 11k upload needed more than a day.
_ai_semaphore = asyncio.Semaphore(max(1, settings.RESUME_AI_CONCURRENCY))
_save_lock = asyncio.Lock()
_NUM_WORKERS = max(settings.RESUME_AI_CONCURRENCY, settings.RESUME_EXTRACT_WORKERS) + 2

# Finished batches are dropped from memory after this long.
_BATCH_RETENTION_SECONDS = 6 * 3600
# Attempts for one resume when the LLM answers "rate limited" / "overloaded".
_AI_RATE_LIMIT_ATTEMPTS = 3

_PARSED_FIELDS = (
    "phone", "current_designation", "experience_years", "skills", "education",
    "certifications", "work_experience", "location", "personal_details",
    "keywords", "summary",
)


def _new_batch(batch_id: str, total: int, parse_mode: str, org_id: Optional[int]) -> None:
    now = time.time()
    for old_id in [b for b, p in _bulk_progress.items()
                   if p.get("finished_at") and now - p["finished_at"] > _BATCH_RETENTION_SECONDS]:
        _bulk_progress.pop(old_id, None)
        _bulk_owner.pop(old_id, None)
    _bulk_progress[batch_id] = {
        "total": total,
        "completed": 0,
        "failed": 0,
        "replaced": 0,
        "status": "processing",
        "parse_mode": parse_mode,
    }
    _bulk_owner[batch_id] = org_id


def _record_outcome(batch_id: str, outcome: str) -> None:
    """Count one file of a batch: "completed", "replaced" (completed, and an
    older copy of the same resume was replaced) or "failed"."""
    p = _bulk_progress.get(batch_id)
    if p is None:
        return
    if outcome == "failed":
        p["failed"] += 1
    else:
        p["completed"] += 1
        if outcome == "replaced":
            p["replaced"] += 1
    if p["status"] != "completed" and p["completed"] + p["failed"] >= p["total"]:
        p["status"] = "completed"
        p["finished_at"] = time.time()


def _org_rows(db: Session, org_id: Optional[int]):
    """ResumeMetadata rows of one organization — org-less rows only match
    other org-less rows, so nothing is ever merged across tenants."""
    q = db.query(ResumeMetadata)
    if org_id is not None:
        return q.filter(ResumeMetadata.organization_id == org_id)
    return q.filter(ResumeMetadata.organization_id.is_(None))


def _same_file_copies(db: Session, meta: ResumeMetadata) -> list:
    """Other finished rows of the org holding these exact bytes. Copies still
    queued are left alone: they merge themselves when their turn comes."""
    if not meta.file_hash:
        return []
    return _org_rows(db, meta.organization_id).filter(
        ResumeMetadata.id != meta.id,
        ResumeMetadata.file_hash == meta.file_hash,
        ResumeMetadata.parsing_status.in_(("completed", "failed")),
    ).all()


def _replace_older_copies(db: Session, target: ResumeMetadata, losers: list, new_file: str) -> List[str]:
    """Fold `losers` (older records of the same resume) into `target`:
    candidates linked to them are relinked to `target`, then the rows are
    deleted. Returns their files, to delete once nothing references them."""
    from app.models.candidate import Candidate

    if not losers:
        return []
    # …and to the new file: they would otherwise keep showing the old resume.
    db.query(Candidate).filter(Candidate.resume_metadata_id.in_([m.id for m in losers])).update(
        {"resume_metadata_id": target.id, "resume_path": new_file}, synchronize_session=False
    )
    stale = [m.resume_file_path for m in losers if m.resume_file_path and m.resume_file_path != new_file]
    for m in losers:
        db.delete(m)
    db.flush()
    return stale


def _claim_bulk_row(meta_id: int, file_path: str, batch_id: str, parse_mode: str) -> Optional[str]:
    """First step for a queue item (runs under _save_lock).

    Returns None when the file must be read and parsed (the row is then marked
    processing). Otherwise the item is finished here:
    - "replaced": the org already holds a parsed copy of these exact bytes.
      That record now points at the new upload, the placeholder and the old
      file are removed — no re-read, no second LLM call.
    - "superseded": another upload of the same person already merged into this
      row (so this file was replaced by a newer one).
    - "missing": the row was deleted meanwhile (Clear All / Cleanup).
    """
    db = SessionLocal()
    try:
        meta = db.query(ResumeMetadata).filter(ResumeMetadata.id == meta_id).first()
        if meta is None:
            return "missing"
        if meta.parsing_status != "pending" or meta.resume_file_path != file_path:
            return "superseded"

        copies = _same_file_copies(db, meta)
        reusable = [
            c for c in copies
            if c.parsing_status == "completed"
            # Choosing AI mode means wanting the AI parse: a regex-parsed copy
            # is re-parsed rather than reused.
            and (parse_mode != "ai" or c.parse_mode == "ai")
            # A regex parse made by an older parser is redone, so parser fixes
            # (e.g. locations) reach files that are uploaded again.
            and (c.parse_mode == "ai" or c.parser_version == FAST_PARSER_VERSION)
        ]
        if reusable:
            target = max(reusable, key=lambda c: c.id)
            previous_file = target.resume_file_path
            losers = [c for c in copies if c.id != target.id] + [meta]
            stale = _replace_older_copies(db, target, losers, file_path)
            if previous_file and previous_file != file_path:
                stale.append(previous_file)
            target.resume_file_path = file_path
            target.batch_id = batch_id
            target.updated_at = datetime.utcnow()
            db.commit()
            _remove_unused_resume_files(stale)
            schedule_compression(file_path)
            return "replaced"

        meta.parsing_status = "processing"
        meta.parsing_error = None
        db.commit()
        return None
    finally:
        db.close()


# How the regex parser writes its summary ("Name | 4 years experience |
# based in Pune | Skills: ..."); an LLM summary is prose.
_FAST_SUMMARY_RE = re.compile(r"(?:^|\| )(?:\d+(?:\.\d+)? years experience|based in |Skills: )")


def _was_ai_parsed(row: ResumeMetadata) -> bool:
    """Whether a stored parse came from the LLM. Rows from before parse_mode
    was recorded are judged by their summary's shape."""
    if row.parse_mode:
        return row.parse_mode == "ai"
    return bool(row.summary) and not _FAST_SUMMARY_RE.search(row.summary)


def _file_sha256(stored_path: Optional[str]) -> Optional[str]:
    full = resolve_upload_file(stored_path, ("resumes",))
    if not full:
        return None
    digest = hashlib.sha256()
    try:
        with open(full, "rb") as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError:
        return None
    return digest.hexdigest()


def _save_parsed_once(db: Session, meta_id: int, file_path: str, batch_id: str, parse_mode: str,
                      raw_text: str, fields: dict) -> str:
    from app.services.fast_resume_parser import is_role_mailbox

    meta = db.query(ResumeMetadata).filter(ResumeMetadata.id == meta_id).first()
    if meta is None:
        return "failed"
    new_hash = meta.file_hash
    # The email written on the resume is the candidate's identity. A shared
    # inbox (a vendor's "resumes@...", a company "hr@...") is not: keyed on it,
    # every resume carrying that header would collapse into one record.
    email = (fields.get("email") or "").strip() or None
    if email and is_role_mailbox(email):
        email = None

    # The same person (email, case-insensitive) or the same file (hash) seen
    # before in this org: the newest upload wins and the older record + file go.
    email_match = None
    if email:
        email_match = _org_rows(db, meta.organization_id).filter(
            ResumeMetadata.id != meta.id,
            func.lower(ResumeMetadata.candidate_email) == email.lower(),
        ).order_by(ResumeMetadata.id.desc()).first()
    copies = _same_file_copies(db, meta)
    if email_match is not None:
        target = email_match
    else:
        parsed_copies = [c for c in copies if c.parsing_status == "completed"]
        target = max(parsed_copies, key=lambda c: c.id) if parsed_copies else meta

    losers = [c for c in copies if c.id != target.id]
    if target is not meta:
        losers.append(meta)
    previous_file = target.resume_file_path if target is not meta else None
    stale = _replace_older_copies(db, target, losers, file_path)
    if previous_file and previous_file != file_path:
        stale.append(previous_file)

    # Records stored before file fingerprints existed: fingerprint the old file
    # now. Same bytes re-uploaded in fast mode must not overwrite an AI parse
    # with the less accurate regex one (e.g. resumes an earlier AI run already
    # parsed, uploaded again in fast mode).
    if target is not meta and target.file_hash is None and previous_file:
        target.file_hash = _file_sha256(previous_file)
    keep_existing_parse = (
        target is not meta
        and new_hash is not None
        and target.file_hash == new_hash
        and parse_mode == "fast"
        and _was_ai_parsed(target)
        and target.parsing_status == "completed"
    )
    if keep_existing_parse:
        target.resume_file_path = file_path
        target.batch_id = batch_id
        target.updated_at = datetime.utcnow()
        db.commit()
        _remove_unused_resume_files(stale)
        return "replaced"

    if target is meta and email:
        meta.candidate_email = email
    target.candidate_name = fields.get("name") or target.candidate_name
    target.phone = fields.get("phone")
    target.current_designation = fields.get("current_designation")
    target.experience_years = fields.get("experience_years")
    target.skills = json.dumps(fields.get("skills") or [])
    target.education = json.dumps(fields.get("education") or [])
    target.certifications = json.dumps(fields.get("certifications") or [])
    target.work_experience = json.dumps(fields.get("work_experience") or [])
    target.location = fields.get("location")
    target.personal_details = json.dumps(fields.get("personal_details") or {})
    target.keywords = json.dumps(fields.get("keywords") or [])
    target.summary = fields.get("summary")
    target.raw_text = raw_text
    target.resume_file_path = file_path
    target.file_hash = new_hash
    target.batch_id = batch_id
    target.parse_mode = parse_mode
    target.parser_version = FAST_PARSER_VERSION if parse_mode == "fast" else None
    target.parsing_status = "completed"
    target.parsing_error = None
    target.parsed_at = datetime.utcnow()
    db.commit()

    # Old files go only once no other row (e.g. a hired candidate) uses them.
    _remove_unused_resume_files(stale)
    return "replaced" if losers else "completed"


def _save_parsed(meta_id: int, file_path: str, batch_id: str, parse_mode: str,
                 raw_text: str, fields: dict) -> str:
    """Store a parse result, merging older copies (runs under _save_lock).
    Returns "completed", "replaced" or "failed"."""
    for attempt in (1, 2):
        db = SessionLocal()
        try:
            return _save_parsed_once(db, meta_id, file_path, batch_id, parse_mode, raw_text, fields)
        except IntegrityError:
            # Another code path (e.g. a candidate application) stored the same
            # email meanwhile; on the retry the email lookup finds that row.
            db.rollback()
            if attempt == 2:
                raise
        finally:
            db.close()
    return "failed"  # unreachable


def _mark_failed(meta_id: int, message: str) -> None:
    db = SessionLocal()
    try:
        meta = db.query(ResumeMetadata).filter(ResumeMetadata.id == meta_id).first()
        if meta is not None:
            meta.parsing_status = "failed"
            meta.parsing_error = message[:500]
            db.commit()
    except Exception as exc:
        db.rollback()
        logger.warning("Could not mark resume %s as failed: %s", meta_id, exc)
    finally:
        db.close()


def _fields_from_fast(parsed: dict) -> dict:
    fields = {k: parsed.get(k) for k in _PARSED_FIELDS}
    fields["email"] = parsed.get("candidate_email")
    fields["name"] = parsed.get("candidate_name")
    return fields


def _is_rate_limited(exc: Exception) -> bool:
    status = getattr(exc, "status_code", None) or getattr(exc, "code", None)
    name = type(exc).__name__.lower()
    return status in (429, 529) or "ratelimit" in name or "overloaded" in name or "resourceexhausted" in name


async def _fields_from_ai(raw_text: str) -> dict:
    from app.schemas.ai import ParseResumeRequest
    from app.services.resume_service import parse_resume

    timeout = max(10, settings.RESUME_AI_TIMEOUT_SECONDS)
    for attempt in range(1, _AI_RATE_LIMIT_ATTEMPTS + 1):
        try:
            async with _ai_semaphore:
                # The SDK's own timeout is 10 minutes (plus retries); one stalled
                # request used to hold the only parse slot that long.
                result = await asyncio.wait_for(parse_resume(ParseResumeRequest(resume_text=raw_text)), timeout)
            break
        except asyncio.TimeoutError:
            raise RuntimeError("AI parsing timed out. Upload this resume again to retry.")
        except Exception as exc:
            if attempt < _AI_RATE_LIMIT_ATTEMPTS and _is_rate_limited(exc):
                logger.warning("LLM rate-limited during bulk parse (attempt %d); backing off", attempt)
                await asyncio.sleep(15 * attempt)  # outside the slot, so it is not held idle
                continue
            raise
    data = result.parsed_data.model_dump()
    fields = {k: data.get(k) for k in _PARSED_FIELDS}
    fields["email"] = data.get("email")
    fields["name"] = data.get("name")
    return fields


async def _process_bulk_item(meta_id: int, file_path: str, batch_id: str, parse_mode: str) -> None:
    from app.services.extraction_pool import extract_text_isolated

    async with _save_lock:
        claim = await asyncio.to_thread(_claim_bulk_row, meta_id, file_path, batch_id, parse_mode)
    if claim is not None:
        _record_outcome(batch_id, "failed" if claim == "missing" else "replaced")
        return

    try:
        raw_text, fast_parsed = await extract_text_isolated(file_path, fast_parse=(parse_mode != "ai"))
        if not raw_text or len(raw_text.strip()) < 20:
            raise ValueError("Could not extract meaningful text from file")
        if parse_mode == "ai":
            fields = await _fields_from_ai(raw_text)
        else:
            fields = _fields_from_fast(fast_parsed or {})
        async with _save_lock:
            outcome = await asyncio.to_thread(
                _save_parsed, meta_id, file_path, batch_id, parse_mode, raw_text, fields
            )
        if outcome != "failed":
            schedule_compression(file_path)
    except Exception as exc:
        if isinstance(exc, (RuntimeError, ValueError)):
            logger.warning("Bulk parse failed for resume %s (%s): %s", meta_id, os.path.basename(file_path), exc)
        else:
            logger.error("Bulk parse failed for resume %s: %s", meta_id, exc, exc_info=True)
        await asyncio.to_thread(_mark_failed, meta_id, _parse_failure_message(exc))
        outcome = "failed"
    _record_outcome(batch_id, outcome)


async def _bulk_parse_worker():
    while True:
        meta_id, file_path, batch_id, parse_mode = await _bulk_queue.get()
        try:
            await _process_bulk_item(meta_id, file_path, batch_id, parse_mode)
        except Exception as exc:  # never let a worker die
            logger.error("Bulk worker error for resume %s: %s", meta_id, exc, exc_info=True)
            _record_outcome(batch_id, "failed")
        finally:
            _bulk_queue.task_done()


def _ensure_bulk_worker():
    """Start the background parse workers once, on the running loop."""
    if _bulk_workers:
        return
    loop = asyncio.get_running_loop()
    for _ in range(_NUM_WORKERS):
        _bulk_workers.append(loop.create_task(_bulk_parse_worker()))


def _reset_unfinished_bulk_rows() -> list:
    db = SessionLocal()
    try:
        unfinished = (
            ResumeMetadata.batch_id.isnot(None),
            ResumeMetadata.parsing_status.in_(("pending", "processing")),
            ResumeMetadata.resume_file_path.isnot(None),
        )
        items = (
            db.query(ResumeMetadata.id, ResumeMetadata.resume_file_path,
                     ResumeMetadata.batch_id, ResumeMetadata.parse_mode)
            .filter(*unfinished)
            .order_by(ResumeMetadata.id)
            .all()
        )
        # "processing" rows were mid-parse when the server stopped.
        db.query(ResumeMetadata).filter(*unfinished).filter(
            ResumeMetadata.parsing_status == "processing"
        ).update({"parsing_status": "pending"}, synchronize_session=False)
        db.commit()
        return [(i, path, batch, mode if mode in ("fast", "ai") else "fast") for i, path, batch, mode in items]
    finally:
        db.close()


async def requeue_unfinished_bulk_rows() -> int:
    """Startup: queue again the bulk rows a restart left unfinished."""
    items = await asyncio.to_thread(_reset_unfinished_bulk_rows)
    if not items:
        return 0
    _ensure_bulk_worker()
    for item in items:
        _bulk_queue.put_nowait(item)
    logger.info("Re-queued %d unfinished bulk resume parse(s) after restart", len(items))
    return len(items)


def _save_upload_with_hash(upload: UploadFile, file_path: str) -> str:
    """Write the upload to `file_path` (never overwriting) once it passes the
    safety checks, and return the SHA-256 of its bytes. Raises UnsafeFile."""
    return store_resume_upload(upload, file_path)


def _store_bulk_uploads(db: Session, files: List[UploadFile], batch_id: str, parse_mode: str,
                        org_id: Optional[int]) -> Tuple[list, list]:
    """Save each file and its pending row. Returns (queue items, skipped files)."""
    queued, skipped = [], []
    for file in files:
        name = file.filename or "resume"
        if file.content_type not in ALLOWED_RESUME_TYPES:
            skipped.append({"file": name, "reason": "not a PDF, DOC or DOCX file"})
            continue
        problem = resume_upload_problem(file)
        if problem:
            logger.warning("Skipped bulk resume %s: %s", name, problem)
            skipped.append({"file": name, "reason": problem})
            continue

        safe_name = _stored_resume_name(name, default_ext=ALLOWED_RESUME_TYPES[file.content_type])
        file_path = os.path.join(RESUME_UPLOAD_DIR, safe_name)
        try:
            file_hash = _save_upload_with_hash(file, file_path)
            meta = ResumeMetadata(
                # Unique placeholder until parsing finds the real email (the
                # old name-derived one could collide within one batch).
                candidate_email=f"noemail_{uuid.uuid4().hex}@pending.local",
                candidate_name=Path(name).stem.replace("_", " ").replace("-", " ").title(),
                resume_file_path=file_path,
                file_hash=file_hash,
                batch_id=batch_id,
                parse_mode=parse_mode,
                source="bulk_upload",
                parsing_status="pending",
                organization_id=org_id,
            )
            db.add(meta)
            db.flush()
            meta_id = meta.id
            db.commit()
        except UnsafeFile as exc:  # refused and already deleted
            skipped.append({"file": name, "reason": exc.reason})
            continue
        except Exception as exc:
            db.rollback()
            logger.error("Could not store bulk resume %s: %s", name, exc)
            safe_unlink_upload(file_path, ("resumes",))
            skipped.append({"file": name, "reason": "could not be saved"})
            continue
        queued.append((meta_id, file_path, batch_id, parse_mode))
    return queued, skipped


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
        "Upload up to 100 resume files (PDF/DOC/DOCX) per request. Files are "
        "saved and queued for background parsing ('fast' regex or 'ai'). "
        "Returns a batch_id to track progress.\n\n"
        "Duplicates: a file whose bytes match an earlier upload, or whose email "
        "matches an existing record, replaces that record — the older record "
        "and its file are removed."
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

    batch_id = uuid.uuid4().hex[:12]
    _new_batch(batch_id, len(files), parse_mode, user.organization_id)

    # Writing up to 100 files and their rows is blocking disk + DB work: done
    # in a thread so live interviews on this event loop never stall behind it.
    queued, skipped = await asyncio.to_thread(
        _store_bulk_uploads, db, files, batch_id, parse_mode, user.organization_id
    )
    # Skipped files count as failed. Total stays len(files): it used to be
    # decremented as well, so a batch reported "completed" too early — and one
    # where every file was skipped never completed, freezing the upload screen.
    for _ in skipped:
        _record_outcome(batch_id, "failed")
    for item in queued:
        _bulk_queue.put_nowait(item)

    return {
        "batch_id": batch_id,
        "queued": len(queued),
        "total_files": len(files),
        "skipped": skipped,
        "message": f"Queued {len(queued)} resume(s) for parsing. Use GET /resume-database/batch/{batch_id} to track progress.",
    }


# ── Batch Progress ───────────────────────────────────────────────────────────

@router.get(
    "/batch/{batch_id}",
    summary="Check bulk upload batch progress",
)
def get_batch_progress(batch_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    progress = _bulk_progress.get(batch_id)
    if progress is not None:
        # Only the uploader's organization (or a global super admin) may poll a batch
        if not can_access_org(user, _bulk_owner.get(batch_id)):
            raise HTTPException(status_code=404, detail="Batch not found")
        return progress

    # Not in memory: the server restarted since the upload. Report from the
    # rows (requeue_unfinished_bulk_rows picked the unfinished ones back up).
    rows = (
        db.query(ResumeMetadata.organization_id, ResumeMetadata.parsing_status, func.count(ResumeMetadata.id))
        .filter(ResumeMetadata.batch_id == batch_id)
        .group_by(ResumeMetadata.organization_id, ResumeMetadata.parsing_status)
        .all()
    )
    if not rows or not all(can_access_org(user, org_id) for org_id, _, _ in rows):
        raise HTTPException(status_code=404, detail="Batch not found")
    counts: dict = {}
    for _, status, n in rows:
        counts[status] = counts.get(status, 0) + n
    unfinished = counts.get("pending", 0) + counts.get("processing", 0)
    return {
        "total": sum(counts.values()),
        "completed": counts.get("completed", 0),
        "failed": counts.get("failed", 0),
        "replaced": 0,
        "status": "processing" if unfinished else "completed",
        "parse_mode": None,
        "recovered": True,
    }


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

    def _cell(value) -> str:
        # Spreadsheet formula injection: names come from uploaded file names and
        # resume contents, and a cell starting with = + - @ runs as a formula
        # when the CSV is opened in Excel (e.g. =HYPERLINK(...) exfiltration).
        text = str(value)
        return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text

    for m in failed:
        filename = os.path.basename(m.resume_file_path) if m.resume_file_path else ""
        writer.writerow([_cell(v) for v in (
            m.id,
            filename,
            m.candidate_name or "",
            m.candidate_email or "",
            m.parsing_error or "Unknown error",
            m.created_at.isoformat() if m.created_at else "",
        )])

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

    # Exclude placeholder records that never parsed. A parsed resume with no
    # usable email keeps its placeholder email and is listed (shown as "—").
    query = query.filter(_NOT_UNPARSED_PLACEHOLDER)

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

    # Base filter: exclude placeholder records that never parsed
    base_filters = [_NOT_UNPARSED_PLACEHOLDER]
    if org_id is not None:
        base_filters.append(ResumeMetadata.organization_id == org_id)

    total = db.query(func.count(ResumeMetadata.id)).filter(*base_filters).scalar() or 0
    completed = db.query(func.count(ResumeMetadata.id)).filter(
        *base_filters, ResumeMetadata.parsing_status == "completed",
    ).scalar() or 0
    # Uploads still parsing and uploads that failed are placeholder rows (no
    # email yet), which base_filters leaves out — count them here, or a bulk
    # upload's failures never show up (and can't be retried or cleaned up).
    org_filter = [ResumeMetadata.organization_id == org_id] if org_id is not None else []
    pending = db.query(func.count(ResumeMetadata.id)).filter(
        *org_filter, ResumeMetadata.parsing_status.in_(["pending", "processing"]),
    ).scalar() or 0
    failed = db.query(func.count(ResumeMetadata.id)).filter(
        *org_filter, ResumeMetadata.parsing_status == "failed",
    ).scalar() or 0

    return {
        "total": total,
        "completed": completed,
        "pending": pending,
        "failed": failed,
    }


# ── Search (filters, facet counts, relevance) ────────────────────────────────

def _clean_values(values: List[str], allowed: Optional[tuple] = None, limit: int = 50) -> List[str]:
    out = []
    for v in values[:limit]:
        v = (v or "").strip()[:100]
        if v and (allowed is None or v in allowed) and v not in out:
            out.append(v)
    return out


@router.get(
    "/search",
    summary="Search the resume database: keywords, filters, facet counts and relevance",
    description=(
        "`q`: every word must match; \"quoted words\" match as a phrase; a leading "
        "minus excludes (-intern). Repeat list parameters for several values "
        "(`city=Noida&city=Pune`). Facet counts are disjunctive: each section's "
        "counts apply every other active filter."
    ),
)
def search_resume_database(
    q: str = Query("", max_length=500),
    location: str = Query("", max_length=200),
    exp_min: Optional[float] = Query(None, ge=0, le=60),
    exp_max: Optional[float] = Query(None, ge=0, le=60),
    ctc: List[str] = Query([]),
    notice: List[str] = Query([]),
    city: List[str] = Query([]),
    relocate: bool = False,
    skills: List[str] = Query([]),
    skills_mode: str = Query("all", pattern="^(all|any)$"),
    certs: List[str] = Query([]),
    education: List[str] = Query([]),
    freshness: Optional[int] = Query(None, ge=1, le=3650, description="Resume parsed within N days"),
    pipeline: List[str] = Query([]),
    source: List[str] = Query([]),
    quality: Optional[str] = Query(None, pattern="^(high|medium|low)$"),
    sort: str = Query("relevance"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    facets: bool = True,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    params = resume_search.SearchParams(
        q=q,
        location=location,
        exp_min=exp_min,
        exp_max=exp_max,
        ctc=_clean_values(ctc, tuple(k for k, *_ in resume_search.CTC_BUCKETS) + (resume_search.NA,)),
        notice=_clean_values(notice, tuple(k for k, *_ in resume_search.NOTICE_BUCKETS) + (resume_search.NA,)),
        city=_clean_values(city),
        relocate=relocate,
        skills=_clean_values(skills, limit=20),
        skills_mode=skills_mode,
        certs=_clean_values(certs, limit=20),
        education=_clean_values(education, tuple(k for k, _ in resume_search.EDUCATION) + (resume_search.NA,)),
        freshness_days=freshness,
        pipeline=_clean_values(pipeline, resume_search.PIPELINE_STATUSES),
        source=_clean_values(source, tuple(k for k, _ in resume_search.SOURCES)),
        quality=quality,
        sort=sort if sort in resume_search.SORTS else "relevance",
        page=page,
        page_size=page_size,
        include_facets=facets,
    )
    return resume_search.search(db, get_org_id(user), params)


# ── Pipeline status (Shortlist, Contacted, ...) ──────────────────────────────

class PipelineStatusUpdate(BaseModel):
    status: str


class BulkPipelineStatusUpdate(BaseModel):
    ids: List[int] = Field(..., min_length=1, max_length=500)
    status: str


class BulkResumeIds(BaseModel):
    ids: List[int] = Field(..., min_length=1, max_length=500)


def _check_pipeline_status(status: str) -> str:
    if status not in resume_search.PIPELINE_STATUSES:
        raise HTTPException(status_code=422, detail=f"Unknown status. Use one of: {', '.join(resume_search.PIPELINE_STATUSES)}")
    return status


def _org_scoped_ids(db: Session, user: User, ids: List[int]):
    query = db.query(ResumeMetadata).filter(ResumeMetadata.id.in_(ids))
    org_id = get_org_id(user)
    if org_id is not None:
        query = query.filter(ResumeMetadata.organization_id == org_id)
    return query


@router.post("/bulk/pipeline-status", summary="Set the pipeline status of several resumes")
def bulk_set_pipeline_status(
    body: BulkPipelineStatusUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(_require_resume_admin),
):
    status = _check_pipeline_status(body.status)
    updated = _org_scoped_ids(db, user, body.ids).update(
        {"pipeline_status": status, "updated_at": datetime.utcnow()}, synchronize_session=False
    )
    db.commit()
    return {"updated": updated, "pipeline_status": status}


@router.post("/bulk/delete", summary="Delete several resume records and their files")
def bulk_delete_resumes(
    body: BulkResumeIds,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    user: User = Depends(_require_resume_admin),
):
    from app.models.candidate import Candidate

    _check_resume_db_permission(user, db)
    rows = _org_scoped_ids(db, user, body.ids).all()
    ids = [m.id for m in rows]
    if ids:
        db.query(Candidate).filter(Candidate.resume_metadata_id.in_(ids)).update(
            {"resume_metadata_id": None}, synchronize_session="fetch"
        )
    file_paths = [m.resume_file_path for m in rows if m.resume_file_path]
    for meta in rows:
        db.delete(meta)
    db.commit()
    # Files go after the response, and only those nothing else references.
    background_tasks.add_task(_remove_unused_resume_files, file_paths)
    return {"deleted": len(ids)}


@router.patch("/{meta_id}/pipeline-status", summary="Set one resume's pipeline status (e.g. shortlist)")
def set_pipeline_status(
    meta_id: int,
    body: PipelineStatusUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(_require_resume_admin),
):
    status = _check_pipeline_status(body.status)
    meta = _org_scoped_ids(db, user, [meta_id]).first()
    if not meta:
        raise HTTPException(status_code=404, detail="Resume not found")
    meta.pipeline_status = status
    meta.updated_at = datetime.utcnow()
    db.commit()
    return {"id": meta.id, "pipeline_status": meta.pipeline_status}


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

    # Fields sent as null are cleared (e.g. a notice period set back to "Not
    # stated"); fields left out are untouched.
    update_data = body.model_dump(exclude_unset=True)

    # skills is stored as a JSON string in the DB
    if "skills" in update_data:
        update_data["skills"] = json.dumps(update_data["skills"] or [])

    for field, value in update_data.items():
        setattr(meta, field, value)

    meta.updated_at = datetime.utcnow()
    mark_recruiter_edit(db)
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


# ── Retry failed (after a reader fix, e.g. legacy .doc support) ───────────────

def _reset_failed_rows(org_id: Optional[int], batch_id: str) -> Tuple[list, int]:
    """Failed rows whose file is still on disk -> pending, in a new batch, each
    in the parse mode it was uploaded with. Returns (queue items, files gone)."""
    db = SessionLocal()
    try:
        query = db.query(ResumeMetadata).filter(
            ResumeMetadata.parsing_status == "failed",
            ResumeMetadata.resume_file_path.isnot(None),
        )
        if org_id is not None:
            query = query.filter(ResumeMetadata.organization_id == org_id)
        items, missing = [], 0
        for meta in query.order_by(ResumeMetadata.id).all():
            if not resolve_upload_file(meta.resume_file_path, ("resumes",)):
                missing += 1
                continue
            mode = meta.parse_mode if meta.parse_mode in ("fast", "ai") else "fast"
            meta.parsing_status = "pending"
            meta.parsing_error = None
            meta.batch_id = batch_id
            meta.parse_mode = mode
            items.append((meta.id, meta.resume_file_path, batch_id, mode))
        db.commit()
        return items, missing
    finally:
        db.close()


@router.post(
    "/retry-failed",
    summary="Read every failed resume again",
    description=(
        "Re-queues failed resumes whose file is still stored — useful after the "
        "readers improve (legacy Word .doc, damaged DOCX/PDF). Each is parsed in "
        "the mode it was uploaded with. Track it with GET /batch/{batch_id}."
    ),
)
async def retry_failed_resumes(
    db: Session = Depends(get_db),
    user: User = Depends(_require_resume_admin),
):
    _check_resume_db_permission(user, db)
    _ensure_bulk_worker()
    batch_id = uuid.uuid4().hex[:12]
    items, missing = await asyncio.to_thread(_reset_failed_rows, get_org_id(user), batch_id)
    if items:
        modes = {mode for *_, mode in items}
        _new_batch(batch_id, len(items), modes.pop() if len(modes) == 1 else "mixed", user.organization_id)
        for item in items:
            _bulk_queue.put_nowait(item)
    return {"batch_id": batch_id if items else None, "queued": len(items), "missing_files": missing}


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
        # Step 1: Fast regex parse — off the event loop, so a large or hostile
        # resume can't stall every other request while it parses.
        result = await asyncio.to_thread(fast_parse_resume, meta.raw_text)

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
            regex_result = await asyncio.to_thread(fast_parse_resume, meta.raw_text)
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
    # The whole body is read into memory: refuse oversized imports up front.
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > _MAX_IMPORT_BODY_BYTES:
        raise HTTPException(status_code=413, detail="Import is too large. Split it into smaller files.")
    try:
        body = await request.json()
    except ValueError:
        raise HTTPException(status_code=400, detail="Expected a JSON array of resume records")
    if not isinstance(body, list):
        raise HTTPException(status_code=400, detail="Expected a JSON array of resume records")
    if len(body) > _MAX_IMPORT_RECORDS:
        raise HTTPException(
            status_code=413, detail=f"Import at most {_MAX_IMPORT_RECORDS} records at a time."
        )
    for record in body:
        # raw_text feeds the regex parser and LLM re-parsing; a resume is a
        # few thousand characters, not megabytes.
        if isinstance(record, dict) and isinstance(record.get("raw_text"), str):
            record["raw_text"] = record["raw_text"][:_MAX_IMPORT_RAW_TEXT]

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
                source="import",
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
    problem = resume_upload_problem(file)
    if problem:
        return {"status": "skipped", "reason": problem}
    file_path = os.path.join(RESUME_UPLOAD_DIR, safe_name)
    try:
        await asyncio.to_thread(store_resume_upload, file, file_path)
    except UnsafeFile as exc:
        return {"status": "skipped", "reason": exc.reason}

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
            source="import",
        )
        db.add(meta)
        db.commit()
    except Exception as exc:
        logger.error("Import with file failed for %s: %s", email, exc)
        db.rollback()
        # The row was not stored, so its file must not linger
        safe_unlink_upload(file_path, ("resumes",))
        raise HTTPException(status_code=500, detail="Import failed. Please try again.")
    schedule_compression(file_path)
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
        "pipeline_status": meta.pipeline_status or "new",
        "source": meta.source,
        "current_company": meta.current_company,
        "city": meta.city,
        "current_ctc": meta.annual_package if meta.annual_package is not None else meta.current_ctc,
        "expected_ctc": meta.expected_ctc,
        "notice_period_days": meta.notice_period_days,
        "open_to_relocate": meta.open_to_relocate,
        "highest_education": meta.highest_education,
        "parse_quality": meta.parse_quality,
        "recruiter_fields": _parse_json(meta.recruiter_fields),
        "parsing_error": meta.parsing_error,
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
