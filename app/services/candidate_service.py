import logging
from typing import List, Optional

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.candidate import Candidate, CandidateStatus, EmailLog
from app.models.job import Job
from app.schemas.candidate import CandidateCreate, CandidateUpdate

logger = logging.getLogger(__name__)


class DuplicateCandidateEmailError(ValueError):
    """Raised when an update would duplicate a candidate email in the same job."""


def _reconcile_stale_completed_candidates(
    db: Session, candidates: list[Candidate]
) -> None:
    """One-shot self-healing pass.

    A candidate may have actually sat (and finished) the interview but still
    carry interview_status="Scheduled" and candidate_stage="Candidates" —
    because an earlier Reschedule overwrote those fields, or because the
    session was completed by the conversation engine on a path that never
    touched the candidate row. Repair them here so the AI Round tab and
    Interview Completed counters reflect reality on the next load.

    Detection rule: interview_status is still a transitional value AND the
    candidate has a terminal (completed/terminated) interview session.

    NOTE: this deliberately does NOT require a non-null score. Gating on the
    score meant a candidate whose AI evaluation FAILED (stub row, score null)
    was skipped forever and stayed stuck on "Scheduled" — the exact case this
    self-heal exists for. Interview completion and evaluation success are
    independent facts; the status reflects the former only.
    """
    from app.models.interview import InterviewSession, SessionStatus

    stale_statuses = {"Scheduled", "New", "Disconnected – Awaiting Rejoin"}
    candidate_ids = [
        c.id for c in candidates if c.interview_status in stale_statuses
    ]
    if not candidate_ids:
        return

    completed_session_ids = {
        cid for (cid,) in db.query(InterviewSession.candidate_id).filter(
            InterviewSession.candidate_id.in_(candidate_ids),
            InterviewSession.status.in_(
                [SessionStatus.completed, SessionStatus.terminated]
            ),
        ).distinct().all()
    }
    if not completed_session_ids:
        return

    dirty = False
    for c in candidates:
        if c.id in completed_session_ids and c.interview_status in stale_statuses:
            logger.info(
                "[reconcile] candidate=%s has a terminal session but "
                "interview_status=%s (score=%s) — restoring Completed/AI Round",
                c.id, c.interview_status, c.score,
            )
            c.interview_status = "Completed"
            if c.candidate_stage in (None, "Candidates"):
                c.candidate_stage = "AI Round"
            dirty = True
    if dirty:
        try:
            db.commit()
        except Exception as exc:
            logger.warning("[reconcile] commit failed: %s", exc)
            db.rollback()


def get_candidates(
    db: Session,
    status: Optional[str] = None,
    job_id: Optional[int] = None,
    stage: Optional[str] = None,
    org_id: Optional[int] = None,
) -> list:
    query = db.query(Candidate, Job.title).outerjoin(Job, Candidate.job_id == Job.id)

    # Filter out placeholder emails that haven't been parsed yet, and
    # permanently failed entries (kept in DB for upload-response reporting only).
    # Use IS NULL OR != to avoid excluding legacy rows where parsing_status is NULL.
    from sqlalchemy import or_
    query = query.filter(
        ~Candidate.email.like("pending_%@resume.local"),
        or_(Candidate.parsing_status.is_(None), Candidate.parsing_status != "failed"),
    )

    # Organization scoping
    if org_id is not None:
        query = query.filter(Job.organization_id == org_id)

    if status:
        # Match enum by value (e.g. "Selected" → CandidateStatus.selected)
        try:
            status_enum = CandidateStatus(status)
        except ValueError:
            status_enum = status
        query = query.filter(Candidate.status == status_enum)
    if job_id:
        query = query.filter(Candidate.job_id == job_id)
    if stage:
        query = query.filter(Candidate.candidate_stage == stage)

    rows = query.order_by(Candidate.applied_date.desc()).all()

    # Self-heal any rows where an earlier Reschedule overwrote a completed
    # candidate's status/stage back to "Scheduled" / "Candidates". This is
    # cheap when nothing is stale (one indexed query that returns 0 rows).
    _reconcile_stale_completed_candidates(db, [c for c, _ in rows])

    # Status priority for dedup — higher = keep this one
    _STATUS_PRIORITY = {
        "Hired": 6, "Selected": 5, "Offered": 4,
        "Interview": 3, "Applied": 2, "Rejected": 1,
    }

    def _dedup_key(c_dict: dict) -> str | None:
        """Return a dedup key: email, or name+phone fallback."""
        email = (c_dict.get("email") or "").strip().lower()
        if email and not email.endswith("@resume.local"):
            return f"email:{email}"
        return None

    def _name_phone_key(c_dict: dict) -> str | None:
        name = (c_dict.get("name") or "").strip().lower()
        phone = (c_dict.get("phone") or "").strip().replace(" ", "").replace("-", "")
        if name and phone and len(phone) >= 7:
            return f"np:{name}:{phone[-7:]}"
        return None

    def _priority(c_dict: dict) -> int:
        s = c_dict.get("status") or ""
        # s may be a CandidateStatus enum member; use .value for lookup
        key = s.value if isinstance(s, CandidateStatus) else s
        return _STATUS_PRIORITY.get(key, 0)

    # When scoped to a specific job, skip dedup — each row is a distinct candidate record
    # for that job. Dedup only applies when listing across all jobs (global candidates view).
    if job_id:
        results = []
        for candidate, job_title in rows:
            c_dict = candidate.__dict__.copy()
            c_dict.pop("_sa_instance_state", None)
            c_dict["job_title"] = job_title
            results.append(c_dict)
        return results

    seen_email: dict[str, dict] = {}
    seen_name_phone: dict[str, dict] = {}
    results: list[dict] = []

    for candidate, job_title in rows:
        c_dict = candidate.__dict__.copy()
        c_dict.pop("_sa_instance_state", None)
        c_dict["job_title"] = job_title

        dedup_k = _dedup_key(c_dict)
        np_k = _name_phone_key(c_dict)

        # Check if we already have this candidate by either key
        existing = None
        if dedup_k and dedup_k in seen_email:
            existing = seen_email[dedup_k]
        elif np_k and np_k in seen_name_phone:
            existing = seen_name_phone[np_k]

        if existing is None:
            results.append(c_dict)
            if dedup_k:
                seen_email[dedup_k] = c_dict
            if np_k:
                seen_name_phone[np_k] = c_dict
        else:
            # Keep the one with higher priority (more progressed status)
            if _priority(c_dict) > _priority(existing):
                idx = results.index(existing)
                results[idx] = c_dict
                if dedup_k:
                    seen_email[dedup_k] = c_dict
                if np_k:
                    seen_name_phone[np_k] = c_dict

    return results


def get_candidate_by_id(db: Session, candidate_id: int) -> Optional[Candidate]:
    return db.query(Candidate).filter(Candidate.id == candidate_id).first()


def create_candidate(db: Session, data: CandidateCreate) -> Candidate:
    # Dedup check: if same email + job_id exists, update instead of creating
    if data.email and data.job_id:
        existing = db.query(Candidate).filter(
            Candidate.email == data.email,
            Candidate.job_id == data.job_id,
        ).first()
        if existing:
            for field, value in data.model_dump(exclude_none=True).items():
                setattr(existing, field, value)
            db.commit()
            db.refresh(existing)
            return existing

    candidate = Candidate(
        name=data.name,
        role=data.role,
        email=data.email,
        phone=data.phone,
        location=data.location,
        status=data.status,
        image=data.image,
        job_id=data.job_id,
        resume_path=data.resume_path,
        interview_status="New",
        candidate_stage="Candidates",
    )
    db.add(candidate)
    db.commit()
    db.refresh(candidate)
    return candidate


_STATUS_TO_STAGE = {
    "Applied": "Candidates",
    "Interview": "AI Round",
    "Offered": "Offer-Made",
    "Selected": "Offer-Made",
    "Hired": "Offer-Made",
    "Rejected": "Rejected",
}

_STATUS_TO_INTERVIEW_STATUS = {
    "Applied": "New",
    "Interview": "Scheduled",
    "Offered": "Completed",
    "Selected": "Completed",
    "Hired": "Completed",
    "Rejected": "Completed",
}


def update_candidate(
    db: Session, candidate_id: int, data: CandidateUpdate
) -> Optional[Candidate]:
    candidate = db.query(Candidate).filter(Candidate.id == candidate_id).first()
    if not candidate:
        return None

    updates = data.model_dump(exclude_none=True)

    for field in ("name", "role", "email", "phone", "location", "image", "resume_path"):
        if field in updates and isinstance(updates[field], str):
            updates[field] = updates[field].strip()

    if "email" in updates:
        updates["email"] = updates["email"].lower()

    target_email = (updates.get("email") or candidate.email or "").strip().lower()
    target_job_id = updates.get("job_id", candidate.job_id)
    if target_email and target_job_id is not None:
        duplicate = (
            db.query(Candidate)
            .filter(
                Candidate.id != candidate_id,
                Candidate.job_id == target_job_id,
                func.lower(Candidate.email) == target_email,
            )
            .first()
        )
        if duplicate:
            raise DuplicateCandidateEmailError(
                "A candidate with this email already exists for this job."
            )

    # When status changes, auto-sync candidate_stage and interview_status
    new_status = updates.get("status")
    if new_status:
        if "candidate_stage" not in updates and new_status in _STATUS_TO_STAGE:
            updates["candidate_stage"] = _STATUS_TO_STAGE[new_status]
        if "interview_status" not in updates and new_status in _STATUS_TO_INTERVIEW_STATUS:
            updates["interview_status"] = _STATUS_TO_INTERVIEW_STATUS[new_status]

    for field, value in updates.items():
        setattr(candidate, field, value)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        message = str(getattr(exc, "orig", exc))
        if (
            "uq_candidate_email_job" in message
            or "candidates.email, candidates.job_id" in message
            or "candidates_email_job" in message
        ):
            raise DuplicateCandidateEmailError(
                "A candidate with this email already exists for this job."
            ) from exc
        raise
    db.refresh(candidate)
    return candidate


def _cleanup_candidate_files(db: Session, candidate: Candidate) -> None:
    """Delete all files associated with a candidate: recordings, screenshots, resume."""
    import os
    import shutil
    import logging

    from app.models.interview import InterviewSession, InterviewScreenshot

    logger = logging.getLogger("candidate_cleanup")
    _is_serverless = bool(os.environ.get("VERCEL") or os.environ.get("AWS_LAMBDA_FUNCTION_NAME"))
    _upload_root = "/tmp/uploads" if _is_serverless else "uploads"

    # 1. Delete recording file
    if candidate.recording_link:
        # recording_link is like "/uploads/recordings/123.webm"
        rel = candidate.recording_link.lstrip("/")
        if _is_serverless:
            rel = rel.replace("uploads/", "tmp/uploads/", 1) if rel.startswith("uploads/") else rel
        file_path = os.path.join(os.getcwd(), rel) if not _is_serverless else f"/{rel}"
        try:
            if os.path.isfile(file_path):
                os.remove(file_path)
                logger.info("Deleted recording: %s", file_path)
        except Exception as e:
            logger.warning("Failed to delete recording %s: %s", file_path, e)

    # 2. Delete screenshot directories for all sessions of this candidate
    session_ids = [
        s.id for s in
        db.query(InterviewSession.id).filter(InterviewSession.candidate_id == candidate.id).all()
    ]
    for sid in session_ids:
        screenshot_dir = f"{_upload_root}/screenshots/{sid}"
        try:
            if os.path.isdir(screenshot_dir):
                shutil.rmtree(screenshot_dir)
                logger.info("Deleted screenshot dir: %s", screenshot_dir)
        except Exception as e:
            logger.warning("Failed to delete screenshot dir %s: %s", screenshot_dir, e)

    # 3. Delete resume file
    if candidate.resume_path:
        rel = candidate.resume_path.lstrip("/")
        if _is_serverless:
            rel = rel.replace("uploads/", "tmp/uploads/", 1) if rel.startswith("uploads/") else rel
        file_path = os.path.join(os.getcwd(), rel) if not _is_serverless else f"/{rel}"
        try:
            if os.path.isfile(file_path):
                os.remove(file_path)
                logger.info("Deleted resume: %s", file_path)
        except Exception as e:
            logger.warning("Failed to delete resume %s: %s", file_path, e)


def delete_candidate(db: Session, candidate_id: int) -> bool:
    candidate = db.query(Candidate).filter(Candidate.id == candidate_id).first()
    if not candidate:
        return False

    # Clean up associated files before deleting the DB record
    _cleanup_candidate_files(db, candidate)

    # Clear email logs explicitly. Their FK gained ON DELETE CASCADE only in
    # migration j4k5l6m7n8o9; on a database that has not reached it yet the
    # constraint is still NO ACTION and the DELETE below would fail with a
    # ForeignKeyViolation for any candidate who was ever emailed. Every other
    # child row is removed by the database's own cascade.
    db.query(EmailLog).filter(EmailLog.candidate_id == candidate_id).delete(
        synchronize_session=False
    )

    db.delete(candidate)
    db.commit()
    return True
