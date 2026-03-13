from typing import List, Optional

from sqlalchemy.orm import Session

from app.models.candidate import Candidate
from app.models.job import Job
from app.schemas.candidate import CandidateCreate, CandidateUpdate


def get_candidates(
    db: Session,
    status: Optional[str] = None,
    job_id: Optional[int] = None,
    stage: Optional[str] = None,
) -> list:
    query = db.query(Candidate, Job.title).outerjoin(Job, Candidate.job_id == Job.id)

    # Filter out placeholder emails that haven't been parsed yet
    query = query.filter(
        ~Candidate.email.like("pending_%@resume.local")
    )

    if status:
        query = query.filter(Candidate.status == status)
    if job_id:
        query = query.filter(Candidate.job_id == job_id)
    if stage:
        query = query.filter(Candidate.candidate_stage == stage)

    rows = query.order_by(Candidate.applied_date.desc()).all()

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
        return _STATUS_PRIORITY.get(s, 0)

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

    # When status changes, auto-sync candidate_stage and interview_status
    new_status = updates.get("status")
    if new_status:
        if "candidate_stage" not in updates and new_status in _STATUS_TO_STAGE:
            updates["candidate_stage"] = _STATUS_TO_STAGE[new_status]
        if "interview_status" not in updates and new_status in _STATUS_TO_INTERVIEW_STATUS:
            updates["interview_status"] = _STATUS_TO_INTERVIEW_STATUS[new_status]

    for field, value in updates.items():
        setattr(candidate, field, value)
    db.commit()
    db.refresh(candidate)
    return candidate


def delete_candidate(db: Session, candidate_id: int) -> bool:
    candidate = db.query(Candidate).filter(Candidate.id == candidate_id).first()
    if not candidate:
        return False
    db.delete(candidate)
    db.commit()
    return True
