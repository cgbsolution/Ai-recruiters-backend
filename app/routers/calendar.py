"""
Calendar router — tenant-scoped feed of scheduled interviews for the admin
Calendar view.

The calendar's single source of truth is the `candidates` table: a candidate is
"on the calendar" once it has an `interview_date`. Tenancy is enforced exactly
like the rest of the admin API — by joining to the candidate's `Job` and
filtering on `Job.organization_id` (see `app.auth.get_org_id`). Only a global
super admin (`app.auth.is_global_admin`) gets an unfiltered view.

No new columns / migrations: this endpoint only READS existing data and derives
a normalized status `category` the frontend uses for colors, badges, and the
per-day summary counts.
"""
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.auth import get_current_user, get_org_id, is_global_admin
from app.database import get_db
from app.models.candidate import Candidate, EmailLog
from app.models.interview import InterviewSession, SessionStatus
from app.models.job import Job
from app.models.user import User

router = APIRouter(dependencies=[Depends(get_current_user)])


# ── Response schema ──────────────────────────────────────────────────────────
class CalendarInterview(BaseModel):
    id: int                       # candidate id (stable per scheduled interview)
    candidate_id: int
    candidate_name: str
    candidate_email: Optional[str] = None
    job_id: Optional[int] = None
    job_title: Optional[str] = None        # "Job Position"
    department: Optional[str] = None
    round: Optional[str] = None            # hiring stage → "Interview Round"
    interviewer: Optional[str] = None      # hiring manager / recruiter
    status: str                            # raw interview_status (for display)
    category: str                          # normalized: scheduled | in_progress |
    #                                        completed | cancelled | rescheduled | no_show
    interview_date: Optional[datetime] = None   # naive UTC (serialized without "Z")
    interview_timezone: Optional[str] = None
    meeting_link: Optional[str] = None


# ── Status normalization ─────────────────────────────────────────────────────
# Canonical calendar categories the UI knows how to color/badge. The raw
# `interview_status` free-text is mapped into one of these. `pending` in the
# day-summary = total − completed − cancelled, so every non-completed,
# non-cancelled category lands in "pending" naturally.
CAT_SCHEDULED = "scheduled"
CAT_IN_PROGRESS = "in_progress"
CAT_COMPLETED = "completed"
CAT_CANCELLED = "cancelled"
CAT_RESCHEDULED = "rescheduled"
CAT_NO_SHOW = "no_show"


def _normalize_category(raw_status: Optional[str], *, has_active_session: bool, was_rescheduled: bool) -> str:
    """Map a candidate's free-text interview_status into a calendar category.

    Order matters: an active session wins over the stored status (it may lag),
    terminal states are checked before the upcoming/scheduled fallback.
    """
    s = (raw_status or "").strip().lower()

    if has_active_session:
        return CAT_IN_PROGRESS
    # mid-interview connectivity states surfaced by the live session flow
    if "awaiting rejoin" in s or ("disconnected" in s and "network" in s):
        return CAT_IN_PROGRESS
    if s == "completed":
        return CAT_COMPLETED
    # Terminal/concluded states — the interview ran and ended one way or another.
    if "cheating" in s or "terminated" in s or "ended by candidate" in s:
        return CAT_COMPLETED
    if s == "cancelled":
        return CAT_CANCELLED
    # Candidate never showed / dropped before starting.
    if "no show" in s or "no-show" in s or "dropped" in s:
        return CAT_NO_SHOW
    if was_rescheduled and s in ("scheduled", "new", "added from database", "applied"):
        return CAT_RESCHEDULED
    # Default: it has a date → it's an upcoming, scheduled interview.
    return CAT_SCHEDULED


@router.get(
    "/interviews",
    response_model=List[CalendarInterview],
    summary="List scheduled interviews for the calendar (org-scoped)",
    description=(
        "Returns every candidate in the caller's organization that has an "
        "`interview_date`, enriched with job position, department, round, "
        "interviewer, and a normalized status `category` for the calendar UI.\n\n"
        "Optional `start`/`end` (ISO datetimes) limit the result to interviews "
        "within that window — the frontend passes the visible calendar range."
    ),
)
def list_calendar_interviews(
    start: Optional[datetime] = Query(None, description="Window start (inclusive), ISO datetime"),
    end: Optional[datetime] = Query(None, description="Window end (exclusive), ISO datetime"),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    org_id = get_org_id(user)  # 403 for a non-super-admin without an organization

    q = (
        db.query(Candidate, Job)
        .join(Job, Candidate.job_id == Job.id)
        .filter(Candidate.interview_date.isnot(None))
    )

    # ── Tenancy: only this org's interviews (global super admin → all) ──
    # Decided by the caller's role, not by org_id being None, so a missing org
    # can never widen the result to every tenant.
    if not is_global_admin(user):
        if org_id is None:
            return []
        q = q.filter(Job.organization_id == org_id)

    # Hide placeholder rows still awaiting resume parsing / permanently failed.
    q = q.filter(~Candidate.email.like("pending_%@resume.local"))

    if start is not None:
        q = q.filter(Candidate.interview_date >= start)
    if end is not None:
        q = q.filter(Candidate.interview_date < end)

    rows = q.order_by(Candidate.interview_date.asc()).all()
    candidate_ids = [c.id for c, _ in rows]

    # ── One extra query each: who is mid-interview, and who was rescheduled ──
    active_session_ids: set[int] = set()
    rescheduled_ids: set[int] = set()
    if candidate_ids:
        active_rows = (
            db.query(InterviewSession.candidate_id)
            .filter(
                InterviewSession.candidate_id.in_(candidate_ids),
                InterviewSession.status == SessionStatus.in_progress,
            )
            .all()
        )
        active_session_ids = {cid for (cid,) in active_rows if cid is not None}

        resched_rows = (
            db.query(EmailLog.candidate_id)
            .filter(
                EmailLog.candidate_id.in_(candidate_ids),
                EmailLog.trigger_event == "reschedule",
            )
            .all()
        )
        rescheduled_ids = {cid for (cid,) in resched_rows if cid is not None}

    results: List[CalendarInterview] = []
    for candidate, job in rows:
        category = _normalize_category(
            candidate.interview_status,
            has_active_session=candidate.id in active_session_ids,
            was_rescheduled=candidate.id in rescheduled_ids,
        )
        interviewer = (job.manager_name or candidate.added_by) if job else candidate.added_by
        results.append(
            CalendarInterview(
                id=candidate.id,
                candidate_id=candidate.id,
                candidate_name=candidate.name,
                candidate_email=candidate.email,
                job_id=job.id if job else None,
                job_title=job.title if job else None,
                department=job.department if job else None,
                round=candidate.candidate_stage,
                interviewer=interviewer,
                status=candidate.interview_status or "Scheduled",
                category=category,
                interview_date=candidate.interview_date,
                interview_timezone=candidate.interview_timezone,
                meeting_link=candidate.interview_link,
            )
        )

    return results
