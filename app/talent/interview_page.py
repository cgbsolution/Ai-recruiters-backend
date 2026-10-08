"""
What the candidate's interview link (/t/interview/<token>) shows and does.
The link carries purpose "interview" and the interview id; it works until the
interview is done or the link expires.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy.orm import Session

from app.models.job import Job
from app.models.organization import Organization
from app.models.talent import AccessToken, InterviewBooking, JobMatch, TalentInterview
from app.talent import scheduling, tokens
from app.talent.settings import get_job_settings, get_settings, org_zone

AI_DISCLOSURE = (
    "This first-round interview is run by an AI interviewer, not a person. It asks questions about your experience "
    "for this role and records your answers (text, or audio if you choose to speak) so the hiring team can review "
    "them. A recruiter reviews every interview — the AI never makes the decision. You can ask to speak to a person "
    "instead at any time."
)


class LinkError(Exception):
    pass


def resolve(db: Session, raw: str) -> TalentInterview:
    row: Optional[AccessToken] = tokens.lookup(db, raw, "interview")
    if row is None or not row.ref_id:
        raise LinkError()
    interview = db.get(TalentInterview, uuid.UUID(row.ref_id))
    if interview is None:
        raise LinkError()
    return interview


def state(db: Session, interview: TalentInterview, now: Optional[datetime] = None) -> dict:
    now = now or datetime.utcnow()
    match = db.get(JobMatch, interview.match_id)
    job = db.get(Job, match.job_id)
    org = db.get(Organization, job.organization_id)
    js = get_job_settings(db, job.id, job.organization_id)
    zone = org_zone(get_settings(db, job.organization_id))
    offered = (db.query(InterviewBooking).filter(InterviewBooking.interview_id == interview.id,
                                                 InterviewBooking.status == "offered")
               .order_by(InterviewBooking.starts_at).all())
    opens, closes = scheduling.join_window(interview)
    can_start = (interview.status == "scheduled" and interview.mode != "live_voice" and opens is not None
                 and opens <= now <= closes)
    return {
        "organization": org.name if org else "", "job_title": job.title, "status": interview.status,
        "mode": interview.mode, "question_count": js.question_count, "time_limit_minutes": js.time_limit_minutes,
        "slots": [{"id": str(b.id), "label": scheduling.label(b.starts_at, zone), "starts_at": b.starts_at.isoformat()}
                  for b in offered],
        "scheduled_label": scheduling.label(interview.scheduled_at, zone) if interview.scheduled_at else None,
        "scheduled_at": interview.scheduled_at.isoformat() if interview.scheduled_at else None,
        "opens_at": opens.isoformat() if opens else None,
        "can_start": can_start,
        "can_reschedule": interview.status in ("scheduled", "offered") and interview.reschedule_count < scheduling.AUTO_RESCHEDULES,
        "needs_recruiter": interview.needs_recruiter,
        "meeting_link": interview.meeting_link if interview.mode == "live_voice" else None,
        "disclosure": AI_DISCLOSURE,
        "consented": interview.consent_recorded_at is not None,
    }
