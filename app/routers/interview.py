import asyncio
import hmac
import json
import logging
import os
import secrets
import shutil
import subprocess
import time
import traceback
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, Request, UploadFile, status
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.candidate import Candidate
from app.models.interview import (
    CandidateFeedback,
    CodingSubmission,
    Evaluation,
    InterviewMessage,
    InterviewScreenshot,
    InterviewSession,
    InterviewStatusLog,
    InterviewToken,
    MeetingSchedule,
    MeetingSlot,
    SessionStatus,
    Violation,
    ViolationType,
)
from app.models.job import Job
from app.models.organization import Organization
from app.models.user import User, UserRole
from app.database import SessionLocal
from app.auth import get_current_user, require_roles
from app.authz import authorize_session
from app.invites import email_access_allowed
from app.media import UPLOAD_ROOT, client_resume_path, safe_upload_name
from app.session_auth import (
    require_session_token,
    require_session_token_or_query,
    session_token_matches,
    stored_session_token,
)


def _check_and_increment_candidate_quota(db, job_id: int) -> None:
    """Check lifetime candidate quota and increment counter atomically.

    Raises 403 if exceeded. Uses SELECT ... FOR UPDATE to serialise concurrent
    registrations on the SAME org so two simultaneous requests can't both
    pass the check and overshoot the quota. Without this, a candidate
    double-clicking submit while the org is at limit-minus-one would burn an
    extra quota slot (see audit #12).
    """
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job or not job.organization_id:
        return
    # Row-level lock on the organization for the duration of this transaction.
    # Released on commit/rollback by SQLAlchemy. On SQLite (tests) FOR UPDATE
    # is a no-op — that's fine, tests don't exercise concurrency.
    org = (
        db.query(Organization)
        .filter(Organization.id == job.organization_id)
        .with_for_update()
        .first()
    )
    if not org or (org.max_candidates_per_job or 0) == 0:
        return
    if (org.total_candidates_used or 0) >= org.max_candidates_per_job:
        raise HTTPException(
            status_code=403,
            detail="You have exceeded the candidate limit. Please upgrade your plan.",
        )
    org.total_candidates_used = (org.total_candidates_used or 0) + 1
    db.flush()
from app.schemas.interview import (
    ApplyForJobRequest,
    ApplyForJobResponse,
    CandidateEvaluationRead,
    EvaluationRead,
    FeedbackRequest,
    FeedbackResponse,
    InterviewMessageRequest,
    InterviewMessageResponse,
    JobConfig,
    MeetingSlotRead,
    RecordingUploadResponse,
    ResumeSessionResponse,
    ScheduleMeetingRequest,
    ScheduleMeetingResponse,
    VerifyAccessRequest,
    VerifyAccessResponse,
    ViolationRequest,
    ViolationResponse,
)
from app.services import avatar_interview_service as svc
from app.services.file_compression import schedule_compression
from app.services.file_safety import UnsafeFile, store_resume_upload
from slowapi import Limiter
from slowapi.util import get_remote_address

def _client_ip(request: Request) -> str:
    """Real client IP for rate limiting. Behind a proxy/LB (Heroku, Vercel,
    nginx) request.client.host is the proxy's IP, so every candidate shared
    ONE rate-limit bucket — a handful of concurrent interviews tripped the
    10/min message limit and everyone got 429s. Prefer X-Forwarded-For."""
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return get_remote_address(request)


limiter = Limiter(key_func=_client_ip)

router = APIRouter()

VIOLATION_TERMINATE_THRESHOLD = 5

# Cache whether the configured Deepgram key can mint scoped grant tokens.
# None = not probed yet; True = supported; False = not supported (use master key).
# Avoids a doomed /v1/auth/grant round-trip on every STT (re)connect.
_DG_GRANT_SUPPORTED: Optional[bool] = None


# ── Helpers ───────────────────────────────────────────────────────────────────

def _get_token_or_404(token: str, db: Session) -> InterviewToken:
    from datetime import timedelta

    obj = db.query(InterviewToken).filter(
        InterviewToken.token == token,
        InterviewToken.is_active == True,
    ).first()
    if not obj:
        raise HTTPException(status_code=404, detail="Interview link not found or no longer active")

    # Check if token has expired
    if obj.expires_at and obj.expires_at < datetime.utcnow():
        # Before rejecting, check if any candidate for this job still has a
        # "Scheduled" status (meaning they haven't taken the interview yet).
        # This covers:
        #   - Future interviews (scheduled for later today or tomorrow)
        #   - Interviews scheduled for "right now" (within the last few hours)
        #   - Candidates who were scheduled but haven't started yet
        # We give a generous 24-hour grace window past the scheduled time.
        has_pending_candidate = (
            db.query(Candidate)
            .filter(
                Candidate.job_id == obj.job_id,
                Candidate.interview_status == "Scheduled",
            )
            .first()
        )
        if has_pending_candidate:
            # Extend token to cover all scheduled interviews + 24h buffer
            latest_scheduled = (
                db.query(func.max(Candidate.interview_date))
                .filter(
                    Candidate.job_id == obj.job_id,
                    Candidate.interview_status == "Scheduled",
                    Candidate.interview_date != None,
                )
                .scalar()
            )
            new_expiry = datetime.utcnow() + timedelta(hours=24)
            if latest_scheduled:
                new_expiry = max(new_expiry, latest_scheduled + timedelta(hours=24))
            obj.expires_at = new_expiry
            db.commit()
        else:
            raise HTTPException(status_code=410, detail="This interview link has expired")
    return obj


def _get_session_or_404(session_id: int, db: Session) -> InterviewSession:
    obj = db.query(InterviewSession).filter(InterviewSession.id == session_id).first()
    if not obj:
        raise HTTPException(status_code=404, detail="Session not found")
    return obj


def _get_or_mint_session_access_token(session: InterviewSession, db: Session) -> str:
    """Return the session's access token, minting one into session_metadata if
    missing. Sent to the candidate by verify-access; required on every
    /interview/sessions/{id}/* route by app.session_auth.require_session_token.

    Fails closed: if a freshly minted token can't be persisted (and no
    concurrent request stored one), raise 503 instead of handing the client a
    token the server doesn't know — every session call would then be refused."""
    existing = stored_session_token(session)
    if existing:
        return existing
    sid = session.id
    try:
        meta = json.loads(session.session_metadata or "{}")
    except Exception:
        meta = {}
    if not isinstance(meta, dict):
        meta = {}
    tok = secrets.token_urlsafe(32)
    meta["access_token"] = tok
    session.session_metadata = json.dumps(meta)
    try:
        db.commit()
        return tok
    except Exception as exc:
        logging.error(f"[SessionAuth] could not persist access token for session {sid}: {exc}")
        try:
            db.rollback()
        except Exception:
            pass
    # A concurrent request may have minted and stored one — use it if so.
    try:
        db.refresh(session)
        stored = stored_session_token(session)
    except Exception:
        stored = None
    if stored:
        return stored
    raise HTTPException(
        status_code=503,
        detail="Could not start your interview session right now. Please try again in a moment.",
    )


# ── Public Endpoints ──────────────────────────────────────────────────────────

def _session_rate_key(request: Request) -> str:
    """Rate-limit bucket keyed on the `session_id` query parameter, falling back
    to the client IP. Candidates behind one shared office IP must not share a
    bucket, or they trip each other's limit mid-interview."""
    sid = (request.query_params.get("session_id") or "").strip()
    if sid.isascii() and sid.isdigit() and len(sid) <= 18:
        return f"session:{int(sid)}"
    return _client_ip(request)


# How long after an interview ends its token may still fetch TTS, so the
# avatar's closing lines are still spoken once /complete has landed.
_TTS_AFTER_END_GRACE_SECONDS = 15 * 60


def _require_speaking_session(session_id: int, request: Request, db: Session) -> InterviewSession:
    """The caller must hold the X-Session-Token of a live (or just-ended)
    session. Same answer for a missing session and a wrong token."""
    session = db.query(InterviewSession).filter(InterviewSession.id == session_id).first() if session_id else None
    if not session or not session_token_matches(session, request.headers.get("x-session-token")):
        raise HTTPException(status_code=403, detail="Invalid or missing session token")
    if session.status in (SessionStatus.pending, SessionStatus.in_progress):
        return session
    ended = session.completed_at
    if ended is not None and (datetime.utcnow() - ended).total_seconds() <= _TTS_AFTER_END_GRACE_SECONDS:
        return session
    raise HTTPException(status_code=403, detail="No active interview session")


# Deepgram token — MUST be before /interview/{token} to avoid wildcard match
@router.get(
    "/interview/deepgram-token",
    summary="Get a short-lived Deepgram STT token for an active session",
    tags=["Interview"],
)
@limiter.limit("30/minute", key_func=_session_rate_key)
async def get_deepgram_token(
    request: Request,
    session_id: int = 0,
    db: Session = Depends(get_db),
):
    """Previously this returned the raw master DEEPGRAM_API_KEY to ANY
    unauthenticated caller — abuse burned the STT quota and live interviews
    lost transcription. Now it:
      1. requires an active interview session and its X-Session-Token,
      2. returns a short-lived scoped token via Deepgram's /v1/auth/grant,
         falling back to the master key only if the grant API itself is down
         (so a Deepgram control-plane outage can't kill live interviews).
    Response: { key, scheme } — scheme is the WS subprotocol ("bearer" for
    grant tokens, "token" for the legacy raw key)."""
    from app.config import settings
    import httpx

    if not settings.DEEPGRAM_API_KEY:
        raise HTTPException(status_code=503, detail="Deepgram STT not configured")

    if not session_id:
        raise HTTPException(status_code=401, detail="session_id is required")
    session = db.query(InterviewSession).filter(InterviewSession.id == session_id).first()
    # Token first, compared in constant time and failing closed (a session with
    # no stored token is refused), with one answer for "no such session" and
    # "wrong token" so ids can't be probed. Only then must it still be active.
    if not session or not session_token_matches(session, request.headers.get("x-session-token")):
        raise HTTPException(status_code=403, detail="Invalid or missing session token")
    if session.status not in (SessionStatus.pending, SessionStatus.in_progress):
        raise HTTPException(status_code=403, detail="No active interview session")

    # Prefer a short-lived SCOPED token (Deepgram /v1/auth/grant) so the raw
    # master key never reaches the browser. BUT many API keys lack the scope to
    # mint grant tokens (they return 403 "Insufficient permissions"); probing on
    # every reconnect then adds a doomed round-trip and latency to live STT.
    # So we probe ONCE per process and cache the verdict in _DG_GRANT_SUPPORTED:
    #   - unknown (None) → try the grant.
    #   - False          → skip straight to the master key (scheme "token").
    # Either way the browser WS subprotocol is [scheme, key]; "token" works with
    # both the master key and grant tokens, "bearer" only with grant tokens.
    global _DG_GRANT_SUPPORTED
    if _DG_GRANT_SUPPORTED is not False:
        try:
            async with httpx.AsyncClient(timeout=6.0) as client:
                res = await client.post(
                    "https://api.deepgram.com/v1/auth/grant",
                    headers={"Authorization": f"Token {settings.DEEPGRAM_API_KEY}"},
                    json={"ttl_seconds": 600},
                )
            if res.status_code == 200:
                access_token = (res.json() or {}).get("access_token")
                if access_token:
                    _DG_GRANT_SUPPORTED = True
                    return {"key": access_token, "scheme": "bearer", "expires_in": 600}
            # Non-200 (e.g. 403 insufficient permissions) → this key can't grant.
            _DG_GRANT_SUPPORTED = False
            logging.info(
                f"[Deepgram] auth/grant unavailable for this key "
                f"({res.status_code}) — using session-gated master key."
            )
        except Exception as exc:
            # Transient control-plane error — don't latch False; just fall back
            # this once and retry the grant on a future request.
            logging.warning(f"[Deepgram] auth/grant errored ({exc}) — falling back to master key")

    # Session-gated master key. Still protected: this endpoint requires an
    # active session (+ its X-Session-Token), so the key is never handed to an
    # anonymous caller the way it was before — but an admitted candidate can
    # still copy a key that never expires. See DEEPGRAM_ALLOW_MASTER_KEY_FALLBACK.
    if not settings.DEEPGRAM_ALLOW_MASTER_KEY_FALLBACK:
        logging.error("[Deepgram] no grant token available and master-key fallback is disabled")
        raise HTTPException(status_code=503, detail="Speech recognition is temporarily unavailable")
    logging.warning(
        "[Deepgram] handing the master API key to session %s — mint-capable key recommended "
        "(set DEEPGRAM_ALLOW_MASTER_KEY_FALLBACK=false once it is)", session_id,
    )
    return {"key": settings.DEEPGRAM_API_KEY, "scheme": "token"}


# Google Cloud TTS — Indian female voice
GOOGLE_TTS_VOICE_MAP = {
    "en": {"languageCode": "en-IN", "name": "en-IN-Wavenet-A", "ssmlGender": "FEMALE"},
    "hi": {"languageCode": "hi-IN", "name": "hi-IN-Wavenet-A", "ssmlGender": "FEMALE"},
}

# Fallback TTS (edge-tts / Microsoft Neural) — same Indian female profile.
# Used whenever Google TTS is unusable (missing key, billing disabled, quota,
# timeout). Silence mid-interview is far worse than a slightly different voice.
EDGE_TTS_VOICE_MAP = {
    "en": "en-IN-NeerjaExpressiveNeural",
    "hi": "hi-IN-SwaraNeural",
}

# Circuit breaker: a 401/403 from Google means a config/billing problem that
# will not fix itself within one interview. Once seen, stop paying the network
# round-trip on every single question and go straight to the fallback voice.
_google_tts_blocked_until: float = 0.0
_GOOGLE_TTS_COOLDOWN_SEC = 900  # 15 min


async def _synthesize_edge_tts(text: str, language: str) -> str:
    """Synthesize speech with edge-tts and return base64-encoded MP3."""
    import base64
    import edge_tts

    voice = EDGE_TTS_VOICE_MAP.get(language, EDGE_TTS_VOICE_MAP["en"])
    comm = edge_tts.Communicate(text, voice, rate="+5%")
    chunks = bytearray()
    async for chunk in comm.stream():
        if chunk["type"] == "audio":
            chunks.extend(chunk["data"])
    if not chunks:
        raise RuntimeError("edge-tts returned no audio")
    return base64.b64encode(bytes(chunks)).decode("ascii")


@router.post(
    "/interview/tts",
    summary="Generate speech audio from text using Google Cloud TTS",
    tags=["Interview"],
)
# The avatar only speaks inside the interview room, after admission, so the
# caller (useGoogleTTS) holds a session token. This used to be open to anyone:
# a script could burn paid Google WaveNet characters (5,000 per call, 300
# calls a minute per spoofable IP) with no job link at all. The ceiling is an
# anti-abuse bound, not a fairness limit — a 429 leaves the avatar silent
# mid-interview — and it is per session, so candidates sharing an office IP
# don't share it.
@limiter.limit("300/minute", key_func=_session_rate_key)
async def generate_tts(
    payload: dict,
    request: Request,
    session_id: int = 0,
    db: Session = Depends(get_db),
):
    """
    Convert text to speech using Google Cloud Text-to-Speech, falling back to
    edge-tts (Microsoft Neural, same Indian female profile) whenever Google is
    unavailable — missing key, billing disabled, quota, or timeout.
    Returns base64-encoded MP3 audio.
    Request: { "text": "...", "language": "en" | "hi" }
    Response: { "audio": "base64-encoded-mp3", "provider": "google" | "edge" }
    """
    global _google_tts_blocked_until
    from app.config import settings
    import httpx

    _require_speaking_session(session_id, request, db)

    text = (payload.get("text") or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="Text is required")

    # Strip markdown / code-formatting so the voice never reads symbols aloud
    # (e.g. `useState` → useState, **bold** → bold, ~~x~~ → x). Defense-in-depth:
    # the frontend also cleans before sending, but doing it here too guarantees
    # clean speech regardless of the client build or any other caller.
    import re as _re
    text = _re.sub(r"```[\s\S]*?```", " ", text)          # fenced code blocks
    text = text.replace("`", "").replace("~~", "")          # inline code / strikethrough
    text = _re.sub(r"\*\*|\*|__|_", "", text)               # bold / italic
    text = _re.sub(r"(?m)^\s{0,3}#{1,6}\s+", "", text)      # markdown headings
    text = _re.sub(r"(?m)^\s{0,3}>\s?", "", text)           # blockquotes
    text = _re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)   # links [text](url) → text
    text = _re.sub(r"[*_~#`]", "", text)                    # any stray markdown chars
    text = _re.sub(r"\s{2,}", " ", text).strip()            # collapse whitespace
    if not text:
        raise HTTPException(status_code=400, detail="Text is required")
    if len(text) > 5000:
        text = text[:5000]

    language = payload.get("language", "en")
    voice = GOOGLE_TTS_VOICE_MAP.get(language, GOOGLE_TTS_VOICE_MAP["en"])

    # ── Primary: Google Cloud TTS ───────────────────────────────────────────
    google_skipped_reason: Optional[str] = None
    if not settings.GOOGLE_TTS_API_KEY:
        google_skipped_reason = "no API key configured"
    elif time.time() < _google_tts_blocked_until:
        google_skipped_reason = "circuit breaker open (auth/billing failure)"

    if google_skipped_reason is None:
        # The key goes in a header, not the query string: httpx logs request
        # URLs at INFO, which printed the key into the backend logs.
        url = "https://texttospeech.googleapis.com/v1/text:synthesize"
        google_headers = {"X-Goog-Api-Key": settings.GOOGLE_TTS_API_KEY}
        body = {
            "input": {"text": text},
            "voice": voice,
            "audioConfig": {
                "audioEncoding": "MP3",
                "speakingRate": 0.95,
                "pitch": 0.0,
                "volumeGainDb": 0.0,
            },
        }
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                response = await client.post(url, json=body, headers=google_headers)

            if response.status_code == 200:
                audio = (response.json() or {}).get("audioContent", "")
                if audio:
                    return {"audio": audio, "provider": "google"}
                logging.error("[GoogleTTS] 200 with empty audioContent — falling back")
            else:
                logging.error(
                    f"[GoogleTTS] API error {response.status_code}: {response.text[:300]}"
                )
                # 401/403 = bad key, API not enabled, or billing disabled. Not
                # transient: stop hammering Google for the cooldown window.
                if response.status_code in (401, 403):
                    _google_tts_blocked_until = time.time() + _GOOGLE_TTS_COOLDOWN_SEC
                    logging.error(
                        "[GoogleTTS] auth/billing failure — using fallback voice for "
                        f"the next {_GOOGLE_TTS_COOLDOWN_SEC}s"
                    )
        except httpx.TimeoutException:
            logging.error("[GoogleTTS] timeout — falling back")
        except Exception as e:
            logging.error(f"[GoogleTTS] Failed: {e} — falling back")
    else:
        logging.warning(f"[GoogleTTS] skipped ({google_skipped_reason}) — using fallback voice")

    # ── Fallback: edge-tts (free, no billing, same Indian female profile) ────
    try:
        audio = await _synthesize_edge_tts(text, language)
        return {"audio": audio, "provider": "edge"}
    except Exception as e:
        logging.error(f"[EdgeTTS] fallback failed: {e}")
        raise HTTPException(status_code=502, detail="Text-to-speech unavailable")


@router.get(
    "/interview/{token}",
    response_model=JobConfig,
    summary="Validate token & get job configuration",
    description=(
        "Validates the interview URL token and returns the full job configuration needed "
        "to render the candidate-facing interview UI.\n\n"
        "**Returns:** job title, department, location, experience range, required skills, "
        "interview settings (max questions, mode, avatar/webcam/face-match flags, special instructions).\n\n"
        "**Errors:** `404` if token is invalid or inactive."
    ),
    tags=["Interview"],
)
def get_interview_config(token: str, db: Session = Depends(get_db)):
    tok = _get_token_or_404(token, db)
    job = db.query(Job).filter(Job.id == tok.job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    settings_obj = job.interview_settings
    skills = [s.skill for s in job.skills] if job.skills else []

    # When the code editor is disabled, coding turns are forced to 0 even if
    # max_coding_questions has a leftover value from when it was enabled.
    _code_enabled = settings_obj.code_editor_required if settings_obj else False
    _coding_count = (
        (settings_obj.max_coding_questions if settings_obj else 0)
        if _code_enabled else 0
    )
    return JobConfig(
        job_id=job.id,
        title=job.title,
        department=job.department,
        location=job.location,
        experience_level=job.experience_level,
        experience_min=job.experience_min,
        experience_max=job.experience_max,
        # Plan count, not raw max_questions — "Only these questions" asks
        # exactly the predefined list, so the progress counter must match it.
        max_questions=svc._build_question_plan(job, settings_obj)[2],
        max_coding_questions=_coding_count,
        mode=settings_obj.mode.value if settings_obj else "TRADITIONAL",
        virtual_avatar=settings_obj.virtual_avatar if settings_obj else True,
        webcam_required=settings_obj.webcam_required if settings_obj else True,
        face_match=settings_obj.face_match if settings_obj else False,
        resume_based=settings_obj.resume_based if settings_obj else True,
        code_editor_required=settings_obj.code_editor_required if settings_obj else True,
        allow_mobile=settings_obj.allow_mobile if settings_obj else False,
        secondary_cam=settings_obj.secondary_cam if settings_obj else False,
        manual_interview=settings_obj.manual_interview if settings_obj else False,
        special_instructions=settings_obj.special_instructions if settings_obj else None,
        auto_skip_timeout=settings_obj.auto_skip_timeout if settings_obj else 0,
        skills=skills,
        organization_name=job.organization.name if job.organization else None,
        manager_name=job.manager_name,
        manager_email=job.manager_email,
    )


# ── Verify Access (invitation-based) ─────────────────────────────────────────

# Interview statuses of candidates who were never scheduled for an interview:
# added by a recruiter, applied through the public link, or imported from the
# resume database. Like any candidate holding a personal invite, they can't be
# admitted by email alone.
_NEVER_SCHEDULED_STATUSES = {"New", "Applied", "Added from Database"}


@router.post(
    "/interview/{token}/verify-access",
    response_model=VerifyAccessResponse,
    summary="Verify candidate access by personal invite (or legacy email)",
    description=(
        "Admits a candidate to this job's interview. Body: `{ invite?, email? }` (at least one).\n\n"
        "- `invite` valid for this job → 'granted', 'completed' or 'terminated' for that candidate.\n"
        "- `invite` invalid or for another job → 'invalid_invite'.\n"
        "- `email` only: a candidate who has a personal invite, or was never scheduled → "
        "'invite_required'; a legacy scheduled candidate without an invite → the normal flow; "
        "an email that is not on this job → 'apply'."
    ),
    tags=["Interview"],
)
@limiter.limit("10/minute")
def verify_access(
    token: str,
    payload: VerifyAccessRequest,
    request: Request,
    db: Session = Depends(get_db),
):
    tok = _get_token_or_404(token, db)

    if payload.invite:
        # 1a. Personal invite link: the secret identifies the candidate and
        #     must belong to THIS job's link.
        candidate = db.query(Candidate).filter(
            Candidate.invite_token == payload.invite,
            Candidate.job_id == tok.job_id,
        ).first()
        if not candidate or not hmac.compare_digest(
            (candidate.invite_token or "").encode("utf-8"), payload.invite.encode("utf-8")
        ):
            return VerifyAccessResponse(
                access="invalid_invite",
                message="This interview link is invalid or no longer active. Please use the latest link from your email, or contact HR.",
            )
    else:
        # 1b. Email only (legacy ?candidate=<email> links).
        #     Match is case-insensitive: candidates added via resume parsing or
        #     manual recruiter entry may be stored with the original casing
        #     (e.g. "Coder.Shadab92@gmail.com"), but the URL/form value gets
        #     lowercased here. Without func.lower the join silently misses,
        #     sending an already-invited candidate to the apply form.
        email_str = str(payload.email).strip().lower()
        candidate = db.query(Candidate).filter(
            func.lower(Candidate.email) == email_str,
            Candidate.job_id == tok.job_id,
        ).first()

        if not candidate:
            # Not on this job. Nothing from other records (the resume database,
            # other organizations) is revealed to an anonymous caller.
            return VerifyAccessResponse(
                access="apply",
                message="No record found. Please apply for this job by uploading your resume.",
            )

        # Anyone who knows an email could otherwise open — or take over — that
        # candidate's interview. Email alone admits only legacy candidates who
        # were scheduled before personal invite links existed.
        _status = (candidate.interview_status or "New").strip()
        if not email_access_allowed(candidate) or _status in _NEVER_SCHEDULED_STATUSES:
            return VerifyAccessResponse(
                access="invite_required",
                message="Please open the personal interview link sent to your email to continue.",
            )

    # Serialize admissions per candidate. Without this lock two simultaneous
    # verify-access calls both saw "no active session" and each created one,
    # handing the candidate two sessions: a free retake, and a clean second
    # session whose completion overwrote a cheating termination in the first.
    # Held until this request's transaction ends (commit, or the rollback when
    # get_db closes the session on an early return).
    db.query(Candidate.id).filter(Candidate.id == candidate.id).with_for_update().first()

    # 2. Check for completed session
    completed_session = (
        db.query(InterviewSession)
        .filter(
            InterviewSession.candidate_id == candidate.id,
            InterviewSession.job_id == tok.job_id,
            InterviewSession.status == SessionStatus.completed,
        )
        .first()
    )
    if completed_session:
        return VerifyAccessResponse(
            access="completed",
            candidate_name=candidate.name,
            message="You have already completed your interview for this position.",
        )

    # 3. Check for terminated session (only block if it was an admin-initiated
    #    termination, NOT a browser-drop that the candidate should be able to
    #    rejoin from)
    terminated = (
        db.query(InterviewSession)
        .filter(
            InterviewSession.candidate_id == candidate.id,
            InterviewSession.job_id == tok.job_id,
            InterviewSession.status == SessionStatus.terminated,
        )
        .first()
    )
    if terminated:
        return VerifyAccessResponse(
            access="terminated",
            candidate_name=candidate.name,
            message="Your previous interview was terminated. Please contact HR.",
        )

    # Common scheduling fields for all "granted" responses.
    # interview_date is stored as NAIVE UTC (see candidates.schedule-interview);
    # serialise it as timezone-AWARE UTC so the browser's `new Date(...)` parses
    # it correctly instead of treating it as candidate-local wall time (which
    # blocked/admitted candidates at the wrong moment by their UTC offset).
    _idate = getattr(candidate, "interview_date", None)
    if _idate is not None and _idate.tzinfo is None:
        _idate = _idate.replace(tzinfo=timezone.utc)
    sched_fields = dict(
        interview_date=_idate,
        interview_timezone=getattr(candidate, "interview_timezone", None),
        joining_flexibility=getattr(candidate, "joining_flexibility", "anytime"),
        allow_joining_early=getattr(candidate, "allow_joining_early", False),
    )

    # 4. Check for resumable session (pending / in_progress).
    #    Also handle "dropped" sessions: if a candidate closed the tab, the
    #    /drop endpoint records `dropped_at` in session_metadata but keeps the
    #    session active so they can rejoin within a 10-minute grace window.
    #    After 10 min the session is terminated and evaluation is triggered.
    _DROP_GRACE_MINUTES = 10

    active_session = (
        db.query(InterviewSession)
        .filter(
            InterviewSession.candidate_id == candidate.id,
            InterviewSession.job_id == tok.job_id,
            InterviewSession.status.in_([SessionStatus.pending, SessionStatus.in_progress]),
        )
        .order_by(InterviewSession.created_at.desc())
        .first()
    )

    if active_session:
        # Check if session was dropped and grace period has expired
        _meta = {}
        try:
            _meta = json.loads(active_session.session_metadata or "{}")
        except Exception:
            pass
        dropped_at_str = _meta.get("dropped_at")
        if dropped_at_str:
            from datetime import timedelta
            try:
                dropped_at = datetime.fromisoformat(dropped_at_str)
                if datetime.utcnow() - dropped_at > timedelta(minutes=_DROP_GRACE_MINUTES):
                    # Grace period expired — terminate the session
                    active_session.status = SessionStatus.terminated
                    active_session.completed_at = datetime.utcnow()
                    _cand = db.query(Candidate).filter(Candidate.id == active_session.candidate_id).first()
                    if _cand:
                        _cand.interview_status = "Candidate Dropped Interview"
                        log = InterviewStatusLog(
                            candidate_id=_cand.id,
                            previous_status="Disconnected – Awaiting Rejoin",
                            new_status="Candidate Dropped Interview",
                            changed_by="system",
                            reason=f"Candidate did not rejoin within {_DROP_GRACE_MINUTES} minutes after disconnect",
                        )
                        db.add(log)
                    db.commit()
                    return VerifyAccessResponse(
                        access="terminated",
                        candidate_name=candidate.name,
                        message="Your interview session timed out because you disconnected for too long. Please contact HR.",
                    )
            except Exception:
                pass  # malformed date — allow resume

            # Within grace period — clear the drop flag so candidate can resume cleanly
            _meta.pop("dropped_at", None)
            active_session.session_metadata = json.dumps(_meta)
            db.commit()

        return VerifyAccessResponse(
            access="granted",
            candidate_name=candidate.name,
            session_id=active_session.id,
            candidate_id=candidate.id,
            can_resume=True,
            session_status=active_session.status.value,
            message="Welcome back! You can resume your interview.",
            session_token=_get_or_mint_session_access_token(active_session, db),
            **sched_fields,
        )

    # 5. Candidate is invited but has no session yet — create one
    job = db.query(Job).filter(Job.id == tok.job_id).first()
    metadata_json = json.dumps({
        "user_agent": request.headers.get("user-agent", ""),
        "ip": request.client.host if request.client else "",
        # Stored with the row itself, so the session never exists without its token.
        "access_token": secrets.token_urlsafe(32),
    })
    settings_obj = job.interview_settings if job else None
    interview_session = InterviewSession(
        token_id=tok.id,
        candidate_id=candidate.id,
        job_id=tok.job_id,
        status=SessionStatus.pending,
        total_questions=svc._build_question_plan(job, settings_obj)[2] if job else 8,
        session_metadata=metadata_json,
    )
    db.add(interview_session)
    db.commit()
    db.refresh(interview_session)

    return VerifyAccessResponse(
        access="granted",
        candidate_name=candidate.name,
        session_id=interview_session.id,
        candidate_id=candidate.id,
        can_resume=False,
        message="Access verified. Ready to start your interview.",
        session_token=_get_or_mint_session_access_token(interview_session, db),
        **sched_fields,
    )


# ── Upload resume file (no candidate record — just save file) ────────────────

_RESUME_EXTS = {".pdf", ".doc", ".docx"}
# ~2 GB: a 45-minute interview is ~750 MB at the highest quality tier.
MAX_RECORDING_SIZE = 2 * 1024 * 1024 * 1024


@router.post(
    "/interview/{token}/upload-resume",
    summary="Upload a resume file for job application",
    tags=["Interview"],
)
@limiter.limit("10/minute")
async def upload_resume_for_apply(
    token: str,
    request: Request,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    """Save a resume file to disk and return the path. No candidate record is created."""
    _get_token_or_404(token, db)  # validate token

    allowed = {
        "application/pdf",
        "application/msword",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    }
    if file.content_type not in allowed:
        raise HTTPException(status_code=400, detail="Only PDF, DOC, or DOCX files are accepted.")
    # The stored name is built server-side: the client file name is reduced to
    # a safe stem plus an allowed extension (it used to be written verbatim).
    safe = safe_upload_name(file.filename, _RESUME_EXTS)
    if safe is None:
        raise HTTPException(status_code=400, detail="Only PDF, DOC, or DOCX files are accepted.")
    stem, ext = safe

    upload_dir = os.path.join(UPLOAD_ROOT, "resumes")
    os.makedirs(upload_dir, exist_ok=True)

    safe_name = f"{datetime.utcnow().strftime('%Y%m%d%H%M%S%f')}_{stem}{ext}"
    file_path = os.path.join(upload_dir, safe_name)

    # Size, real type (from the bytes, not the client's word), dangerous
    # content and — when configured — antivirus, before the file is stored.
    try:
        await asyncio.to_thread(store_resume_upload, file, file_path)
    except UnsafeFile as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.reason)

    schedule_compression(file_path)
    return {"resume_path": file_path}


# ── Apply for Job (upload resume without registration) ──────────────────────

@router.post(
    "/interview/{token}/apply",
    response_model=ApplyForJobResponse,
    summary="Apply for job — upload resume and basic details",
    description=(
        "Allows a candidate who hasn't been invited to submit their resume and basic details.\n"
        "The resume is stored in the database and parsed as metadata.\n"
        "The candidate will NOT be able to start the interview — only invited candidates can.\n"
        "After applying, the recruiter can review and invite the candidate."
    ),
    tags=["Interview"],
)
@limiter.limit("5/minute")
def apply_for_job(
    token: str,
    payload: ApplyForJobRequest,
    background_tasks: BackgroundTasks,
    request: Request,
    db: Session = Depends(get_db),
):
    from app.models.resume_metadata import ResumeMetadata

    tok = _get_token_or_404(token, db)
    job = db.query(Job).filter(Job.id == tok.job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    email_str = str(payload.email).strip().lower()

    # The resume path is echoed back from /upload-resume. Accept only a file
    # that really is in the resumes upload folder — the value used to be stored
    # verbatim and was later read, served and deleted from disk.
    resume_path = client_resume_path(payload.resume_path)
    if not resume_path:
        raise HTTPException(status_code=400, detail="Resume upload not found. Please upload your resume again.")

    # One upload, one application. Otherwise a single uploaded file could be
    # replayed with random emails, each application consuming the
    # organization's candidate quota (or attaching someone else's file).
    used_by_other = (
        db.query(Candidate.id)
        .filter(Candidate.resume_path == resume_path, func.lower(Candidate.email) != email_str)
        .first()
        or db.query(ResumeMetadata.id)
        .filter(
            ResumeMetadata.resume_file_path == resume_path,
            func.lower(ResumeMetadata.candidate_email) != email_str,
        )
        .first()
    )
    if used_by_other:
        raise HTTPException(status_code=400, detail="Resume upload not found. Please upload your resume again.")

    # Resume-database rows belong to an organization: look them up and create
    # them only inside the job's organization. This lookup used to match the
    # email across ALL tenants and overwrote another company's record. A job
    # without an organization gets no resume-database row at all.
    meta_id: Optional[int] = None
    parse_meta = False
    if job.organization_id is not None:
        meta = db.query(ResumeMetadata).filter(
            func.lower(ResumeMetadata.candidate_email) == email_str,
            ResumeMetadata.organization_id == job.organization_id,
        ).first()
        if meta is None:
            meta = ResumeMetadata(candidate_email=email_str, organization_id=job.organization_id)
            db.add(meta)
        # Fill a new (or still empty) record. An existing record with a resume
        # is left alone: this form is anonymous, and anyone who knew a person's
        # email could otherwise replace the name, phone and resume the
        # organization holds for them. The job's candidate row below still
        # gets the newly uploaded resume.
        if not meta.resume_file_path:
            meta.candidate_name = payload.name
            meta.phone = payload.phone
            meta.resume_file_path = resume_path
            meta.parsing_status = "pending"
            parse_meta = True
        try:
            db.commit()
        except IntegrityError:
            # A concurrent submit for the same email created the row first.
            db.rollback()
            raise HTTPException(
                status_code=409,
                detail="Your application is already being submitted. Please try again in a moment.",
            )
        meta_id = meta.id

    # Also create/update a candidate record for this job
    existing_candidate = db.query(Candidate).filter(
        func.lower(Candidate.email) == email_str,
        Candidate.job_id == tok.job_id,
    ).first()

    if not existing_candidate:
        _check_and_increment_candidate_quota(db, tok.job_id)
        candidate = Candidate(
            name=payload.name,
            email=email_str,
            phone=payload.phone or None,
            role=job.title,
            job_id=tok.job_id,
            resume_path=resume_path,
            interview_status="Applied",
            candidate_stage="Candidates",
            resume_metadata_id=meta_id,
        )
        db.add(candidate)
        try:
            db.commit()
        except IntegrityError:
            # A concurrent submit created this candidate first; the quota
            # increment made in this transaction is rolled back with it.
            db.rollback()

    # Queue background parsing
    if meta_id is not None and parse_meta:
        background_tasks.add_task(_parse_resume_metadata_bg, meta_id)

    return ApplyForJobResponse(
        success=True,
        message="Your application has been submitted. The recruiter will review your resume and may invite you for an interview.",
        resume_metadata_id=meta_id,
    )


def _parse_resume_metadata_bg(meta_id: int):
    """Background task to parse resume and update metadata.

    Retries up to 3 times with exponential backoff on transient failures
    (OOM, network glitch fetching from a remote file store, etc.). Permanent
    failures (file truly unreadable) abort early after attempt 1 since the
    same input will fail the same way.
    """
    from app.services.fast_resume_parser import extract_text_from_file, fast_parse_resume
    import time as _time

    last_exc: Optional[str] = None
    permanent_failure = False
    for attempt in range(1, 4):
        if permanent_failure:
            break
        db = SessionLocal()
        try:
            from app.models.resume_metadata import ResumeMetadata
            meta = db.query(ResumeMetadata).filter(ResumeMetadata.id == meta_id).first()
            if not meta or not meta.resume_file_path:
                return

            meta.parsing_status = "processing"
            db.commit()

            raw_text = extract_text_from_file(meta.resume_file_path)
            if not raw_text:
                # Permanent: same input will produce same empty output.
                meta.parsing_status = "failed"
                meta.parsing_error = (
                    "No text could be extracted from this PDF. "
                    "It may be a scanned / image-only document. "
                    "Please re-upload as a text-based PDF or DOCX."
                )
                db.commit()
                permanent_failure = True
                return

            parsed = fast_parse_resume(raw_text)
            meta.raw_text = raw_text
            meta.candidate_name = parsed.get("candidate_name") or meta.candidate_name
            meta.phone = parsed.get("phone") or meta.phone
            meta.skills = json.dumps(parsed.get("skills", []))
            meta.experience_years = parsed.get("experience_years")
            meta.education = json.dumps(parsed.get("education", []))
            meta.certifications = json.dumps(parsed.get("certifications", []))
            meta.work_experience = json.dumps(parsed.get("work_experience", []))
            meta.location = parsed.get("location") or meta.location
            meta.current_designation = parsed.get("current_designation")
            meta.keywords = json.dumps(parsed.get("keywords", []))
            meta.personal_details = json.dumps(parsed.get("personal_details") or {})
            meta.summary = parsed.get("summary")
            meta.parsing_status = "completed"
            meta.parsed_at = datetime.utcnow()
            db.commit()
            return
        except Exception as e:
            last_exc = f"{type(e).__name__}: {e}"
            logging.exception(
                "[ResumeParse] attempt %d/3 failed for meta_id=%s: %s",
                attempt, meta_id, last_exc,
            )
            if attempt < 3:
                _time.sleep(attempt * 2)  # 2s, 4s
        finally:
            db.close()

    # All retries failed (transient) — mark failed for visibility so an admin
    # can manually re-parse later.
    if last_exc and not permanent_failure:
        try:
            db = SessionLocal()
            from app.models.resume_metadata import ResumeMetadata
            meta = db.query(ResumeMetadata).filter(ResumeMetadata.id == meta_id).first()
            if meta:
                meta.parsing_status = "failed"
                meta.parsing_error = last_exc[:500]
                db.commit()
        except Exception as final_exc:
            logging.error("[ResumeParse] final-failure write also failed: %s", final_exc)
        finally:
            try:
                db.close()
            except Exception:
                pass


@router.get(
    "/interview/sessions/{session_id}/resume",
    dependencies=[Depends(require_session_token)],
    response_model=ResumeSessionResponse,
    summary="Get session state for recovery",
    description="Returns the current session state including all messages for interview recovery after disconnect.",
    tags=["Interview"],
)
def resume_session(session_id: int, db: Session = Depends(get_db)):
    session = _get_session_or_404(session_id, db)

    if session.status not in (SessionStatus.pending, SessionStatus.in_progress):
        raise HTTPException(status_code=400, detail="Session cannot be resumed — it is already completed or terminated.")

    messages = (
        db.query(InterviewMessage)
        .filter(InterviewMessage.session_id == session_id)
        .order_by(InterviewMessage.created_at)
        .all()
    )

    return ResumeSessionResponse(
        session_id=session.id,
        status=session.status.value,
        current_question_index=session.current_question_index or 0,
        violation_count=session.violation_count or 0,
        messages=[
            {"role": m.role, "content": m.content, "feedback": m.feedback, "question_index": m.question_index}
            for m in messages
        ],
    )


@router.post(
    "/interview/sessions/{session_id}/message",
    dependencies=[Depends(require_session_token)],
    response_model=InterviewMessageResponse,
    summary="Send answer → receive next AI question",
    description=(
        "Sends the candidate's spoken/typed answer and returns the AI interviewer's next question.\n\n"
        "**Behaviour:**\n"
        "- Saves the candidate message to `interview_messages`.\n"
        "- Builds the full conversation history and calls **Claude Sonnet** with an adaptive system prompt "
        "(includes job context, resume summary, and interview rules).\n"
        "- Saves the AI reply to `interview_messages`.\n"
        "- When all questions are exhausted the AI sets `is_complete: true` in its JSON response, "
        "the session status becomes `completed`, and `is_complete` is forwarded to the client.\n\n"
        "**Errors:** `400` if session is already `completed` or `terminated`."
    ),
    tags=["Interview"],
)
@limiter.limit("10/minute")
async def interview_message(
    session_id: int,
    payload: InterviewMessageRequest,
    request: Request,
    db: Session = Depends(get_db),
):
    session = _get_session_or_404(session_id, db)

    # ── Idempotency: a client retry after a network timeout may race a first
    # request that actually completed server-side. Replay the stored response
    # for a repeated turn_id instead of running a duplicate AI turn (which
    # double-saved the answer and advanced the question index twice).
    # Checked BEFORE the status guards: if the first request completed the
    # interview, the retry must get the closing message back, not a 400.
    turn_id = (payload.turn_id or "").strip()
    if turn_id:
        try:
            _meta = json.loads(session.session_metadata or "{}")
        except Exception:
            _meta = {}
        _last = _meta.get("last_turn") or {}
        if _last.get("id") == turn_id and isinstance(_last.get("response"), dict):
            logging.info(f"[interview_message] Replaying turn {turn_id} for session {session_id}")
            return InterviewMessageResponse(**_last["response"])

    if session.status == SessionStatus.terminated:
        raise HTTPException(status_code=400, detail="Interview has been terminated due to violations")
    if session.status == SessionStatus.completed:
        raise HTTPException(status_code=400, detail="Interview is already completed")

    # Mark as in_progress on first message
    if session.status == SessionStatus.pending:
        session.status = SessionStatus.in_progress
        session.started_at = datetime.utcnow()
        db.flush()

    result = await svc.conduct_turn(session, payload.content, db)

    if turn_id:
        try:
            _meta = json.loads(session.session_metadata or "{}")
            _meta["last_turn"] = {"id": turn_id, "response": result}
            session.session_metadata = json.dumps(_meta)
            db.commit()
        except Exception:
            try:
                db.rollback()
            except Exception:
                pass

    return InterviewMessageResponse(**result)


@router.post(
    "/interview/sessions/{session_id}/start",
    dependencies=[Depends(require_session_token)],
    response_model=InterviewMessageResponse,
    summary="Start interview → AI opening message",
    description=(
        "Marks the session as `in_progress` and triggers the first AI turn with no candidate input "
        "(the AI introduces itself and asks question #1).\n\n"
        "Call this once after the candidate completes the permission/warning flow. "
        "Subsequent turns use `POST /interview/sessions/{id}/message`.\n\n"
        "**Errors:** `400` if session is not in `pending` or `in_progress` state."
    ),
    tags=["Interview"],
)
@limiter.limit("5/minute")
async def start_interview(
    session_id: int,
    request: Request,
    language: str = "en",
    db: Session = Depends(get_db),
):
    session = _get_session_or_404(session_id, db)

    if session.status not in (SessionStatus.pending, SessionStatus.in_progress):
        raise HTTPException(status_code=400, detail="Session is not in a startable state")

    session.status = SessionStatus.in_progress
    session.started_at = datetime.utcnow()
    session.interview_language = language
    db.flush()

    result = await svc.conduct_turn(session, None, db)
    return InterviewMessageResponse(**result)


@router.post(
    "/interview/sessions/{session_id}/violation",
    dependencies=[Depends(require_session_token)],
    response_model=ViolationResponse,
    summary="Log a cheating / integrity violation",
    description=(
        "Records a cheating event detected by the frontend monitoring system.\n\n"
        "**Violation types:**\n"
        "- `tab_switch` — candidate navigated away from the interview tab\n"
        "- `fullscreen_exit` — candidate exited fullscreen mode\n"
        "- `face_not_detected` — no face visible in webcam frame\n"
        "- `multiple_faces` — more than one person detected in frame\n\n"
        "**Auto-termination:** When `total_violations` reaches **5**, the session status is set to "
        "`terminated` and `should_terminate: true` is returned. The frontend should stop the interview immediately.\n\n"
        "**Returns:** `violation_id`, cumulative `total_violations`, and `should_terminate` flag."
    ),
    tags=["Interview"],
)
@limiter.limit("20/minute")
def log_violation(
    session_id: int,
    payload: ViolationRequest,
    request: Request,
    db: Session = Depends(get_db),
):
    session = _get_session_or_404(session_id, db)

    try:
        v_type = ViolationType(payload.violation_type)
    except ValueError:
        raise HTTPException(status_code=422, detail=f"Invalid violation_type: {payload.violation_type}")

    violation = Violation(
        session_id=session_id,
        violation_type=v_type,
        details=payload.details,
        occurred_at=datetime.utcnow(),
    )
    db.add(violation)

    session.violation_count = (session.violation_count or 0) + 1
    # Only a live session can be auto-terminated. The session token stays
    # valid after the interview ends, so late (or deliberately posted)
    # violations could otherwise flip a COMPLETED interview to "Terminated –
    # Cheating Detected". Late events are still recorded as evidence.
    should_terminate = (
        session.violation_count >= VIOLATION_TERMINATE_THRESHOLD
        and session.status in (SessionStatus.pending, SessionStatus.in_progress)
    )

    if should_terminate:
        session.status = SessionStatus.terminated
        session.completed_at = datetime.utcnow()
        # Mark candidate as terminated due to cheating
        candidate = db.query(Candidate).filter(Candidate.id == session.candidate_id).first()
        if candidate:
            log = InterviewStatusLog(
                candidate_id=candidate.id,
                previous_status=candidate.interview_status,
                new_status="Terminated – Cheating Detected",
                changed_by="system",
                reason=f"Auto-terminated after {session.violation_count} violations",
            )
            db.add(log)
            candidate.interview_status = "Terminated – Cheating Detected"

    # Retry the commit on transient DB errors. A failed violation write
    # bypasses cheating detection (a candidate at violation #5 could keep
    # interviewing if this drops). We never want that, so retry hard.
    import time as _time
    commit_ok = False
    last_err: Optional[str] = None
    for attempt in range(1, 5):
        try:
            db.commit()
            db.refresh(violation)
            commit_ok = True
            break
        except Exception as exc:
            last_err = f"{type(exc).__name__}: {exc}"
            logging.error(
                f"[Violation] commit attempt {attempt}/4 failed for session "
                f"{session_id} (violation #{session.violation_count}): {last_err}"
            )
            try:
                db.rollback()
            except Exception:
                pass
            # Re-add the row after rollback (rollback discards pending objects).
            db.add(Violation(
                session_id=session_id,
                violation_type=v_type,
                details=payload.details,
                occurred_at=datetime.utcnow(),
            ))
            # Re-apply session/candidate mutations. The rollback expired the
            # in-memory increment (the object reloads the OLD count from the
            # DB), so we must re-increment — without "+ 1" a retried commit
            # recorded the violation row but silently lost the count increase.
            session.violation_count = (session.violation_count or 0) + 1
            if should_terminate:
                session.status = SessionStatus.terminated
                session.completed_at = datetime.utcnow()
                candidate = db.query(Candidate).filter(Candidate.id == session.candidate_id).first()
                if candidate:
                    candidate.interview_status = "Terminated – Cheating Detected"
            if attempt < 4:
                _time.sleep(attempt * 0.5)

    if not commit_ok:
        logging.critical(
            f"[Violation] PERSIST FAILED after 4 attempts for session "
            f"{session_id}. last_error={last_err}. Cheating detection may "
            f"have been bypassed for this event."
        )
        # Still return 200 with should_terminate=True so the client at least
        # honors the termination locally if it was already pending.
        return ViolationResponse(
            violation_id=0,
            session_id=session_id,
            total_violations=session.violation_count or 0,
            should_terminate=should_terminate,
        )

    return ViolationResponse(
        violation_id=violation.id,
        session_id=session_id,
        total_violations=session.violation_count,
        should_terminate=should_terminate,
    )


def _set_eval_state(session_id: int, state: str, error: Optional[str] = None) -> None:
    """Persist evaluation status (`processing` / `failed` / `done`) on the
    session's session_metadata so the frontend's GET /evaluation can return
    a clear state instead of a vague 404 forever."""
    db = SessionLocal()
    try:
        session = db.query(InterviewSession).filter(InterviewSession.id == session_id).first()
        if not session:
            return
        try:
            meta = json.loads(session.session_metadata or "{}")
        except Exception:
            meta = {}
        meta["evaluation_state"] = state
        meta["evaluation_state_at"] = datetime.utcnow().isoformat()
        if error is not None:
            meta["evaluation_error"] = error[:500]
        elif state != "failed":
            meta.pop("evaluation_error", None)
        session.session_metadata = json.dumps(meta)
        db.commit()
    finally:
        db.close()


# Sessions whose evaluation is running in this process. /complete (repeatable
# on a finished session that has no evaluation yet) and the recruiter's
# "regenerate" both queue an evaluation; without this guard every call started
# another full Opus run for the same session.
_EVALUATIONS_IN_FLIGHT: set = set()


async def _run_evaluation_background(session_id: int):
    """Single-flight wrapper around _evaluate_session_with_retries."""
    if session_id in _EVALUATIONS_IN_FLIGHT:
        logging.getLogger("app.interview").info(
            f"[Evaluation] session {session_id}: already running — duplicate request ignored"
        )
        return
    _EVALUATIONS_IN_FLIGHT.add(session_id)
    try:
        await _evaluate_session_with_retries(session_id)
    finally:
        _EVALUATIONS_IN_FLIGHT.discard(session_id)


async def _evaluate_session_with_retries(session_id: int):
    """Background task: generate evaluation with automatic retry + exponential
    backoff. Wrapped in asyncio.wait_for so a hung LLM can't block forever.

    Guarantees (this is the user's main 'data not saving' concern):
    - The evaluation_state in session_metadata is ALWAYS updated, even if
      _do_evaluation crashes with an unexpected exception.
    - If all retries fail, a STUB Evaluation row is created with null scores
      and a failure-summary so GET /evaluation returns 200 (not 404) and the
      retry button in the UI actually has something to retry.
    """
    # Budget math has to actually add up, or the outer wrapper kills the task
    # mid-attempt and every failure reports as a bare "timed out" no matter what
    # really went wrong. Per attempt the evaluation makes up to 2 LLM calls of
    # up to 240s each (see eval_timeout in generate_evaluation), so ~500s; two
    # attempts plus backoff fit inside the 15-minute ceiling below.
    MAX_RETRIES = 2
    TOTAL_TIMEOUT = 900

    _set_eval_state(session_id, "processing")
    last_error: Optional[str] = None

    async def _do_evaluation():
        nonlocal last_error
        for attempt in range(MAX_RETRIES):
            db = SessionLocal()
            try:
                logging.info(f"[Evaluation] Attempt {attempt + 1}/{MAX_RETRIES} for session {session_id}")
                session = db.query(InterviewSession).filter(InterviewSession.id == session_id).first()
                if not session:
                    logging.error(f"[Evaluation] Session {session_id} not found in DB")
                    last_error = "Session not found"
                    return
                if session.evaluation:
                    logging.info(f"[Evaluation] Session {session_id} already has evaluation — skipping")
                    _set_eval_state(session_id, "done")
                    return

                messages_count = db.query(InterviewMessage).filter(InterviewMessage.session_id == session_id).count()
                if messages_count == 0:
                    logging.error(f"[Evaluation] Session {session_id} has NO messages — cannot evaluate")
                    last_error = "No interview transcript to evaluate"
                    return

                logging.info(f"[Evaluation] Session {session_id} — {messages_count} messages, calling AI...")
                await svc.generate_evaluation(session, db)
                logging.info(f"[Evaluation] Session {session_id} — evaluation saved successfully")
                _set_eval_state(session_id, "done")
                return

            except Exception as e:
                last_error = f"{type(e).__name__}: {e}"
                logging.exception(
                    f"[Evaluation] FAILED attempt {attempt + 1} for session {session_id}"
                )
                if attempt < MAX_RETRIES - 1:
                    # Exponential backoff with jitter so we don't hammer a
                    # struggling LLM provider: 2s, 5s, 11s, capped at 30s.
                    import random
                    backoff = min(30, (2 ** (attempt + 1)) + random.uniform(0, 1))
                    await asyncio.sleep(backoff)
                else:
                    logging.error(f"[Evaluation] ALL {MAX_RETRIES} RETRIES FAILED for session {session_id}")
            finally:
                db.close()

    # Top-level guard: any unhandled exception inside _do_evaluation (e.g. an
    # asyncio cancellation, a bug in _set_eval_state, an import error) must
    # still leave the candidate with a "failed" state and a stub row — never
    # a permanent "processing..." spinner.
    try:
        try:
            await asyncio.wait_for(_do_evaluation(), timeout=TOTAL_TIMEOUT)
        except asyncio.TimeoutError:
            last_error = f"Evaluation timed out after {TOTAL_TIMEOUT}s"
            logging.error(f"[Evaluation] TOTAL TIMEOUT ({TOTAL_TIMEOUT}s) for session {session_id}")
        except Exception as outer:
            last_error = f"{type(outer).__name__}: {outer}"
            logging.exception(
                f"[Evaluation] _do_evaluation crashed unexpectedly for session {session_id}"
            )
    finally:
        # If we got here without a `done` state already set, mark as failed
        # AND insert a stub Evaluation row so the recruiter UI shows
        # "evaluation failed — retry" instead of a 404 dead end.
        db = SessionLocal()
        try:
            session = db.query(InterviewSession).filter(InterviewSession.id == session_id).first()
            if session and not session.evaluation:
                _set_eval_state(session_id, "failed", error=last_error or "Unknown error")
                try:
                    stub = Evaluation(
                        session_id=session_id,
                        candidate_id=session.candidate_id,
                        summary=(
                            "Evaluation could not be generated automatically. "
                            f"Last error: {last_error or 'Unknown error'}. "
                            "Click 'Re-generate' to try again."
                        ),
                        meets_threshold=False,
                    )
                    db.add(stub)
                    db.commit()
                    logging.info(
                        f"[Evaluation] Stub row created for failed session {session_id}"
                    )
                except Exception as stub_exc:
                    logging.error(
                        f"[Evaluation] Could not create stub row for session "
                        f"{session_id}: {stub_exc}"
                    )
                    try:
                        db.rollback()
                    except Exception:
                        pass
        except Exception as final_exc:
            logging.exception(
                f"[Evaluation] Final cleanup also failed for session {session_id}: {final_exc}"
            )
        finally:
            db.close()


@router.post(
    "/interview/sessions/{session_id}/complete",
    dependencies=[Depends(require_session_token)],
    status_code=202,
    summary="Complete interview & trigger evaluation",
    description=(
        "Marks the session as `completed` and triggers AI evaluation in the background.\n\n"
        "Returns `202 Accepted` immediately. Poll `GET /interview/sessions/{id}/evaluation` "
        "every 3–5 seconds until it returns `200` with the evaluation data.\n\n"
        "**Errors:** `400` if session is already `completed` or `terminated`."
    ),
    tags=["Interview"],
)
@limiter.limit("10/minute")
async def complete_interview(
    session_id: int,
    request: Request,
    background_tasks: BackgroundTasks,
    early_termination: bool = False,
    termination_reason: Optional[str] = None,
    db: Session = Depends(get_db),
):
    """
    Complete interview & trigger background evaluation.

    termination_reason values:
    - None / not provided → "Completed" (normal completion)
    - "ended_by_candidate" → "Ended by Candidate"
    - "network_disconnect" → "Disconnected – Network Issue"
    - "candidate_terminated" → "Candidate Terminated Prematurely"
    - any other truthy value → "Candidate Terminated Prematurely"
    """
    session = _get_session_or_404(session_id, db)

    # Decide the candidate-row status this completion call implies.
    if not early_termination:
        new_status = "Completed"
    elif termination_reason == "ended_by_candidate":
        new_status = "Ended by Candidate"
    elif termination_reason == "network_disconnect":
        new_status = "Disconnected – Network Issue"
    else:
        new_status = "Candidate Terminated Prematurely"

    candidate = db.query(Candidate).filter(Candidate.id == session.candidate_id).first()

    # Statuses that indicate the candidate is still mid-flow (not a final
    # outcome). If the session was force-terminated while the candidate was
    # mid-interview, these are the values the candidate row may be stuck on —
    # so even on the early-return path below we must reconcile them.
    TRANSITIONAL_STATUSES = {
        "New",
        "Scheduled",
        "Disconnected – Awaiting Rejoin",
    }

    # Already completed/terminated → don't restart the session lifecycle, but
    # DO reconcile candidate.interview_status if it's stuck on a transitional
    # value (e.g. "Disconnected – Awaiting Rejoin" because /drop ran but the
    # session got auto-terminated before the rejoined /complete landed).
    if session.status in (SessionStatus.completed, SessionStatus.terminated):
        if candidate and candidate.interview_status in TRANSITIONAL_STATUSES:
            log = InterviewStatusLog(
                candidate_id=candidate.id,
                previous_status=candidate.interview_status,
                new_status=new_status,
                changed_by="system",
                reason="Reconciled on /complete (session was already terminated)",
            )
            db.add(log)
            candidate.interview_status = new_status
            db.commit()
        if not session.evaluation:
            background_tasks.add_task(_run_evaluation_background, session_id)
        # Backstop: assemble the live-uploaded recording parts if the client
        # never called /recording-finalize (e.g. tab closed right after ending).
        background_tasks.add_task(_finalize_recording_safety_net, session_id)
        return {"status": "processing", "session_id": session_id}

    session.status = SessionStatus.completed
    session.completed_at = datetime.utcnow()

    if candidate:
        log = InterviewStatusLog(
            candidate_id=candidate.id,
            previous_status=candidate.interview_status,
            new_status=new_status,
            changed_by="system",
            reason=termination_reason or "Interview completed normally",
        )
        db.add(log)
        candidate.interview_status = new_status

    db.commit()

    # Trigger evaluation in background — returns 202 immediately
    background_tasks.add_task(_run_evaluation_background, session_id)
    # Backstop: assemble the live-uploaded recording parts if the client never
    # called /recording-finalize (e.g. tab/browser/laptop closed right after).
    background_tasks.add_task(_finalize_recording_safety_net, session_id)
    return {"status": "processing", "session_id": session_id}


@router.post(
    "/interview/sessions/{session_id}/drop",
    dependencies=[Depends(require_session_token_or_query)],
    status_code=200,
    summary="Mark session as dropped (tab close / network disconnect)",
    description=(
        "Lightweight endpoint called via sendBeacon when the candidate closes the tab or disconnects.\n\n"
        "Records a `dropped_at` timestamp in `session_metadata` but **keeps the session in its "
        "current status** (`pending` / `in_progress`) so the candidate can resume if they come back "
        "within 10 minutes.  The `/verify-access` endpoint checks for stale drops and terminates "
        "them only after the grace window expires.\n\n"
        "Does NOT generate an evaluation (fast — safe for use with navigator.sendBeacon).\n\n"
        "Idempotent: silently succeeds if session is already completed or terminated."
    ),
    tags=["Interview"],
)
def drop_interview(session_id: int, db: Session = Depends(get_db)):
    session = db.query(InterviewSession).filter(InterviewSession.id == session_id).first()
    if not session:
        return {"ok": False, "reason": "not_found"}

    # Only act on active sessions — record the drop but do NOT terminate immediately.
    # This gives the candidate a 10-minute window to refresh / rejoin.
    if session.status in (SessionStatus.pending, SessionStatus.in_progress):
        try:
            meta = json.loads(session.session_metadata or "{}")
        except Exception:
            meta = {}
        meta["dropped_at"] = datetime.utcnow().isoformat()
        meta["drop_count"] = meta.get("drop_count", 0) + 1
        session.session_metadata = json.dumps(meta)

        # Update candidate status to "Disconnected" (informational, not terminal)
        candidate = db.query(Candidate).filter(Candidate.id == session.candidate_id).first()
        if candidate and candidate.interview_status not in ("Completed", "Candidate Terminated Prematurely"):
            log = InterviewStatusLog(
                candidate_id=candidate.id,
                previous_status=candidate.interview_status,
                new_status="Disconnected – Awaiting Rejoin",
                changed_by="system",
                reason="Tab closed or browser navigated away during interview",
            )
            db.add(log)
            candidate.interview_status = "Disconnected – Awaiting Rejoin"

        db.commit()

    return {"ok": True}


@router.post(
    "/interview/sessions/{session_id}/recording-finalize-beacon",
    dependencies=[Depends(require_session_token_or_query)],
    status_code=200,
    summary="Tab-close beacon: assemble live-uploaded recording parts now",
    description=(
        "Fired via navigator.sendBeacon when the candidate closes the tab while a "
        "recording exists (including on the completion screen, where the /drop "
        "beacon no longer fires). Schedules the same idempotent, size-guarded "
        "safety-net that stitches the uploaded parts into the final video, so the "
        "recording isn't left unstitched when the client never reaches "
        "/recording-finalize. No-auth (like /drop) for beacon reliability — it "
        "only assembles parts already uploaded for this session."
    ),
    tags=["Interview"],
)
@limiter.limit("30/minute")
async def recording_finalize_beacon(
    session_id: int,
    request: Request,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    session = db.query(InterviewSession).filter(InterviewSession.id == session_id).first()
    if not session:
        return {"ok": False, "reason": "not_found"}
    background_tasks.add_task(_finalize_recording_safety_net, session_id)
    return {"ok": True}


@router.get(
    "/interview/sessions/{session_id}/evaluation",
    dependencies=[Depends(require_session_token)],
    response_model=CandidateEvaluationRead,
    summary="Get evaluation result (candidate view)",
    description=(
        "Candidate-facing: whether the stored evaluation for a completed session "
        "meets the job's threshold. Scores, recommendation and reviewer feedback "
        "are recruiter-only (`GET /candidates/{id}/evaluation`).\n\n"
        "**Errors:** `404` if the evaluation has not been generated yet "
        "(call `POST /complete` first)."
    ),
    tags=["Interview"],
)
def get_evaluation(session_id: int, db: Session = Depends(get_db)):
    session = _get_session_or_404(session_id, db)
    if not session.evaluation:
        raise HTTPException(status_code=404, detail="Evaluation not yet generated")
    full = _format_evaluation(session.evaluation)
    return CandidateEvaluationRead(
        id=full.id,
        session_id=full.session_id,
        candidate_id=full.candidate_id,
        meets_threshold=full.meets_threshold,
        created_at=full.created_at,
    )


@router.get(
    "/interview/sessions/{session_id}/evaluation-status",
    dependencies=[Depends(require_session_token)],
    summary="Cheap status probe for the evaluation background task",
    description=(
        "Returns one of: `done` (evaluation row exists), `processing` (background "
        "task running), `failed` (background task gave up — show retry button), "
        "or `idle` (no task ever started for this session).\n\n"
        "The frontend polls this in a tight loop; it never 404s, so the browser "
        "console doesn't fill with red errors while waiting."
    ),
    tags=["Interview"],
)
def get_evaluation_status(session_id: int, db: Session = Depends(get_db)):
    session = _get_session_or_404(session_id, db)
    if session.evaluation:
        return {"state": "done"}
    try:
        meta = json.loads(session.session_metadata or "{}")
    except Exception:
        meta = {}
    state = meta.get("evaluation_state", "idle")
    # The stored error is raw exception text (LLM provider / SQL details) — it
    # stays server-side for the recruiter view; the candidate gets a fixed line.
    error = "Evaluation could not be generated yet." if state == "failed" else None
    return {"state": state, "error": error}


@router.post(
    "/interview/sessions/{session_id}/feedback",
    dependencies=[Depends(require_session_token)],
    response_model=FeedbackResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Submit candidate experience feedback",
    description=(
        "Records the candidate's post-interview feedback.\n\n"
        "**Fields:**\n"
        "- `rating` — integer 1–5 (required)\n"
        "- `experience_comment` — optional free-text comment\n"
        "- `technical_issues` — boolean, whether the candidate experienced tech problems\n"
        "- `would_recommend` — boolean, NPS-style recommendation flag\n\n"
        "Only one feedback record is allowed per session. "
        "**Errors:** `400` if feedback was already submitted."
    ),
    tags=["Interview"],
)
def submit_feedback(
    session_id: int,
    payload: FeedbackRequest,
    db: Session = Depends(get_db),
):
    session = _get_session_or_404(session_id, db)
    if session.feedback:
        raise HTTPException(status_code=400, detail="Feedback already submitted for this session")

    fb = CandidateFeedback(
        session_id=session_id,
        rating=payload.rating,
        experience_comment=payload.experience_comment,
        technical_issues=payload.technical_issues,
        would_recommend=payload.would_recommend,
    )
    db.add(fb)
    db.commit()
    db.refresh(fb)

    return FeedbackResponse(feedback_id=fb.id, message="Thank you for your feedback!")


@router.get(
    "/interview/sessions/{session_id}/slots",
    dependencies=[Depends(require_session_token)],
    response_model=List[MeetingSlotRead],
    summary="List available meeting slots",
    description=(
        "Returns all unbooked future meeting slots for the job associated with this session.\n\n"
        "Slots are ordered by `start_time` ascending. Only slots where `is_booked = false` "
        "and `start_time > now (UTC)` are returned.\n\n"
        "Slots are created by admins via `POST /jobs/{job_id}/meeting-slots`."
    ),
    tags=["Interview"],
)
def get_meeting_slots(session_id: int, db: Session = Depends(get_db)):
    session = _get_session_or_404(session_id, db)
    slots = (
        db.query(MeetingSlot)
        .filter(
            MeetingSlot.job_id == session.job_id,
            MeetingSlot.is_booked == False,
            MeetingSlot.start_time > datetime.utcnow(),
        )
        .order_by(MeetingSlot.start_time)
        .all()
    )
    return slots


@router.post(
    "/interview/sessions/{session_id}/schedule-meeting",
    dependencies=[Depends(require_session_token)],
    response_model=ScheduleMeetingResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Book a post-interview meeting slot",
    description=(
        "Books one of the available meeting slots for the candidate.\n\n"
        "**Side effects:**\n"
        "- Sets `meeting_slots.is_booked = true` (atomic — prevents double-booking).\n"
        "- Creates a `MeetingSchedule` record linking the session, slot, and candidate.\n\n"
        "Only one meeting can be scheduled per session. "
        "**Errors:** `400` if already scheduled · `404` if the slot is unavailable or already booked."
    ),
    tags=["Interview"],
)
def schedule_meeting(
    session_id: int,
    payload: ScheduleMeetingRequest,
    db: Session = Depends(get_db),
):
    session = _get_session_or_404(session_id, db)
    if session.meeting_schedule:
        raise HTTPException(status_code=400, detail="Meeting already scheduled for this session")

    # The slot must belong to THIS session's job: filtering on id alone let a
    # candidate book (and so block) any tenant's slot by trying slot ids, and
    # read its times back in the response.
    # FOR UPDATE: two candidates racing for the same slot both passed the
    # is_booked check and double-booked it. The loser now waits, re-checks
    # is_booked after the winner commits, and gets the 404.
    slot = db.query(MeetingSlot).filter(
        MeetingSlot.id == payload.slot_id,
        MeetingSlot.job_id == session.job_id,
        MeetingSlot.is_booked == False,
        MeetingSlot.start_time > datetime.utcnow(),
    ).with_for_update().first()
    if not slot:
        raise HTTPException(status_code=404, detail="Slot not available")

    slot.is_booked = True
    schedule = MeetingSchedule(
        session_id=session_id,
        slot_id=slot.id,
        candidate_id=session.candidate_id,
    )
    db.add(schedule)
    db.commit()
    db.refresh(schedule)

    return ScheduleMeetingResponse(
        schedule_id=schedule.id,
        slot_id=slot.id,
        start_time=slot.start_time,
        end_time=slot.end_time,
        message="Meeting scheduled successfully!",
    )


@router.post(
    "/interview/sessions/{session_id}/upload-recording",
    dependencies=[Depends(require_session_token)],
    response_model=RecordingUploadResponse,
    summary="Upload interview recording",
    description="Stores the candidate's interview video recording and updates the candidate record with the link and duration.",
    tags=["Interview"],
)
@limiter.limit("10/minute")
async def upload_recording(
    session_id: int,
    request: Request,
    file: UploadFile = File(...),
    duration: str = Form(default=""),
    recording_id: str = Form(default=""),
    recovery: str = Form(default=""),
    db: Session = Depends(get_db),
):
    session = _get_session_or_404(session_id, db)

    _upload_root = UPLOAD_ROOT
    os.makedirs(f"{_upload_root}/recordings", exist_ok=True)
    # Server-chosen extension. It used to be taken from the client filename,
    # which decided both the stored name and — while /uploads was public — the
    # Content-Type it was served with, so an "x.html" upload became HTML on the
    # API origin and repointed the candidate's recording link at it.
    # Fixed, not merely allow-listed: letting the client pick ".mp4" also side-
    # stepped the "keep the larger existing file" guard below, which compares
    # against {sid}{ext} — a different path — so a 1-byte ".mp4" could repoint
    # the candidate's recording link while {sid}.webm sat untouched on disk.
    ext = ".webm"
    final_path = f"{_upload_root}/recordings/{session_id}{ext}"
    # Refuse only to REPLACE evidence that already exists. A blanket "interview
    # is over" check would kill orphan recovery, which re-uploads a recording
    # the browser kept in IndexedDB on some later app load (hours or days after
    # a crash) — the only path left to rescue a video the server never got.
    if os.path.isfile(final_path):
        _reject_late_evidence(session)
    # Stream to a temp path first so a partial write never clobbers a good
    # prior recording. We promote to the final path only after we've verified
    # the upload is non-empty AND at least as big as what's already on disk.
    # uuid, not a second-resolution timestamp: two uploads for one session in
    # the same second shared a temp path, and the first os.replace() renamed it
    # out from under the second writer, corrupting the result.
    tmp_path = f"{final_path}.incoming-{uuid.uuid4().hex}"

    who = _candidate_label(db, session)
    tag = f"{who} recording_id={recording_id or '-'} recovery={bool(recovery)}"
    try:
        total_size = 0
        with open(tmp_path, "wb") as f:
            while chunk := await file.read(65536):  # 64KB chunks for faster streaming
                total_size += len(chunk)
                # A 45-minute interview is ~750 MB at the highest quality tier;
                # anything past this is not a recording, and the loop had no
                # cap at all, so one caller could fill the disk.
                if total_size > MAX_RECORDING_SIZE:
                    raise HTTPException(status_code=413, detail="Recording is too large.")
                f.write(chunk)
        _rec_logger.info(f"[Recording] Wrote {total_size / (1024*1024):.2f} MB to {tmp_path} ({tag})")
    except HTTPException:
        # e.g. the 413 above — keep the real status (the client's telemetry and
        # its retry policy both key on it) instead of reporting a generic 500.
        try:
            if os.path.isfile(tmp_path):
                os.remove(tmp_path)
        except OSError:
            pass
        raise
    except Exception as e:
        logging.error(f"[Recording] FAILED to save for session {session_id}: {e} ({tag})")
        try:
            if os.path.isfile(tmp_path):
                os.remove(tmp_path)
        except OSError:
            pass
        # The exception text (server file paths) stays in the log above.
        raise HTTPException(status_code=500, detail="Failed to save recording. Please retry.")

    # Guard rail: never accept a zero-byte upload. This has been a silent
    # loss mode in the past — the client hit the endpoint but no bytes
    # landed, and we'd still overwrite a good file with a blank one.
    if total_size == 0:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        logging.error(f"[Recording] REJECTED empty upload ({tag})")
        raise HTTPException(status_code=400, detail="Recording is empty (0 bytes)")

    # Guard rail: if there's already a recording on disk that is larger than
    # what we just received, keep the existing one. A re-upload that's
    # smaller than the known-good copy almost always means a partial or a
    # corrupted retry, and replacing would be a silent data loss. Operators
    # can still inspect the `.incoming-*` sidecar if they need to recover.
    if os.path.isfile(final_path):
        try:
            existing_size = os.path.getsize(final_path)
        except OSError:
            existing_size = 0
        if existing_size > total_size:
            logging.warning(
                f"[Recording] Keeping existing {existing_size} bytes at {final_path}; "
                f"rejected replacement of only {total_size} bytes. "
                f"Sidecar preserved at {tmp_path} ({tag})"
            )
            return RecordingUploadResponse(
                recording_link=f"/uploads/recordings/{session_id}{ext}",
                video_duration=duration or "0:00",
                message="Existing recording is larger — preserved; new upload kept as sidecar.",
            )

    try:
        os.replace(tmp_path, final_path)
    except OSError as e:
        logging.error(f"[Recording] FAILED to promote tmp file: {e} ({tag})")
        raise HTTPException(status_code=500, detail="Failed to finalise recording. Please retry.")

    file_path = final_path
    recording_link = f"/uploads/recordings/{session_id}{ext}"

    # ── Fix WebM metadata ────────────────────────────────────────────────────
    # Chrome's MediaRecorder writes WebM files WITHOUT duration metadata or a
    # seek index (Cues).  This means browsers report duration=Infinity and the
    # seekbar is broken.  A quick ffmpeg remux (copy, no re-encode) writes the
    # proper headers in ~1-2 seconds even for long recordings.
    if ext.lower() == ".webm":
        try:
            from app.main import _FFMPEG as _ff
            if _ff:
                fixed_path = file_path + ".fixed.webm"
                proc = await asyncio.to_thread(
                    subprocess.run,
                    [_ff, "-y", "-i", file_path, "-c", "copy", fixed_path],
                    capture_output=True, timeout=60,
                )
                if proc.returncode == 0 and os.path.isfile(fixed_path):
                    os.replace(fixed_path, file_path)
                    logging.info("[Recording] Remuxed %s — seekbar metadata fixed", file_path)
                else:
                    # Remux failed — keep the original (seekbar won't work but video still plays)
                    if os.path.isfile(fixed_path):
                        os.remove(fixed_path)
                    logging.warning("[Recording] Remux failed for %s: %s",
                                    file_path, (proc.stderr or b"").decode(errors="replace")[-200:])
        except Exception as exc:
            logging.warning("[Recording] Remux skipped for %s: %s", file_path, exc)

    # Update candidate record. The file is already durable on disk at this
    # point — even if this DB commit fails, the recording is recoverable from
    # /uploads/recordings/{session_id}.* and an operator can repair the link.
    # We retry the commit on transient errors so we don't fail this loud.
    db_link_saved = False
    if session.candidate_id:
        candidate = db.query(Candidate).filter(Candidate.id == session.candidate_id).first()
        if candidate:
            candidate.recording_link = recording_link
            candidate.video_duration = duration or "0:00"
            for attempt in range(1, 4):
                try:
                    db.commit()
                    db_link_saved = True
                    break
                except Exception as link_exc:
                    logging.error(
                        f"[Recording] candidate.recording_link commit attempt {attempt}/3 "
                        f"failed for session {session_id}: {link_exc}"
                    )
                    try:
                        db.rollback()
                    except Exception:
                        pass
                    if attempt == 3:
                        # File is durable on disk; DB link update failed. Log
                        # loud so an operator can repair, but return success
                        # because the candidate's data is NOT lost.
                        logging.error(
                            f"[Recording] FILE SAVED but DB link NOT updated for session "
                            f"{session_id}. Recover via: UPDATE candidates SET "
                            f"recording_link='{recording_link}', video_duration='"
                            f"{duration or '0:00'}' WHERE id={session.candidate_id};"
                        )
                    else:
                        import time as _t
                        _t.sleep(attempt * 0.5)

    # Definitive, candidate-named outcome line so an operator can confirm at a
    # glance whether THIS candidate's video landed on the server.
    if db_link_saved or not session.candidate_id:
        _rec_logger.info(
            f"[Recording] SAVED OK (single-shot) for {who} — "
            f"{total_size / (1024*1024):.2f} MB -> {recording_link}"
        )
    else:
        _rec_logger.error(
            f"[Recording] SAVE PARTIAL for {who} — file is on disk at {recording_link} "
            f"but the DB link could NOT be updated (manual repair needed)."
        )

    return RecordingUploadResponse(
        recording_link=recording_link,
        video_duration=duration or "0:00",
        message=(
            "Recording saved successfully."
            if db_link_saved or not session.candidate_id
            else "Recording file saved; link update pending — admin will be notified."
        ),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Live incremental recording upload
#
# The single-shot upload above sends the whole recording in ONE request at the
# END of the interview — so if the candidate closes the tab/browser/laptop, the
# entire video is lost. To make the recording survive ANY way the session ends,
# the client instead streams the recording WHILE the interview runs: each batch
# of MediaRecorder chunks is POSTed as a "part" to /recording-part, then
# /recording-finalize stitches the parts (in order) into the final .webm.
#
# By the time the interview ends the video is already on the server, so a
# tab/browser/laptop close can no longer lose it. The only unrecoverable bytes
# are the last ~few seconds being recorded at the instant of a hard power-off
# that never comes back — those never left the device.
#
# Parts are named by their START chunk index, zero-padded, so finalize can sort
# and concatenate them in order. Re-uploading a range overwrites the same file
# (idempotent). MediaRecorder chunks of one recording, concatenated in order,
# form a valid WebM — which is exactly what the single-shot path already relied
# on client-side.
# ─────────────────────────────────────────────────────────────────────────────

def _recording_upload_root() -> str:
    _is_serverless = bool(os.environ.get("VERCEL") or os.environ.get("AWS_LAMBDA_FUNCTION_NAME"))
    return "/tmp/uploads" if _is_serverless else "uploads"


# Logger under the "app" namespace. main.py sets the ROOT logger to WARNING but
# "app" to INFO, so bare logging.info() is suppressed — these outcome lines must
# go through an app.* logger to actually appear in the Dokploy logs.
_rec_logger = logging.getLogger("app.recording")
_proctor_logger = logging.getLogger("app.proctor")


# Server-side face detection lives in app.utils.face_detection so this router
# and the secondary-camera router share one cached classifier — and one
# warning when the installed OpenCV cannot provide one, instead of a failure
# logged per screenshot.
from app.utils.face_detection import count_faces_in_image, image_size_is_safe  # noqa: E402


def _candidate_label(db: Session, session: InterviewSession) -> str:
    """Human-readable candidate identity for recording logs, e.g.
    "candidate 'Adarsh Sasi' <adarsh@x.com> (id=21, session=16)". So an operator
    can see exactly WHOSE video was (or wasn't) saved."""
    sid = getattr(session, "id", "?")
    try:
        if session and session.candidate_id:
            c = db.query(Candidate).filter(Candidate.id == session.candidate_id).first()
            if c:
                return (
                    f"candidate '{c.name or 'Unknown'}' "
                    f"<{c.email or 'no-email'}> (id={c.id}, session={sid})"
                )
        return f"candidate (none linked) (session={sid})"
    except Exception:
        return f"candidate (lookup failed) (session={sid})"


def _parts_dir(upload_root: str, session_id: int) -> str:
    return f"{upload_root}/recordings/.parts/{session_id}"


def _sanitize_recid(recording_id: str) -> str:
    """Filesystem-safe segment-dir name for a recording id. Empty (legacy
    clients that never send one) maps to '_legacy'."""
    rid = "".join(c for c in (recording_id or "").strip() if c.isalnum() or c in "-_")
    return rid or "_legacy"


def _reject_late_evidence(session) -> None:
    """Proctoring evidence may only be written while the interview is live, or
    during a short grace window after it ends — the client legitimately finishes
    uploading its tail then.

    Session tokens are never rotated, so without this a candidate who was
    auto-terminated for cheating keeps a valid token and can replace their
    recording, identity photo or screenshots days later, and the recruiter's
    evaluation page would show the fabricated evidence."""
    from datetime import timedelta

    # Generous windows on purpose: a 700 MB upload over a poor connection, with
    # retries, legitimately runs for a long time after /complete stamps
    # completed_at, and an auto-terminated candidate's upload is exactly the
    # evidence worth keeping. Tampering is bounded by the callers, which only
    # apply this when something would be overwritten.
    grace = {
        SessionStatus.completed: timedelta(hours=6),
        SessionStatus.terminated: timedelta(hours=1),
    }.get(session.status)
    if grace is None:
        return  # pending / in_progress — the interview is still live
    ended = session.completed_at or datetime.utcnow()
    if datetime.utcnow() - ended > grace:
        raise HTTPException(
            status_code=409,
            detail="This interview has ended — new recordings or photos are no longer accepted.",
        )


def _session_is_terminal(session) -> bool:
    """True once the interview is genuinely over (completed/terminated) — i.e.
    the candidate can no longer rejoin and EXTEND the recording. Only then is it
    safe to DELETE the live parts; deleting earlier would discard a segment a
    returning candidate is about to continue past (resume data loss)."""
    try:
        return session is not None and session.status in (
            SessionStatus.completed, SessionStatus.terminated,
        )
    except Exception:
        return False


def _segment_dirs(session_parts_dir: str):
    """Return the per-recording-session segment directories under a session's
    parts dir, OLDEST FIRST, so a drop+rejoin assembles in chronological order.

    Handles two layouts so a deploy mid-interview never loses data:
      - new:    .parts/{sid}/{recording_id}/*.bin   (one subdir per recording)
      - legacy: .parts/{sid}/*.bin                  (flat = a single segment)
    """
    if not os.path.isdir(session_parts_dir):
        return []

    def _oldest_mtime(d: str, names) -> float:
        try:
            return min(os.path.getmtime(os.path.join(d, n)) for n in names)
        except (OSError, ValueError):
            return 0.0

    try:
        entries = os.listdir(session_parts_dir)
    except OSError:
        return []

    segments = []  # (sort_key, dir_path)
    flat_bins = [f for f in entries if f.endswith(".bin")]
    if flat_bins:
        segments.append((_oldest_mtime(session_parts_dir, flat_bins), session_parts_dir))
    for e in entries:
        sub = os.path.join(session_parts_dir, e)
        if os.path.isdir(sub):
            try:
                sub_bins = [f for f in os.listdir(sub) if f.endswith(".bin")]
            except OSError:
                sub_bins = []
            if sub_bins:
                segments.append((_oldest_mtime(sub, sub_bins), sub))

    segments.sort(key=lambda x: x[0])
    return [d for _, d in segments]


def _concat_segment_parts(seg_dir: str, out_path: str) -> int:
    """Byte-concatenate ONE recording session's parts (in chunk-index order)
    into out_path. The parts of a single MediaRecorder session, joined in order,
    are a valid WebM. Returns bytes written (0 if none)."""
    try:
        part_files = sorted(
            (f for f in os.listdir(seg_dir) if f.endswith(".bin")),
            key=lambda n: int(n.split(".")[0]),
        )
    except (ValueError, OSError):
        try:
            part_files = sorted(f for f in os.listdir(seg_dir) if f.endswith(".bin"))
        except OSError:
            return 0
    total = 0
    with open(out_path, "wb") as out:
        for name in part_files:
            with open(os.path.join(seg_dir, name), "rb") as fin:
                while True:
                    buf = fin.read(1 << 20)
                    if not buf:
                        break
                    out.write(buf)
                    total += len(buf)
    return total


def _ffmpeg_concat(seg_files, out_path: str) -> bool:
    """Join independent WebM segments into one valid file via ffmpeg's concat
    demuxer (stream copy, no re-encode). Raw byte-concatenation of separate
    MediaRecorder sessions does NOT produce a valid single video — each has its
    own header — so this is required when a candidate dropped and rejoined."""
    try:
        from app.main import _FFMPEG as _ff
        if not _ff:
            return False
        list_path = out_path + ".concat.txt"
        with open(list_path, "w") as lf:
            for sf in seg_files:
                # Forward slashes only: ffmpeg's concat demuxer treats backslash
                # as an escape char, so Windows paths must be normalised (and it
                # accepts '/' on Windows too).
                safe = os.path.abspath(sf).replace("\\", "/")
                lf.write(f"file '{safe}'\n")
        proc = subprocess.run(
            # `-f webm` forces the output muxer: the caller's out_path is a
            # temp name like "{sid}.webm.assembling-<ts>" with no recognised
            # extension, so without this ffmpeg can't pick a format and fails.
            [_ff, "-y", "-f", "concat", "-safe", "0", "-i", list_path,
             "-c", "copy", "-f", "webm", out_path],
            capture_output=True, timeout=300,
        )
        try:
            os.remove(list_path)
        except OSError:
            pass
        return proc.returncode == 0 and os.path.isfile(out_path) and os.path.getsize(out_path) > 0
    except Exception as exc:
        logging.warning(f"[Recording] ffmpeg concat failed: {exc}")
        return False


def _remux_webm_inplace(path: str) -> None:
    """Rewrite WebM headers so duration/seekbar work (copy, no re-encode)."""
    try:
        from app.main import _FFMPEG as _ff
        if not _ff:
            return
        fixed = path + ".fixed.webm"
        proc = subprocess.run(
            [_ff, "-y", "-i", path, "-c", "copy", fixed],
            capture_output=True, timeout=120,
        )
        if proc.returncode == 0 and os.path.isfile(fixed):
            os.replace(fixed, path)
            logging.info("[Recording] Remuxed %s — seekbar metadata fixed", path)
        elif os.path.isfile(fixed):
            os.remove(fixed)
    except Exception as exc:
        logging.warning("[Recording] Remux skipped for %s: %s", path, exc)


def _concat_parts_to_webm(upload_root: str, session_id: int) -> Optional[tuple]:
    """Assemble the final .webm from a session's live-uploaded parts.

    Each recording SEGMENT (one MediaRecorder session — there is more than one
    only when the candidate dropped and rejoined) is byte-concatenated from its
    own parts, then segments are joined in chronological order. A single segment
    is already a valid WebM (fast path). Multiple segments are joined with
    ffmpeg's concat demuxer, because byte-concatenating independent recordings
    produces an INVALID file that plays only the first segment — which was a
    silent video-loss path for every drop+rejoin interview.

    Idempotent and size-guarded: never replaces a larger existing file with a
    smaller assembly. Returns (final_path, total_bytes) or None if no parts."""
    pd = _parts_dir(upload_root, session_id)
    segments = _segment_dirs(pd)
    if not segments:
        return None

    final_path = f"{upload_root}/recordings/{session_id}.webm"
    os.makedirs(f"{upload_root}/recordings", exist_ok=True)
    seg_tmpdir = f"{final_path}.segs-{int(time.time())}"
    tmp = f"{final_path}.assembling-{int(time.time())}"
    total = 0
    try:
        os.makedirs(seg_tmpdir, exist_ok=True)
        seg_files = []
        for i, seg in enumerate(segments):
            seg_out = os.path.join(seg_tmpdir, f"seg_{i:03d}.webm")
            if _concat_segment_parts(seg, seg_out) > 0:
                seg_files.append(seg_out)

        if not seg_files:
            return None

        if len(seg_files) == 1:
            # Single recording session — already a valid WebM.
            os.replace(seg_files[0], tmp)
        else:
            # Candidate dropped + rejoined → multiple independent recordings.
            if not _ffmpeg_concat(seg_files, tmp):
                # ffmpeg unavailable: byte-join as a last resort so at least the
                # first segment is recoverable. Log loudly — this file may only
                # play the first segment in some players.
                _rec_logger.warning(
                    f"[Recording] session {session_id}: joining {len(seg_files)} "
                    f"resume segments WITHOUT ffmpeg — result may play only the "
                    f"first segment. Install/enable ffmpeg for a clean join."
                )
                with open(tmp, "wb") as out:
                    for sf in seg_files:
                        with open(sf, "rb") as fin:
                            shutil.copyfileobj(fin, out)
        total = os.path.getsize(tmp) if os.path.isfile(tmp) else 0
    except Exception as e:
        logging.error(f"[Recording] concat failed for session {session_id}: {e}")
        for p in (tmp,):
            try:
                if os.path.isfile(p):
                    os.remove(p)
            except OSError:
                pass
        return None
    finally:
        shutil.rmtree(seg_tmpdir, ignore_errors=True)

    if total == 0:
        try:
            os.remove(tmp)
        except OSError:
            pass
        return None

    if os.path.isfile(final_path):
        try:
            existing = os.path.getsize(final_path)
        except OSError:
            existing = 0
        if existing >= total:
            # A bigger/equal recording already exists (e.g. the single-shot
            # upload landed). Keep it; drop our assembly.
            try:
                os.remove(tmp)
            except OSError:
                pass
            return (final_path, existing)

    try:
        os.replace(tmp, final_path)
    except OSError as e:
        logging.error(f"[Recording] promote assembled file failed session {session_id}: {e}")
        return None
    return (final_path, total)


def _save_recording_link(db: Session, session: InterviewSession, recording_link: str, duration: str) -> bool:
    """Persist candidate.recording_link with the same retry policy as the
    single-shot path. Returns True if the DB link was saved."""
    if not session.candidate_id:
        return False
    candidate = db.query(Candidate).filter(Candidate.id == session.candidate_id).first()
    if not candidate:
        return False
    candidate.recording_link = recording_link
    if duration:
        candidate.video_duration = duration
    for attempt in range(1, 4):
        try:
            db.commit()
            return True
        except Exception as exc:
            logging.error(f"[Recording] link commit attempt {attempt}/3 failed (session {session.id}): {exc}")
            try:
                db.rollback()
            except Exception:
                pass
            if attempt < 3:
                time.sleep(attempt * 0.5)
    return False


@router.post(
    "/interview/sessions/{session_id}/recording-part",
    dependencies=[Depends(require_session_token)],
    status_code=200,
    summary="Upload one part of an in-progress interview recording",
    tags=["Interview"],
)
@limiter.limit("600/minute")
async def upload_recording_part(
    session_id: int,
    request: Request,
    file: UploadFile = File(...),
    start_index: int = Form(...),
    recording_id: str = Form(default=""),
    db: Session = Depends(get_db),
):
    session = _get_session_or_404(session_id, db)
    _reject_late_evidence(session)
    upload_root = _recording_upload_root()
    # Store parts under a per-recording subdir so a drop+rejoin (which mints a
    # NEW recording id) cannot overwrite the earlier segment's parts. Assembly
    # joins all segments in order.
    pd = f"{_parts_dir(upload_root, session_id)}/{_sanitize_recid(recording_id)}"
    os.makedirs(pd, exist_ok=True)

    if start_index < 0:
        raise HTTPException(status_code=422, detail="start_index must be >= 0")
    # Disk-fill guard: parts had no size, count or total limit, so one session
    # token could fill the volume every tenant's recordings are written to.
    used = _session_part_bytes(upload_root, session_id)
    if used >= MAX_SESSION_PART_BYTES:
        raise HTTPException(status_code=413, detail="Recording storage limit reached for this session")

    part_path = f"{pd}/{start_index:012d}.bin"
    tmp = f"{part_path}.incoming-{int(time.time() * 1000)}"
    total = 0
    try:
        with open(tmp, "wb") as f:
            while chunk := await file.read(65536):
                total += len(chunk)
                if total > MAX_RECORDING_PART_BYTES:
                    raise HTTPException(status_code=413, detail="Recording part too large")
                f.write(chunk)
        if total == 0:
            os.remove(tmp)
            return {"ok": True, "skipped": "empty", "start_index": start_index}
        os.replace(tmp, part_path)
        _PART_BYTES_BY_SESSION[session_id] = used + total
        _rec_logger.info(
            f"[Recording] live chunk saved — session {session_id} "
            f"start_index={start_index} ({total / 1024:.0f} KB)"
        )
    except Exception as e:
        try:
            if os.path.isfile(tmp):
                os.remove(tmp)
        except OSError:
            pass
        if isinstance(e, HTTPException):
            raise
        logging.error(f"[Recording] part {start_index} save failed session={session_id}: {e}")
        raise HTTPException(status_code=500, detail="part save failed")
    return {"ok": True, "bytes": total, "start_index": start_index}


# A live part is a few seconds of video (~1-3 MB); a whole 45-minute interview
# is ~750 MB at the highest quality. Both ceilings sit far above real use.
MAX_RECORDING_PART_BYTES = 64 * 1024 * 1024
MAX_SESSION_PART_BYTES = 4 * 1024 * 1024 * 1024
# Bytes of live parts on disk per session, counted once from disk (after a
# restart) and then kept up to date as parts arrive.
_PART_BYTES_BY_SESSION: Dict[int, int] = {}


def _session_part_bytes(upload_root: str, session_id: int) -> int:
    cached = _PART_BYTES_BY_SESSION.get(session_id)
    if cached is not None:
        return cached
    total = 0
    for dirpath, _dirs, files in os.walk(_parts_dir(upload_root, session_id)):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(dirpath, name))
            except OSError:
                pass
    _PART_BYTES_BY_SESSION[session_id] = total
    return total


@router.post(
    "/interview/sessions/{session_id}/recording-finalize",
    dependencies=[Depends(require_session_token)],
    response_model=RecordingUploadResponse,
    summary="Stitch uploaded recording parts into the final video",
    tags=["Interview"],
)
@limiter.limit("30/minute")
async def finalize_recording(
    session_id: int,
    request: Request,
    recording_id: str = Form(default=""),
    duration: str = Form(default=""),
    db: Session = Depends(get_db),
):
    session = _get_session_or_404(session_id, db)
    who = _candidate_label(db, session)
    upload_root = _recording_upload_root()

    result = await asyncio.to_thread(_concat_parts_to_webm, upload_root, session_id)
    if not result:
        _rec_logger.error(
            f"[Recording] SAVE FAILED for {who} — no uploaded parts found to "
            f"finalize (recording never reached the server)."
        )
        raise HTTPException(status_code=404, detail="No recording parts to finalize")
    final_path, total = result
    await asyncio.to_thread(_remux_webm_inplace, final_path)

    recording_link = f"/uploads/recordings/{session_id}.webm"
    db_saved = _save_recording_link(db, session, recording_link, duration or "0:00")

    # Parts are stitched and the link is saved. Only DELETE the parts once the
    # interview is genuinely over (completed/terminated). If the session is still
    # resumable, keep the parts so a rejoining candidate's earlier segment can be
    # re-joined with the new one; the sweeper cleans them up later.
    if db_saved or not session.candidate_id:
        if _session_is_terminal(session):
            shutil.rmtree(_parts_dir(upload_root, session_id), ignore_errors=True)
        _rec_logger.info(
            f"[Recording] SAVED OK (finalized from live parts) for {who} — "
            f"{total / (1024*1024):.2f} MB -> {recording_link}"
        )
    else:
        _rec_logger.error(
            f"[Recording] SAVE PARTIAL for {who} — file assembled at {recording_link} "
            f"but the DB link could NOT be updated (manual repair needed)."
        )
    return RecordingUploadResponse(
        recording_link=recording_link,
        video_duration=duration or "0:00",
        message="Recording finalized from live-uploaded parts.",
    )


# At most two assemblies at a time, and never two for the same session. This
# task is queued by /complete (twice), by the tab-close beacon and by the
# sweeper, and each run waits 45s and then runs ffmpeg. While it was a plain
# `def`, Starlette ran it in the AnyIO worker threadpool — the same ~40 threads
# that serve every sync route — so one candidate could stall the API for every
# tenant just by re-triggering it.
_FINALIZE_SLOTS = asyncio.Semaphore(2)
_finalize_in_flight: set = set()


async def _finalize_recording_safety_net(session_id: int):
    """Server-side backstop: if the candidate's browser died before calling
    /recording-finalize, stitch whatever parts DID arrive into a playable video
    so a recording is present even when the client never finishes. Idempotent
    and size-guarded, so it can never clobber a good single-shot upload."""
    upload_root = _recording_upload_root()
    if not os.path.isdir(_parts_dir(upload_root, session_id)):
        return  # no live parts — nothing to recover
    if session_id in _finalize_in_flight:
        return  # already waiting from /complete or the tab-close beacon
    _finalize_in_flight.add(session_id)
    try:
        # Give the client a window to upload its tail + call finalize itself first.
        await asyncio.sleep(45)
        async with _FINALIZE_SLOTS:
            await asyncio.to_thread(_finalize_recording_now, session_id)
    finally:
        _finalize_in_flight.discard(session_id)


def _finalize_recording_now(session_id: int):
    """The blocking half of the safety net (ffmpeg + DB). Always run in a thread."""
    upload_root = _recording_upload_root()
    pd = _parts_dir(upload_root, session_id)
    # If a full recording already exists (client finalize or single-shot landed),
    # only re-stitch if parts somehow total MORE (handled by the size guard).
    result = _concat_parts_to_webm(upload_root, session_id)
    if not result:
        return
    fp, total = result
    _remux_webm_inplace(fp)
    db = SessionLocal()
    try:
        session = db.query(InterviewSession).filter(InterviewSession.id == session_id).first()
        if session:
            who = _candidate_label(db, session)
            saved = _save_recording_link(db, session, f"/uploads/recordings/{session_id}.webm", "")
            if saved:
                # Only delete parts when the interview is genuinely over. This
                # safety-net also fires on a tab-close DROP (via the beacon),
                # where the session is still resumable — deleting then would lose
                # the pre-drop segment when the candidate rejoins. The sweeper
                # cleans non-terminal sessions up after the resume window.
                if _session_is_terminal(session):
                    shutil.rmtree(pd, ignore_errors=True)
                _rec_logger.info(
                    f"[Recording] SAVED OK (safety-net) for {who} — "
                    f"{total / (1024*1024):.2f} MB. Client never called finalize; "
                    f"server assembled the uploaded parts."
                )
            else:
                _rec_logger.error(
                    f"[Recording] SAVE PARTIAL (safety-net) for {who} — file assembled "
                    f"from parts but the DB link could NOT be updated (manual repair needed)."
                )
        else:
            _rec_logger.error(
                f"[Recording] SAVE FAILED (safety-net) — session {session_id} not found "
                f"in DB; assembled {total / (1024*1024):.2f} MB of parts but cannot link it."
            )
    finally:
        db.close()


# A recording is only "stale" (safe to assemble + clean up) once NO new live
# part has arrived for this long. While an interview is running, the client
# POSTs a part every ~4 s; 10 minutes also matches the resume grace window, so
# we never assemble/delete parts a returning candidate might still extend.
_STALE_PARTS_SECONDS = 600


# Upload sidecars ({sid}.webm.incoming-*) are deliberately kept when an upload is
# rejected for being smaller than the recording already on disk, so an operator
# can recover from one. Nothing ever reaped them, and each can be up to
# MAX_RECORDING_SIZE.
_INCOMING_TTL_SECONDS = 24 * 3600


def _reap_stale_incoming(upload_root: str) -> int:
    """Delete upload sidecars older than a day. Never raises."""
    rec_dir = f"{upload_root}/recordings"
    cutoff = time.time() - _INCOMING_TTL_SECONDS
    removed = 0
    try:
        names = os.listdir(rec_dir)
    except OSError:
        return 0
    for name in names:
        if ".incoming-" not in name:
            continue
        path = os.path.join(rec_dir, name)
        try:
            if os.path.isfile(path) and os.path.getmtime(path) < cutoff:
                os.remove(path)
                removed += 1
        except OSError:
            continue
    if removed:
        _rec_logger.info(f"[Recording] sweeper removed {removed} stale .incoming-* sidecar(s)")
    return removed


def sweep_orphan_recording_parts() -> int:
    """Backstop sweeper: assemble any interview recordings whose live-uploaded
    parts were never stitched into a final video — e.g. the candidate's tab/
    laptop closed before /complete or /recording-finalize ran, so neither the
    client finalize nor the /complete safety-net fired. Idempotent and
    size-guarded (never clobbers a larger existing file). Only touches
    recordings with no new part for _STALE_PARTS_SECONDS, so an in-progress
    interview is never disturbed.

    Logs a candidate-named 'SAVED OK (sweeper)' line for each recovery so the
    backend terminal ALWAYS shows a confirmation that a finished interview's
    video landed on disk — even when the browser never told us the interview
    ended. Returns the number of recordings newly assembled."""
    upload_root = _recording_upload_root()
    _reap_stale_incoming(upload_root)
    parts_root = f"{upload_root}/recordings/.parts"
    if not os.path.isdir(parts_root):
        return 0
    try:
        session_dirs = os.listdir(parts_root)
    except OSError:
        return 0

    recovered = 0
    now = time.time()
    for name in session_dirs:
        if not name.isdigit():
            continue
        session_id = int(name)
        pd = f"{parts_root}/{name}"
        if not os.path.isdir(pd):
            continue

        # Collect .bin parts across BOTH layouts (legacy flat + per-recording
        # subdirs) by walking the session's parts tree.
        bin_paths = []
        try:
            for root, _dirs, files in os.walk(pd):
                for f in files:
                    if f.endswith(".bin"):
                        bin_paths.append(os.path.join(root, f))
        except OSError:
            continue
        if not bin_paths:
            continue

        # Skip if a part arrived recently — the interview may still be live OR
        # the candidate is within the resume window and about to extend it.
        try:
            newest = max(os.path.getmtime(p) for p in bin_paths)
        except OSError:
            continue
        if now - newest < _STALE_PARTS_SECONDS:
            continue

        final_path = f"{upload_root}/recordings/{session_id}.webm"
        try:
            parts_total = sum(os.path.getsize(p) for p in bin_paths)
        except OSError:
            parts_total = 0

        # If a final video already exists and is at least as large as the parts,
        # the recording is already saved — just drop the now-redundant parts.
        if os.path.isfile(final_path):
            try:
                if os.path.getsize(final_path) >= parts_total > 0:
                    shutil.rmtree(pd, ignore_errors=True)
                    continue
            except OSError:
                pass

        result = _concat_parts_to_webm(upload_root, session_id)
        if not result:
            continue
        fp, total = result
        _remux_webm_inplace(fp)

        db = SessionLocal()
        try:
            session = db.query(InterviewSession).filter(InterviewSession.id == session_id).first()
            if not session:
                _rec_logger.error(
                    f"[Recording] SWEEPER — assembled {total / (1024*1024):.2f} MB for "
                    f"session {session_id} but no matching DB session; left file at {fp}"
                )
                continue
            who = _candidate_label(db, session)
            saved = _save_recording_link(
                db, session, f"/uploads/recordings/{session_id}.webm", ""
            )
            if saved:
                shutil.rmtree(pd, ignore_errors=True)
                recovered += 1
                _rec_logger.info(
                    f"[Recording] SAVED OK (sweeper) for {who} — "
                    f"{total / (1024*1024):.2f} MB -> /uploads/recordings/{session_id}.webm "
                    f"(client never finalized; server recovered the live parts)"
                )
            else:
                _rec_logger.error(
                    f"[Recording] SWEEPER SAVE PARTIAL for {who} — assembled at "
                    f"/uploads/recordings/{session_id}.webm but the DB link could NOT "
                    f"be updated (manual repair needed)."
                )
        finally:
            db.close()

    return recovered


@router.post(
    "/interview/sessions/{session_id}/capture-photo",
    dependencies=[Depends(require_session_token)],
    status_code=200,
    summary="Capture and save candidate profile photo",
    description="Accepts a JPEG photo from the candidate's webcam during the pre-interview photo capture step.",
    tags=["Interview"],
)
@limiter.limit("5/minute")
async def capture_candidate_photo(
    session_id: int,
    request: Request,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    session = db.query(InterviewSession).filter(InterviewSession.id == session_id).first()
    if not session or not session.candidate_id:
        raise HTTPException(status_code=404, detail="Session or candidate not found")
    _reject_late_evidence(session)

    candidate = db.query(Candidate).filter(Candidate.id == session.candidate_id).first()
    if not candidate:
        raise HTTPException(status_code=404, detail="Candidate not found")

    photo_dir = f"{UPLOAD_ROOT}/photos"
    os.makedirs(photo_dir, exist_ok=True)

    # Always a re-encoded JPEG under a server-chosen name. The extension used to
    # come from the client filename, so an ".html"/".svg" "photo" was stored —
    # and, while /uploads was public, served — as that type; a PIL failure was
    # swallowed, which kept the raw bytes. Unreadable uploads are now rejected.
    file_path = f"{photo_dir}/{candidate.id}.jpg"
    tmp_path = f"{file_path}.incoming-{uuid.uuid4().hex}"

    MAX_PHOTO_SIZE = 5 * 1024 * 1024  # 5 MB
    total_size = 0
    try:
        with open(tmp_path, "wb") as f:
            while chunk := await file.read(8192):
                total_size += len(chunk)
                if total_size > MAX_PHOTO_SIZE:
                    raise HTTPException(status_code=413, detail="Photo too large (max 5 MB)")
                f.write(chunk)

        # Compress photo: resize to max 400x500, JPEG quality 80
        from PIL import Image
        if not image_size_is_safe(tmp_path):
            raise ValueError("image dimensions too large or unreadable")
        with Image.open(tmp_path) as img:
            img = img.convert("RGB")
            img.thumbnail((400, 500), Image.LANCZOS)
            img.save(file_path, "JPEG", quality=80, optimize=True)
    except HTTPException:
        raise
    except Exception as exc:
        logging.warning(f"[Photo] rejected unreadable upload for candidate {candidate.id}: {exc}")
        raise HTTPException(status_code=400, detail="Photo could not be read. Please retake it.")
    finally:
        try:
            if os.path.isfile(tmp_path):
                os.remove(tmp_path)
        except OSError:
            pass

    relative_path = f"/uploads/photos/{candidate.id}.jpg"
    candidate.image = relative_path
    db.commit()

    return {"ok": True, "image_url": relative_path}


@router.post(
    "/interview/sessions/{session_id}/screenshot",
    dependencies=[Depends(require_session_token)],
    status_code=200,
    summary="Upload a periodic proctoring screenshot",
    description=(
        "Accepts a JPEG snapshot from the candidate's webcam, captured every 13–15 seconds "
        "by the frontend. Stored under `uploads/screenshots/{session_id}/`. "
        "Silent — never fails the interview if this call errors."
    ),
    tags=["Interview"],
)
@limiter.limit("10/minute")
async def upload_screenshot(
    session_id: int,
    request: Request,
    file: UploadFile = File(...),
    timestamp: str = Form(default=""),
    face_count: str = Form(default=""),
    db: Session = Depends(get_db),
):
    MAX_SCREENSHOT_SIZE = 2 * 1024 * 1024  # 2 MB per screenshot

    session = db.query(InterviewSession).filter(InterviewSession.id == session_id).first()
    if not session:
        return {"ok": False}
    # Silent (this endpoint must never fail the interview), but a finished
    # interview stops accepting new proctoring frames — see _reject_late_evidence.
    try:
        _reject_late_evidence(session)
    except HTTPException:
        return {"ok": False, "reason": "interview_ended"}

    screenshot_dir = f"{UPLOAD_ROOT}/screenshots/{session_id}"
    os.makedirs(screenshot_dir, exist_ok=True)

    # Server-generated name. The client timestamp used to land in the path, so a
    # "/" in it raised FileNotFoundError (a 500), and any character outside
    # [A-Za-z0-9_-] made the saved frame unservable through /media.
    filename = f"{datetime.utcnow().strftime('%Y%m%dT%H%M%S%f')}-{uuid.uuid4().hex[:8]}.jpg"
    raw_path = f"{screenshot_dir}/{filename}"

    total_size = 0
    with open(raw_path, "wb") as f:
        while chunk := await file.read(8192):
            total_size += len(chunk)
            if total_size > MAX_SCREENSHOT_SIZE:
                f.close()
                os.remove(raw_path)
                return {"ok": False, "reason": "too_large"}
            f.write(chunk)

    # Refuse images whose declared size would decode to gigabytes (checked from
    # the header, before PIL or OpenCV decode a single pixel).
    if not image_size_is_safe(raw_path):
        try:
            os.remove(raw_path)
        except OSError:
            pass
        return {"ok": False, "reason": "invalid_image"}

    # Compress screenshot: resize to max 800x600, JPEG quality 70
    try:
        from PIL import Image
        with Image.open(raw_path) as img:
            img.thumbnail((800, 600), Image.LANCZOS)
            img.save(raw_path, "JPEG", quality=70, optimize=True)
    except Exception:
        pass  # keep the original if compression fails

    relative_path = f"/uploads/screenshots/{session_id}/{filename}"

    # Count faces SERVER-SIDE (OpenCV). The browser no longer runs MediaPipe —
    # that ~300 MB in-tab model was the main OOM-crash driver. Fall back to any
    # client-provided hint (legacy clients), then to "assume single face".
    detected_faces = await asyncio.to_thread(count_faces_in_image, raw_path)
    if detected_faces is None:
        if face_count.strip():
            try:
                detected_faces = int(face_count)
            except ValueError:
                detected_faces = 1
        else:
            detected_faces = 1
    elif detected_faces != 1:
        # 0 = candidate not visible; >1 = another person in frame. Surface it in
        # the backend logs for the proctoring audit (NOT auto-terminated here:
        # Haar is less precise than MediaPipe, so a human reviews the flagged
        # screenshots rather than risk wrongly ending a genuine candidate).
        _proctor_logger.info(
            f"[Proctor] session {session_id} — screenshot face_count={detected_faces} "
            f"({'no face visible' if detected_faces == 0 else 'multiple faces in frame'}) "
            f"-> {relative_path}"
        )

    # Persist screenshot record to DB for proctoring audit trail. If the DB
    # write fails, the file IS already on disk — so we don't fail the request,
    # we just log loud so an operator can backfill the audit trail. The
    # candidate's interview must NOT halt over a screenshot DB hiccup.
    screenshot = InterviewScreenshot(
        session_id=session_id,
        file_path=relative_path,
        captured_at=datetime.utcnow(),
        face_count=detected_faces,
    )
    db_saved = False
    try:
        db.add(screenshot)
        db.commit()
        db_saved = True
    except Exception as db_exc:
        logging.error(
            f"[Screenshot] DB write failed for session {session_id} "
            f"(file IS saved at {relative_path}): {db_exc}"
        )
        try:
            db.rollback()
        except Exception:
            pass

    return {
        "ok": True,
        "path": relative_path,
        "face_count": detected_faces,
        "db_saved": db_saved,
    }


# ── Helpers ────────────────────────────────────────────────────────────────────

def _format_evaluation(ev) -> EvaluationRead:
    strengths = []
    weaknesses = []
    try:
        strengths = json.loads(ev.strengths) if ev.strengths else []
    except Exception:
        pass
    try:
        weaknesses = json.loads(ev.weaknesses) if ev.weaknesses else []
    except Exception:
        pass

    return EvaluationRead(
        id=ev.id,
        session_id=ev.session_id,
        candidate_id=ev.candidate_id,
        technical_score=ev.technical_score,
        communication_score=ev.communication_score,
        problem_solving_score=ev.problem_solving_score,
        confidence_score=ev.confidence_score,
        coding_score=getattr(ev, "coding_score", None),
        overall_score=ev.overall_score,
        recommendation=ev.recommendation.value if ev.recommendation else None,
        summary=ev.summary,
        strengths=strengths,
        weaknesses=weaknesses,
        detailed_feedback=ev.detailed_feedback,
        meets_threshold=ev.meets_threshold or False,
        created_at=ev.created_at,
    )


# ══════════════════════════════════════════════════════════════════════════════
# CODE EXECUTION ENDPOINTS
# ══════════════════════════════════════════════════════════════════════════════

@router.post(
    "/interview/sessions/{session_id}/run-code",
    dependencies=[Depends(require_session_token)],
    summary="Execute code against test cases during interview",
    description="Run candidate's code in a sandboxed environment. Returns test case results.",
)
@limiter.limit("30/minute")
async def run_code(
    request: Request,
    session_id: int,
    db: Session = Depends(get_db),
):
    """
    Execute code during an interview session.

    Body:
    {
        "language": "python",
        "code": "print(input())",
        "test_cases": [{"input": "hello", "expected_output": "hello", "description": "Echo test"}]
    }
    """
    body = await request.json()

    session = db.query(InterviewSession).filter(InterviewSession.id == session_id).first()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    if session.status not in (SessionStatus.in_progress, SessionStatus.pending):
        raise HTTPException(status_code=400, detail="Interview is not active")

    language = body.get("language", "python")
    code = body.get("code", "")
    test_cases = body.get("test_cases", [])

    if not code.strip():
        raise HTTPException(status_code=400, detail="Code cannot be empty")

    from app.services.code_execution_service import run_test_cases, execute_code

    if test_cases:
        result = await run_test_cases(language, code, test_cases, timeout=10)
    else:
        # Just run the code without test cases (free run)
        exec_result = await execute_code(language, code, stdin=body.get("stdin", ""), timeout=10)
        result = {
            "total": 0,
            "passed": 0,
            "failed": 0,
            "results": [],
            "output": exec_result,
        }

    return result


@router.post(
    "/interview/sessions/{session_id}/save-coding-submission",
    dependencies=[Depends(require_session_token)],
    summary="Persist a structured coding submission record",
    description=(
        "Lightweight endpoint that ONLY stores a `coding_submissions` row. "
        "Does not run code, does not trigger AI. The candidate UI calls "
        "this alongside the existing `/run-code` + transcript flow so that "
        "the recruiter dashboard has structured data (per-test results, "
        "language, code, timing) without disturbing the live AI conversation."
    ),
)
@limiter.limit("20/minute")
async def save_coding_submission(
    request: Request,
    session_id: int,
    db: Session = Depends(get_db),
):
    body = await request.json()

    session = db.query(InterviewSession).filter(InterviewSession.id == session_id).first()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    # Submissions belong to the live interview. Afterwards the session token
    # still works, and a submission added then was folded into any later
    # "regenerate evaluation".
    if session.status not in (SessionStatus.pending, SessionStatus.in_progress):
        raise HTTPException(status_code=409, detail="This interview has ended")

    code = (body.get("code") or "").strip()
    language = body.get("language", "python")
    if not code:
        raise HTTPException(status_code=400, detail="Code cannot be empty")
    if len(code) > 100_000:
        raise HTTPException(status_code=413, detail="Code is too long")

    test_result = body.get("test_result") or {}
    if not isinstance(test_result, dict):
        test_result = {}
    # The pass counts are still client-reported (see the security notes); at
    # least keep them self-consistent and bounded.
    try:
        _total = max(0, min(int(test_result.get("total", 0)), 100))
        _passed = max(0, min(int(test_result.get("passed", 0)), _total))
    except (TypeError, ValueError):
        _total, _passed = 0, 0
    test_result = dict(test_result, total=_total, passed=_passed)
    title = (body.get("title") or "").strip() or None
    description = (body.get("description") or "").strip() or None
    difficulty = (body.get("difficulty") or "").strip() or None
    question_index = body.get("question_index", session.current_question_index)

    submission = CodingSubmission(
        session_id=session.id,
        question_index=question_index,
        title=title,
        description=description,
        difficulty=difficulty,
        language=language,
        code=code,
        test_cases_total=int(test_result.get("total", 0)),
        test_cases_passed=int(test_result.get("passed", 0)),
        test_results=json.dumps(test_result.get("results", [])),
        execution_time_ms=body.get("execution_time_ms"),
    )
    db.add(submission)
    db.commit()
    db.refresh(submission)

    return {
        "id": submission.id,
        "session_id": submission.session_id,
        "question_index": submission.question_index,
        "language": submission.language,
        "test_cases_total": submission.test_cases_total,
        "test_cases_passed": submission.test_cases_passed,
        "submitted_at": submission.submitted_at.isoformat(),
    }


@router.get(
    "/interview/sessions/{session_id}/coding-submissions",
    summary="List coding submissions for a session",
    description="Returns all coding submissions made by the candidate during the session, in chronological order. Used by the recruiter evaluation page.",
)
async def list_coding_submissions(
    session_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    # Recruiter-facing (the evaluation page), not the candidate — so this one is
    # authorized by the admin JWT plus the session's organization, not by the
    # candidate session token. It used to have no auth at all and was exempt
    # from the session-token middleware, so anyone could walk sequential ids and
    # read every tenant's submitted code.
    authorize_session(db, session_id, user)

    rows = (
        db.query(CodingSubmission)
        .filter(CodingSubmission.session_id == session_id)
        .order_by(CodingSubmission.submitted_at.asc(), CodingSubmission.id.asc())
        .all()
    )

    out = []
    for r in rows:
        try:
            test_results = json.loads(r.test_results) if r.test_results else []
        except (TypeError, ValueError) as parse_exc:
            logging.warning(
                "[list_coding_submissions] corrupt test_results JSON on row "
                "id=%s (session=%s): %s — returning empty list",
                r.id, session_id, parse_exc,
            )
            test_results = []
        out.append({
            "id": r.id,
            "question_index": r.question_index,
            "title": r.title,
            "description": r.description,
            "difficulty": r.difficulty,
            "language": r.language,
            "code": r.code,
            "test_cases_total": r.test_cases_total,
            "test_cases_passed": r.test_cases_passed,
            "test_results": test_results,
            "execution_time_ms": r.execution_time_ms,
            "submitted_at": r.submitted_at.isoformat() if r.submitted_at else None,
        })

    return {"submissions": out, "count": len(out)}


@router.get(
    "/interview/code/languages",
    summary="Get supported programming languages",
)
async def get_languages():
    """Return list of supported languages for the code editor."""
    from app.services.code_execution_service import get_supported_languages
    return {"languages": get_supported_languages()}


@router.get(
    "/interview/code/starter/{language}",
    summary="Get starter code template for a language",
)
async def get_starter_code(language: str):
    """Return starter code template for a given language."""
    from app.services.code_execution_service import get_starter_code as _get_starter
    code = _get_starter(language)
    return {"language": language, "code": code}


@router.post(
    "/interview/code/test-run",
    summary="Standalone code sandbox test (super-admin only)",
    description=(
        "Runs code through the exact same sandbox pipeline used during "
        "interviews, but without requiring an active InterviewSession. "
        "Intended for super-admins to debug Piston/Judge0 connectivity and "
        "language support without scheduling a candidate."
    ),
)
@limiter.limit("30/minute")
async def test_run_code(
    request: Request,
    _user: User = Depends(require_roles(UserRole.super_admin)),
):
    """Body: { language, code, test_cases?, stdin? } — same shape as
    /run-code, minus the session_id requirement."""
    body = await request.json()
    language = body.get("language", "python")
    code = body.get("code", "")
    test_cases = body.get("test_cases", [])
    stdin = body.get("stdin", "")

    if not code.strip():
        raise HTTPException(status_code=400, detail="Code cannot be empty")

    from app.services.code_execution_service import run_test_cases, execute_code

    if test_cases:
        return await run_test_cases(language, code, test_cases, timeout=10)
    exec_result = await execute_code(language, code, stdin=stdin, timeout=10)
    return {
        "total": 0,
        "passed": 0,
        "failed": 0,
        "results": [],
        "output": exec_result,
    }
