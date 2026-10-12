"""
The only way the Talent Agent sends anything.

send_touch() is idempotent and retry-safe for a (profile, campaign, touch_no):

1. The touch row is inserted first (unique key); a row already past `queued`
   is returned as-is — re-running a step never sends twice.
2. Master switch + loop flag, campaign state, then the consent gate
   (can_contact: consent, suppression, reachability), quiet hours, campaign
   and domain caps — all checked at the moment of sending.
3. The row moves queued → sending in one conditional UPDATE; only the caller
   that wins that UPDATE talks to a transport.
4. Outcome: sent | skipped (final) | deferred (still queued, retry_at) |
   failed (final) | unknown (may have been delivered: final, never retried).

Every send, skip and failure is written to audit_log in the same transaction.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional, Tuple

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.models.organization import Organization
from app.models.resume_metadata import ResumeMetadata
from app.models.talent import Campaign, CampaignMember, Touch
from app.talent import caps, tokens
from app.talent.audit import audit
from app.talent.config import talent_config
from app.talent.consent import can_contact
from app.talent.settings import get_settings, loop_enabled, next_send_window
from app.talent.templates import TEMPLATE_PURPOSE, brand_context, render
from app.talent.transports import (
    OutgoingEmail,
    OutgoingWhatsApp,
    PermanentSendError,
    TransientSendError,
    UnknownSendError,
    email_transport,
    sender_domain,
    whatsapp_template_for,
    whatsapp_transport,
)

logger = logging.getLogger("app.talent.outbox")

FINAL = {"sent", "skipped", "failed", "unknown", "bounced", "complained"}
CLAIM_TIMEOUT = timedelta(minutes=10)
MAX_ATTEMPTS = 5

# Which loop must be on for a message kind.
KIND_LOOP = {
    "refresh_touch_1": "refresh", "refresh_touch_2": "refresh", "refresh_touch_3": "refresh",
    "invite": "invite", "invite_reminder": "invite",
    "interview_offer": "interview", "interview_confirmed": "interview", "interview_reminder_24h": "interview",
    "interview_reminder_1h": "interview", "interview_missed": "interview",
    "not_moving_forward": None,  # a recruiter's decision; only the master switch applies
}


# The page each kind links to. Refresh links belong to the touch itself; the
# others name the match / interview they act on and must be passed in.
DEFAULT_ACTION = {"refresh_touch_1": ("refresh", "@touch"), "refresh_touch_2": ("refresh", "@touch"),
                  "refresh_touch_3": ("refresh", "@touch")}
NEEDS_ACTION = {"invite", "invite_reminder", "interview_offer", "interview_confirmed", "interview_reminder_24h",
                "interview_reminder_1h", "interview_missed"}


@dataclass
class SendResult:
    status: str
    touch_id: Optional[uuid.UUID] = None
    reason: Optional[str] = None
    retry_at: Optional[datetime] = None

    def to_dict(self) -> dict:
        return {"status": self.status, "touch_id": str(self.touch_id) if self.touch_id else None,
                "reason": self.reason, "retry_at": self.retry_at.isoformat() if self.retry_at else None}


def first_name(profile: ResumeMetadata) -> str:
    name = (profile.candidate_name or "").strip()
    return name.split()[0].title() if name else ""


def _upsert_touch(db: Session, *, profile_id: int, campaign_id, touch_no: int, channel: str, purpose: str,
                  kind: str) -> Touch:
    db.execute(
        pg_insert(Touch.__table__)
        .values(id=uuid.uuid4(), profile_id=profile_id, campaign_id=campaign_id, touch_no=touch_no, channel=channel,
                purpose=purpose, template=kind, status="queued", attempts=0, created_at=datetime.utcnow())
        .on_conflict_do_nothing(constraint="uq_touches_profile_campaign_no")
    )
    return (
        db.query(Touch)
        .filter(Touch.profile_id == profile_id, Touch.campaign_id == campaign_id, Touch.touch_no == touch_no)
        .populate_existing()
        .one()
    )


def _finish(db: Session, touch: Touch, status: str, campaign: Campaign, *, reason: Optional[str] = None,
            action: str, detail: Optional[dict] = None) -> SendResult:
    touch.status = status
    if reason:
        touch.skip_reason = reason[:60]
    audit(
        db, actor="agent", action=action, subject_type="touch", subject_id=touch.id,
        organization_id=campaign.organization_id, profile_id=touch.profile_id,
        detail={"campaign_id": str(campaign.id), "kind": touch.template, "touch_no": touch.touch_no,
                "channel": touch.channel, "reason": reason, **(detail or {})},
    )
    db.commit()
    return SendResult(status, touch.id, reason)


def _campaign_sent_today(db: Session, campaign_id, now: datetime) -> int:
    start = datetime(now.year, now.month, now.day)
    return db.query(sa.func.count(Touch.id)).filter(
        Touch.campaign_id == campaign_id, Touch.status == "sent", Touch.sent_at >= start
    ).scalar() or 0


def _profile_summary(profile: ResumeMetadata) -> dict:
    """Shown in the profile-update email: the candidate's own data, to their own address."""
    try:
        skills = json.loads(profile.skills) if isinstance(profile.skills, str) else (profile.skills or [])
    except ValueError:
        skills = []
    names = [str(s.get("name") if isinstance(s, dict) else s).strip() for s in skills]
    updated = profile.last_verified_at or profile.created_at
    return {
        "profile_role": (profile.current_designation or "").strip()[:120] or None,
        "profile_skills": [n[:40] for n in names if n][:5],
        "profile_updated": updated.strftime("%B %Y") if updated else None,
    }


def _context(db: Session, profile: ResumeMetadata, campaign: Campaign, touch: Touch,
             action: Optional[Tuple[str, str]], extra: Optional[dict]) -> dict:
    org = db.get(Organization, campaign.organization_id)
    row = get_settings(db, campaign.organization_id)
    brand = brand_context(org.name if org else "Our recruitment team", name=row.brand_name,
                          logo_url=row.brand_logo_url, color=row.brand_color, signature=row.email_signature,
                          message=row.refresh_message)
    unsub = tokens.issue(db, profile.id, "unsubscribe", ref_id=str(touch.id))
    my_data = tokens.issue(db, profile.id, "my_data", ref_id=str(touch.id))
    ctx = {
        "org_name": brand["name"],
        "brand": brand,
        **_profile_summary(profile),
        "first_name": first_name(profile),
        "unsubscribe_url": tokens.page_url("unsubscribe", unsub),
        "one_click_unsubscribe_url": tokens.api_unsubscribe_url(unsub),
        "my_data_url": tokens.page_url("my_data", my_data),
        "action_url": "",
    }
    if action is not None:
        purpose, ref_id = action
        if ref_id == "@touch":  # the link belongs to this very message
            ref_id = str(touch.id)
        raw = tokens.issue(db, profile.id, purpose, ref_id=ref_id)
        ctx["action_url"] = tokens.page_url(purpose, raw)
    ctx.update(extra or {})
    return ctx


def send_touch(
    db: Session,
    *,
    campaign_id,
    profile_id: int,
    touch_no: int,
    kind: str,
    priority: str = caps.BULK,
    channel: str = "email",
    action: Optional[Tuple[str, str]] = None,
    extra_ctx: Optional[dict] = None,
    now: Optional[datetime] = None,
) -> SendResult:
    now = now or datetime.utcnow()
    purpose = TEMPLATE_PURPOSE[kind]
    action = action or DEFAULT_ACTION.get(kind)
    if action is None and kind in NEEDS_ACTION:
        raise ValueError(f"{kind} needs the page its link opens (action=(purpose, ref_id))")
    campaign = db.get(Campaign, campaign_id)
    if campaign is None:
        return SendResult("skipped", reason="campaign_missing")

    touch = _upsert_touch(db, profile_id=profile_id, campaign_id=campaign_id, touch_no=touch_no, channel=channel,
                          purpose=purpose, kind=kind)
    db.commit()
    if touch.status in FINAL:
        return SendResult(touch.status, touch.id, "already_" + touch.status)
    if touch.status == "sending":
        if touch.claimed_at and now - touch.claimed_at > CLAIM_TIMEOUT:
            # A worker died mid-send: it may or may not have gone out. Never resend.
            return _finish(db, touch, "unknown", campaign, reason="claim_expired", action="message.unknown")
        return SendResult("deferred", touch.id, "in_flight", now + timedelta(minutes=5))

    loop = KIND_LOOP.get(kind)
    if not talent_config.TALENT_AGENT_ENABLED or (loop and not loop_enabled(db, campaign.organization_id, loop)):
        return _finish(db, touch, "skipped", campaign, reason=f"loop_off:{loop or 'agent'}", action="message.skipped")
    if campaign.status in ("cancelled", "completed"):
        return _finish(db, touch, "skipped", campaign, reason=f"campaign_{campaign.status}", action="message.skipped")
    if campaign.status == "paused":
        return SendResult("paused", touch.id, "campaign_paused", now + timedelta(hours=1))

    decision = can_contact(db, profile_id, channel, purpose)
    if not decision.allowed:
        return _finish(db, touch, "skipped", campaign, reason=decision.reason, action="message.skipped")

    org_settings = get_settings(db, campaign.organization_id)
    window = next_send_window(org_settings, now)
    if window is not None:
        return SendResult("deferred", touch.id, "quiet_hours", window)
    if campaign.daily_cap and _campaign_sent_today(db, campaign.id, now) >= campaign.daily_cap:
        return SendResult("deferred", touch.id, "campaign_cap", caps.next_day_start(now))

    domain = (org_settings.sender_domain or sender_domain()).lower()
    if not caps.reserve(db, domain, priority, now):
        db.commit()
        return SendResult("deferred", touch.id, "domain_cap", caps.next_day_start(now))

    t = Touch.__table__
    claimed = db.execute(
        t.update().where(t.c.id == touch.id, t.c.status == "queued")
        .values(status="sending", claimed_at=now, sender_domain=domain, attempts=t.c.attempts + 1)
    ).rowcount
    if claimed != 1:
        caps.release(db, domain, priority, now)
        db.commit()
        return SendResult("deferred", touch.id, "claimed_elsewhere", now + timedelta(minutes=5))
    db.commit()
    db.refresh(touch)

    profile = db.get(ResumeMetadata, profile_id)
    try:
        ctx = _context(db, profile, campaign, touch, action, extra_ctx)
        if channel == "whatsapp":
            template = whatsapp_template_for(kind)
            if not template:
                raise PermanentSendError(f"no approved WhatsApp template for {kind}")
            message_id = whatsapp_transport().send(decision.permit, OutgoingWhatsApp(
                template=template, language="en", parameters=[ctx["first_name"] or "there", ctx["org_name"],
                                                             ctx["action_url"]], message_id_hint=str(touch.id)))
        else:
            subject, text, html = render(kind, ctx)
            message_id = email_transport().send(decision.permit, OutgoingEmail(
                subject=subject, text=text, html=html, from_name=ctx["org_name"], message_id_hint=str(touch.id),
                unsubscribe_url=ctx["one_click_unsubscribe_url"]))
    except TransientSendError as exc:
        db.rollback()
        caps.release(db, domain, priority, now)
        touch = db.get(Touch, touch.id)
        if touch.attempts >= MAX_ATTEMPTS:
            touch.error = str(exc)[:500]
            return _finish(db, touch, "failed", campaign, reason="too_many_attempts", action="message.failed")
        touch.status, touch.error = "queued", str(exc)[:500]
        db.commit()
        return SendResult("deferred", touch.id, "transient_error",
                          now + timedelta(seconds=exc.retry_after_seconds))
    except PermanentSendError as exc:
        db.rollback()
        touch = db.get(Touch, touch.id)
        touch.error = str(exc)[:500]
        result = _finish(db, touch, "failed", campaign, reason="hard_bounce" if exc.hard_bounce else "refused",
                         action="message.failed")
        if exc.hard_bounce:
            from app.talent import deliverability

            deliverability.record_event(db, kind="bounce_hard", touch_id=touch.id, provider="smtp")
        return result
    except UnknownSendError as exc:
        db.rollback()
        touch = db.get(Touch, touch.id)
        touch.error = str(exc)[:500]
        return _finish(db, touch, "unknown", campaign, reason="delivery_unknown", action="message.unknown")

    touch.status, touch.sent_at, touch.provider_message_id, touch.error = "sent", now, message_id, None
    member = db.get(CampaignMember, (campaign.id, profile_id))
    if member is not None:
        member.last_event_at = now
        if member.status == "queued":
            member.status = "in_progress"
    audit(
        db, actor="agent", action="message.sent", subject_type="touch", subject_id=touch.id,
        organization_id=campaign.organization_id, profile_id=profile_id,
        detail={"campaign_id": str(campaign.id), "kind": kind, "touch_no": touch_no, "channel": channel,
                "purpose": purpose, "sender_domain": domain, "priority": priority},
    )
    db.commit()
    return SendResult("sent", touch.id)
