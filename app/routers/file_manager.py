"""
File Manager router — browse and delete files in backend upload directories.
Super Admin only.
"""
import os
import logging
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.auth import require_roles
from app.database import get_db
from app.media import UPLOAD_ROOT, sign_media_url
from app.models.user import UserRole

logger = logging.getLogger(__name__)

router = APIRouter(
    dependencies=[Depends(require_roles(UserRole.super_admin))],
)

_UPLOAD_ROOT = UPLOAD_ROOT  # app.media is the single source of truth

ALLOWED_FOLDERS = {"jd", "photos", "recordings", "resumes", "screenshots"}


# ── Schemas ─────────────────────────────────────────────────────────────────

class FileInfo(BaseModel):
    name: str
    path: str  # relative URL path e.g. /uploads/jd/file.pdf
    size: int  # bytes
    modified: str  # ISO timestamp
    is_dir: bool
    # Signed /media URL for viewing; None for files that are never served
    # (e.g. jd, recording temp/sidecar files)
    url: Optional[str] = None


class FileListResponse(BaseModel):
    folder: str
    files: List[FileInfo]
    total: int
    page: int
    page_size: int
    has_more: bool


class BulkDeleteRequest(BaseModel):
    folder: str
    filenames: List[str]


# ── Helpers ─────────────────────────────────────────────────────────────────

def _validate_folder(folder: str) -> str:
    """Validate folder name and return the absolute directory path."""
    if folder not in ALLOWED_FOLDERS:
        raise HTTPException(status_code=400, detail=f"Invalid folder: {folder}")
    dir_path = os.path.join(_UPLOAD_ROOT, folder)
    os.makedirs(dir_path, exist_ok=True)
    return dir_path


def _resolve_in_folder(dir_path: str, name: str) -> Optional[str]:
    """Absolute path for a folder-relative name ("file.pdf", or for screenshots
    "{session_id}/file.jpg"), or None unless it resolves strictly inside the
    folder. Absolute paths, drive letters, empty parts and '..' are refused."""
    if not name or "\x00" in name:
        return None
    parts = name.replace("\\", "/").split("/")
    if any(p in ("", ".", "..") for p in parts):
        return None
    root = os.path.realpath(dir_path)
    full = os.path.realpath(os.path.join(root, *parts))
    try:
        if full == root or os.path.commonpath([root, full]) != root:
            return None
    except ValueError:  # different drives on Windows
        return None
    return full


def _collect_files(dir_path: str, folder: str) -> List[FileInfo]:
    """Walk a directory (including subdirectories) and return all files."""
    files: List[FileInfo] = []

    for root, dirs, filenames in os.walk(dir_path):
        for fname in filenames:
            full_path = os.path.join(root, fname)
            try:
                stat = os.stat(full_path)
            except OSError:
                continue

            # Build relative path from the folder root for URL
            rel = os.path.relpath(full_path, _UPLOAD_ROOT).replace(os.sep, "/")
            url_path = f"/uploads/{rel}"

            files.append(FileInfo(
                name=fname,
                path=url_path,
                size=stat.st_size,
                modified=datetime.fromtimestamp(stat.st_mtime).isoformat(),
                is_dir=False,
            ))

    # Sort newest first
    files.sort(key=lambda f: f.modified, reverse=True)
    return files


# ── Endpoints ───────────────────────────────────────────────────────────────

# Declared BEFORE /files/{folder}: routes match in order, so with the dynamic
# route first every call to /files/stats was answered by list_files as
# "Invalid folder: stats" (400) and this endpoint was unreachable.
@router.get("/files/stats")
def folder_stats():
    """Return file count and total size for each upload folder."""
    stats = []
    for folder in sorted(ALLOWED_FOLDERS):
        dir_path = os.path.join(_UPLOAD_ROOT, folder)
        if not os.path.isdir(dir_path):
            stats.append({"folder": folder, "file_count": 0, "total_size": 0})
            continue

        file_count = 0
        total_size = 0
        for root, _, filenames in os.walk(dir_path):
            for fname in filenames:
                file_count += 1
                try:
                    total_size += os.path.getsize(os.path.join(root, fname))
                except OSError:
                    pass

        stats.append({"folder": folder, "file_count": file_count, "total_size": total_size})

    return stats


@router.get("/files/{folder}", response_model=FileListResponse)
def list_files(folder: str, page: int = 1, page_size: int = 50):
    """List files in a given upload folder with pagination."""
    dir_path = _validate_folder(folder)
    all_files = _collect_files(dir_path, folder)
    total = len(all_files)

    start = (page - 1) * page_size
    end = start + page_size
    page_files = all_files[start:end]
    # Sign only the page being returned (router is super-admin only)
    for f in page_files:
        f.url = sign_media_url(f.path)

    return FileListResponse(
        folder=folder,
        files=page_files,
        total=total,
        page=page,
        page_size=page_size,
        has_more=end < total,
    )


@router.delete("/files/{folder}/{filename:path}")
def delete_file(folder: str, filename: str):
    """Delete a single file from an upload folder."""
    dir_path = _validate_folder(folder)
    # Same resolver as bulk-delete, and `{filename:path}` so nested names match:
    # basename-only handling meant deleting one screenshot ("{session_id}/x.jpg")
    # always 404'd, while still being the place traversal had to be stopped.
    file_path = _resolve_in_folder(dir_path, filename)
    if not file_path or not os.path.isfile(file_path):
        raise HTTPException(status_code=404, detail="File not found")

    os.remove(file_path)
    logger.info("File deleted: %s/%s", folder, filename)
    return {"deleted": filename}


@router.post("/files/bulk-delete")
def bulk_delete_files(body: BulkDeleteRequest):
    """Delete multiple files from an upload folder."""
    dir_path = _validate_folder(body.folder)
    deleted = []
    not_found = []

    for filename in body.filenames:
        # Names are relative to the folder ("file.pdf", or for screenshots
        # "{session_id}/file.jpg") and must resolve inside it — the old
        # fallback joined the raw name, so "../../app/main.py" escaped.
        file_path = _resolve_in_folder(dir_path, filename)
        if not file_path or not os.path.isfile(file_path):
            not_found.append(filename)
            continue

        try:
            os.remove(file_path)
            deleted.append(filename)
        except OSError as e:
            logger.warning("Failed to delete %s: %s", file_path, e)
            not_found.append(filename)

    logger.info("Bulk delete in %s: %d deleted, %d not found", body.folder, len(deleted), len(not_found))
    return {"deleted": deleted, "not_found": not_found, "deleted_count": len(deleted)}
