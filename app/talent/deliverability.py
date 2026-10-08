"""
Bounces, complaints and unsubscribes reported by the mail provider (or seen
during the SMTP conversation), dead-lettering, and the auto-pause.

Everything that protects people and the sender's reputation happens
synchronously here, in the database, when the event is recorded:
  hard bounce        → touch bounced, profile unreachable, address suppressed
  N soft bounces     → profile unreachable
  complaint          → address suppressed, email consent withdrawn
  campaign health    → auto-pause when bounce rate > 3% or complaint rate > 0.1%
  domain health      → same, over the last 7 days, pauses the sender domain
The Inngest events emitted afterwards only end the waiting touch sequences sooner.

Rates use max(sends, TALENT_HEALTH_MIN_SAMPLE) as the denominator: early
in a campaign a handful of bounces still trips the pause.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Optional

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.models.resume_metadata import ResumeMetadata
from app.models.talent import Campaign, CampaignMember, DeliverabilityEvent, SenderDomain, Touch
from app.talent import consent
from app.talent.audit import audit
from app.talent.config import talent_config

logger = logging.getLogger("app.talent.deliverability")

KINDS = ("delivered", "bounce_hard", "bounce_soft", "complaint", "unsubscribe")
LEFT = ("sent", "bounced", "complained")  # touches that actually left


def _stop_sequences(profile_id: int, reason: str, campaign_id=None) -> None:
    from app.agent.client import emit

    emit("talent/member.stopped", {"profile_id": profile_id, "campaign_id": str(campaign_id) if campaign_id else None,
                                   "reason": reason})


def mark_unreachable(db: Session, profile: ResumeMetadata, reason: str, actor: str = "agent") -> None:
    if profile.verification_status != "unreachable":
        profile.verification_status = "unreachable"
        audit(db, actor=actor, action="profile.unreachable", subject_type="profile", subject_id=profile.id,
              organization_id=profile.organization_id, profile_id=profile.id, detail={"reason": reason})
    db.query(CampaignMember).filter(
        CampaignMember.profile_id == profile.id, CampaignMember.status.in_(("queued", "in_progress"))
    ).update({"status": "bounced", "status_reason": reason[:80], "completed_at": datetime.utcnow()},
             synchronize_session=False)


def opt_out(db: Session, profile: ResumeMetadata, *, source: str, actor: str, suppress_reason: str,
            channels=("email", "whatsapp", "sms")) -> None:
    """Unsubscribe / complaint: suppress the addresses, withdraw consent, end sequences."""
    consent.suppress(db, email=profile.candidate_email if "email" in channels else None,
                     phone=profile.phone if ("whatsapp" in channels or "sms" in channels) else None,
                     reason=suppress_reason)
    consent.withdraw_all(db, profile.id, source=source, actor=actor, channels=channels,
                         organization_id=profile.organization_id)
    db.query(CampaignMember).filter(
        CampaignMember.profile_id == profile.id, CampaignMember.status.in_(("queued", "in_progress"))
    ).update({"status": "opted_out", "status_reason": source[:80], "completed_at": datetime.utcnow()},
             synchronize_session=False)
    audit(db, actor=actor, action="profile.opted_out", subject_type="profile", subject_id=profile.id,
          organization_id=profile.organization_id, profile_id=profile.id,
          detail={"source": source, "channels": list(channels)})


def record_event(
    db: Session, *, kind: str, touch_id=None, provider_message_id: Optional[str] = None,
    provider: Optional[str] = None, provider_event_id: Optional[str] = None,
    occurred_at: Optional[datetime] = None,
) -> Optional[DeliverabilityEvent]:
    """Record one provider event and act on it. Idempotent on provider_event_id."""
    if kind not in KINDS:
        raise ValueError(f"unknown deliverability event {kind!r}")
    if provider_event_id and db.query(DeliverabilityEvent.id).filter(
        DeliverabilityEvent.provider_event_id == provider_event_id
    ).first():
        return None
    touch = db.get(Touch, touch_id) if touch_id else None
    if touch is None and provider_message_id:
        touch = db.query(Touch).filter(Touch.provider_message_id == provider_message_id).first()

    event = DeliverabilityEvent(
        touch_id=touch.id if touch else None, campaign_id=touch.campaign_id if touch else None,
        sender_domain=touch.sender_domain if touch else None, kind=kind, provider=provider,
        provider_event_id=provider_event_id, occurred_at=occurred_at or datetime.utcnow(),
    )
    db.add(event)
    if touch is None:
        db.commit()
        return event

    profile = db.get(ResumeMetadata, touch.profile_id)
    campaign = db.get(Campaign, touch.campaign_id)
    org_id = campaign.organization_id if campaign else None
    stop_reason = None
    if kind == "bounce_hard":
        touch.status, touch.bounced_at = "bounced", event.occurred_at
        if profile is not None:
            consent.suppress(db, email=profile.candidate_email, reason="hard_bounce")
            mark_unreachable(db, profile, "hard_bounce")
        stop_reason = "bounced"
    elif kind == "bounce_soft":
        touch.bounced_at = touch.bounced_at or event.occurred_at
        if profile is not None:
            db.flush()
            soft = (
                db.query(sa.func.count(DeliverabilityEvent.id))
                .join(Touch, Touch.id == DeliverabilityEvent.touch_id)
                .filter(Touch.profile_id == profile.id, DeliverabilityEvent.kind == "bounce_soft")
                .scalar()
            )
            if soft >= talent_config.TALENT_SOFT_BOUNCES_UNREACHABLE:
                mark_unreachable(db, profile, "repeated_soft_bounces")
                stop_reason = "bounced"
    elif kind == "complaint":
        touch.status, touch.complained_at = "complained", event.occurred_at
        if profile is not None:
            opt_out(db, profile, source="spam_complaint", actor="agent", suppress_reason="complaint",
                    channels=("email",))
        stop_reason = "complaint"
    elif kind == "unsubscribe" and profile is not None:
        opt_out(db, profile, source="provider_unsubscribe", actor="candidate", suppress_reason="unsubscribe")
        stop_reason = "opted_out"

    audit(db, actor="agent", action=f"deliverability.{kind}", subject_type="touch", subject_id=touch.id,
          organization_id=org_id, profile_id=touch.profile_id,
          detail={"provider": provider, "campaign_id": str(touch.campaign_id)})
    db.commit()
    if campaign is not None:
        evaluate_campaign_health(db, campaign)
    if touch.sender_domain:
        evaluate_domain_health(db, touch.sender_domain)
    if stop_reason and profile is not None:
        _stop_sequences(profile.id, stop_reason)
    return event


# ── Health ───────────────────────────────────────────────────────────────────

def _rates(sent: int, bounces: int, complaints: int) -> dict:
    denominator = max(sent, int(talent_config.TALENT_HEALTH_MIN_SAMPLE), 1)
    return {
        "sent": sent, "bounces": bounces, "complaints": complaints,
        "bounce_rate": bounces / max(sent, 1), "complaint_rate": complaints / max(sent, 1),
        "judged_bounce_rate": bounces / denominator, "judged_complaint_rate": complaints / denominator,
    }


def _breaches(r: dict) -> Optional[str]:
    if r["judged_bounce_rate"] > talent_config.TALENT_BOUNCE_PAUSE_RATE:
        return f"bounce rate {r['bounces']}/{r['sent']} above {talent_config.TALENT_BOUNCE_PAUSE_RATE:.1%}"
    if r["judged_complaint_rate"] > talent_config.TALENT_COMPLAINT_PAUSE_RATE:
        return f"complaint rate {r['complaints']}/{r['sent']} above {talent_config.TALENT_COMPLAINT_PAUSE_RATE:.2%}"
    return None


def campaign_health(db: Session, campaign_id) -> dict:
    sent = db.query(sa.func.count(Touch.id)).filter(Touch.campaign_id == campaign_id, Touch.status.in_(LEFT)).scalar()
    bounced_touches = (
        db.query(sa.func.count(sa.distinct(DeliverabilityEvent.touch_id)))
        .filter(DeliverabilityEvent.campaign_id == campaign_id,
                DeliverabilityEvent.kind.in_(("bounce_hard", "bounce_soft")))
        .scalar()
    )
    complaints = (
        db.query(sa.func.count(sa.distinct(DeliverabilityEvent.touch_id)))
        .filter(DeliverabilityEvent.campaign_id == campaign_id, DeliverabilityEvent.kind == "complaint")
        .scalar()
    )
    return _rates(sent or 0, bounced_touches or 0, complaints or 0)


def domain_health(db: Session, domain: str, days: int = 7) -> dict:
    since = datetime.utcnow() - timedelta(days=days)
    sent = db.query(sa.func.count(Touch.id)).filter(
        Touch.sender_domain == domain, Touch.status.in_(LEFT), Touch.sent_at >= since
    ).scalar()
    events = dict(
        db.query(DeliverabilityEvent.kind, sa.func.count(sa.distinct(DeliverabilityEvent.touch_id)))
        .filter(DeliverabilityEvent.sender_domain == domain, DeliverabilityEvent.occurred_at >= since)
        .group_by(DeliverabilityEvent.kind).all()
    )
    return _rates(sent or 0, events.get("bounce_hard", 0) + events.get("bounce_soft", 0), events.get("complaint", 0))


def evaluate_campaign_health(db: Session, campaign: Campaign) -> bool:
    """Pause an active campaign whose bounce/complaint rate is too high. True if paused now."""
    if campaign.status != "active":
        return False
    reason = _breaches(campaign_health(db, campaign.id))
    if not reason:
        return False
    campaign.status, campaign.paused_reason, campaign.paused_at = "paused", f"auto: {reason}", datetime.utcnow()
    audit(db, actor="agent", action="campaign.auto_paused", subject_type="campaign", subject_id=campaign.id,
          organization_id=campaign.organization_id, detail={"reason": reason})
    db.commit()
    logger.warning("Campaign %s auto-paused: %s", campaign.id, reason)
    return True


def evaluate_domain_health(db: Session, domain: str) -> bool:
    row = db.get(SenderDomain, domain)
    if row is None or row.paused:
        return False
    reason = _breaches(domain_health(db, domain))
    if not reason:
        return False
    row.paused, row.paused_reason = True, f"auto: {reason}"
    audit(db, actor="agent", action="sender_domain.auto_paused", subject_type="sender_domain", subject_id=domain,
          detail={"reason": reason})
    db.commit()
    logger.warning("Sender domain %s auto-paused: %s", domain, reason)
    return True


def dead_letter_sweep(db: Session, organization_id: int) -> list:
    """refresh.dead_letter catch-up: profiles with a hard bounce, or repeated
    soft bounces, that are not yet marked unreachable. Returns their ids."""
    soft = talent_config.TALENT_SOFT_BOUNCES_UNREACHABLE
    counts = (
        db.query(Touch.profile_id,
                 sa.func.count().filter(DeliverabilityEvent.kind == "bounce_hard").label("hard"),
                 sa.func.count().filter(DeliverabilityEvent.kind == "bounce_soft").label("soft"))
        .join(DeliverabilityEvent, DeliverabilityEvent.touch_id == Touch.id)
        .join(Campaign, Campaign.id == Touch.campaign_id)
        .join(ResumeMetadata, ResumeMetadata.id == Touch.profile_id)
        .filter(Campaign.organization_id == organization_id, ResumeMetadata.verification_status != "unreachable")
        .group_by(Touch.profile_id)
        .all()
    )
    marked = []
    for profile_id, hard, soft_count in counts:
        if hard or soft_count >= soft:
            profile = db.get(ResumeMetadata, profile_id)
            mark_unreachable(db, profile, "hard_bounce" if hard else "repeated_soft_bounces")
            marked.append(profile_id)
    db.commit()
    return marked
