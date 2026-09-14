"""
Candidate interview-session authentication.

A per-session access token is minted when a candidate is admitted
(interview._get_or_mint_session_access_token) and stored in
InterviewSession.session_metadata["access_token"]. Every candidate call on
/interview/sessions/{session_id}/* (and the secondary-camera / observer-publish
routes keyed by session id) must present it.

This replaces the path-regex middleware that used to live in app.main. That
middleware parsed the id with `(\\d+)` while the router parses it as a lax int,
so `/sessions/+5/`, `/5.0/` or `/5_0/` skipped the check yet still reached
session 5 / 50. A FastAPI dependency receives the SAME parsed `session_id` the
handler does, so the two can no longer disagree. It also fails closed: a
session with no stored token, a wrong token, or a failed lookup is refused
(the middleware let all three through).
"""
import hmac
import json
import logging
from typing import Optional

from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.interview import InterviewSession

logger = logging.getLogger(__name__)

SESSION_TOKEN_HEADER = "x-session-token"


def stored_session_token(session: InterviewSession) -> Optional[str]:
    """The access token stored on a session, or None when it has none."""
    try:
        meta = json.loads(session.session_metadata or "{}")
    except (TypeError, ValueError):
        return None
    token = meta.get("access_token") if isinstance(meta, dict) else None
    return token if isinstance(token, str) and token else None


def session_token_matches(session: InterviewSession, provided: Optional[str]) -> bool:
    """Constant-time comparison; False when either side is missing."""
    expected = stored_session_token(session)
    if not expected or not provided:
        return False
    return hmac.compare_digest(provided.encode("utf-8"), expected.encode("utf-8"))


def _load_and_check(session_id: int, provided: Optional[str], db: Session) -> InterviewSession:
    try:
        session = db.query(InterviewSession).filter(InterviewSession.id == session_id).first()
    except Exception:
        logger.exception("[SessionAuth] session lookup failed for session %s", session_id)
        raise HTTPException(
            status_code=503,
            detail="Could not verify your interview session right now. Please retry.",
        )
    # Same answer for "no such session" and "wrong token", so sequential ids
    # can't be probed for existence.
    if session is None or not session_token_matches(session, provided):
        raise HTTPException(status_code=403, detail="Invalid or missing session token")
    return session


def require_session_token(
    session_id: int,
    request: Request,
    db: Session = Depends(get_db),
) -> InterviewSession:
    """Dependency: the X-Session-Token header must match the session's token.

    Returns the loaded InterviewSession (bound to the request's db session)."""
    return _load_and_check(session_id, request.headers.get(SESSION_TOKEN_HEADER), db)


def require_session_token_or_query(
    session_id: int,
    request: Request,
    db: Session = Depends(get_db),
) -> InterviewSession:
    """Like require_session_token, but also accepts `?st=` — ONLY for routes
    called via navigator.sendBeacon, which cannot set request headers."""
    provided = request.headers.get(SESSION_TOKEN_HEADER) or request.query_params.get("st")
    return _load_and_check(session_id, provided, db)
