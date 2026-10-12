"""
Loop A — keep every CV fresh.

Sequence (run by app.agent.refresh_touch): touch 1 → 3 days → touch 2 →
4 days → touch 3, stopping the moment the candidate responds, opts out or
bounces. `send_step()` is one durable step of it.

Candidate page (/t/refresh/<token>): `open_link()`, `page_data()`,
`attach_cv()` (optional new CV), `submit()` — which sets last_verified_at,
marks the profile fresh, records consent choices and creates a CV version
that app.agent.refresh_respond then parses and embeds.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy.orm import Session

from app.media import UPLOAD_ROOT, resume_upload_problem, safe_upload_name
from app.services.file_safety import UnsafeFile, store_resume_upload
from app.models.organization import Organization
from app.models.resume_metadata import CANDIDATE_CONFIRMABLE, ResumeMetadata, mark_candidate_confirmation
from app.models.talent import AccessToken, CampaignMember, ContactConsent, CvVersion, Touch
from app.talent import caps, consent, cv, outbox, tokens
from app.talent.audit import audit
from app.talent.campaigns import ACTIVE_MEMBER, finish_member
from app.talent.settings import consent_text, get_settings
from app.talent.transports import whatsapp_template_for

KINDS = {1: "refresh_touch_1", 2: "refresh_touch_2", 3: "refresh_touch_3"}
JOB_SEARCH_STATUSES = ("active", "open", "not_looking")
MAX_UPLOADS_PER_LINK = 3
RESUME_EXTS = {".pdf", ".docx"}  # public upload: no legacy .doc


class LinkError(Exception):
    """The link is unknown, expired or already used (the page shows one message for all)."""


# ── Sequence ─────────────────────────────────────────────────────────────────

def _channel_for(db: Session, profile_id: int, touch_no: int, organization_id: int) -> str:
    """Touches 1-2 by email; touch 3 by WhatsApp when the candidate allowed it,
    the org has WhatsApp and an approved template exists — otherwise email."""
    if touch_no != 3 or not whatsapp_template_for(KINDS[3]):
        return "email"
    org = db.get(Organization, organization_id)
    if org is None or not org.allow_whatsapp:
        return "email"
    allowed = consent.can_contact(db, profile_id, "whatsapp", "refresh")
    return "whatsapp" if allowed.allowed else "email"


def send_step(db: Session, campaign_id: str, profile_id: int, touch_no: int, now: Optional[datetime] = None) -> dict:
    """One durable step: send touch N unless the sequence should stop.
    Returns {"status": sent|deferred|paused|stopped, "retry_at", "reason"}."""
    member = db.get(CampaignMember, (uuid.UUID(campaign_id), profile_id))
    if member is None or member.status not in ACTIVE_MEMBER:
        return {"status": "stopped", "reason": member.status if member else "not_a_member"}
    profile = db.get(ResumeMetadata, profile_id)
    if profile is None:
        return {"status": "stopped", "reason": "profile_deleted"}
    if profile.last_verified_at and profile.last_verified_at >= member.enrolled_at:
        finish_member(db, campaign_id, profile_id, "responded", "verified_elsewhere")
        return {"status": "stopped", "reason": "responded"}

    from app.models.talent import Campaign

    campaign = db.get(Campaign, uuid.UUID(campaign_id))
    channel = _channel_for(db, profile_id, touch_no, campaign.organization_id)
    result = outbox.send_touch(
        db, campaign_id=campaign.id, profile_id=profile_id, touch_no=touch_no, kind=KINDS[touch_no],
        priority=caps.BULK, channel=channel, action=("refresh", "@touch"), now=now,
    )
    if result.status in ("skipped", "failed", "unknown", "bounced", "complained"):
        if result.status == "skipped" or touch_no == 1:
            finish_member(db, campaign_id, profile_id, "skipped", result.reason)
            return {"status": "stopped", "reason": result.reason}
    out = result.to_dict()
    if out["status"] in ("unknown", "failed"):
        out["status"] = "sent"  # never resend; carry on with the cadence
    return out


def end_sequence(db: Session, campaign_id: str, profile_id: int) -> bool:
    """After the last wait: no answer to three touches."""
    return finish_member(db, campaign_id, profile_id, "no_response", "three_touches")


# ── Candidate page ───────────────────────────────────────────────────────────

def open_link(db: Session, raw: str) -> AccessToken:
    row = tokens.lookup(db, raw, "refresh")
    if row is None:
        raise LinkError()
    touch = db.get(Touch, uuid.UUID(row.ref_id)) if row.ref_id else None
    if touch is not None and touch.opened_at is None:
        touch.opened_at = datetime.utcnow()
        db.commit()
    return row


def page_data(db: Session, row: AccessToken) -> dict:
    profile = db.get(ResumeMetadata, row.profile_id)
    if profile is None:
        raise LinkError()
    org = db.get(Organization, profile.organization_id)
    settings_row = get_settings(db, profile.organization_id)
    consents = {(c.channel, c.purpose): c.status for c in
                db.query(ContactConsent).filter(ContactConsent.profile_id == profile.id)}
    pending_cv = (
        db.query(CvVersion).filter(CvVersion.profile_id == profile.id, CvVersion.source == "refresh_upload",
                                   CvVersion.parse_status == "staged", CvVersion.received_at >= row.created_at)
        .order_by(CvVersion.version_no.desc()).first()
    )
    return {
        "organization": org.name if org else "",
        "first_name": outbox.first_name(profile),
        "fields": {
            "current_designation": profile.current_designation,
            "current_company": profile.current_company,
            "location": profile.location,
            "current_ctc": profile.current_ctc if profile.current_ctc is not None else profile.annual_package,
            "expected_ctc": profile.expected_ctc,
            "notice_period_days": profile.notice_period_days,
            "job_search_status": profile.job_search_status,
            "open_to_relocate": profile.open_to_relocate,
        },
        "skills": (cv.snapshot(profile)["skills"] or [])[:15],
        "last_verified_at": profile.last_verified_at,
        "consent_text": consent_text(settings_row),
        "consents": {
            "jobs": consents.get(("email", "jobs")) == "granted",
            "interview": consents.get(("email", "interview")) == "granted",
            "refresh": consents.get(("email", "refresh")) == "granted",
            "whatsapp": consents.get(("whatsapp", "jobs")) == "granted",
        },
        "new_cv_uploaded": pending_cv is not None,
        "expires_at": row.expires_at,
    }


def attach_cv(db: Session, row: AccessToken, upload) -> CvVersion:
    """Store a newer CV sent from the refresh page as a pending version."""
    problem = resume_upload_problem(upload)
    if problem:
        raise ValueError(problem)
    used = db.query(CvVersion).filter(CvVersion.profile_id == row.profile_id, CvVersion.source == "refresh_upload",
                                      CvVersion.received_at >= row.created_at).count()
    if used >= MAX_UPLOADS_PER_LINK:
        raise ValueError("too many uploads from this link")
    safe = safe_upload_name(upload.filename, RESUME_EXTS)
    if safe is None:
        raise ValueError("Please upload a PDF or DOCX file.")
    stem, ext = safe
    folder = os.path.join(UPLOAD_ROOT, "resumes")
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, f"{datetime.utcnow():%Y%m%d%H%M%S%f}_refresh_{stem}{ext}")
    profile = db.get(ResumeMetadata, row.profile_id)
    try:
        # A public page: PDF / DOCX only, checked (and virus-scanned) before it's stored.
        file_hash = store_resume_upload(upload, path, allow_doc=False)
    except UnsafeFile as exc:
        audit(db, actor="candidate", action="cv.upload_refused", subject_type="profile", subject_id=profile.id,
              organization_id=profile.organization_id, profile_id=profile.id, detail={"reason": exc.reason[:160]})
        db.commit()
        raise
    # Staged until the candidate submits the page; only then is it parsed and used.
    version = cv.new_version(db, profile, source="refresh_upload", file_path=path.replace("\\", "/"),
                             file_hash=file_hash)
    version.parse_status = "staged"
    audit(db, actor="candidate", action="cv.uploaded", subject_type="cv_version", subject_id=version.id,
          organization_id=profile.organization_id, profile_id=profile.id, detail={"via": "refresh_page"})
    db.commit()
    return version


def _clean_text(value, limit: int = 255) -> Optional[str]:
    if value is None:
        return None
    value = " ".join(str(value).split())[:limit]
    return value or None


def _clean_number(value, lo: float, hi: float) -> Optional[float]:
    if value in (None, ""):
        return None
    number = float(value)
    if not lo <= number <= hi:
        raise ValueError(f"value out of range ({lo}-{hi})")
    return number


def submit(db: Session, row: AccessToken, payload: dict) -> dict:
    """The candidate confirmed (and maybe edited) their profile."""
    profile = db.get(ResumeMetadata, row.profile_id)
    if profile is None:
        raise LinkError()
    fields = payload.get("fields") or {}
    status = fields.get("job_search_status")
    if status is not None and status not in JOB_SEARCH_STATUSES:
        raise ValueError("job_search_status must be active, open or not_looking")
    values = {
        "current_designation": _clean_text(fields.get("current_designation")),
        "current_company": _clean_text(fields.get("current_company")),
        "location": _clean_text(fields.get("location")),
        "current_ctc": _clean_number(fields.get("current_ctc"), 0, 1000),
        "expected_ctc": _clean_number(fields.get("expected_ctc"), 0, 1000),
        "notice_period_days": None if fields.get("notice_period_days") in (None, "")
        else int(_clean_number(fields.get("notice_period_days"), 0, 365)),
        "open_to_relocate": None if fields.get("open_to_relocate") is None else bool(fields.get("open_to_relocate")),
    }
    if not tokens.consume(db, row):
        raise LinkError()

    now = datetime.utcnow()
    changed = [k for k, v in values.items() if k in CANDIDATE_CONFIRMABLE and getattr(profile, k) != v]
    mark_candidate_confirmation(db)
    for key in changed:
        setattr(profile, key, values[key])
    profile.job_search_status = status or profile.job_search_status
    profile.last_verified_at = now
    profile.verification_status = "fresh"
    profile.retention_until = cv.retention_date(db, profile, now)

    choices = payload.get("consents") or {}
    for purpose in ("jobs", "interview", "refresh"):
        if purpose in choices:
            consent.set_consent(db, profile.id, "email", purpose, bool(choices[purpose]), source="candidate_page",
                                actor="candidate", organization_id=profile.organization_id)
    if "whatsapp" in choices:
        for purpose in ("jobs", "interview", "refresh"):
            consent.set_consent(db, profile.id, "whatsapp", purpose, bool(choices["whatsapp"]),
                                source="candidate_page", actor="candidate", organization_id=profile.organization_id)

    uploaded = (
        db.query(CvVersion).filter(CvVersion.profile_id == profile.id, CvVersion.source == "refresh_upload",
                                   CvVersion.parse_status == "staged", CvVersion.received_at >= row.created_at)
        .order_by(CvVersion.version_no.desc()).first()
    )
    if uploaded is not None:
        uploaded.parse_status = "pending"
    version = uploaded or cv.new_version(db, profile, source="refresh_confirm", file_path=profile.resume_file_path,
                                         file_hash=profile.file_hash)

    touch = db.get(Touch, uuid.UUID(row.ref_id)) if row.ref_id else None
    campaign_id = None
    if touch is not None:
        touch.responded_at = now
        touch.opened_at = touch.opened_at or now
        campaign_id = touch.campaign_id
        member = db.get(CampaignMember, (touch.campaign_id, profile.id))
        if member is not None and member.status in ACTIVE_MEMBER:
            member.status, member.status_reason, member.completed_at = "responded", "refresh_page", now
    audit(db, actor="candidate", action="profile.refreshed", subject_type="profile", subject_id=profile.id,
          organization_id=profile.organization_id, profile_id=profile.id,
          detail={"fields_changed": changed, "new_cv": uploaded is not None, "job_search_status": status,
                  "cv_version_id": str(version.id)})
    db.commit()

    from app.agent.client import emit

    emit("talent/refresh.responded", {"profile_id": profile.id, "cv_version_id": str(version.id),
                                      "campaign_id": str(campaign_id) if campaign_id else None})
    emit("talent/member.stopped", {"profile_id": profile.id, "campaign_id": str(campaign_id) if campaign_id else None,
                                   "reason": "responded"})
    return {"ok": True, "fields_changed": changed, "cv_version_id": str(version.id)}
