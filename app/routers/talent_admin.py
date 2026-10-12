"""
Talent Agent admin API (/talent/...): Phase 0 clean-up, settings and loop
switches, consent, campaigns, deliverability, erasure and the audit view.
Job matching, approvals and interview review live in app.routers.talent_jobs.

Every route is scoped to one organization through app.talent.access.talent_org.
"""

import re
import uuid
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.auth import get_current_user
from app.config import settings as app_settings
from app.database import get_db
from app.models.resume_metadata import ResumeMetadata
from app.models.talent import AuditLog, Campaign
from app.models.user import User
from app.talent import caps, campaigns, consent, deliverability, identity, phase0, retention
from app.talent import settings as talent_settings
from app.talent.access import require_talent_admin, talent_org
from app.talent.audit import audit, user_actor
from app.talent.config import talent_config

router = APIRouter(dependencies=[Depends(get_current_user)])

_DOMAIN = re.compile(r"^(?=.{3,253}$)([a-z0-9-]{1,63}\.)+[a-z]{2,63}$")


def _emit(name: str, data: dict) -> bool:
    from app.agent.client import emit

    return emit(name, data)


# ── Phase 0: inspect and clean ──────────────────────────────────────────────

class BackfillRequest(BaseModel):
    check_mx: bool = False


class MergeRequest(BaseModel):
    dry_run: bool = True


@router.get("/phase0/report", summary="Duplicate groups and email validity in the talent pool")
def phase0_report(org_id: int = Depends(talent_org), db: Session = Depends(get_db)):
    return phase0.report(db, org_id)


@router.post("/phase0/backfill", summary="Compute dedupe keys, email status and freshness for every profile")
def phase0_backfill(
    body: BackfillRequest,
    org_id: int = Depends(talent_org),
    user: User = Depends(require_talent_admin),
    db: Session = Depends(get_db),
):
    return phase0.backfill(db, org_id, check_mx=body.check_mx, actor=user_actor(user))


@router.post("/phase0/merge-duplicates", summary="Fold same-email duplicates into the newest CV (dry run by default)")
def phase0_merge(
    body: MergeRequest,
    org_id: int = Depends(talent_org),
    user: User = Depends(require_talent_admin),
    db: Session = Depends(get_db),
):
    return phase0.merge_duplicates(db, org_id, actor=user_actor(user), dry_run=body.dry_run)


# ── Settings & loop switches ────────────────────────────────────────────────

def _sender_domain(db: Session, org_id: int) -> str:
    from app.talent.transports import sender_domain

    return (talent_settings.get_settings(db, org_id).sender_domain or sender_domain()).lower()


def _smtp_ok() -> bool:
    return bool(app_settings.SMTP_HOST and app_settings.SMTP_USER and app_settings.SMTP_PASS)


def _settings_out(db: Session, org_id: int) -> dict:
    from app.talent import claude, embeddings

    row = talent_settings.get_settings(db, org_id)
    domain = _sender_domain(db, org_id)
    warnings = []
    if not talent_config.TALENT_AGENT_ENABLED:
        warnings.append("TALENT_AGENT_ENABLED is off: no loop runs, whatever the switches below say.")
    if not talent_config.INNGEST_EVENT_KEY and not talent_config.inngest_is_dev:
        warnings.append("INNGEST_EVENT_KEY is not set: the loops cannot be scheduled.")
    if not talent_config.TALENT_PUBLIC_API_URL:
        warnings.append("TALENT_PUBLIC_API_URL is not set: one-click unsubscribe (a Gmail/Yahoo bulk-sender "
                        "requirement) falls back to the unsubscribe page.")
    if not embeddings.enabled():
        warnings.append("EMBEDDING_PROVIDER is not set: matching uses keyword similarity instead of vectors.")
    elif embeddings.provider() == "ollama" and (problem := embeddings.ollama_problem()):
        warnings.append(problem)
    if not claude.available():
        warnings.append("ANTHROPIC_API_KEY is not set: no AI parsing, ranking or interview scoring.")
    if talent_config.TALENT_EMAIL_TRANSPORT == "smtp" and not _smtp_ok():
        warnings.append("SMTP is not configured: email sends will wait.")
    return {
        "flags": {k: bool((row.flags or {}).get(k, False)) for k in talent_settings.LOOPS + talent_settings.EXTRA_FLAGS},
        "agent_enabled": talent_config.TALENT_AGENT_ENABLED,
        "timezone": row.timezone, "quiet_hours_start": row.quiet_hours_start, "quiet_hours_end": row.quiet_hours_end,
        "refresh_after_days": row.refresh_after_days, "retention_months": row.retention_months,
        "default_autonomy": row.default_autonomy, "sender_domain": domain,
        "consent_text": talent_settings.consent_text(row),
        "send_ramp": caps.get_domain(db, domain).ramp,
        "models": {"parse": talent_config.TALENT_PARSE_MODEL, "rerank": talent_config.TALENT_RERANK_MODEL,
                   "scorer": talent_config.TALENT_SCORER_MODEL, "interview": talent_config.TALENT_INTERVIEW_MODEL},
        "embedding_provider": talent_config.EMBEDDING_PROVIDER or None,
        "warnings": warnings,
    }


class SettingsUpdate(BaseModel):
    flags: Optional[Dict[str, bool]] = None
    timezone: Optional[str] = None
    quiet_hours_start: Optional[int] = None
    quiet_hours_end: Optional[int] = None
    refresh_after_days: Optional[int] = None
    retention_months: Optional[int] = None
    default_autonomy: Optional[str] = None
    sender_domain: Optional[str] = None
    consent_text: Optional[str] = None
    send_ramp: Optional[List[Dict[str, int]]] = None


@router.get("/settings", summary="Talent Agent settings, loop switches and configuration warnings")
def get_talent_settings(org_id: int = Depends(talent_org), db: Session = Depends(get_db)):
    out = _settings_out(db, org_id)
    db.commit()
    return out


@router.put("/settings", summary="Change Talent Agent settings and loop switches")
def update_talent_settings(
    body: SettingsUpdate,
    org_id: int = Depends(talent_org),
    user: User = Depends(require_talent_admin),
    db: Session = Depends(get_db),
):
    from zoneinfo import ZoneInfo

    row = talent_settings.get_or_create_settings(db, org_id)
    changes = body.model_dump(exclude_unset=True)
    if changes.get("flags") is not None:
        unknown = set(changes["flags"]) - set(talent_settings.LOOPS + talent_settings.EXTRA_FLAGS)
        if unknown:
            raise HTTPException(status_code=400, detail=f"Unknown switches: {sorted(unknown)}")
        row.flags = {**(row.flags or {}), **changes["flags"]}
    if changes.get("timezone"):
        try:
            ZoneInfo(changes["timezone"])
        except Exception:
            raise HTTPException(status_code=400, detail="Unknown time zone")
        row.timezone = changes["timezone"]
    for key in ("quiet_hours_start", "quiet_hours_end"):
        if changes.get(key) is not None:
            if not 0 <= int(changes[key]) <= 23:
                raise HTTPException(status_code=400, detail=f"{key} must be an hour 0-23")
            setattr(row, key, int(changes[key]))
    if changes.get("refresh_after_days") is not None:
        row.refresh_after_days = max(30, min(730, int(changes["refresh_after_days"])))
    if changes.get("retention_months") is not None:
        row.retention_months = max(1, min(120, int(changes["retention_months"])))
    if changes.get("default_autonomy") is not None:
        if changes["default_autonomy"] not in talent_settings.AUTONOMY_MODES:
            raise HTTPException(status_code=400, detail="default_autonomy must be autopilot or copilot")
        row.default_autonomy = changes["default_autonomy"]
    if "sender_domain" in changes:
        domain = (changes["sender_domain"] or "").strip().lower() or None
        if domain and not _DOMAIN.match(domain):
            raise HTTPException(status_code=400, detail="Not a domain name")
        row.sender_domain = domain
    if "consent_text" in changes:
        row.consent_text = (changes["consent_text"] or "").strip()[:4000] or None
    if changes.get("send_ramp"):
        ramp = sorted(({"from_day": int(s["from_day"]), "cap": int(s["cap"])} for s in changes["send_ramp"]),
                      key=lambda s: s["from_day"])
        if ramp[0]["from_day"] != 0 or any(not 0 <= s["cap"] <= 100_000 for s in ramp):
            raise HTTPException(status_code=400, detail="The ramp must start at day 0, with caps between 0 and 100000")
        db.flush()
        caps.get_domain(db, _sender_domain(db, org_id)).ramp = ramp
    row.updated_by = user.id
    audit(db, actor=user_actor(user), action="settings.updated", subject_type="organization", subject_id=org_id,
          organization_id=org_id, detail={"changed": sorted(changes), "flags": changes.get("flags")})
    db.commit()
    return _settings_out(db, org_id)


# ── Segments, campaigns, funnel ─────────────────────────────────────────────

class PilotRequest(BaseModel):
    size: int = 2000
    seed: Optional[int] = None


class CampaignStatusRequest(BaseModel):
    status: str  # pause | resume | cancel
    reason: Optional[str] = None


def _campaign_or_404(db: Session, org_id: int, campaign_id: str) -> Campaign:
    try:
        cid = uuid.UUID(campaign_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Campaign not found")
    row = db.get(Campaign, cid)
    if row is None or row.organization_id != org_id:
        raise HTTPException(status_code=404, detail="Campaign not found")
    return row


def _campaign_out(db: Session, c: Campaign) -> dict:
    return {"id": str(c.id), "kind": c.kind, "name": c.name, "status": c.status, "job_id": c.job_id,
            "segment": c.segment, "paused_reason": c.paused_reason, "created_at": c.created_at,
            "funnel": campaigns.funnel(db, c.id)}


@router.get("/segments", summary="Talent pool counts for the campaign console")
def get_segments(org_id: int = Depends(talent_org), db: Session = Depends(get_db)):
    return campaigns.segments(db, org_id)


@router.get("/campaigns", summary="Campaigns with their funnels")
def list_campaigns(kind: Optional[str] = None, org_id: int = Depends(talent_org), db: Session = Depends(get_db)):
    q = db.query(Campaign).filter(Campaign.organization_id == org_id)
    if kind:
        q = q.filter(Campaign.kind == kind)
    return [_campaign_out(db, c) for c in q.order_by(Campaign.created_at.desc()).limit(100)]


@router.get("/campaigns/{campaign_id}", summary="One campaign and its funnel")
def get_campaign(campaign_id: str, org_id: int = Depends(talent_org), db: Session = Depends(get_db)):
    return _campaign_out(db, _campaign_or_404(db, org_id, campaign_id))


@router.post("/campaigns/pilot", summary="Create a stratified refresh pilot (CV age <12, 12-24, 24+ months)")
def create_refresh_pilot(
    body: PilotRequest,
    org_id: int = Depends(talent_org),
    user: User = Depends(require_talent_admin),
    db: Session = Depends(get_db),
):
    if not 1 <= body.size <= 10_000:
        raise HTTPException(status_code=400, detail="size must be between 1 and 10000")
    campaign = campaigns.create_pilot(db, org_id, size=body.size, actor=user_actor(user), seed=body.seed)
    _emit("talent/refresh.sweep.requested", {"organization_id": org_id})
    return _campaign_out(db, campaign)


@router.post("/campaigns/{campaign_id}/status", summary="Pause, resume or cancel a campaign")
def set_campaign_status(
    campaign_id: str,
    body: CampaignStatusRequest,
    org_id: int = Depends(talent_org),
    user: User = Depends(require_talent_admin),
    db: Session = Depends(get_db),
):
    row = _campaign_or_404(db, org_id, campaign_id)
    target = {"pause": "paused", "resume": "active", "cancel": "cancelled"}.get(body.status)
    if target is None:
        raise HTTPException(status_code=400, detail="status must be pause, resume or cancel")
    campaigns.set_status(db, row, target, actor=user_actor(user), reason=body.reason)
    return _campaign_out(db, row)


@router.post("/sweep", summary="Run the refresh sweep for this organization now")
def run_sweep_now(org_id: int = Depends(talent_org), user: User = Depends(require_talent_admin)):
    return {"queued": _emit("talent/refresh.sweep.requested", {"organization_id": org_id})}


# ── Deliverability ──────────────────────────────────────────────────────────

@router.get("/deliverability", summary="Today's send budget, ramp and bounce/complaint health")
def get_deliverability(org_id: int = Depends(talent_org), db: Session = Depends(get_db)):
    domain = _sender_domain(db, org_id)
    today = datetime.utcnow().date()
    dom = caps.get_domain(db, domain)
    out = {
        "usage_today": caps.usage(db, domain, today),
        "ramp_started_on": dom.ramp_started_on.isoformat(),
        "ramp": dom.ramp,
        "forecast": [{"day": (today + timedelta(days=i)).isoformat(), "cap": caps.cap_for(dom, today + timedelta(days=i))}
                     for i in range(0, 22, 7)],
        "domain_health_7d": deliverability.domain_health(db, domain),
        "thresholds": {"bounce_rate": talent_config.TALENT_BOUNCE_PAUSE_RATE,
                       "complaint_rate": talent_config.TALENT_COMPLAINT_PAUSE_RATE,
                       "min_sample": talent_config.TALENT_HEALTH_MIN_SAMPLE},
    }
    db.commit()
    return out


@router.post("/sender-domain/resume", summary="Resume the sender domain after an auto-pause")
def resume_sender_domain(
    org_id: int = Depends(talent_org),
    user: User = Depends(require_talent_admin),
    db: Session = Depends(get_db),
):
    domain = _sender_domain(db, org_id)
    dom = caps.get_domain(db, domain)
    dom.paused, dom.paused_reason = False, None
    audit(db, actor=user_actor(user), action="sender_domain.resumed", subject_type="sender_domain",
          subject_id=domain, organization_id=org_id)
    db.commit()
    return {"domain": domain, "paused": False}


# ── Consent & suppression ───────────────────────────────────────────────────

class ConsentRecord(BaseModel):
    email: str
    channel: str = "email"
    purpose: str
    granted: bool = True


class ConsentImport(BaseModel):
    records: List[ConsentRecord]
    source: str
    basis: str  # the lawful basis / evidence relied on — stored on every row


class ConsentBulk(BaseModel):
    profile_ids: Optional[List[int]] = None
    resume_source: Optional[str] = None  # application | bulk_upload | import
    channel: str = "email"
    purposes: List[str]
    basis: str
    confirm: bool = False


class ProfileConsentChange(BaseModel):
    channel: str
    purpose: str
    granted: bool
    basis: str


class SuppressRequest(BaseModel):
    email: Optional[str] = None
    phone: Optional[str] = None


def _profile_or_404(db: Session, org_id: int, profile_id: int) -> ResumeMetadata:
    row = db.get(ResumeMetadata, profile_id)
    if row is None or row.organization_id != org_id:
        raise HTTPException(status_code=404, detail="Profile not found")
    return row


@router.post("/consent/import", summary="Record consent evidence for profiles, matched by email")
def import_consent(
    body: ConsentImport,
    org_id: int = Depends(talent_org),
    user: User = Depends(require_talent_admin),
    db: Session = Depends(get_db),
):
    if len(body.basis.strip()) < 10:
        raise HTTPException(status_code=400, detail="Describe the basis for this consent (at least 10 characters).")
    if len(body.records) > 50_000:
        raise HTTPException(status_code=400, detail="At most 50000 records per import")
    by_email: Dict[str, List[int]] = {}
    for pid, email in db.query(ResumeMetadata.id, ResumeMetadata.candidate_email).filter(
            ResumeMetadata.organization_id == org_id):
        key = identity.canonical_email(email)
        if key:
            by_email.setdefault(key, []).append(pid)
    applied = unmatched = invalid = 0
    for rec in body.records:
        ids = by_email.get(identity.canonical_email(rec.email) or "")
        if not ids:
            unmatched += 1
            continue
        for pid in ids:
            try:
                consent.set_consent(db, pid, rec.channel, rec.purpose, rec.granted,
                                    source=f"import:{body.source}"[:120], actor=user_actor(user), basis=body.basis,
                                    organization_id=org_id)
                applied += 1
            except ValueError:
                invalid += 1
    audit(db, actor=user_actor(user), action="consent.imported", subject_type="organization", subject_id=org_id,
          organization_id=org_id,
          detail={"applied": applied, "unmatched": unmatched, "invalid": invalid, "source": body.source})
    db.commit()
    return {"applied": applied, "unmatched": unmatched, "invalid": invalid}


@router.post("/consent/bulk", summary="Record consent for a set of profiles on a stated basis (an admin decision)")
def bulk_consent(
    body: ConsentBulk,
    org_id: int = Depends(talent_org),
    user: User = Depends(require_talent_admin),
    db: Session = Depends(get_db),
):
    if len(body.basis.strip()) < 20:
        raise HTTPException(status_code=400, detail="Describe the lawful basis for contacting these people.")
    q = db.query(ResumeMetadata).filter(ResumeMetadata.organization_id == org_id)
    if body.profile_ids:
        q = q.filter(ResumeMetadata.id.in_(body.profile_ids))
    elif body.resume_source:
        q = q.filter(ResumeMetadata.source == body.resume_source)
    else:
        raise HTTPException(status_code=400, detail="Choose profile_ids or a resume_source")
    profiles = q.all()
    # Never re-grant someone who opted out or complained.
    eligible = [p for p in profiles if not consent.is_suppressed(db, email=p.candidate_email, phone=p.phone)]
    if not body.confirm:
        return {"would_apply": len(eligible), "suppressed_skipped": len(profiles) - len(eligible),
                "confirm": "Send again with confirm=true to record it."}
    try:
        for p in eligible:
            for purpose in body.purposes:
                existing = consent.consent_summary(db, p.id)
                if any(c["purpose"] == purpose and c["channel"] == body.channel and c["status"] == "withdrawn"
                       for c in existing):
                    continue  # a withdrawal by the candidate is never overridden in bulk
                consent.set_consent(db, p.id, body.channel, purpose, True, source="admin_bulk",
                                    actor=user_actor(user), basis=body.basis, organization_id=org_id)
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc))
    audit(db, actor=user_actor(user), action="consent.bulk_recorded", subject_type="organization", subject_id=org_id,
          organization_id=org_id,
          detail={"profiles": len(eligible), "purposes": body.purposes, "channel": body.channel})
    db.commit()
    return {"applied": len(eligible), "suppressed_skipped": len(profiles) - len(eligible)}


@router.get("/profiles/{profile_id}/consent", summary="A profile's consent and contact state")
def get_profile_consent(profile_id: int, org_id: int = Depends(talent_org), db: Session = Depends(get_db)):
    p = _profile_or_404(db, org_id, profile_id)
    return {
        "consents": consent.consent_summary(db, p.id),
        "suppressed": consent.is_suppressed(db, email=p.candidate_email, phone=p.phone),
        "verification_status": p.verification_status, "email_status": p.email_status,
        "last_verified_at": p.last_verified_at,
    }


@router.post("/profiles/{profile_id}/consent", summary="Record a consent the candidate gave a recruiter")
def set_profile_consent(
    profile_id: int,
    body: ProfileConsentChange,
    org_id: int = Depends(talent_org),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    p = _profile_or_404(db, org_id, profile_id)
    if len(body.basis.strip()) < 5:
        raise HTTPException(status_code=400, detail="Say how the candidate gave this consent.")
    try:
        consent.set_consent(db, p.id, body.channel, body.purpose, body.granted, source="recruiter",
                            actor=user_actor(user), basis=body.basis, organization_id=org_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    db.commit()
    return {"consents": consent.consent_summary(db, p.id)}


@router.post("/suppression", summary="Add an email or phone to the do-not-contact list")
def add_suppression(
    body: SuppressRequest,
    org_id: int = Depends(talent_org),
    user: User = Depends(require_talent_admin),
    db: Session = Depends(get_db),
):
    if not body.email and not body.phone:
        raise HTTPException(status_code=400, detail="email or phone is required")
    added = consent.suppress(db, email=body.email, phone=body.phone, reason="admin")
    audit(db, actor=user_actor(user), action="suppression.added", subject_type="organization", subject_id=org_id,
          organization_id=org_id, detail={"entries": added})
    db.commit()
    return {"added": added}


@router.delete("/profiles/{profile_id}", summary="Erase a candidate: profile, CV versions, interviews, recordings")
def erase_profile(
    profile_id: int,
    org_id: int = Depends(talent_org),
    user: User = Depends(require_talent_admin),
    db: Session = Depends(get_db),
):
    _profile_or_404(db, org_id, profile_id)
    result = retention.erase_profile(db, profile_id, actor=user_actor(user), reason="admin_erasure")
    _emit("talent/member.stopped", {"profile_id": profile_id, "campaign_id": None, "reason": "erased"})
    return result


@router.get("/retention", summary="Profiles past their retention date (deleted daily only if the retention switch is on)")
def retention_report(org_id: int = Depends(talent_org), db: Session = Depends(get_db)):
    due = retention.due_for_deletion(db, org_id, limit=100_000)
    row = talent_settings.get_settings(db, org_id)
    return {"due": len(due), "deletes_enabled": bool((row.flags or {}).get("retention")),
            "retention_months": row.retention_months}


# ── Audit view ──────────────────────────────────────────────────────────────

@router.get("/audit", summary="Audit log, filterable by candidate, action and actor")
def get_audit(
    profile_id: Optional[int] = None,
    action: Optional[str] = None,
    actor: Optional[str] = None,
    subject_type: Optional[str] = None,
    since: Optional[datetime] = None,
    until: Optional[datetime] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    org_id: int = Depends(talent_org),
    db: Session = Depends(get_db),
):
    q = db.query(AuditLog).filter(AuditLog.organization_id == org_id)
    if profile_id is not None:
        q = q.filter(AuditLog.profile_id == profile_id)
    if action:
        q = q.filter(AuditLog.action.startswith(action, autoescape=True))
    if actor:
        q = q.filter(AuditLog.actor == actor)
    if subject_type:
        q = q.filter(AuditLog.subject_type == subject_type)
    if since:
        q = q.filter(AuditLog.at >= since)
    if until:
        q = q.filter(AuditLog.at < until)
    total = q.count()
    rows = q.order_by(AuditLog.at.desc(), AuditLog.id.desc()).offset((page - 1) * page_size).limit(page_size).all()
    actions = [a for (a,) in db.query(AuditLog.action).filter(AuditLog.organization_id == org_id).distinct().limit(300)]
    actors = [a for (a,) in db.query(AuditLog.actor).filter(AuditLog.organization_id == org_id).distinct().limit(300)]
    return {
        "total": total, "page": page, "page_size": page_size, "actions": sorted(actions), "actors": sorted(actors),
        "items": [{"id": r.id, "at": r.at, "actor": r.actor, "action": r.action, "subject_type": r.subject_type,
                   "subject_id": r.subject_id, "profile_id": r.profile_id, "detail": r.detail} for r in rows],
    }
