"""
Object-level authorization for tenant data.

Every admin endpoint that accepts an object id — in the path, the query string
or the request body — must resolve it through one of these helpers before
reading or writing the object. They FAIL CLOSED: an object with no owning
organization (a job with organization_id NULL, a candidate with no job) is
visible only to a global super admin (app.auth.is_global_admin), and a caller
with no organization is refused by app.auth.get_org_id.

All helpers answer 404 rather than 403 for objects the caller may not see, so
sequential ids can't be used to probe which ids exist in other tenants.
"""
from typing import Optional

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.auth import get_org_id
from app.models.user import User


def _not_found(what: str) -> HTTPException:
    return HTTPException(status_code=404, detail=f"{what} not found")


def can_access_org(user: User, organization_id: Optional[int]) -> bool:
    """True when `user` may see an object owned by `organization_id`."""
    org_id = get_org_id(user)
    if org_id is None:  # global super admin
        return True
    return organization_id is not None and organization_id == org_id


def authorize_job(db: Session, job_id: Optional[int], user: User):
    """Load a job the caller may access, or raise 404."""
    from app.models.job import Job

    job = db.query(Job).filter(Job.id == job_id).first() if job_id is not None else None
    if job is None or not can_access_org(user, job.organization_id):
        raise _not_found("Job")
    return job


def job_org_id(db: Session, job_id: Optional[int]) -> Optional[int]:
    from app.models.job import Job

    if job_id is None:
        return None
    return db.query(Job.organization_id).filter(Job.id == job_id).scalar()


def authorize_candidate(db: Session, candidate_id: Optional[int], user: User):
    """Load a candidate the caller may access, or raise 404.

    A candidate's tenant is its job's organization. Candidates without a job
    (or whose job has no organization) belong to no tenant, so only a global
    super admin may reach them — the old helper skipped the check entirely for
    job-less rows, exposing them to every logged-in user."""
    from app.models.candidate import Candidate

    candidate = (
        db.query(Candidate).filter(Candidate.id == candidate_id).first()
        if candidate_id is not None
        else None
    )
    if candidate is None:
        raise _not_found("Candidate")
    if get_org_id(user) is None:
        return candidate
    if not can_access_org(user, job_org_id(db, candidate.job_id)):
        raise _not_found("Candidate")
    return candidate


def authorize_session(db: Session, session_id: Optional[int], user: User):
    """Load an interview session (admin view) the caller may access, or raise 404."""
    from app.models.interview import InterviewSession

    session = (
        db.query(InterviewSession).filter(InterviewSession.id == session_id).first()
        if session_id is not None
        else None
    )
    if session is None:
        raise _not_found("Session")
    if get_org_id(user) is None:
        return session
    if not can_access_org(user, job_org_id(db, session.job_id)):
        raise _not_found("Session")
    return session
