from typing import List, Optional

from sqlalchemy.orm import Session

from app.models.candidate import Candidate
from app.schemas.candidate import CandidateCreate, CandidateUpdate


def get_candidates(
    db: Session,
    status: Optional[str] = None,
    job_id: Optional[int] = None,
    stage: Optional[str] = None,
) -> List[Candidate]:
    query = db.query(Candidate)
    if status:
        query = query.filter(Candidate.status == status)
    if job_id:
        query = query.filter(Candidate.job_id == job_id)
    if stage:
        query = query.filter(Candidate.candidate_stage == stage)
    return query.order_by(Candidate.applied_date.desc()).all()


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


def update_candidate(
    db: Session, candidate_id: int, data: CandidateUpdate
) -> Optional[Candidate]:
    candidate = db.query(Candidate).filter(Candidate.id == candidate_id).first()
    if not candidate:
        return None
    for field, value in data.model_dump(exclude_none=True).items():
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
