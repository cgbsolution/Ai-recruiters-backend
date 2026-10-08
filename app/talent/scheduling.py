"""
Loop D — scheduling.

offer_interview()  an interested candidate gets an interview and 3 slots.
                   Slots come from the job's panel (panel_user_ids): business
                   hours in the org's time zone, minus each member's Google
                   Calendar busy times and their booked interviews. No panel →
                   the AI interviews alone and any business-hours slot works.
book()             under a per-owner Postgres advisory lock, with an overlap
                   check (and the partial unique index as a backstop), so two
                   candidates can never hold the same person's time.
reschedule() / no_show()
                   one automatic re-offer per interview; after that the
                   interview is flagged for a recruiter instead.

Calendar events are created on the panel member's calendar WITHOUT the
candidate as an attendee: Google would otherwise email the candidate itself,
outside the consent gate. Candidates hear about times only through the outbox.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, time, timedelta, timezone
from typing import List, Optional, Tuple
from zoneinfo import ZoneInfo

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.job import Job
from app.models.talent import InterviewBooking, JobMatch, TalentInterview
from app.models.user import User
from app.talent import tokens
from app.talent.audit import audit
from app.talent.settings import get_job_settings, get_settings, org_zone

logger = logging.getLogger("app.talent.scheduling")

SLOTS_OFFERED = 3
MIN_LEAD = timedelta(hours=20)
HORIZON_DAYS = 10
DAY_START, DAY_END = time(10, 0), time(18, 0)
AUTO_RESCHEDULES = 1
NO_SHOW_GRACE = timedelta(minutes=30)
ASYNC_JOIN_EARLY = timedelta(minutes=15)
_LOCK_BASE = 727_300_000


class SlotTaken(Exception):
    pass


class NotReschedulable(Exception):
    pass


def _utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def _naive(dt: datetime) -> datetime:
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


def label(dt_utc: datetime, zone: ZoneInfo) -> str:
    local = _utc(dt_utc).astimezone(zone)
    return local.strftime("%a %d %b, %I:%M %p ") + (local.tzname() or "")


# ── Availability ─────────────────────────────────────────────────────────────

def google_busy(user: User, start: datetime, end: datetime) -> Optional[List[Tuple[datetime, datetime]]]:
    """Busy intervals (naive UTC) from the user's Google Calendar, or None if
    the calendar can't be read (then that person is not offered)."""
    if not (user.google_access_token and user.google_refresh_token):
        return []
    try:
        from googleapiclient.discovery import build

        from app.services.google_calendar_service import _get_credentials

        service = build("calendar", "v3", credentials=_get_credentials(user.google_access_token,
                                                                       user.google_refresh_token),
                        cache_discovery=False)
        body = {"timeMin": _utc(start).isoformat(), "timeMax": _utc(end).isoformat(), "items": [{"id": "primary"}]}
        cal = service.freebusy().query(body=body).execute()["calendars"]["primary"]
        if cal.get("errors"):
            return None
        return [(_naive(datetime.fromisoformat(b["start"].replace("Z", "+00:00"))),
                 _naive(datetime.fromisoformat(b["end"].replace("Z", "+00:00")))) for b in cal.get("busy", [])]
    except Exception as exc:  # noqa: BLE001 — an unreadable calendar is "unavailable", never "free"
        logger.warning("Could not read a panel calendar (%s)", type(exc).__name__)
        return None


def _booked(db: Session, owner_id: int, start: datetime, end: datetime) -> List[Tuple[datetime, datetime]]:
    return [(b.starts_at, b.ends_at) for b in db.query(InterviewBooking).filter(
        InterviewBooking.owner_user_id == owner_id, InterviewBooking.status == "booked",
        InterviewBooking.starts_at < end, InterviewBooking.ends_at > start)]


def _overlaps(a_start, a_end, intervals) -> bool:
    return any(a_start < e and a_end > s for s, e in intervals)


def candidate_slots(db: Session, job: Job, now: Optional[datetime] = None, count: int = SLOTS_OFFERED,
                    exclude: Optional[List[datetime]] = None) -> List[Tuple[datetime, datetime, Optional[int]]]:
    """[(start, end, owner_user_id)] naive UTC — at most one slot per day, the
    first free one, alternating mornings and afternoons."""
    now = now or datetime.utcnow()
    js = get_job_settings(db, job.id, job.organization_id)
    zone = org_zone(get_settings(db, job.organization_id))
    length = timedelta(minutes=int(js.slot_minutes or 45))
    panel = []
    if js.panel_user_ids:
        panel = db.query(User).filter(User.id.in_(js.panel_user_ids), User.organization_id == job.organization_id,
                                      User.is_active.is_(True)).all()
    window_start, window_end = now + MIN_LEAD, now + timedelta(days=HORIZON_DAYS)
    busy = {}
    for member in panel:
        cal = google_busy(member, window_start, window_end)
        if cal is not None:
            busy[member.id] = cal + _booked(db, member.id, window_start, window_end)
    if panel and not busy:
        return []  # a panel is required and no calendar could be read
    exclude = exclude or []
    slots = []
    local_now = _utc(window_start).astimezone(zone)
    for day in range(HORIZON_DAYS + 1):
        if len(slots) >= count:
            break
        date = (local_now + timedelta(days=day)).date()
        if date.weekday() >= 5:
            continue
        starts = []
        t = datetime.combine(date, DAY_START, tzinfo=zone)
        while t + length <= datetime.combine(date, DAY_END, tzinfo=zone):
            starts.append(t)
            t += timedelta(minutes=30)
        if len(slots) % 2 == 1:
            starts = starts[len(starts) // 2:] + starts[: len(starts) // 2]  # afternoon first on alternate days
        for s in starts:
            start, end = _naive(s), _naive(s + length)
            if start < window_start or start in exclude:
                continue
            if not panel:
                slots.append((start, end, None))
                break
            owner = next((uid for uid, intervals in busy.items() if not _overlaps(start, end, intervals)), None)
            if owner is not None:
                slots.append((start, end, owner))
                break
    return slots


# ── Offer, book, reschedule ─────────────────────────────────────────────────

def _mode(db: Session, job: Job) -> str:
    from app.talent.settings import loop_enabled

    mode = get_job_settings(db, job.id, job.organization_id).interview_mode or "async_text"
    if mode == "live_voice" and not loop_enabled(db, job.organization_id, "live_voice"):
        return "async_voice"  # live voice stays behind its switch until it is trusted
    return mode


def propose(db: Session, interview: TalentInterview, now: Optional[datetime] = None) -> List[InterviewBooking]:
    match = db.get(JobMatch, interview.match_id)
    job = db.get(Job, match.job_id)
    db.query(InterviewBooking).filter(InterviewBooking.interview_id == interview.id,
                                      InterviewBooking.status == "offered").update({"status": "released"})
    previous = [b.starts_at for b in db.query(InterviewBooking).filter(InterviewBooking.interview_id == interview.id)]
    rows = []
    for start, end, owner in candidate_slots(db, job, now, exclude=previous):
        row = InterviewBooking(interview_id=interview.id, owner_user_id=owner, starts_at=start, ends_at=end,
                               status="offered")
        db.add(row)
        rows.append(row)
    if not rows:
        interview.needs_recruiter = True
        audit(db, actor="agent", action="interview.no_slots", subject_type="interview", subject_id=interview.id,
              organization_id=match.organization_id, profile_id=match.profile_id)
    db.flush()
    return rows


def offer_interview(db: Session, match_id, now: Optional[datetime] = None) -> Tuple[TalentInterview, str]:
    """Create (or reuse) the interview for an interested candidate, propose
    slots and return their interview page link."""
    match = db.get(JobMatch, uuid.UUID(str(match_id)))
    job = db.get(Job, match.job_id)
    interview = db.query(TalentInterview).filter_by(match_id=match.id).first()
    if interview is None:
        js = get_job_settings(db, job.id, job.organization_id)
        interview = TalentInterview(match_id=match.id, mode=_mode(db, job), status="offered",
                                    deadline_at=(now or datetime.utcnow()) + timedelta(days=4))
        db.add(interview)
        db.flush()
        propose(db, interview, now)
        audit(db, actor="agent", action="interview.offered", subject_type="interview", subject_id=interview.id,
              organization_id=match.organization_id, profile_id=match.profile_id,
              detail={"mode": interview.mode, "job_id": job.id, "slot_minutes": js.slot_minutes})
    raw = tokens.issue(db, match.profile_id, "interview", ref_id=str(interview.id))
    db.commit()
    from app.agent.client import emit

    emit("talent/interview.offered", {"interview_id": str(interview.id), "attempt": interview.reschedule_count,
                                      "key": f"{interview.id}:{interview.reschedule_count}:offer"})
    return interview, tokens.page_url("interview", raw)


def _lock_owner(db: Session, owner_id: int) -> None:
    db.execute(sa.text("SELECT pg_advisory_xact_lock(:k)"), {"k": _LOCK_BASE + int(owner_id)})


def book(db: Session, interview_id, booking_id, now: Optional[datetime] = None) -> TalentInterview:
    now = now or datetime.utcnow()
    interview = (db.query(TalentInterview).filter(TalentInterview.id == uuid.UUID(str(interview_id)))
                 .with_for_update().first())
    booking = db.get(InterviewBooking, uuid.UUID(str(booking_id)))
    if interview is None or booking is None or booking.interview_id != interview.id:
        raise SlotTaken("unknown slot")
    if interview.status not in ("offered",) or booking.status != "offered":
        raise SlotTaken("this slot is no longer available")
    if booking.starts_at < now + timedelta(hours=1):
        raise SlotTaken("this slot is too soon")
    owner = db.get(User, booking.owner_user_id) if booking.owner_user_id else None
    if owner is not None:
        _lock_owner(db, owner.id)
        clash = _booked(db, owner.id, booking.starts_at, booking.ends_at)
        cal = google_busy(owner, booking.starts_at, booking.ends_at)
        if clash or cal is None or _overlaps(booking.starts_at, booking.ends_at, cal or []):
            booking.status = "released"
            db.commit()
            raise SlotTaken("that time was just taken — please pick another")
    booking.status = "booked"
    db.query(InterviewBooking).filter(InterviewBooking.interview_id == interview.id, InterviewBooking.id != booking.id,
                                      InterviewBooking.status == "offered").update({"status": "released"})
    interview.status, interview.scheduled_at, interview.slot_ends_at = "scheduled", booking.starts_at, booking.ends_at
    interview.calendar_owner_user_id = booking.owner_user_id
    match = db.get(JobMatch, interview.match_id)
    match.status = "scheduled"
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise SlotTaken("that time was just taken — please pick another")
    audit(db, actor="candidate", action="interview.booked", subject_type="interview", subject_id=interview.id,
          organization_id=match.organization_id, profile_id=match.profile_id,
          detail={"starts_at": booking.starts_at.isoformat(), "owner_user_id": booking.owner_user_id,
                  "attempt": interview.reschedule_count})
    db.commit()
    if interview.mode == "live_voice":
        from app.talent import live_voice

        live_voice.ensure_application(db, interview)  # the live interviewer's job application + invite
        db.commit()
    if owner is not None:
        _calendar_event(db, interview, owner)
    from app.agent.client import emit

    emit("talent/interview.booked", {"interview_id": str(interview.id), "attempt": interview.reschedule_count,
                                     "key": f"{interview.id}:{interview.reschedule_count}:booked"})
    return interview


def _calendar_event(db: Session, interview: TalentInterview, owner: User) -> None:
    if not (owner.google_access_token and owner.google_refresh_token):
        return
    try:
        from app.services.google_calendar_service import create_meet_event

        match = db.get(JobMatch, interview.match_id)
        job = db.get(Job, match.job_id)
        zone = org_zone(get_settings(db, job.organization_id))
        result = create_meet_event(
            owner.google_access_token, owner.google_refresh_token, summary=f"Interview · {job.title}",
            description="AnytimeHire Talent Agent interview. Candidate details are in AnytimeHire.",
            start_dt=_utc(interview.scheduled_at).astimezone(zone).replace(tzinfo=None),
            end_dt=_utc(interview.slot_ends_at).astimezone(zone).replace(tzinfo=None),
            attendee_email="", timezone=str(zone),
        )
        interview.calendar_event_id, interview.meeting_link = result.get("event_id"), result.get("meet_link")
        if result.get("new_access_token"):
            owner.google_access_token = result["new_access_token"]
        db.commit()
    except Exception as exc:  # noqa: BLE001 — the booking stands without a calendar entry
        logger.warning("Calendar event not created (%s)", type(exc).__name__)
        db.rollback()


def _release_booking(db: Session, interview: TalentInterview) -> None:
    db.query(InterviewBooking).filter(InterviewBooking.interview_id == interview.id,
                                      InterviewBooking.status == "booked").update({"status": "released"})


def reschedule(db: Session, interview_id, actor: str, now: Optional[datetime] = None,
               by_recruiter: bool = False) -> TalentInterview:
    """One automatic re-offer per interview; after that a recruiter steps in.
    A recruiter may always re-offer (each re-offer is a new attempt number)."""
    interview = db.get(TalentInterview, uuid.UUID(str(interview_id)))
    allowed = ("scheduled", "offered", "no_show", "expired", "human_requested") if by_recruiter else (
        "scheduled", "offered", "no_show")
    if interview is None or interview.status not in allowed:
        raise NotReschedulable("this interview can't be moved")
    match = db.get(JobMatch, interview.match_id)
    if not by_recruiter and interview.reschedule_count >= AUTO_RESCHEDULES:
        interview.needs_recruiter = True
        audit(db, actor=actor, action="interview.reschedule_flagged", subject_type="interview",
              subject_id=interview.id, organization_id=match.organization_id, profile_id=match.profile_id)
        db.commit()
        raise NotReschedulable("a recruiter will contact you to find another time")
    _release_booking(db, interview)
    interview.reschedule_count += 1
    interview.status, interview.scheduled_at, interview.slot_ends_at = "offered", None, None
    if by_recruiter:
        interview.needs_recruiter = False
    interview.deadline_at = (now or datetime.utcnow()) + timedelta(days=4)
    match.status = "invited"
    propose(db, interview, now)
    audit(db, actor=actor, action="interview.rescheduled", subject_type="interview", subject_id=interview.id,
          organization_id=match.organization_id, profile_id=match.profile_id,
          detail={"attempt": interview.reschedule_count})
    db.commit()
    from app.agent.client import emit

    emit("talent/interview.rescheduled", {"interview_id": str(interview.id)})
    if actor != "agent":  # a no-show re-offer is sent by the reminders run itself (interview_missed)
        emit("talent/interview.offered", {"interview_id": str(interview.id), "attempt": interview.reschedule_count,
                                          "key": f"{interview.id}:{interview.reschedule_count}:offer"})
    return interview


def no_show_check(db: Session, interview_id: str, attempt: int, now: Optional[datetime] = None) -> dict:
    """After the slot: a candidate who never started is a no-show — the first
    time they are offered new times, the second time a recruiter is flagged."""
    now = now or datetime.utcnow()
    interview = db.get(TalentInterview, uuid.UUID(interview_id))
    if interview is None or interview.reschedule_count != attempt or interview.status != "scheduled":
        return {"status": "moved_on"}
    if interview.started_at is not None:
        return {"status": "started"}
    if interview.slot_ends_at and now < interview.slot_ends_at + NO_SHOW_GRACE:
        return {"status": "too_early", "retry_at": (interview.slot_ends_at + NO_SHOW_GRACE).isoformat()}
    match = db.get(JobMatch, interview.match_id)
    interview.no_show_count += 1
    audit(db, actor="agent", action="interview.no_show", subject_type="interview", subject_id=interview.id,
          organization_id=match.organization_id, profile_id=match.profile_id,
          detail={"no_shows": interview.no_show_count})
    if interview.reschedule_count < AUTO_RESCHEDULES:
        db.commit()
        reschedule(db, interview.id, actor="agent", now=now)
        return {"status": "reoffered"}
    _release_booking(db, interview)
    interview.status, interview.needs_recruiter = "no_show", True
    db.commit()
    return {"status": "flagged"}


def expire_offer(db: Session, interview_id: str, attempt: int) -> bool:
    interview = db.get(TalentInterview, uuid.UUID(interview_id))
    if interview is None or interview.status != "offered" or interview.reschedule_count != attempt:
        return False
    match = db.get(JobMatch, interview.match_id)
    interview.status, interview.needs_recruiter = "expired", True
    db.query(InterviewBooking).filter(InterviewBooking.interview_id == interview.id,
                                      InterviewBooking.status == "offered").update({"status": "released"})
    audit(db, actor="agent", action="interview.offer_expired", subject_type="interview", subject_id=interview.id,
          organization_id=match.organization_id, profile_id=match.profile_id)
    db.commit()
    return True


def human_requested(db: Session, interview: TalentInterview) -> None:
    """The candidate asked to speak to a person instead of the AI."""
    match = db.get(JobMatch, interview.match_id)
    interview.status, interview.human_requested_at, interview.needs_recruiter = "human_requested", datetime.utcnow(), True
    _release_booking(db, interview)
    audit(db, actor="candidate", action="interview.human_requested", subject_type="interview", subject_id=interview.id,
          organization_id=match.organization_id, profile_id=match.profile_id)
    db.commit()


# ── Messages ─────────────────────────────────────────────────────────────────

def message_context(db: Session, interview: TalentInterview) -> dict:
    match = db.get(JobMatch, interview.match_id)
    job = db.get(Job, match.job_id)
    zone = org_zone(get_settings(db, job.organization_id))
    offered = (db.query(InterviewBooking).filter(InterviewBooking.interview_id == interview.id,
                                                 InterviewBooking.status == "offered")
               .order_by(InterviewBooking.starts_at).all())
    return {"job_title": job.title, "slot_labels": [label(b.starts_at, zone) for b in offered],
            "when_label": label(interview.scheduled_at, zone) if interview.scheduled_at else ""}


TOUCH_OFFSETS = {"interview_offer": 1, "interview_confirmed": 2, "interview_reminder_24h": 3,
                 "interview_reminder_1h": 4, "interview_missed": 5}


def send_interview_message(db: Session, interview_id: str, kind: str, attempt: int,
                           expect_scheduled_at: Optional[str] = None, now: Optional[datetime] = None) -> dict:
    """One interview email, idempotent per (interview attempt, kind). Skips if
    the interview moved on (rescheduled, cancelled, started)."""
    from app.talent import caps, outbox
    from app.talent.campaigns import get_or_create_campaign

    interview = db.get(TalentInterview, uuid.UUID(interview_id))
    if interview is None or interview.reschedule_count != attempt:
        return {"status": "stopped", "reason": "moved_on"}
    if kind in ("interview_confirmed", "interview_reminder_24h", "interview_reminder_1h"):
        if interview.status != "scheduled" or (
                expect_scheduled_at and interview.scheduled_at.isoformat() != expect_scheduled_at):
            return {"status": "stopped", "reason": "not_scheduled"}
    if kind in ("interview_offer", "interview_missed") and interview.status != "offered":
        return {"status": "stopped", "reason": "not_offered"}
    current = now or datetime.utcnow()
    if kind == "interview_reminder_24h" and interview.scheduled_at - current < timedelta(hours=20):
        return {"status": "stopped", "reason": "too_late"}
    if kind == "interview_reminder_1h" and interview.scheduled_at - current < timedelta(minutes=10):
        return {"status": "stopped", "reason": "too_late"}
    match = db.get(JobMatch, interview.match_id)
    job = db.get(Job, match.job_id)
    campaign = get_or_create_campaign(db, job.organization_id, "interview", f"job-{job.id}", f"Interviews · {job.title}",
                                      job_id=job.id)
    db.commit()
    result = outbox.send_touch(
        db, campaign_id=campaign.id, profile_id=match.profile_id, touch_no=100 * attempt + TOUCH_OFFSETS[kind],
        kind=kind, priority=caps.TRANSACTIONAL, action=("interview", str(interview.id)),
        extra_ctx=message_context(db, interview), now=now,
    )
    return result.to_dict()


def join_window(interview: TalentInterview) -> Tuple[Optional[datetime], Optional[datetime]]:
    """When an async interview may be taken: from 15 minutes before the slot
    until the no-show check runs."""
    if not interview.scheduled_at:
        return None, None
    return interview.scheduled_at - ASYNC_JOIN_EARLY, (interview.slot_ends_at or interview.scheduled_at) + NO_SHOW_GRACE
