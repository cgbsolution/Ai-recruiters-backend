"""
Audit log writer. Every outbound message, AI score, human decision and
deletion goes through `audit()`.

The log must never hold candidate PII (it outlives erasure on purpose), so
string values in `detail` are scrubbed of anything that looks like an email
address or phone number before they are stored. Pass ids, counts, field names
and hashes.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Optional

from sqlalchemy.orm import Session

from app.models.talent import AuditLog

logger = logging.getLogger("app.talent.audit")

_EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
# 10+ digits, optionally grouped (+91 98765 43210, 98765-43210 …)
_PHONE = re.compile(r"(?<![\w-])\+?\d[\d \-()]{8,}\d(?![\w-])")


def scrub(value: Any) -> Any:
    """Recursively replace email- and phone-like substrings in strings."""
    if isinstance(value, str):
        out = _EMAIL.sub("[email]", value)
        return _PHONE.sub(lambda m: "[phone]" if sum(c.isdigit() for c in m.group()) >= 10 else m.group(), out)
    if isinstance(value, dict):
        return {str(k): scrub(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [scrub(v) for v in value]
    return value


def user_actor(user) -> str:
    return f"user:{user.id}" if user is not None else "system"


def audit(
    db: Session,
    *,
    actor: str,
    action: str,
    subject_type: str,
    subject_id: Any = None,
    organization_id: Optional[int] = None,
    profile_id: Optional[int] = None,
    detail: Optional[dict] = None,
    commit: bool = False,
) -> AuditLog:
    """Add an audit row to `db` (committed with the caller's transaction unless
    commit=True). Writing the row inside the same transaction as the change it
    describes is the point: the change and its log entry land together or not
    at all."""
    entry = AuditLog(
        actor=actor[:80],
        action=action[:80],
        subject_type=subject_type[:40],
        subject_id=str(subject_id)[:64] if subject_id is not None else None,
        organization_id=organization_id,
        profile_id=profile_id,
        detail=scrub(detail) if detail else None,
    )
    db.add(entry)
    if commit:
        db.commit()
    return entry
