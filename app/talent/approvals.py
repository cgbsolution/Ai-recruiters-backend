"""
Human decisions. The AI orders the queue; people decide outcomes.

Kinds:
    invite          copilot jobs: a recruiter approves who is invited
    submit          sending a candidate to the client — always a human
    reject_message  telling a candidate "not moving forward" — always a human

`require()` is what the senders call. It returns an approval that a user
decided ("approved", decided_by set) or raises NotApproved — there is no
path where a score, a timeout or the agent itself fills that in.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import List, Optional

from sqlalchemy.orm import Session

from app.models.talent import JobMatch, TalentApproval
from app.talent.audit import audit, user_actor

KINDS = ("invite", "submit", "reject_message")


class NotApproved(PermissionError):
    pass


def pending(db: Session, match: JobMatch, kind: str) -> Optional[TalentApproval]:
    return db.query(TalentApproval).filter_by(match_id=match.id, kind=kind, status="pending").first()


def request(db: Session, match: JobMatch, kind: str) -> TalentApproval:
    """Ask a recruiter. Idempotent: an open request is returned as is."""
    if kind not in KINDS:
        raise ValueError(f"unknown approval kind {kind!r}")
    existing = pending(db, match, kind)
    if existing is not None:
        return existing
    row = TalentApproval(kind=kind, organization_id=match.organization_id, job_id=match.job_id, match_id=match.id,
                         status="pending")
    db.add(row)
    db.flush()
    audit(db, actor="agent", action=f"approval.requested.{kind}", subject_type="job_match", subject_id=match.id,
          organization_id=match.organization_id, profile_id=match.profile_id,
          detail={"approval_id": str(row.id), "job_id": match.job_id, "ai_score": match.ai_score})
    return row


def decide(db: Session, approval: TalentApproval, user, approve: bool, note: Optional[str] = None) -> TalentApproval:
    if user is None or getattr(user, "id", None) is None:
        raise NotApproved("only a signed-in user can decide")
    if approval.status != "pending":
        return approval
    approval.status = "approved" if approve else "declined"
    approval.decided_by, approval.decided_at = user.id, datetime.utcnow()
    approval.note = (note or "")[:1000] or None
    match = db.get(JobMatch, approval.match_id) if approval.match_id else None
    audit(db, actor=user_actor(user), action=f"approval.{approval.status}.{approval.kind}", subject_type="job_match",
          subject_id=approval.match_id, organization_id=approval.organization_id,
          profile_id=match.profile_id if match else None,
          detail={"approval_id": str(approval.id), "job_id": approval.job_id, "note": bool(approval.note)})
    db.flush()  # sessions run without autoflush: make the decision visible to require() at once
    return approval


def record_direct(db: Session, match: JobMatch, kind: str, user, note: Optional[str] = None) -> TalentApproval:
    """A recruiter acted directly (e.g. "invite this person"): the action is its own approval."""
    row = request(db, match, kind)
    return decide(db, row, user, True, note)


def require(db: Session, match: JobMatch, kind: str) -> TalentApproval:
    row = (
        db.query(TalentApproval)
        .filter_by(match_id=match.id, kind=kind, status="approved")
        .filter(TalentApproval.decided_by.isnot(None))
        .order_by(TalentApproval.decided_at.desc())
        .first()
    )
    if row is None:
        raise NotApproved(f"{kind} for this candidate has not been approved by a recruiter")
    return row


def queue(db: Session, organization_id: int, job_id: Optional[int] = None, kind: Optional[str] = None) -> List[TalentApproval]:
    q = db.query(TalentApproval).filter_by(organization_id=organization_id, status="pending")
    if job_id is not None:
        q = q.filter_by(job_id=job_id)
    if kind:
        q = q.filter_by(kind=kind)
    return q.order_by(TalentApproval.requested_at).all()


def get(db: Session, approval_id: str) -> Optional[TalentApproval]:
    try:
        return db.get(TalentApproval, uuid.UUID(str(approval_id)))
    except ValueError:
        return None
