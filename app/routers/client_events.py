"""
Candidate-side telemetry sink.

The interview runs in the candidate's browser, so when a recording fails to
upload, a tab crashes, or memory spikes, the server normally sees nothing. The
frontend emits best-effort `navigator.sendBeacon` events to this endpoint; we
persist them (for later analysis) AND log them (so they show up immediately in
the Dokploy log viewer next to the rest of the backend logs).

Design rules:
  - NEVER fail. This is fire-and-forget telemetry — a bad payload or a DB hiccup
    must not surface to the candidate. We always return 204.
  - Accept `text/plain` bodies. sendBeacon sends the JSON as a text/plain Blob
    so the cross-origin request stays a CORS "simple request" (no preflight,
    which beacons can't satisfy). So we read the raw body and parse it ourselves
    rather than relying on FastAPI's application/json body parsing.
"""

import json
import logging
import re

from fastapi import APIRouter, Depends, Request, Response
from slowapi import Limiter
from slowapi.util import get_remote_address
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.client_event import ClientEvent

logger = logging.getLogger("app.client_events")  # under "app" → INFO is shown
limiter = Limiter(key_func=get_remote_address)

router = APIRouter()

# Event types that signal a real problem — logged at WARNING so they stand out
# in the log viewer even if INFO is filtered.
_PROBLEM_EVENTS = {
    "js_error",
    "unhandled_rejection",
    "react_error",
    "abnormal_unload",
    "recording_empty",
    "recording_missing",
    "recording_upload_failed",
    "recording_upload_attempt_failed",
    # Device Test mic check: a candidate blocked at the gate, or our speech
    # service failing, both need to be visible without filtering INFO.
    "mic_check_failed",
    "mic_check_stt_unavailable",
}

# Control characters (CR/LF included) and Unicode line separators. The payload
# is anonymous, so a raw newline in it could forge extra backend log lines.
_LOG_UNSAFE_CHARS = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]")
_LOG_MESSAGE_MAX_CHARS = 2000


def _log_safe(value, limit: int) -> str:
    """Single-line, length-capped text for the log line only (the DB row keeps
    the original value, which is length-capped separately)."""
    text = _LOG_UNSAFE_CHARS.sub(" ", str(value))
    return text if len(text) <= limit else text[:limit] + "..."


def _coerce_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _coerce_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


@router.post("/client-events", status_code=204)
@limiter.limit("120/minute")
async def ingest_client_event(request: Request, db: Session = Depends(get_db)):
    # Read the raw body — the beacon arrives as text/plain, not application/json.
    try:
        raw = await request.body()
        payload = json.loads(raw.decode("utf-8")) if raw else {}
        if not isinstance(payload, dict):
            payload = {}
    except Exception:
        # Malformed beacon — log and swallow. Telemetry must never 4xx/5xx loud.
        logger.warning("[ClientEvent] dropped malformed beacon body")
        return Response(status_code=204)

    event_type = str(payload.get("type") or "unknown")[:60]
    message = payload.get("message")
    context = payload.get("context")
    # session_id is most useful for correlation — the client puts it in context.
    session_id = None
    if isinstance(context, dict):
        session_id = _coerce_int(context.get("sessionId"))

    # Log first (cheap, always visible in Dokploy), persist second.
    log_line = (
        f"[ClientEvent] {_log_safe(event_type, 60)} session={session_id or '-'} :: "
        f"{_log_safe(message or '', _LOG_MESSAGE_MAX_CHARS)}"
    )
    if event_type in _PROBLEM_EVENTS:
        logger.warning(log_line)
    else:
        logger.info(log_line)

    try:
        row = ClientEvent(
            session_id=session_id,
            event_type=event_type,
            message=(str(message)[:4000] if message is not None else None),
            context=(json.dumps(context)[:8000] if context is not None else None),
            url=(str(payload.get("url"))[:1000] if payload.get("url") else None),
            user_agent=(str(payload.get("userAgent"))[:500] if payload.get("userAgent") else None),
            device_memory_gb=_coerce_float(payload.get("deviceMemoryGb")),
            hardware_concurrency=_coerce_int(payload.get("hardwareConcurrency")),
            client_at=(str(payload.get("at"))[:40] if payload.get("at") is not None else None),
        )
        db.add(row)
        db.commit()
    except Exception as exc:
        # DB problem — the log line above already captured the event, so we
        # haven't lost the signal. Roll back and stay silent to the client.
        try:
            db.rollback()
        except Exception:
            pass
        logger.warning(f"[ClientEvent] persisted-to-log only (DB write failed): {exc}")

    return Response(status_code=204)
