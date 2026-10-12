"""
Private media: upload paths, safe file handling and short-lived signed URLs.

Uploaded interview recordings, candidate photos, proctoring screenshots and
resumes used to be served by a PUBLIC StaticFiles mount at /uploads, with
sequential names (/uploads/recordings/25.webm) — anyone could download every
tenant's videos. Now:

  * Nothing under the upload root is publicly routable.
  * An authorized API response (the caller already passed app.authz checks)
    carries a signed URL: /media/<category>/<file>?exp=…&sig=…
  * GET/HEAD /media/... verifies the HMAC and expiry and streams the file
    (FileResponse supports Range, so <video> seeking works).

Signed URLs, not headers, because every consumer is an <img>/<video>/<iframe>
/<a href> load that cannot attach the Bearer token. Expiry is rounded up to the
hour so a page that re-polls gets the SAME URL (no video reload mid-playback,
browser caching keeps working).

Stored DB values are never rewritten: `image` / `recording_link` /
`InterviewScreenshot.file_path` stay "/uploads/..." and `resume_path` /
`resume_file_path` stay disk paths. Signing happens only when building a
response, into NEW fields.
"""
import asyncio
import base64
import hashlib
import hmac
import logging
import os
import re
import shutil
import subprocess
import time
import uuid
from functools import lru_cache
from typing import Iterable, Optional, Tuple
from urllib.parse import quote, urlencode

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from app.config import settings

logger = logging.getLogger(__name__)

IS_SERVERLESS = bool(os.environ.get("VERCEL") or os.environ.get("AWS_LAMBDA_FUNCTION_NAME"))
UPLOAD_ROOT = "/tmp/uploads" if IS_SERVERLESS else "uploads"

# category -> {extension: content type}. Only these are ever served.
MEDIA_TYPES = {
    "recordings": {".webm": "video/webm", ".mp4": "video/mp4"},
    "photos": {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"},
    "screenshots": {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"},
    "resumes": {
        ".pdf": "application/pdf",
        ".doc": "application/msword",
        ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    },
}

# File-name rules per category. Recordings and photos are named by numeric id
# by the server; anything else in those folders (.incoming-*, .fixed.webm,
# .assembling-*, .parts/, .mp4_cache/) is a temp/sidecar and never servable.
_NAME_RULES = {
    "recordings": re.compile(r"^\d+\.(webm|mp4)$", re.IGNORECASE),
    "photos": re.compile(r"^\d+\.(jpg|jpeg|png|webp)$", re.IGNORECASE),
    "screenshots": re.compile(r"^[A-Za-z0-9_\-]+\.(jpg|jpeg|png|webp)$", re.IGNORECASE),
    "resumes": re.compile(r"^[^./\\\x00-\x1f][^/\\\x00-\x1f]*\.(pdf|doc|docx)$", re.IGNORECASE),
}

_STORED_PREFIXES = ("/tmp/uploads/", "tmp/uploads/", "./uploads/", "/uploads/", "uploads/")

MEDIA_URL_TTL_SECONDS = 6 * 3600
_EXPIRY_BUCKET_SECONDS = 3600


# ── Path handling ────────────────────────────────────────────────────────────

def to_upload_relpath(stored: Optional[str]) -> Optional[str]:
    """Normalize any stored form of an uploaded file's path to
    "<category>/<file>" (or "screenshots/<session>/<file>"), or None when the
    value is not a servable upload (URLs, placeholders, temp files, traversal)."""
    if not stored or not isinstance(stored, str):
        return None
    s = stored.strip().replace("\\", "/")
    if "\x00" in s or "?" in s or "#" in s:
        return None
    for prefix in _STORED_PREFIXES:
        if s.startswith(prefix):
            s = s[len(prefix):]
            break
    else:
        return None
    parts = s.split("/")
    if any(p in ("", ".", "..") for p in parts):
        return None
    category = parts[0]
    rule = _NAME_RULES.get(category)
    if rule is None:
        return None
    if category == "screenshots":
        if len(parts) != 3 or not parts[1].isdigit():
            return None
    elif len(parts) != 2:
        return None
    if not rule.match(parts[-1]) or ".incoming" in parts[-1]:
        return None
    return "/".join(parts)


def upload_abspath(rel: str) -> Optional[str]:
    """Absolute path for a normalized relpath, contained inside UPLOAD_ROOT."""
    root = os.path.realpath(UPLOAD_ROOT)
    full = os.path.realpath(os.path.join(root, *rel.split("/")))
    try:
        if os.path.commonpath([root, full]) != root:
            return None
    except ValueError:  # different drives on Windows
        return None
    return full


def resolve_upload_file(stored: Optional[str], categories: Iterable[str]) -> Optional[str]:
    """Existing absolute file for a stored path in one of `categories`, else None.
    Use before reading a DB/client-supplied path (e.g. text extraction)."""
    rel = to_upload_relpath(stored)
    if not rel or rel.split("/", 1)[0] not in set(categories):
        return None
    full = upload_abspath(rel)
    return full if full and os.path.isfile(full) else None


def safe_unlink_upload(stored: Optional[str], categories: Iterable[str]) -> bool:
    """Delete an uploaded file ONLY if it resolves inside the upload root under
    one of `categories`. Returns True if a file was removed. Never raises.

    Callers deleting resumes should also check resume_file_in_use() first: the
    same file can be referenced by several rows (hire, add-matched, apply)."""
    full = resolve_upload_file(stored, categories)
    if not full:
        return False
    try:
        os.remove(full)
        return True
    except OSError as exc:
        logger.warning("Could not delete upload %s: %s", full, exc)
        return False


def resume_file_in_use(db, stored: Optional[str], *, exclude_candidate_id: Optional[int] = None,
                       exclude_meta_id: Optional[int] = None) -> bool:
    """True when another Candidate or ResumeMetadata row still references the
    same resume file (compared after normalization)."""
    rel = to_upload_relpath(stored)
    if not rel:
        return False
    from app.models.candidate import Candidate
    from app.models.resume_metadata import ResumeMetadata

    base = rel.rsplit("/", 1)[-1]
    q1 = db.query(Candidate.id, Candidate.resume_path).filter(
        Candidate.resume_path.endswith(base, autoescape=True)
    )
    if exclude_candidate_id is not None:
        q1 = q1.filter(Candidate.id != exclude_candidate_id)
    if any(to_upload_relpath(p) == rel for _, p in q1.all()):
        return True
    q2 = db.query(ResumeMetadata.id, ResumeMetadata.resume_file_path).filter(
        ResumeMetadata.resume_file_path.endswith(base, autoescape=True)
    )
    if exclude_meta_id is not None:
        q2 = q2.filter(ResumeMetadata.id != exclude_meta_id)
    return any(to_upload_relpath(p) == rel for _, p in q2.all())


def safe_upload_name(filename: Optional[str], allowed_exts: Iterable[str],
                     default_ext: Optional[str] = None) -> Optional[Tuple[str, str]]:
    """(stem, ext) for a client-supplied file name: directory parts stripped,
    unsafe characters replaced, extension lower-cased and restricted to
    `allowed_exts` (falls back to `default_ext`, else None when not allowed)."""
    base = os.path.basename((filename or "").replace("\\", "/"))
    stem, ext = os.path.splitext(base)
    ext = ext.lower()
    allowed = {e.lower() for e in allowed_exts}
    if ext not in allowed:
        if default_ext is None:
            return None
        ext = default_ext.lower()
    stem = re.sub(r"[^A-Za-z0-9_\-]+", "_", stem).strip("_-")[:80] or "file"
    return stem, ext


# Leading bytes of the accepted resume formats: PDF, DOCX (a zip), legacy DOC (OLE).
RESUME_MAGIC = (b"%PDF", b"PK\x03\x04", b"\xd0\xcf\x11\xe0")


def resume_upload_problem(upload) -> Optional[str]:
    """Why an uploaded resume (a Starlette UploadFile) must be refused, or None.
    Checks the spooled size against MAX_UPLOAD_SIZE_MB and the file's real
    leading bytes — the client-declared content type was the only check on
    several upload routes, and some had no size cap at all."""
    max_bytes = settings.MAX_UPLOAD_SIZE_MB * 1024 * 1024
    if upload.size is not None and upload.size > max_bytes:
        return f"file exceeds the {settings.MAX_UPLOAD_SIZE_MB}MB limit"
    try:
        head = upload.file.read(8)
        upload.file.seek(0)
    except Exception:
        return "file could not be read"
    if not head.startswith(RESUME_MAGIC):
        return "not a PDF, DOC or DOCX file"
    return None


def client_resume_path(stored: Optional[str]) -> Optional[str]:
    """Validate a resume path echoed back by a client (the candidate apply
    flow): it must name an existing file directly in the resumes folder.
    Returns the canonical stored form, else None."""
    rel = to_upload_relpath(stored)
    if not rel or not rel.startswith("resumes/"):
        return None
    full = upload_abspath(rel)
    if not full or not os.path.isfile(full):
        return None
    return os.path.join(UPLOAD_ROOT, "resumes", rel.split("/", 1)[1])


# ── Signed URLs ──────────────────────────────────────────────────────────────

def _signing_key() -> bytes:
    raw = settings.MEDIA_SIGNING_KEY or settings.SECRET_KEY
    return hashlib.sha256(b"anytimehire:media-url:v1\x00" + raw.encode("utf-8")).digest()


def _signature(rel: str, exp: int, download: bool = False, fmt: Optional[str] = None) -> str:
    """Bound to the delivery options as well as path and expiry: an inline
    viewing link must not also authorize ?format=mp4, which runs ffmpeg for up
    to 10 minutes and fills the mp4 cache."""
    payload = f"{rel}\n{exp}\n{int(bool(download))}\n{fmt or ''}"
    mac = hmac.new(_signing_key(), payload.encode("utf-8"), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(mac).rstrip(b"=").decode("ascii")


def sign_media_url(stored: Optional[str], *, download: bool = False, fmt: Optional[str] = None) -> Optional[str]:
    """API-relative signed URL for a stored upload path, or None when the value
    is not a servable upload. ONLY call this after the caller has been
    authorized for the object that owns the file."""
    rel = to_upload_relpath(stored)
    if not rel:
        return None
    now = int(time.time())
    exp = ((now + MEDIA_URL_TTL_SECONDS) // _EXPIRY_BUCKET_SECONDS + 1) * _EXPIRY_BUCKET_SECONDS
    params = {"exp": exp, "sig": _signature(rel, exp, download, fmt)}
    if download:
        params["dl"] = 1
    if fmt:
        params["format"] = fmt
    return f"/media/{quote(rel)}?{urlencode(params)}"


def verify_media_signature(rel: str, exp: int, sig: str, download: bool = False,
                           fmt: Optional[str] = None) -> bool:
    now = time.time()
    if exp <= now or exp > now + MEDIA_URL_TTL_SECONDS + 2 * _EXPIRY_BUCKET_SECONDS:
        return False
    return hmac.compare_digest(_signature(rel, exp, download, fmt), sig or "")


# ── MP4 conversion (recordings) ──────────────────────────────────────────────

@lru_cache(maxsize=1)
def find_ffmpeg() -> Optional[str]:
    """System ffmpeg first, then the bundled imageio-ffmpeg binary."""
    path = shutil.which("ffmpeg")
    if path:
        return path
    try:
        import imageio_ffmpeg

        path = imageio_ffmpeg.get_ffmpeg_exe()
        if path and os.path.isfile(path):
            return path
    except Exception:
        pass
    return None


_MP4_CONVERSIONS = asyncio.Semaphore(2)


async def _mp4_for(src: str) -> str:
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        raise HTTPException(status_code=501, detail="MP4 conversion unavailable — ffmpeg is not installed on the server.")
    cache_dir = os.path.join(UPLOAD_ROOT, "recordings", ".mp4_cache")
    os.makedirs(cache_dir, exist_ok=True)
    stem = os.path.splitext(os.path.basename(src))[0]
    cached = os.path.join(cache_dir, f"{stem}.mp4")

    def _fresh() -> bool:
        return os.path.isfile(cached) and os.path.getmtime(cached) >= os.path.getmtime(src)

    if _fresh():
        return cached
    async with _MP4_CONVERSIONS:
        if _fresh():
            return cached
        tmp = f"{cached}.{uuid.uuid4().hex}.tmp"
        try:
            proc = await asyncio.to_thread(
                subprocess.run,
                [ffmpeg, "-y", "-i", src,
                 "-c:v", "libx264", "-preset", "fast", "-crf", "23",
                 "-c:a", "aac", "-b:a", "128k",
                 "-movflags", "+faststart", "-f", "mp4", tmp],
                capture_output=True, timeout=600,
            )
            if proc.returncode != 0:
                logger.error("ffmpeg conversion failed: %s", proc.stderr.decode(errors="replace")[-500:])
                raise HTTPException(status_code=500, detail="MP4 conversion failed")
            os.replace(tmp, cached)
        except subprocess.TimeoutExpired:
            raise HTTPException(status_code=504, detail="Conversion timed out (video too long)")
        finally:
            if os.path.isfile(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass
    return cached


# ── Route ────────────────────────────────────────────────────────────────────

router = APIRouter(tags=["Media"])

_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Cache-Control": "private, max-age=3600",
    "Referrer-Policy": "no-referrer",
}


# Not in the OpenAPI schema: it is reached only through signed links, and a
# GET+HEAD route there logged "Duplicate Operation ID serve_media..." on boot.
@router.api_route("/media/{rel:path}", methods=["GET", "HEAD"], summary="Serve a signed upload", include_in_schema=False)
async def serve_media(rel: str, exp: int = 0, sig: str = "", dl: int = 0, format: str = "original"):
    canonical = to_upload_relpath("uploads/" + rel)
    signed_fmt = format if format and format != "original" else None
    if not canonical or canonical != rel or not verify_media_signature(
        canonical, exp, sig, download=bool(dl), fmt=signed_fmt
    ):
        raise HTTPException(
            status_code=403,
            detail="This media link is invalid or has expired. Reload the page to get a fresh link.",
        )
    full = upload_abspath(canonical)
    if not full or not os.path.isfile(full):
        raise HTTPException(status_code=404, detail="File not found")

    category = canonical.split("/", 1)[0]
    ext = os.path.splitext(full)[1].lower()
    filename = os.path.basename(full)
    media_type = MEDIA_TYPES[category][ext]
    if format == "mp4" and category == "recordings" and ext == ".webm":
        full = await _mp4_for(full)
        media_type = "video/mp4"
        filename = os.path.splitext(filename)[0] + ".mp4"

    if dl:
        return FileResponse(full, media_type=media_type, filename=filename, headers=dict(_SECURITY_HEADERS))
    return FileResponse(full, media_type=media_type, headers=dict(_SECURITY_HEADERS))
