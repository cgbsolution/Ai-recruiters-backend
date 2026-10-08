"""
Magic links for candidate pages. The token is 32 random bytes; only its
SHA-256 is stored, so a database leak doesn't hand out working links.

Purposes and lifetimes:
    refresh      single-use (consumed on submit), TALENT_REFRESH_LINK_DAYS
    invite       single-use (consumed on answer), TALENT_INVITE_LINK_DAYS
    interview    reusable until the interview is done, TALENT_INTERVIEW_LINK_DAYS
    my_data      reusable, TALENT_MY_DATA_LINK_DAYS
    unsubscribe  reusable, one year — opting out must keep working
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy.orm import Session

from app.config import settings
from app.models.talent import AccessToken
from app.talent.config import talent_config

PURPOSES = ("refresh", "invite", "interview", "my_data", "unsubscribe")
SINGLE_USE = {"refresh", "invite"}


def _ttl(purpose: str) -> timedelta:
    days = {
        "refresh": talent_config.TALENT_REFRESH_LINK_DAYS,
        "invite": talent_config.TALENT_INVITE_LINK_DAYS,
        "interview": talent_config.TALENT_INTERVIEW_LINK_DAYS,
        "my_data": talent_config.TALENT_MY_DATA_LINK_DAYS,
        "unsubscribe": 365,
    }[purpose]
    return timedelta(days=days)


def hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def issue(db: Session, profile_id: int, purpose: str, ref_id: Optional[str] = None,
          ttl: Optional[timedelta] = None) -> str:
    if purpose not in PURPOSES:
        raise ValueError(f"unknown token purpose {purpose!r}")
    raw = secrets.token_urlsafe(32)
    db.add(AccessToken(
        token_hash=hash_token(raw), purpose=purpose, profile_id=profile_id, ref_id=str(ref_id) if ref_id else None,
        single_use=purpose in SINGLE_USE, expires_at=datetime.utcnow() + (ttl or _ttl(purpose)),
    ))
    db.flush()
    return raw


def lookup(db: Session, raw: Optional[str], purpose: str, *, allow_used: bool = False) -> Optional[AccessToken]:
    """The live token row, or None (unknown, wrong purpose, expired, used)."""
    if not raw or len(raw) > 128:
        return None
    row = db.query(AccessToken).filter(AccessToken.token_hash == hash_token(raw)).first()
    if row is None or row.purpose != purpose or row.expires_at < datetime.utcnow():
        return None
    if row.single_use and row.used_at is not None and not allow_used:
        return None
    return row


def consume(db: Session, row: AccessToken) -> bool:
    """Mark a single-use token used. False if someone else got there first."""
    t = AccessToken.__table__
    done = db.execute(
        t.update().where(t.c.id == row.id, t.c.used_at.is_(None)).values(used_at=datetime.utcnow())
    ).rowcount
    return done == 1


def public_base_url() -> str:
    return (talent_config.TALENT_PUBLIC_BASE_URL or settings.frontend_base_url or "http://localhost:3000").rstrip("/")


def page_url(purpose: str, raw: str) -> str:
    path = {"refresh": "refresh", "invite": "invite", "interview": "interview", "my_data": "my-data",
            "unsubscribe": "unsubscribe"}[purpose]
    return f"{public_base_url()}/t/{path}/{raw}"


def api_unsubscribe_url(raw: str) -> str:
    """One-click (RFC 8058) target: the API itself, so it works without the frontend."""
    base = (talent_config.TALENT_PUBLIC_API_URL or "").rstrip("/")
    return f"{base}/talent/public/unsubscribe/{raw}" if base else page_url("unsubscribe", raw)
