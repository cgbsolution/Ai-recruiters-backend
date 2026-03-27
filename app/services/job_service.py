from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from sqlalchemy import func, cast, Date
from sqlalchemy.orm import Session

from app.models.candidate import Candidate
from app.models.job import (
    InterviewSettings,
    Job,
    JobQuestion,
    JobResponsibility,
    JobSkill,
)
from app.schemas.job import JobCreate, JobUpdate


def _insert_children(db: Session, job: Job, data: JobCreate) -> None:
    for r in data.responsibilities:
        db.add(JobResponsibility(job_id=job.id, description=r.description))

    for s in data.skills:
        db.add(JobSkill(
            job_id=job.id,
            name=s.name,
            skill=s.skill,
            skill_criteria=s.skill_criteria,
        ))

    if data.interview_settings:
        s = data.interview_settings
        db.add(InterviewSettings(
            job_id=job.id,
            code_editor_required=s.code_editor_required,
            resume_based=s.resume_based,
            virtual_avatar=s.virtual_avatar,
            allow_mobile=s.allow_mobile,
            face_match=s.face_match,
            webcam_required=s.webcam_required,
            secondary_cam=s.secondary_cam,
            mode=s.mode,
            max_questions=s.max_questions,
            auto_skip_timeout=s.auto_skip_timeout,
            special_instructions=s.special_instructions,
            scoring=s.scoring,
            manual_interview=s.manual_interview,
        ))

    for q in data.questions:
        db.add(JobQuestion(
            job_id=job.id,
            text=q.text,
            category=q.category,
            difficulty=q.difficulty,
        ))


def get_jobs_list(db: Session) -> List[Job]:
    return db.query(Job).order_by(Job.created_at.desc()).all()


def get_job_by_id(db: Session, job_id: int) -> Optional[Job]:
    return db.query(Job).filter(Job.id == job_id).first()


def create_job(db: Session, data: JobCreate) -> Job:
    job = Job(
        title=data.title,
        department=data.department,
        location=data.location,
        job_type=data.job_type,
        experience_level=data.experience_level,
        experience_min=data.experience_min,
        experience_max=data.experience_max,
        openings=data.openings,
        emp_type=data.emp_type,
        status=data.status,
        deadline=data.deadline,
        manager_name=data.manager_name,
        manager_phone=data.manager_phone,
        manager_email=data.manager_email,
        evaluation_scoring=data.evaluation_scoring,
        mix_of_questions=data.mix_of_questions,
        allow_to_contact=data.allow_to_contact,
        upload_jd=data.upload_jd,
    )
    db.add(job)
    db.flush()
    _insert_children(db, job, data)
    db.commit()
    db.refresh(job)
    return job


def update_job(db: Session, job_id: int, data: JobUpdate) -> Optional[Job]:
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        return None

    job.title = data.title
    job.department = data.department
    job.location = data.location
    job.job_type = data.job_type
    job.experience_level = data.experience_level
    job.experience_min = data.experience_min
    job.experience_max = data.experience_max
    job.openings = data.openings
    job.emp_type = data.emp_type
    job.status = data.status
    job.deadline = data.deadline
    job.manager_name = data.manager_name
    job.manager_phone = data.manager_phone
    job.manager_email = data.manager_email
    job.evaluation_scoring = data.evaluation_scoring
    job.mix_of_questions = data.mix_of_questions
    job.allow_to_contact = data.allow_to_contact
    job.upload_jd = data.upload_jd

    db.query(JobResponsibility).filter_by(job_id=job_id).delete()
    db.query(JobSkill).filter_by(job_id=job_id).delete()
    db.query(InterviewSettings).filter_by(job_id=job_id).delete()
    db.query(JobQuestion).filter_by(job_id=job_id).delete()
    db.flush()

    _insert_children(db, job, data)
    db.commit()
    db.refresh(job)
    return job


def _safe_delete(db: Session, sql: str, params: dict, logger):
    """Execute a DELETE inside a SAVEPOINT so failures don't poison the transaction."""
    try:
        with db.begin_nested():
            db.execute(text(sql), params)
    except Exception as e:
        logger.warning("Skipped: %s — %s", sql[:60], e)


def delete_job(db: Session, job_id: int) -> bool:
    from app.models.interview import (
        InterviewToken, InterviewSession, InterviewMessage,
        Violation, Evaluation, CandidateFeedback,
        MeetingSlot, MeetingSchedule,
    )
    from app.models.job_metadata import JobMetadata
    from sqlalchemy import text
    import logging

    logger = logging.getLogger("delete_job")

    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        return False

    try:
        # 1. Collect session IDs for this job
        session_ids = [
            s.id for s in
            db.query(InterviewSession.id).filter(InterviewSession.job_id == job_id).all()
        ]

        if session_ids:
            # 2. Delete deepest children first (session-linked)
            db.query(InterviewMessage).filter(InterviewMessage.session_id.in_(session_ids)).delete(synchronize_session="fetch")
            db.query(Violation).filter(Violation.session_id.in_(session_ids)).delete(synchronize_session="fetch")
            db.query(Evaluation).filter(Evaluation.session_id.in_(session_ids)).delete(synchronize_session="fetch")
            db.query(CandidateFeedback).filter(CandidateFeedback.session_id.in_(session_ids)).delete(synchronize_session="fetch")
            db.query(MeetingSchedule).filter(MeetingSchedule.session_id.in_(session_ids)).delete(synchronize_session="fetch")

            # Delete per_question_evaluations (table may not exist — use SAVEPOINT)
            placeholders = ",".join(f":sid{i}" for i in range(len(session_ids)))
            params = {f"sid{i}": sid for i, sid in enumerate(session_ids)}
            _safe_delete(db, f"DELETE FROM per_question_evaluations WHERE session_id IN ({placeholders})", params, logger)

            # 3. Delete sessions themselves
            db.query(InterviewSession).filter(InterviewSession.job_id == job_id).delete(synchronize_session="fetch")

        # 4. Delete tokens
        db.query(InterviewToken).filter(InterviewToken.job_id == job_id).delete(synchronize_session="fetch")

        # 5. Meeting slots (not session-linked, but job-linked)
        slot_ids = [s.id for s in db.query(MeetingSlot.id).filter(MeetingSlot.job_id == job_id).all()]
        if slot_ids:
            db.query(MeetingSchedule).filter(MeetingSchedule.slot_id.in_(slot_ids)).delete(synchronize_session="fetch")
        db.query(MeetingSlot).filter(MeetingSlot.job_id == job_id).delete(synchronize_session="fetch")

        # 6. Job children
        db.query(JobSkill).filter(JobSkill.job_id == job_id).delete(synchronize_session="fetch")
        db.query(JobResponsibility).filter(JobResponsibility.job_id == job_id).delete(synchronize_session="fetch")
        db.query(JobQuestion).filter(JobQuestion.job_id == job_id).delete(synchronize_session="fetch")
        db.query(InterviewSettings).filter(InterviewSettings.job_id == job_id).delete(synchronize_session="fetch")

        # 7. Job metadata (table may not exist — use SAVEPOINT)
        try:
            with db.begin_nested():
                db.query(JobMetadata).filter(JobMetadata.job_id == job_id).delete(synchronize_session="fetch")
        except Exception as e:
            logger.warning("Skipped job_metadata delete: %s", e)

        # 8. Tables without ORM models (may not exist — use SAVEPOINT for each)
        for tbl in ("job_pre_interview_fields", "scoring_training_data", "ai_credit_usage"):
            _safe_delete(db, f"DELETE FROM {tbl} WHERE job_id = :jid", {"jid": job_id}, logger)

        # 9. Nullify candidate FK
        db.query(Candidate).filter(Candidate.job_id == job_id).update(
            {Candidate.job_id: None}, synchronize_session="fetch"
        )

        # 10. Delete the job
        db.delete(job)
        db.commit()
        return True

    except Exception as e:
        db.rollback()
        logger.error("Failed to delete job %d: %s", job_id, e, exc_info=True)
        raise


def _pct_change(current: int, previous: int) -> float:
    """Percentage change, rounded to 1 decimal."""
    if previous == 0:
        return 0.0
    return round(((current - previous) / previous) * 100, 1)


def get_stats(db: Session) -> List[Dict[str, Any]]:
    from app.models.job import JobStatus
    from app.models.candidate import CandidateStatus

    now = datetime.utcnow()

    # ── Current totals ───────────────────────────────────────────────────
    active_jobs = db.query(func.count(Job.id)).filter(Job.status == JobStatus.open).scalar() or 0
    finished_jobs = db.query(func.count(Job.id)).filter(Job.status == JobStatus.inactive).scalar() or 0
    interview_count = db.query(func.count(Candidate.id)).filter(
        Candidate.status == CandidateStatus.interview, Candidate.job_id.isnot(None)
    ).scalar() or 0
    offered_count = db.query(func.count(Candidate.id)).filter(
        Candidate.status == CandidateStatus.offered, Candidate.job_id.isnot(None)
    ).scalar() or 0
    waiting_count = db.query(func.count(Candidate.id)).filter(
        Candidate.status == CandidateStatus.applied, Candidate.job_id.isnot(None)
    ).scalar() or 0

    # ── Week boundaries ──────────────────────────────────────────────────
    this_monday = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    last_monday = this_monday - timedelta(days=7)
    last_friday_end = last_monday + timedelta(days=5)
    day_names = ["Mon", "Tue", "Wed", "Thu", "Fri"]

    # ── Helper: daily chart data (Mon-Fri of current week) ───────────────
    def _daily_chart_job(status):
        """Count jobs created per day this week with a given status."""
        day_col = cast(Job.created_at, Date).label("day")
        rows = (
            db.query(day_col, func.count(Job.id).label("cnt"))
            .filter(
                Job.status == status,
                Job.created_at >= this_monday,
                Job.created_at < this_monday + timedelta(days=5),
            )
            .group_by(day_col)
            .all()
        )
        by_date = {r.day: int(r.cnt) for r in rows}
        chart = []
        for i in range(5):
            d = (this_monday + timedelta(days=i)).date()
            chart.append({"name": day_names[i], "value": by_date.get(d, 0)})
        return chart

    def _daily_chart_candidate(status):
        """Count candidates updated to a given status per day this week."""
        day_col = cast(Candidate.updated_at, Date).label("day")
        rows = (
            db.query(day_col, func.count(Candidate.id).label("cnt"))
            .filter(
                Candidate.status == status,
                Candidate.job_id.isnot(None),
                Candidate.updated_at >= this_monday,
                Candidate.updated_at < this_monday + timedelta(days=5),
            )
            .group_by(day_col)
            .all()
        )
        by_date = {r.day: int(r.cnt) for r in rows}
        chart = []
        for i in range(5):
            d = (this_monday + timedelta(days=i)).date()
            chart.append({"name": day_names[i], "value": by_date.get(d, 0)})
        return chart

    # ── Previous week totals (for trend %) ───────────────────────────────
    prev_active = db.query(func.count(Job.id)).filter(
        Job.status == JobStatus.open,
        Job.created_at >= last_monday, Job.created_at < last_friday_end,
    ).scalar() or 0
    prev_finished = db.query(func.count(Job.id)).filter(
        Job.status == JobStatus.inactive,
        Job.updated_at >= last_monday, Job.updated_at < last_friday_end,
    ).scalar() or 0
    prev_interview = db.query(func.count(Candidate.id)).filter(
        Candidate.status == CandidateStatus.interview, Candidate.job_id.isnot(None),
        Candidate.updated_at >= last_monday, Candidate.updated_at < last_friday_end,
    ).scalar() or 0
    prev_offered = db.query(func.count(Candidate.id)).filter(
        Candidate.status == CandidateStatus.offered, Candidate.job_id.isnot(None),
        Candidate.updated_at >= last_monday, Candidate.updated_at < last_friday_end,
    ).scalar() or 0
    prev_waiting = db.query(func.count(Candidate.id)).filter(
        Candidate.status == CandidateStatus.applied, Candidate.job_id.isnot(None),
        Candidate.updated_at >= last_monday, Candidate.updated_at < last_friday_end,
    ).scalar() or 0

    # ── Current week totals (for trend comparison) ───────────────────────
    curr_active = db.query(func.count(Job.id)).filter(
        Job.status == JobStatus.open,
        Job.created_at >= this_monday, Job.created_at < this_monday + timedelta(days=5),
    ).scalar() or 0
    curr_finished = db.query(func.count(Job.id)).filter(
        Job.status == JobStatus.inactive,
        Job.updated_at >= this_monday, Job.updated_at < this_monday + timedelta(days=5),
    ).scalar() or 0
    curr_interview = db.query(func.count(Candidate.id)).filter(
        Candidate.status == CandidateStatus.interview, Candidate.job_id.isnot(None),
        Candidate.updated_at >= this_monday, Candidate.updated_at < this_monday + timedelta(days=5),
    ).scalar() or 0
    curr_offered = db.query(func.count(Candidate.id)).filter(
        Candidate.status == CandidateStatus.offered, Candidate.job_id.isnot(None),
        Candidate.updated_at >= this_monday, Candidate.updated_at < this_monday + timedelta(days=5),
    ).scalar() or 0
    curr_waiting = db.query(func.count(Candidate.id)).filter(
        Candidate.status == CandidateStatus.applied, Candidate.job_id.isnot(None),
        Candidate.updated_at >= this_monday, Candidate.updated_at < this_monday + timedelta(days=5),
    ).scalar() or 0

    return [
        {
            "label": "Active Jobs",
            "value": active_jobs,
            "change": _pct_change(curr_active, prev_active),
            "color": "brand",
            "chartData": _daily_chart_job(JobStatus.open),
        },
        {
            "label": "Finished Jobs",
            "value": finished_jobs,
            "change": _pct_change(curr_finished, prev_finished),
            "color": "success",
            "chartData": _daily_chart_job(JobStatus.inactive),
        },
        {
            "label": "Interview Schedule",
            "value": interview_count,
            "change": _pct_change(curr_interview, prev_interview),
            "color": "success",
            "chartData": _daily_chart_candidate(CandidateStatus.interview),
        },
        {
            "label": "Hire Rates",
            "value": offered_count,
            "change": _pct_change(curr_offered, prev_offered),
            "color": "brand",
            "chartData": _daily_chart_candidate(CandidateStatus.offered),
        },
        {
            "label": "Waiting Response",
            "value": waiting_count,
            "change": _pct_change(curr_waiting, prev_waiting),
            "color": "warning",
            "chartData": _daily_chart_candidate(CandidateStatus.applied),
        },
    ]
