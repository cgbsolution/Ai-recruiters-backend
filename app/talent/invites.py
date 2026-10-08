"""
Loop C — rank and invite.

plan()          after matching: the top K invitable matches. Autopilot jobs →
                invite them; copilot jobs → an approval request per match.
send_invite()   the only way an invite leaves: refuses a copilot job without a
                recruiter's approval, and goes through the consent-gated outbox.
respond()       the candidate's answer on /t/invite: interested (with CTC and
                notice confirmed) → an interview is offered; not interested →
                the match closes. No answer: one reminder after 3 days, then
                the match closes (app.agent.invite_send).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import List, Optional

from sqlalchemy.orm import Session

from app.models.job import Job, JobStatus
from app.models.resume_metadata import ResumeMetadata, mark_candidate_confirmation
from app.models.talent import AccessToken, CampaignMember, JobMatch, Touch
from app.talent import approvals, caps, campaigns, consent, outbox, tokens
from app.talent.audit import audit
from app.talent.match_loop import ordered_matches
from app.talent.settings import get_job_settings, loop_enabled

INVITABLE = ("ranked", "needs_refresh")
DONE_REFRESHING = ("responded", "no_response", "skipped", "opted_out", "bounced", "cancelled")


class InviteBlocked(Exception):
    pass


def invite_campaign(db: Session, job: Job):
    return campaigns.get_or_create_campaign(db, job.organization_id, "invite", f"job-{job.id}", f"Invites · {job.title}",
                                            job_id=job.id)


def _refresh_settled(db: Session, match: JobMatch, profile: ResumeMetadata) -> bool:
    """A needs_refresh match may be invited once the profile is fresh again,
    or its refresh sequence is over (the invite asks for CTC and notice anyway)."""
    if profile.verification_status == "fresh":
        return True
    member = (
        db.query(CampaignMember).join(campaigns.Campaign, campaigns.Campaign.id == CampaignMember.campaign_id)
        .filter(CampaignMember.profile_id == profile.id, campaigns.Campaign.job_id == match.job_id,
                campaigns.Campaign.kind == "refresh")
        .first()
    )
    return member is None or member.status in DONE_REFRESHING


def plan(db: Session, job_id: int) -> dict:
    job = db.get(Job, job_id)
    if job is None or job.status != JobStatus.open:
        return {"skip": "job_not_open", "invite": [], "approvals": 0}
    if not loop_enabled(db, job.organization_id, "invite"):
        return {"skip": "loop_off", "invite": [], "approvals": 0}
    js = get_job_settings(db, job.id, job.organization_id)
    top = ordered_matches(db, job.id, js.top_k)
    to_invite, approvals_made, waiting = [], 0, 0
    for match, profile in top:
        if match.status not in INVITABLE or match.ai_score is None:
            continue
        if match.status == "needs_refresh" and not _refresh_settled(db, match, profile):
            waiting += 1
            continue
        if not consent.can_contact(db, profile.id, "email", "jobs").allowed:
            continue  # never queued, never asked about: the gate would refuse anyway
        if js.autonomy_mode == "autopilot":
            to_invite.append(str(match.id))
        else:
            approvals.request(db, match, "invite")
            match.status = "approval_pending"
            approvals_made += 1
    db.commit()
    return {"invite": to_invite, "approvals": approvals_made, "waiting_for_refresh": waiting,
            "autonomy": js.autonomy_mode}


def _role_summary(db: Session, job: Job) -> str:
    from app.models.job_metadata import JobMetadata

    jm = db.query(JobMetadata).filter(JobMetadata.job_id == job.id).first()
    if jm and jm.responsibilities_summary:
        return jm.responsibilities_summary[:600]
    skills = ", ".join((s.skill or s.name) for s in job.skills[:6] if (s.skill or s.name))
    return f"{job.title} in {job.department}" + (f", working with {skills}" if skills else "") + "."


def send_invite(db: Session, match_id: str, touch_no: int = 1, now: Optional[datetime] = None) -> dict:
    """Send the invite (touch 1) or the reminder (touch 2)."""
    match = db.get(JobMatch, uuid.UUID(match_id))
    if match is None:
        return {"status": "stopped", "reason": "match_missing"}
    if match.status in ("declined", "closed", "scheduled", "interviewed", "shortlisted", "submitted"):
        return {"status": "stopped", "reason": match.status}
    if match.response:
        return {"status": "stopped", "reason": "responded"}
    job = db.get(Job, match.job_id)
    js = get_job_settings(db, job.id, job.organization_id)
    if job.status != JobStatus.open:
        return {"status": "stopped", "reason": "job_not_open"}
    if js.autonomy_mode != "autopilot":
        try:
            approval = approvals.require(db, match, "invite")
        except approvals.NotApproved:
            audit(db, actor="agent", action="invite.blocked", subject_type="job_match", subject_id=match.id,
                  organization_id=match.organization_id, profile_id=match.profile_id,
                  detail={"reason": "copilot_without_approval"}, commit=True)
            return {"status": "stopped", "reason": "not_approved"}
        match.invite_approval_id = approval.id
    campaign = invite_campaign(db, job)
    db.commit()
    kind = "invite" if touch_no == 1 else "invite_reminder"
    result = outbox.send_touch(
        db, campaign_id=campaign.id, profile_id=match.profile_id, touch_no=touch_no, kind=kind,
        priority=caps.TRANSACTIONAL, action=("invite", str(match.id)), now=now,
        extra_ctx={"job_title": job.title, "job_location": job.location, "role_summary": _role_summary(db, job)},
    )
    if result.status == "sent" and touch_no == 1:
        match = db.get(JobMatch, match.id)
        match.status, match.invited_at = "invited", now or datetime.utcnow()
        db.commit()
    elif result.status in ("skipped", "failed") and touch_no == 1:
        match = db.get(JobMatch, match.id)
        match.status, match.status_reason = "closed", f"invite_{result.reason}"[:120]
        db.commit()
        return {"status": "stopped", "reason": result.reason}
    out = result.to_dict()
    if out["status"] in ("unknown", "failed"):
        out["status"] = "sent"
    return out


def close_unanswered(db: Session, match_id: str) -> bool:
    match = db.get(JobMatch, uuid.UUID(match_id))
    if match is None or match.response or match.status not in ("invited",):
        return False
    match.status, match.status_reason = "closed", "no_response"
    audit(db, actor="agent", action="invite.closed_no_response", subject_type="job_match", subject_id=match.id,
          organization_id=match.organization_id, profile_id=match.profile_id)
    db.commit()
    return True


# ── Candidate page ──────────────────────────────────────────────────────────

class LinkError(Exception):
    pass


def page_data(db: Session, row: AccessToken) -> dict:
    match = db.get(JobMatch, uuid.UUID(row.ref_id)) if row.ref_id else None
    if match is None:
        raise LinkError()
    job = db.get(Job, match.job_id)
    profile = db.get(ResumeMetadata, match.profile_id)
    from app.models.organization import Organization

    org = db.get(Organization, job.organization_id)
    touch = (db.query(Touch).filter(Touch.profile_id == profile.id, Touch.template.in_(("invite", "invite_reminder")))
             .order_by(Touch.created_at.desc()).first())
    if touch is not None and touch.opened_at is None:
        touch.opened_at = datetime.utcnow()
        db.commit()
    return {
        "organization": org.name if org else "", "first_name": outbox.first_name(profile),
        "job": {"title": job.title, "location": job.location, "department": job.department,
                "type": job.job_type, "summary": _role_summary(db, job),
                "experience": f"{job.experience_min or 0}-{job.experience_max or ''} years".replace("- years", "+ years")},
        "current": {"current_ctc": profile.current_ctc, "expected_ctc": profile.expected_ctc,
                    "notice_period_days": profile.notice_period_days},
        "open": job.status == JobStatus.open and match.status not in ("closed",),
    }


def respond(db: Session, row: AccessToken, payload: dict) -> dict:
    """Interested → returns the interview link; not interested → closes the match."""
    match = db.get(JobMatch, uuid.UUID(row.ref_id)) if row.ref_id else None
    if match is None or match.response:
        raise LinkError()
    interested = bool(payload.get("interested"))
    from app.talent.refresh import _clean_number

    confirmed = {}
    if interested:
        confirmed = {
            "current_ctc": _clean_number(payload.get("current_ctc"), 0, 1000),
            "expected_ctc": _clean_number(payload.get("expected_ctc"), 0, 1000),
            "notice_period_days": None if payload.get("notice_period_days") in (None, "")
            else int(_clean_number(payload.get("notice_period_days"), 0, 365)),
        }
    if not tokens.consume(db, row):
        raise LinkError()
    now = datetime.utcnow()
    profile = db.get(ResumeMetadata, match.profile_id)
    match.response, match.responded_at = ("interested" if interested else "not_interested"), now
    match.confirmed = confirmed or None
    for t in db.query(Touch).filter(Touch.profile_id == profile.id, Touch.template.in_(("invite", "invite_reminder"))):
        t.responded_at = t.responded_at or now
    if interested:
        mark_candidate_confirmation(db)
        for key, value in confirmed.items():
            if value is not None:
                setattr(profile, key, value)
        profile.last_verified_at, profile.verification_status = now, "fresh"
    else:
        match.status, match.status_reason = "declined", "candidate_not_interested"
    audit(db, actor="candidate", action="invite.responded", subject_type="job_match", subject_id=match.id,
          organization_id=match.organization_id, profile_id=profile.id,
          detail={"interested": interested, "confirmed_fields": sorted(k for k, v in confirmed.items() if v is not None)})
    db.commit()

    from app.agent.client import emit

    emit("talent/invite.responded", {"match_id": str(match.id), "interested": interested})
    result = {"ok": True, "interested": interested}
    if interested:
        from app.talent import scheduling

        interview, link = scheduling.offer_interview(db, match.id)
        result["interview_url"] = link
    return result


def invite_now(db: Session, match: JobMatch, user) -> None:
    """A recruiter invites one candidate directly (any autonomy mode)."""
    approvals.record_direct(db, match, "invite", user)
    if match.status in INVITABLE + ("approval_pending",):
        match.status = "approval_pending"
    db.commit()


def approved_match_ids(db: Session, approval_ids: List[str]) -> List[str]:
    out = []
    for aid in approval_ids:
        row = approvals.get(db, aid)
        if row is not None and row.status == "approved" and row.kind == "invite":
            out.append(str(row.match_id))
    return out
