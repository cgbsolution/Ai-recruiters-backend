"""
Outcomes only a person may decide.

submit_to_client()   records the recruiter's approval and marks the match
                     submitted. Nothing is sent anywhere automatically: the
                     recruiter shares the packet (client_packet()).
send_rejection()     the ONLY code path that sends "not moving forward". It
                     demands an approved reject_message decision by a user —
                     a score, a failed interview or a timeout can never call it
                     successfully, and nothing in the agent calls it at all.

Offers and salary discussions are not built into the agent: they stay with people.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy.orm import Session

from app.models.job import Job
from app.models.resume_metadata import ResumeMetadata
from app.models.talent import JobMatch, TalentInterview
from app.talent import approvals, caps, outbox
from app.talent.audit import audit, user_actor
from app.talent.campaigns import get_or_create_campaign


class DecisionError(Exception):
    pass


def submit_to_client(db: Session, match: JobMatch, user, note: Optional[str] = None) -> JobMatch:
    if match.status in ("submitted",):
        return match
    if match.status in ("closed", "declined"):
        raise DecisionError("This candidate's process is closed.")
    approval = approvals.record_direct(db, match, "submit", user, note)
    approvals.require(db, match, "submit")  # belt and braces: refuses anything not decided by a user
    match.status, match.status_reason = "submitted", None
    audit(db, actor=user_actor(user), action="match.submitted_to_client", subject_type="job_match", subject_id=match.id,
          organization_id=match.organization_id, profile_id=match.profile_id,
          detail={"approval_id": str(approval.id), "job_id": match.job_id})
    db.commit()
    return match


def send_rejection(db: Session, match: JobMatch, now: Optional[datetime] = None) -> dict:
    """Send the not-moving-forward message — only after a recruiter approved it."""
    approval = approvals.require(db, match, "reject_message")
    job = db.get(Job, match.job_id)
    campaign = get_or_create_campaign(db, job.organization_id, "decision", f"job-{job.id}", f"Decisions · {job.title}",
                                      job_id=job.id)
    db.commit()
    result = outbox.send_touch(db, campaign_id=campaign.id, profile_id=match.profile_id, touch_no=1,
                               kind="not_moving_forward", priority=caps.TRANSACTIONAL,
                               extra_ctx={"job_title": job.title}, now=now)
    match = db.get(JobMatch, match.id)
    match.status, match.status_reason = "closed", "not_moving_forward"
    audit(db, actor=f"user:{approval.decided_by}", action="match.rejection_sent", subject_type="job_match",
          subject_id=match.id, organization_id=match.organization_id, profile_id=match.profile_id,
          detail={"approval_id": str(approval.id), "send_status": result.status, "reason": result.reason})
    db.commit()
    return result.to_dict()


def reject(db: Session, match: JobMatch, user, note: Optional[str] = None, notify: bool = True) -> dict:
    """A recruiter closes the candidate's process; the message goes only if they chose to notify."""
    approvals.record_direct(db, match, "reject_message", user, note)
    db.commit()
    if notify:
        return send_rejection(db, match)
    match.status, match.status_reason = "closed", "closed_by_recruiter"
    audit(db, actor=user_actor(user), action="match.closed", subject_type="job_match", subject_id=match.id,
          organization_id=match.organization_id, profile_id=match.profile_id, detail={"notified": False})
    db.commit()
    return {"status": "closed"}


def client_packet(db: Session, match: JobMatch) -> dict:
    """What a recruiter shares with the client: evidence, not raw scores alone."""
    profile = db.get(ResumeMetadata, match.profile_id)
    job = db.get(Job, match.job_id)
    iv = db.query(TalentInterview).filter_by(match_id=match.id).first()
    return {
        "job": job.title,
        "candidate": {"name": profile.candidate_name, "current_designation": profile.current_designation,
                      "current_company": profile.current_company, "experience_years": profile.experience_years,
                      "notice_period_days": profile.notice_period_days, "expected_ctc": profile.expected_ctc},
        "must_haves": match.must_haves, "strengths": match.strengths, "gaps": match.gaps,
        "interview": None if iv is None else {
            "summary": iv.summary, "overall_score": iv.overall_score, "reviewed": iv.human_review_status == "reviewed",
            "criteria": [{"criterion": s["criterion"],
                          "score": s["override"]["score"] if s.get("override") else s["score"],
                          "evidence": [e["quote"] for e in s.get("evidence", []) if e.get("verified")][:3]}
                         for s in iv.scores or []],
        },
    }
