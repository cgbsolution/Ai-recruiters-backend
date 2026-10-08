"""
Refresh campaigns (Loop A): who is due, who goes first, pilots, dispatch and
the funnel.

A profile is due for a refresh when it was last heard from (confirmed on the
refresh page, or for never-confirmed rows when the CV arrived) longer ago than
the org's refresh window, it has refresh consent by email, it is reachable and
not suppressed, and it isn't already in a running refresh campaign.

Campaigns:
  rolling   "refresh-YYYY-MM" — the daily sweep enrolls due profiles, highest
            priority first (skills that match the org's open jobs), stalest next.
  pilot     a fixed stratified sample by CV age, enrolled once at creation.

Dispatch moves queued members to in_progress, at most the day's remaining
bulk budget × TALENT_ENROLL_FRACTION (the rest is left for touches 2 and 3 of
earlier cohorts), and emits one `talent/refresh.member.dispatched` per member.
"""

from __future__ import annotations

import random
import uuid
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Sequence, Tuple

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.models.job import Job, JobSkill, JobStatus
from app.models.resume_metadata import ResumeMetadata as RM
from app.models.talent import Campaign, CampaignMember, ContactConsent, SuppressionEntry, Touch
from app.talent import caps, identity
from app.talent.audit import audit
from app.talent.config import talent_config
from app.talent.deliverability import campaign_health
from app.talent.settings import freshness_window, get_settings, org_zone
from app.talent.transports import sender_domain

ACTIVE_MEMBER = ("queued", "in_progress")
DEFAULT_STRATA = ((0, 12), (12, 24), (24, None))  # CV age in months


def anchor_expr():
    return sa.func.coalesce(RM.last_verified_at, RM.created_at)


def _parsed():
    return sa.or_(RM.parsing_status.notin_(("pending", "processing")), RM.parsing_status.is_(None))


def _open_job_skills(db: Session, organization_id: int, limit: int = 40) -> List[str]:
    rows = (
        db.query(sa.func.lower(sa.func.coalesce(JobSkill.skill, JobSkill.name)))
        .join(Job, Job.id == JobSkill.job_id)
        .filter(Job.organization_id == organization_id, Job.status == JobStatus.open)
        .distinct()
        .limit(limit)
        .all()
    )
    return [r[0].strip() for r in rows if r[0] and r[0].strip()]


def priority_expr(skills: Sequence[str]):
    if not skills:
        return sa.literal(0)
    idx = sa.func.lower(sa.func.coalesce(RM.skills_index, ""))
    return sum(sa.case((idx.contains(f"|{s}|", autoescape=True), 1), else_=0) for s in skills)


def asked_since(since: datetime):
    """Criteria on CampaignMember (+ Campaign) rows meaning "this person was
    actually asked to refresh after `since`": the sequence ran its course
    (answered, or three touches unanswered) or a refresh message left. A pilot
    cancelled before dispatch, or a touch 1 skipped for missing consent,
    doesn't count — that person can still be asked."""
    sent = sa.exists().where(
        Touch.campaign_id == CampaignMember.campaign_id, Touch.profile_id == CampaignMember.profile_id,
        Touch.status.in_(("sent", "unknown")),
    )
    return sa.and_(
        Campaign.id == CampaignMember.campaign_id, Campaign.kind == "refresh",
        sa.func.coalesce(CampaignMember.completed_at, CampaignMember.enrolled_at) > since,
        sa.or_(CampaignMember.status.in_(("responded", "no_response")), sent),
    )


def eligible_query(db: Session, organization_id: int, now: datetime, *, require_due: bool = True):
    window = freshness_window(get_settings(db, organization_id))
    consented = sa.exists().where(
        ContactConsent.profile_id == RM.id, ContactConsent.channel == "email",
        ContactConsent.purpose == "refresh", ContactConsent.status == "granted",
    )
    running = sa.exists().where(
        CampaignMember.profile_id == RM.id, CampaignMember.status.in_(ACTIVE_MEMBER),
        Campaign.id == CampaignMember.campaign_id, Campaign.kind == "refresh",
        Campaign.status.in_(("active", "paused")),
    )
    # One refresh cycle per window: someone already asked is not asked again
    # until the window passes, answer or not. Without this, non-responders went
    # straight back to "due" and, sorting first, starved the rest of the
    # backlog (found by the 40K load test).
    asked_recently = sa.exists().where(CampaignMember.profile_id == RM.id, asked_since(now - window))
    q = db.query(RM.id, RM.candidate_email, RM.phone, RM.email_status, RM.created_at, anchor_expr().label("anchor")).filter(
        RM.organization_id == organization_id, _parsed(),
        RM.verification_status.notin_(("unreachable", "refreshing")),
        sa.or_(RM.email_status.in_(identity.EMAIL_SENDABLE), RM.email_status.is_(None)),
        consented, ~running, ~asked_recently,
    )
    if require_due:
        q = q.filter(anchor_expr() < now - window)
    return q


def _suppressed_hashes(db: Session, rows) -> set:
    hashes = {identity.suppression_hash("email", r.candidate_email) for r in rows} - {None}
    if not hashes:
        return set()
    found = set()
    hashes = list(hashes)
    for i in range(0, len(hashes), 1000):
        found |= {h for (h,) in db.query(SuppressionEntry.value_hash).filter(
            SuppressionEntry.value_hash.in_(hashes[i:i + 1000]))}
    return found


def _sendable(db: Session, rows) -> list:
    blocked = _suppressed_hashes(db, rows)
    out = []
    for r in rows:
        status = r.email_status or identity.classify_email(r.candidate_email)
        if status not in identity.EMAIL_SENDABLE:
            continue
        if identity.suppression_hash("email", r.candidate_email) in blocked:
            continue
        out.append(r)
    return out


def get_or_create_campaign(db: Session, organization_id: int, kind: str, cycle_key: str, name: str, *,
                           job_id: Optional[int] = None, segment: Optional[dict] = None,
                           created_by: str = "agent") -> Campaign:
    db.execute(
        pg_insert(Campaign.__table__).values(
            id=uuid.uuid4(), organization_id=organization_id, kind=kind, cycle_key=cycle_key, name=name,
            job_id=job_id, segment=segment, status="active", created_by=created_by, created_at=datetime.utcnow(),
        ).on_conflict_do_nothing(constraint="uq_campaigns_org_kind_cycle")
    )
    return db.query(Campaign).filter_by(organization_id=organization_id, kind=kind, cycle_key=cycle_key).one()


def _add_members(db: Session, campaign: Campaign, members: List[Tuple[int, int]]) -> int:
    if not members:
        return 0
    now = datetime.utcnow()
    added = 0
    for i in range(0, len(members), 1000):
        rows = [{"campaign_id": campaign.id, "profile_id": pid, "status": "queued", "priority": prio, "enrolled_at": now}
                for pid, prio in members[i:i + 1000]]
        res = db.execute(pg_insert(CampaignMember.__table__).values(rows).on_conflict_do_nothing())
        added += res.rowcount or 0
    return added


def sweep_enroll(db: Session, organization_id: int, *, limit: int, now: Optional[datetime] = None) -> dict:
    """Enroll up to `limit` due profiles into this month's rolling campaign."""
    now = now or datetime.utcnow()
    if limit <= 0:
        return {"enrolled": 0}
    skills = _open_job_skills(db, organization_id)
    prio = priority_expr(skills).label("priority")
    rows = (
        eligible_query(db, organization_id, now)
        .add_columns(prio)
        .order_by(sa.desc("priority"), anchor_expr().asc(), RM.id)
        .limit(limit * 2)  # headroom for rows dropped by the suppression / email checks
        .all()
    )
    rows = _sendable(db, rows)[:limit]
    if not rows:
        return {"enrolled": 0}
    campaign = get_or_create_campaign(
        db, organization_id, "refresh", f"refresh-{now:%Y-%m}", f"Profile refresh {now:%B %Y}",
        segment={"type": "rolling"},
    )
    added = _add_members(db, campaign, [(r.id, int(r.priority or 0)) for r in rows])
    audit(db, actor="agent", action="campaign.enrolled", subject_type="campaign", subject_id=campaign.id,
          organization_id=organization_id, detail={"enrolled": added, "open_job_skills": len(skills)})
    db.commit()
    return {"enrolled": added, "campaign_id": str(campaign.id)}


def create_pilot(db: Session, organization_id: int, *, size: int = 2000, strata=DEFAULT_STRATA, actor: str,
                 seed: Optional[int] = None, now: Optional[datetime] = None) -> Campaign:
    """Stratified random sample by CV age, split evenly across strata (a
    stratum with fewer eligible profiles gives its share to the others)."""
    now = now or datetime.utcnow()
    rows = _sendable(db, eligible_query(db, organization_id, now, require_due=False).all())
    buckets: Dict[int, list] = defaultdict(list)
    for r in rows:
        age_months = max(0.0, (now - (r.created_at or now)).days / 30.44)
        for i, (lo, hi) in enumerate(strata):
            if age_months >= lo and (hi is None or age_months < hi):
                buckets[i].append(r.id)
                break
    rng = random.Random(seed if seed is not None else uuid.uuid4().int)
    remaining, chosen = size, {}
    order = sorted(range(len(strata)), key=lambda i: len(buckets[i]))  # smallest first, so leftovers flow up
    for n, i in enumerate(order):
        share = remaining // (len(order) - n)
        pick = rng.sample(buckets[i], min(share, len(buckets[i])))
        chosen[i] = pick
        remaining -= len(pick)
    label = lambda lo, hi: f"{lo}-{hi} months" if hi is not None else f"{lo}+ months"  # noqa: E731
    segment = {
        "type": "pilot", "size": size, "seed_used": seed is not None,
        "strata": [{"label": label(lo, hi), "eligible": len(buckets[i]), "selected": len(chosen.get(i, []))}
                   for i, (lo, hi) in enumerate(strata)],
    }
    local = now.replace(tzinfo=timezone.utc).astimezone(org_zone(get_settings(db, organization_id)))
    campaign = get_or_create_campaign(
        db, organization_id, "refresh", f"pilot-{now:%Y%m%d%H%M%S}-{uuid.uuid4().hex[:6]}",
        f"Refresh pilot {local:%d %b %Y}", segment=segment, created_by=actor,
    )
    added = _add_members(db, campaign, [(pid, 0) for ids in chosen.values() for pid in ids])
    audit(db, actor=actor, action="campaign.created", subject_type="campaign", subject_id=campaign.id,
          organization_id=organization_id, detail={"kind": "refresh_pilot", "members": added, "segment": segment})
    db.commit()
    return campaign


def dispatch_budget(db: Session, organization_id: int, now: Optional[datetime] = None) -> int:
    domain = (get_settings(db, organization_id).sender_domain or sender_domain()).lower()
    left = caps.remaining(db, domain, caps.BULK, now)
    dom = caps.get_domain(db, domain)
    share = int(caps.bulk_cap_for(dom, (now or datetime.utcnow()).date()) * talent_config.TALENT_ENROLL_FRACTION)
    return max(0, min(left, share))


def dispatch_queued(db: Session, organization_id: int, *, limit: int, now: Optional[datetime] = None) -> List[Tuple[str, int]]:
    """Move up to `limit` queued members of the org's active refresh campaigns
    to in_progress. Returns [(campaign_id, profile_id)] to start sequences for."""
    now = now or datetime.utcnow()
    if limit <= 0:
        return []
    picked = (
        db.query(CampaignMember.campaign_id, CampaignMember.profile_id)
        .join(Campaign, Campaign.id == CampaignMember.campaign_id)
        .filter(Campaign.organization_id == organization_id, Campaign.kind == "refresh",
                Campaign.status == "active", CampaignMember.status == "queued")
        .order_by(CampaignMember.priority.desc(), CampaignMember.enrolled_at, CampaignMember.profile_id)
        .limit(limit)
        .with_for_update(skip_locked=True, of=CampaignMember)
        .all()
    )
    out = []
    for campaign_id, profile_id in picked:
        db.query(CampaignMember).filter_by(campaign_id=campaign_id, profile_id=profile_id).update(
            {"status": "in_progress", "last_event_at": now}, synchronize_session=False)
        db.query(RM).filter(RM.id == profile_id, RM.verification_status != "unreachable").update(
            {"verification_status": "refreshing"}, synchronize_session=False)
        out.append((str(campaign_id), int(profile_id)))
    db.commit()
    return out


def stalled_members(db: Session, organization_id: int, older_than: timedelta = timedelta(days=1),
                    limit: int = 500) -> List[Tuple[str, int]]:
    """In-progress members whose sequence never sent touch 1 (an event was
    lost): re-dispatched by the sweep. The sequence is idempotent."""
    cutoff = datetime.utcnow() - older_than
    first_sent = sa.exists().where(Touch.campaign_id == CampaignMember.campaign_id,
                                   Touch.profile_id == CampaignMember.profile_id, Touch.touch_no == 1)
    rows = (
        db.query(CampaignMember.campaign_id, CampaignMember.profile_id)
        .join(Campaign, Campaign.id == CampaignMember.campaign_id)
        .filter(Campaign.organization_id == organization_id, Campaign.kind == "refresh",
                Campaign.status == "active", CampaignMember.status == "in_progress",
                CampaignMember.last_event_at < cutoff, ~first_sent)
        .limit(limit).all()
    )
    return [(str(c), int(p)) for c, p in rows]


def finish_member(db: Session, campaign_id, profile_id: int, status: str, reason: Optional[str] = None) -> bool:
    """End a member's sequence (no_response / responded / …) and release the
    'refreshing' status. False if it had already ended."""
    member = db.get(CampaignMember, (uuid.UUID(str(campaign_id)), profile_id))
    if member is None or member.status not in ACTIVE_MEMBER:
        return False
    member.status, member.status_reason, member.completed_at = status, reason, datetime.utcnow()
    profile = db.get(RM, profile_id)
    if profile is not None and profile.verification_status == "refreshing":
        profile.verification_status = "stale" if status != "responded" else "fresh"
    db.commit()
    return True


def set_status(db: Session, campaign: Campaign, status: str, *, actor: str, reason: Optional[str] = None) -> Campaign:
    if status not in ("active", "paused", "cancelled", "completed"):
        raise ValueError("bad status")
    previous = campaign.status
    campaign.status = status
    if status == "paused":
        campaign.paused_reason, campaign.paused_at = reason or "paused by recruiter", datetime.utcnow()
    elif status == "active":
        campaign.paused_reason = None
    if status in ("cancelled", "completed"):
        db.query(CampaignMember).filter(CampaignMember.campaign_id == campaign.id,
                                        CampaignMember.status.in_(ACTIVE_MEMBER)).update(
            {"status": "cancelled", "completed_at": datetime.utcnow()}, synchronize_session=False)
        db.query(RM).filter(RM.verification_status == "refreshing", RM.id.in_(
            db.query(CampaignMember.profile_id).filter(CampaignMember.campaign_id == campaign.id))).update(
            {"verification_status": "stale"}, synchronize_session=False)
    audit(db, actor=actor, action=f"campaign.{status}", subject_type="campaign", subject_id=campaign.id,
          organization_id=campaign.organization_id, detail={"from": previous, "reason": reason})
    db.commit()
    return campaign


# ── Reporting ────────────────────────────────────────────────────────────────

def funnel(db: Session, campaign_id) -> dict:
    """Straight from the raw touches / members rows."""
    members = dict(
        db.query(CampaignMember.status, sa.func.count()).filter(CampaignMember.campaign_id == campaign_id)
        .group_by(CampaignMember.status).all()
    )
    touch_rows = db.query(Touch.touch_no, Touch.status, sa.func.count()).filter(Touch.campaign_id == campaign_id) \
        .group_by(Touch.touch_no, Touch.status).all()
    by_touch: Dict[int, Dict[str, int]] = defaultdict(dict)
    for no, status, n in touch_rows:
        by_touch[int(no)][status] = n

    def distinct_profiles(*conds) -> int:
        return db.query(sa.func.count(sa.distinct(Touch.profile_id))).filter(
            Touch.campaign_id == campaign_id, *conds).scalar() or 0

    skipped = Counter(dict(
        db.query(Touch.skip_reason, sa.func.count()).filter(Touch.campaign_id == campaign_id, Touch.status == "skipped")
        .group_by(Touch.skip_reason).all()
    ))
    return {
        "members": sum(members.values()),
        "members_by_status": members,
        "sent": distinct_profiles(Touch.status.in_(("sent", "bounced", "complained"))),
        "messages_sent": sum(n for t in by_touch.values() for s, n in t.items() if s in ("sent", "bounced", "complained")),
        "opened": distinct_profiles(Touch.opened_at.isnot(None)),
        "refreshed": members.get("responded", 0),
        "unreachable": members.get("bounced", 0),
        "opted_out": members.get("opted_out", 0),
        "no_response": members.get("no_response", 0),
        "touches": {str(k): v for k, v in sorted(by_touch.items())},
        "skipped_reasons": dict(skipped),
        "health": campaign_health(db, campaign_id),
    }


def segments(db: Session, organization_id: int, now: Optional[datetime] = None) -> dict:
    """Counts for the campaign console."""
    now = now or datetime.utcnow()
    base = db.query(RM).filter(RM.organization_id == organization_id, _parsed())
    by_status = dict(
        db.query(RM.verification_status, sa.func.count()).filter(RM.organization_id == organization_id, _parsed())
        .group_by(RM.verification_status).all()
    )
    consented = db.query(sa.func.count(sa.distinct(ContactConsent.profile_id))).join(
        RM, RM.id == ContactConsent.profile_id).filter(
        RM.organization_id == organization_id, ContactConsent.channel == "email",
        ContactConsent.purpose == "refresh", ContactConsent.status == "granted").scalar()
    age_buckets = {}
    for lo, hi in DEFAULT_STRATA:
        q = base.filter(RM.created_at <= now - timedelta(days=30.44 * lo))
        if hi is not None:
            q = q.filter(RM.created_at > now - timedelta(days=30.44 * hi))
        age_buckets[f"{lo}-{hi}" if hi else f"{lo}+"] = q.count()
    return {
        "profiles": base.count(),
        "by_verification_status": by_status,
        "refresh_consent": consented or 0,
        "due_now": eligible_query(db, organization_id, now).count(),
        "cv_age_months": age_buckets,
    }


# ── Daily jobs (called by app.agent) ────────────────────────────────────────

def sweep_org(db: Session, organization_id: int, *, rolling: bool, now: Optional[datetime] = None) -> dict:
    """Enroll (rolling campaigns only), then dispatch what today's budget
    allows, plus any member whose start event was lost."""
    now = now or datetime.utcnow()
    budget = dispatch_budget(db, organization_id, now)
    enrolled = sweep_enroll(db, organization_id, limit=budget, now=now)["enrolled"] if rolling else 0
    dispatch = dispatch_queued(db, organization_id, limit=budget, now=now)
    stalled = stalled_members(db, organization_id)
    return {"budget": budget, "enrolled": enrolled, "dispatch": [list(p) for p in dispatch + stalled]}


def reclassify_stale(db: Session, organization_id: int, now: Optional[datetime] = None) -> int:
    """refresh.reengage: profiles marked fresh whose window has run out are
    stale again, which puts them back in front of the sweep."""
    now = now or datetime.utcnow()
    window = freshness_window(get_settings(db, organization_id))
    count = (
        db.query(RM).filter(RM.organization_id == organization_id, RM.verification_status == "fresh",
                            anchor_expr() < now - window)
        .update({"verification_status": "stale"}, synchronize_session=False)
    )
    if count:
        audit(db, actor="agent", action="profiles.went_stale", subject_type="organization",
              subject_id=organization_id, organization_id=organization_id, detail={"count": count})
    db.commit()
    return count
