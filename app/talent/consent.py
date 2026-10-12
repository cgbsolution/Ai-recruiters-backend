"""
The consent gate. `can_contact()` is the only way to obtain a ContactPermit,
and the transports (app.talent.transports) refuse to send without one — so no
message can leave the agent unless consent, suppression and reachability all
allow that channel and purpose at the moment of sending.

    decision = can_contact(db, profile_id, "email", "refresh")
    if decision.allowed:
        transport.send(decision.permit, message)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Iterable, List, Optional

from sqlalchemy.orm import Session

from app.models.resume_metadata import ResumeMetadata
from app.models.talent import ContactConsent, SuppressionEntry
from app.talent import identity
from app.talent.audit import audit

CHANNELS = ("email", "whatsapp", "sms")
PURPOSES = ("refresh", "jobs", "interview")
PERMIT_TTL = timedelta(minutes=10)

# Only this module holds the seal; a permit built anywhere else fails
# ContactPermit.check().
_SEAL = object()


@dataclass(frozen=True)
class ContactPermit:
    profile_id: int
    channel: str
    purpose: str
    address: str = field(repr=False)  # email or E.164 digits — never logged
    issued_at: datetime
    _seal: object = field(repr=False, compare=False)

    def check(self, channel: str) -> None:
        if self._seal is not _SEAL:
            raise PermissionError("forged contact permit")
        if self.channel != channel:
            raise PermissionError(f"permit is for {self.channel}, not {channel}")
        if datetime.utcnow() - self.issued_at > PERMIT_TTL:
            raise PermissionError("contact permit expired — call can_contact again")


@dataclass(frozen=True)
class ContactDecision:
    allowed: bool
    reason: str
    permit: Optional[ContactPermit] = None


def _deny(reason: str) -> ContactDecision:
    return ContactDecision(False, reason)


_REASON_TEXT = {
    "no_consent": "No consent recorded for this kind of message",
    "consent_withdrawn": "The candidate withdrew consent",
    "consent_denied": "The candidate declined these messages",
    "suppressed": "On the do-not-contact list (unsubscribed or complained)",
    "email_invalid": "The email address is invalid or can't receive mail",
    "email_placeholder": "No real email address on the CV",
    "email_no_mx": "The email domain can't receive mail",
    "unreachable": "Emails to this address keep bouncing",
    "no_phone": "No phone number on the profile",
    "profile_missing": "The profile no longer exists",
}


def describe(reason: str) -> str:
    """A refusal reason in words a recruiter can act on."""
    if reason in _REASON_TEXT:
        return _REASON_TEXT[reason]
    if reason.startswith("email_"):
        return f"The email address can't be used ({reason[6:].replace('_', ' ')})"
    return reason.replace("_", " ")


def is_suppressed(db: Session, email: Optional[str] = None, phone: Optional[str] = None) -> bool:
    hashes = [h for h in (identity.suppression_hash("email", email), identity.suppression_hash("phone", phone)) if h]
    if not hashes:
        return False
    return db.query(SuppressionEntry.value_hash).filter(SuppressionEntry.value_hash.in_(hashes)).first() is not None


def suppress(db: Session, *, email: Optional[str] = None, phone: Optional[str] = None, reason: str) -> int:
    """Add the address(es) to the global suppression list. Idempotent."""
    added = 0
    for kind, value in (("email", email), ("phone", phone)):
        h = identity.suppression_hash(kind, value)
        if h and db.get(SuppressionEntry, h) is None:
            db.add(SuppressionEntry(value_hash=h, kind=kind, reason=reason))
            added += 1
    db.flush()
    return added


def lift_own_suppression(db: Session, *, email: Optional[str], phone: Optional[str]) -> int:
    """The candidate themselves opted back in: remove suppression they caused by
    unsubscribing or withdrawing. Complaints, bounces and erasures stay."""
    hashes = [h for h in (identity.suppression_hash("email", email), identity.suppression_hash("phone", phone)) if h]
    if not hashes:
        return 0
    removed = db.query(SuppressionEntry).filter(
        SuppressionEntry.value_hash.in_(hashes), SuppressionEntry.reason.in_(("unsubscribe", "withdrawn"))
    ).delete(synchronize_session=False)
    db.flush()
    return removed


def can_contact(db: Session, profile_id: int, channel: str, purpose: str) -> ContactDecision:
    """The one consent check every sender uses."""
    if channel not in CHANNELS or purpose not in PURPOSES:
        return _deny("unknown_channel_or_purpose")
    profile = db.get(ResumeMetadata, profile_id)
    if profile is None:
        return _deny("profile_missing")
    consent = db.get(ContactConsent, (profile_id, channel, purpose))
    if consent is None or consent.status != "granted":
        return _deny("no_consent" if consent is None else f"consent_{consent.status}")
    if is_suppressed(db, email=profile.candidate_email, phone=profile.phone):
        return _deny("suppressed")

    if channel == "email":
        status = profile.email_status or identity.classify_email(profile.candidate_email)
        if status not in identity.EMAIL_SENDABLE:
            return _deny(f"email_{status}")
        if profile.verification_status == "unreachable":
            return _deny("unreachable")
        address = identity.normalize_email(profile.candidate_email)
    else:
        address = identity.normalize_phone(profile.phone)
        if not address:
            return _deny("no_phone")
    return ContactDecision(True, "ok", ContactPermit(profile_id, channel, purpose, address, datetime.utcnow(), _SEAL))


# ── Recording consent ────────────────────────────────────────────────────────

def set_consent(
    db: Session, profile_id: int, channel: str, purpose: str, granted: bool, *,
    source: str, actor: str, basis: Optional[str] = None, organization_id: Optional[int] = None,
) -> ContactConsent:
    if channel not in CHANNELS or purpose not in PURPOSES:
        raise ValueError("unknown channel or purpose")
    now = datetime.utcnow()
    row = db.get(ContactConsent, (profile_id, channel, purpose))
    if row is None:
        row = ContactConsent(profile_id=profile_id, channel=channel, purpose=purpose)
        db.add(row)
    row.status = "granted" if granted else "withdrawn"
    row.source = source[:120]
    row.basis = basis
    row.recorded_by = actor[:80]
    if granted:
        row.granted_at, row.withdrawn_at = now, None
    else:
        row.withdrawn_at = now
    audit(
        db, actor=actor, action="consent.granted" if granted else "consent.withdrawn",
        subject_type="profile", subject_id=profile_id, profile_id=profile_id, organization_id=organization_id,
        detail={"channel": channel, "purpose": purpose, "source": source},
    )
    db.flush()
    return row


def withdraw_all(db: Session, profile_id: int, *, source: str, actor: str,
                 channels: Iterable[str] = CHANNELS, organization_id: Optional[int] = None) -> int:
    """Withdraw every purpose on the given channels (unsubscribe / my-data page)."""
    n = 0
    for channel in channels:
        for purpose in PURPOSES:
            row = db.get(ContactConsent, (profile_id, channel, purpose))
            if row is not None and row.status == "granted":
                set_consent(db, profile_id, channel, purpose, False, source=source, actor=actor,
                            organization_id=organization_id)
                n += 1
            elif row is None:
                db.add(ContactConsent(profile_id=profile_id, channel=channel, purpose=purpose, status="withdrawn",
                                      source=source, recorded_by=actor, withdrawn_at=datetime.utcnow()))
    db.flush()
    return n


def consent_summary(db: Session, profile_id: int) -> List[dict]:
    return [
        {"channel": c.channel, "purpose": c.purpose, "status": c.status, "source": c.source,
         "granted_at": c.granted_at, "withdrawn_at": c.withdrawn_at}
        for c in db.query(ContactConsent).filter(ContactConsent.profile_id == profile_id)
        .order_by(ContactConsent.channel, ContactConsent.purpose)
    ]
