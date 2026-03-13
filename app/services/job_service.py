from typing import Any, Dict, List, Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.candidate import Candidate
from app.models.job import (
    InterviewSettings,
    Job,
    JobPreInterviewField,
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

    for f in data.pre_interview_fields:
        db.add(JobPreInterviewField(
            job_id=job.id,
            field_id=f.id,
            label=f.label,
            type=f.type,
            is_default=f.is_default,
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
    db.query(JobPreInterviewField).filter_by(job_id=job_id).delete()
    db.flush()

    _insert_children(db, job, data)
    db.commit()
    db.refresh(job)
    return job


def delete_job(db: Session, job_id: int) -> bool:
    from app.models.interview import InterviewToken, InterviewSession, MeetingSlot, MeetingSchedule
    from app.models.job_metadata import JobMetadata

    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        return False

    # Delete related records in correct order (child → parent)

    # Interview sessions (via tokens)
    token_ids = [t.id for t in db.query(InterviewToken.id).filter(InterviewToken.job_id == job_id).all()]
    if token_ids:
        db.query(InterviewSession).filter(InterviewSession.token_id.in_(token_ids)).delete(synchronize_session="fetch")
        db.query(InterviewToken).filter(InterviewToken.job_id == job_id).delete(synchronize_session="fetch")

    # Meeting slots and schedules
    db.query(MeetingSchedule).filter(MeetingSchedule.job_id == job_id).delete(synchronize_session="fetch")
    db.query(MeetingSlot).filter(MeetingSlot.job_id == job_id).delete(synchronize_session="fetch")

    # Job children
    db.query(JobSkill).filter(JobSkill.job_id == job_id).delete(synchronize_session="fetch")
    db.query(JobResponsibility).filter(JobResponsibility.job_id == job_id).delete(synchronize_session="fetch")
    db.query(JobQuestion).filter(JobQuestion.job_id == job_id).delete(synchronize_session="fetch")
    db.query(JobPreInterviewField).filter(JobPreInterviewField.job_id == job_id).delete(synchronize_session="fetch")
    db.query(InterviewSettings).filter(InterviewSettings.job_id == job_id).delete(synchronize_session="fetch")

    # Job metadata
    db.query(JobMetadata).filter(JobMetadata.job_id == job_id).delete(synchronize_session="fetch")

    # Nullify candidate FK
    db.query(Candidate).filter(Candidate.job_id == job_id).update(
        {Candidate.job_id: None}, synchronize_session="fetch"
    )

    db.delete(job)
    db.commit()
    return True


def get_stats(db: Session) -> List[Dict[str, Any]]:
    from app.models.job import JobStatus
    from app.models.candidate import CandidateStatus

    active_jobs = db.query(func.count(Job.id)).filter(Job.status == JobStatus.open).scalar() or 0
    finished_jobs = db.query(func.count(Job.id)).filter(Job.status == JobStatus.inactive).scalar() or 0
    interview_count = db.query(func.count(Candidate.id)).filter(Candidate.status == CandidateStatus.interview).scalar() or 0
    offered_count = db.query(func.count(Candidate.id)).filter(Candidate.status == CandidateStatus.offered).scalar() or 0
    waiting_count = db.query(func.count(Candidate.id)).filter(Candidate.status == CandidateStatus.applied).scalar() or 0

    return [
        {
            "label": "Active Jobs",
            "value": active_jobs,
            "change": 0.0,
            "color": "brand",
            "chartData": [
                {"name": "Mon", "value": max(1, active_jobs // 5)},
                {"name": "Tue", "value": max(1, active_jobs // 4)},
                {"name": "Wed", "value": max(1, active_jobs // 3)},
                {"name": "Thu", "value": max(1, active_jobs // 2)},
                {"name": "Fri", "value": active_jobs},
            ],
        },
        {
            "label": "Finished Jobs",
            "value": finished_jobs,
            "change": 0.0,
            "color": "success",
            "chartData": [
                {"name": "Mon", "value": max(1, finished_jobs // 5)},
                {"name": "Tue", "value": max(1, finished_jobs // 4)},
                {"name": "Wed", "value": max(1, finished_jobs // 3)},
                {"name": "Thu", "value": max(1, finished_jobs // 2)},
                {"name": "Fri", "value": finished_jobs},
            ],
        },
        {
            "label": "Interview Schedule",
            "value": interview_count,
            "change": 0.0,
            "color": "success",
            "chartData": [
                {"name": "Mon", "value": max(1, interview_count // 5)},
                {"name": "Tue", "value": max(1, interview_count // 4)},
                {"name": "Wed", "value": max(1, interview_count // 3)},
                {"name": "Thu", "value": max(1, interview_count // 2)},
                {"name": "Fri", "value": interview_count},
            ],
        },
        {
            "label": "Hire Rates",
            "value": offered_count,
            "change": 0.0,
            "color": "brand",
            "chartData": [
                {"name": "Mon", "value": max(1, offered_count // 5)},
                {"name": "Tue", "value": max(1, offered_count // 4)},
                {"name": "Wed", "value": max(1, offered_count // 3)},
                {"name": "Thu", "value": max(1, offered_count // 2)},
                {"name": "Fri", "value": offered_count},
            ],
        },
        {
            "label": "Waiting Response",
            "value": waiting_count,
            "change": 0.0,
            "color": "warning",
            "chartData": [
                {"name": "Mon", "value": max(1, waiting_count // 5)},
                {"name": "Tue", "value": max(1, waiting_count // 4)},
                {"name": "Wed", "value": max(1, waiting_count // 3)},
                {"name": "Thu", "value": max(1, waiting_count // 2)},
                {"name": "Fri", "value": waiting_count},
            ],
        },
    ]
