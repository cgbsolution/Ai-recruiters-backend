"""
Organization management endpoints — Super Admin only.
CRUD operations, user counts, and per-org statistics.
"""
import logging
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth import get_current_user, require_roles
from app.database import get_db
from app.models.organization import Organization
from app.models.user import User, UserRole
from app.models.job import Job
from app.models.candidate import Candidate
from app.models.ai_credit_usage import AiCreditUsage
from app.models.question import Question
from app.models.resume_metadata import ResumeMetadata
from app.refs import OrgId, encode_ref

logger = logging.getLogger(__name__)

router = APIRouter(
    dependencies=[Depends(require_roles(UserRole.super_admin))],
)


# ── Schemas ──────────────────────────────────────────────────────────────────

class OrgCreateRequest(BaseModel):
    name: str
    domain: str
    max_jobs: int = 0
    max_candidates_per_job: int = 0
    allow_resume_database: bool = False
    allow_code_editor: bool = False
    allow_whatsapp: bool = False
    allow_fit_score: bool = False


class OrgUpdateRequest(BaseModel):
    name: str | None = None
    domain: str | None = None
    is_active: bool | None = None
    max_jobs: int | None = None
    max_candidates_per_job: int | None = None
    allow_resume_database: bool | None = None
    allow_code_editor: bool | None = None
    allow_whatsapp: bool | None = None
    allow_fit_score: bool | None = None


class OrgOut(BaseModel):
    id: int
    name: str
    domain: str
    is_active: bool
    created_at: datetime | None = None
    updated_at: datetime | None = None
    user_count: int = 0
    job_count: int = 0

    class Config:
        from_attributes = True


# ── List Organizations ───────────────────────────────────────────────────────

@router.get("/")
def list_organizations(
    db: Session = Depends(get_db),
    search: Optional[str] = Query(None, description="Search by name or domain"),
    is_active: Optional[bool] = Query(None, description="Filter by active status"),
):
    """List all organizations with user and job counts."""
    q = db.query(Organization)

    if search:
        q = q.filter(
            (Organization.name.ilike(f"%{search}%"))
            | (Organization.domain.ilike(f"%{search}%"))
        )
    if is_active is not None:
        q = q.filter(Organization.is_active == is_active)

    orgs = q.order_by(Organization.created_at.desc()).all()

    # Batch-fetch counts
    org_ids = [o.id for o in orgs]

    user_counts = dict(
        db.query(User.organization_id, func.count(User.id))
        .filter(User.organization_id.in_(org_ids))
        .group_by(User.organization_id)
        .all()
    ) if org_ids else {}

    job_counts = dict(
        db.query(Job.organization_id, func.count(Job.id))
        .filter(Job.organization_id.in_(org_ids))
        .group_by(Job.organization_id)
        .all()
    ) if org_ids else {}

    return [
        {
            "id": o.id,
            "ref": encode_ref("org", o.id),
            "name": o.name,
            "domain": o.domain,
            "is_active": o.is_active,
            "max_jobs": o.max_jobs or 0,
            "max_candidates_per_job": o.max_candidates_per_job or 0,
            "total_jobs_used": o.total_jobs_used or 0,
            "total_candidates_used": o.total_candidates_used or 0,
            "allow_resume_database": o.allow_resume_database,
            "allow_code_editor": o.allow_code_editor,
            "allow_whatsapp": o.allow_whatsapp,
            "allow_fit_score": o.allow_fit_score,
            "created_at": o.created_at,
            "updated_at": o.updated_at,
            "user_count": user_counts.get(o.id, 0),
            "job_count": job_counts.get(o.id, 0),
        }
        for o in orgs
    ]


# ── Get Single Organization ─────────────────────────────────────────────────

@router.get("/{org_id}")
def get_organization(org_id: OrgId, db: Session = Depends(get_db)):
    """Get organization details with stats."""
    org = db.query(Organization).filter(Organization.id == org_id).first()
    if not org:
        raise HTTPException(status_code=404, detail="Organization not found")

    user_count = db.query(func.count(User.id)).filter(User.organization_id == org_id).scalar()
    job_count = db.query(func.count(Job.id)).filter(Job.organization_id == org_id).scalar()
    candidate_count = (
        db.query(func.count(Candidate.id))
        .join(Job, Candidate.job_id == Job.id)
        .filter(Job.organization_id == org_id)
        .filter(~Candidate.email.like("pending_%@resume.local"))
        .scalar()
    )

    # Users breakdown by role
    role_breakdown = dict(
        db.query(User.role, func.count(User.id))
        .filter(User.organization_id == org_id)
        .group_by(User.role)
        .all()
    )

    # Token/cost stats for this org
    token_stats = db.query(
        func.coalesce(func.sum(AiCreditUsage.input_tokens), 0).label("input_tokens"),
        func.coalesce(func.sum(AiCreditUsage.output_tokens), 0).label("output_tokens"),
        func.coalesce(func.sum(AiCreditUsage.total_tokens), 0).label("total_tokens"),
        func.coalesce(func.sum(AiCreditUsage.estimated_cost), 0).label("estimated_cost"),
        func.count(AiCreditUsage.id).label("call_count"),
    ).filter(AiCreditUsage.organization_id == org_id).one()

    # Jobs list
    jobs = db.query(Job).filter(Job.organization_id == org_id).order_by(Job.created_at.desc()).all()

    return {
        "id": org.id,
        "ref": encode_ref("org", org.id),
        "name": org.name,
        "domain": org.domain,
        "is_active": org.is_active,
        "max_jobs": org.max_jobs or 0,
        "max_candidates_per_job": org.max_candidates_per_job or 0,
        "total_jobs_used": org.total_jobs_used or 0,
        "total_candidates_used": org.total_candidates_used or 0,
        "allow_resume_database": org.allow_resume_database,
        "allow_code_editor": org.allow_code_editor,
        "allow_whatsapp": org.allow_whatsapp,
        "created_at": org.created_at,
        "updated_at": org.updated_at,
        "user_count": user_count,
        "job_count": job_count,
        "candidate_count": candidate_count,
        "role_breakdown": {k.value if hasattr(k, "value") else k: v for k, v in role_breakdown.items()},
        "token_stats": {
            "input_tokens": int(token_stats.input_tokens),
            "output_tokens": int(token_stats.output_tokens),
            "total_tokens": int(token_stats.total_tokens),
            "estimated_cost": round(float(token_stats.estimated_cost), 4),
            "call_count": int(token_stats.call_count),
        },
        "jobs": [
            {
                "id": j.id,
                "ref": encode_ref("job", j.id),
                "title": j.title,
                "department": j.department,
                "location": j.location,
                "status": j.status.value if hasattr(j.status, "value") else str(j.status),
                "created_at": j.created_at,
            }
            for j in jobs
        ],
    }


# ── Create Organization ─────────────────────────────────────────────────────

@router.post("/", status_code=201)
def create_organization(body: OrgCreateRequest, db: Session = Depends(get_db)):
    """Create a new organization (whitelist a domain)."""
    domain = body.domain.lower().strip()
    name = body.name.strip()

    if not domain or not name:
        raise HTTPException(status_code=422, detail="Name and domain are required")

    existing = db.query(Organization).filter(Organization.domain == domain).first()
    if existing:
        raise HTTPException(status_code=409, detail=f"Domain '{domain}' is already registered")

    org = Organization(
        name=name,
        domain=domain,
        max_jobs=max(0, body.max_jobs),
        max_candidates_per_job=max(0, body.max_candidates_per_job),
        allow_resume_database=body.allow_resume_database,
        allow_code_editor=body.allow_code_editor,
        allow_whatsapp=body.allow_whatsapp,
        allow_fit_score=body.allow_fit_score,
    )
    db.add(org)
    db.commit()
    db.refresh(org)

    logger.info("Organization created: %s (%s)", org.name, org.domain)
    return {
        "id": org.id,
        "ref": encode_ref("org", org.id),
        "name": org.name,
        "domain": org.domain,
        "is_active": org.is_active,
        "max_jobs": org.max_jobs or 0,
        "max_candidates_per_job": org.max_candidates_per_job or 0,
        "total_jobs_used": 0,
        "total_candidates_used": 0,
        "created_at": org.created_at,
        "updated_at": org.updated_at,
        "user_count": 0,
        "job_count": 0,
    }


# ── Update Organization ─────────────────────────────────────────────────────

@router.patch("/{org_id}")
def update_organization(
    org_id: OrgId,
    body: OrgUpdateRequest,
    db: Session = Depends(get_db),
):
    """Update organization name, domain, or active status."""
    org = db.query(Organization).filter(Organization.id == org_id).first()
    if not org:
        raise HTTPException(status_code=404, detail="Organization not found")

    if body.domain is not None:
        new_domain = body.domain.lower().strip()
        conflict = (
            db.query(Organization)
            .filter(Organization.domain == new_domain, Organization.id != org_id)
            .first()
        )
        if conflict:
            raise HTTPException(status_code=409, detail=f"Domain '{new_domain}' is already in use")
        org.domain = new_domain

    if body.name is not None:
        org.name = body.name.strip()
    if body.is_active is not None:
        org.is_active = body.is_active
    if body.max_jobs is not None:
        org.max_jobs = max(0, body.max_jobs)
    if body.max_candidates_per_job is not None:
        org.max_candidates_per_job = max(0, body.max_candidates_per_job)
    if body.allow_resume_database is not None:
        org.allow_resume_database = body.allow_resume_database
    if body.allow_code_editor is not None:
        org.allow_code_editor = body.allow_code_editor
    if body.allow_whatsapp is not None:
        org.allow_whatsapp = body.allow_whatsapp
    if body.allow_fit_score is not None:
        org.allow_fit_score = body.allow_fit_score

    org.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(org)

    logger.info("Organization updated: %s (id=%d)", org.name, org.id)
    return {
        "id": org.id,
        "ref": encode_ref("org", org.id),
        "name": org.name,
        "domain": org.domain,
        "is_active": org.is_active,
        "max_jobs": org.max_jobs or 0,
        "max_candidates_per_job": org.max_candidates_per_job or 0,
        "created_at": org.created_at,
        "updated_at": org.updated_at,
    }


# ── Delete Organization ─────────────────────────────────────────────────────

@router.delete("/{org_id}")
def delete_organization(org_id: OrgId, db: Session = Depends(get_db)):
    """Delete an EMPTY organization (no users, no jobs). An organization that is
    in use must be deactivated instead (PATCH is_active=false)."""
    org = db.query(Organization).filter(Organization.id == org_id).first()
    if not org:
        raise HTTPException(status_code=404, detail="Organization not found")
    org_name = org.name

    # Deleting used to unlink the org's users (organization_id=NULL) while they
    # stayed active, and a NULL organization was treated as global access.
    user_count = db.query(func.count(User.id)).filter(User.organization_id == org_id).scalar() or 0
    job_count = db.query(func.count(Job.id)).filter(Job.organization_id == org_id).scalar() or 0
    if user_count or job_count:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Organization '{org_name}' still has {user_count} user(s) and {job_count} job(s), "
                "so it cannot be deleted. Deactivate the organization instead."
            ),
        )

    try:
        db.delete(org)
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail=(
                f"Organization '{org_name}' still has data (such as resumes or questions), "
                "so it cannot be deleted. Deactivate the organization instead."
            ),
        )

    logger.info("Organization deleted: %s (id=%d)", org_name, org_id)
    return {"message": f"Organization '{org_name}' deleted successfully"}


# ── Per-Org AI Credit Breakdown ──────────────────────────────────────────────

@router.get("/{org_id}/credits")
def org_credits(org_id: OrgId, db: Session = Depends(get_db)):
    """Per-feature token/cost breakdown for one organization."""
    from sqlalchemy import extract
    from datetime import datetime as dt

    org = db.query(Organization).filter(Organization.id == org_id).first()
    if not org:
        raise HTTPException(status_code=404, detail="Organization not found")

    # Per-feature totals
    feature_rows = (
        db.query(
            AiCreditUsage.feature,
            func.sum(AiCreditUsage.input_tokens).label("input_tokens"),
            func.sum(AiCreditUsage.output_tokens).label("output_tokens"),
            func.sum(AiCreditUsage.total_tokens).label("total_tokens"),
            func.sum(AiCreditUsage.estimated_cost).label("estimated_cost"),
            func.count(AiCreditUsage.id).label("call_count"),
        )
        .filter(AiCreditUsage.organization_id == org_id)
        .group_by(AiCreditUsage.feature)
        .all()
    )

    # Monthly totals (current year)
    year = dt.utcnow().year
    monthly_rows = (
        db.query(
            extract("month", AiCreditUsage.created_at).label("month"),
            func.sum(AiCreditUsage.total_tokens).label("total_tokens"),
            func.sum(AiCreditUsage.estimated_cost).label("estimated_cost"),
        )
        .filter(
            AiCreditUsage.organization_id == org_id,
            extract("year", AiCreditUsage.created_at) == year,
        )
        .group_by(extract("month", AiCreditUsage.created_at))
        .order_by(extract("month", AiCreditUsage.created_at))
        .all()
    )

    MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
              "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

    return {
        "features": [
            {
                "feature": r.feature,
                "input_tokens": int(r.input_tokens or 0),
                "output_tokens": int(r.output_tokens or 0),
                "total_tokens": int(r.total_tokens or 0),
                "estimated_cost": round(float(r.estimated_cost or 0), 4),
                "call_count": int(r.call_count or 0),
            }
            for r in feature_rows
        ],
        "monthly": [
            {
                "month": MONTHS[int(r.month) - 1],
                "total_tokens": int(r.total_tokens or 0),
                "estimated_cost": round(float(r.estimated_cost or 0), 4),
            }
            for r in monthly_rows
        ],
    }


# ── Reset Lifetime Usage Counters ────────────────────────────────────────────

@router.post("/{org_id}/reset-usage")
def reset_usage_counters(org_id: OrgId, db: Session = Depends(get_db)):
    """Reset lifetime usage counters (e.g. after plan upgrade)."""
    org = db.query(Organization).filter(Organization.id == org_id).first()
    if not org:
        raise HTTPException(status_code=404, detail="Organization not found")

    org.total_jobs_used = 0
    org.total_candidates_used = 0
    org.updated_at = datetime.utcnow()
    db.commit()

    logger.info("Usage counters reset for org %s (id=%d)", org.name, org.id)
    return {"message": f"Usage counters reset for '{org.name}'"}


# ── Unassigned Data ──────────────────────────────────────────────────────────
#
# A super admin with no organization stores everything it creates (jobs,
# questions, resumes) with organization_id NULL. Only a global super admin can
# see such rows, so the admins of the tenant that really owns them get an empty
# dashboard. These endpoints move that data, and the tenant's users, into an
# organization.

class ClaimUnassignedRequest(BaseModel):
    # Users to link to the organization; each must be on the organization's
    # email domain (see _domain_users_outside).
    user_ids: list[int] = []


def _unassigned_counts(db: Session) -> dict:
    return {
        "jobs": db.query(func.count(Job.id)).filter(Job.organization_id.is_(None)).scalar() or 0,
        "candidates": (
            db.query(func.count(Candidate.id))
            .join(Job, Candidate.job_id == Job.id)
            .filter(Job.organization_id.is_(None))
            .filter(~Candidate.email.like("pending_%@resume.local"))
            .scalar() or 0
        ),
        "questions": db.query(func.count(Question.id)).filter(Question.organization_id.is_(None)).scalar() or 0,
        "resumes": db.query(func.count(ResumeMetadata.id)).filter(ResumeMetadata.organization_id.is_(None)).scalar() or 0,
        "ai_credit_usage": db.query(func.count(AiCreditUsage.id)).filter(AiCreditUsage.organization_id.is_(None)).scalar() or 0,
    }


def _domain_users_outside(db: Session, org: Organization) -> list[User]:
    """Users whose email is on the organization's domain but who are not in it."""
    domain = (org.domain or "").strip().lower()
    if not domain:
        return []
    candidates = db.query(User).filter(
        (User.organization_id.is_(None)) | (User.organization_id != org.id)
    ).all()
    return [u for u in candidates if u.email.lower().endswith("@" + domain)]


@router.get("/unassigned/summary")
def unassigned_summary(db: Session = Depends(get_db)):
    """Counts of data that belongs to no organization."""
    return _unassigned_counts(db)


@router.get("/{org_id}/unassigned")
def unassigned_preview(org_id: OrgId, db: Session = Depends(get_db)):
    """What claiming the unassigned data for this organization would move."""
    org = db.query(Organization).filter(Organization.id == org_id).first()
    if not org:
        raise HTTPException(status_code=404, detail="Organization not found")

    users = _domain_users_outside(db, org)
    org_names = {
        o.id: o.name
        for o in db.query(Organization).filter(
            Organization.id.in_({u.organization_id for u in users if u.organization_id})
        ).all()
    } if users else {}

    return {
        "data": _unassigned_counts(db),
        "users": [
            {
                "id": u.id,
                "name": u.name,
                "email": u.email,
                "role": u.role.value,
                "organization_name": org_names.get(u.organization_id),
            }
            for u in users
        ],
    }


@router.post("/{org_id}/claim-unassigned")
def claim_unassigned(
    org_id: OrgId,
    body: ClaimUnassignedRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles(UserRole.super_admin)),
):
    """Move every row with no organization into this organization, and link the
    selected users on its domain."""
    org = db.query(Organization).filter(Organization.id == org_id).first()
    if not org:
        raise HTTPException(status_code=404, detail="Organization not found")
    if not org.is_active:
        raise HTTPException(
            status_code=422,
            detail=f"Organization '{org.name}' is inactive. Activate it before assigning data to it.",
        )

    eligible = {u.id: u for u in _domain_users_outside(db, org)}
    not_eligible = [uid for uid in body.user_ids if uid not in eligible]
    if not_eligible:
        raise HTTPException(
            status_code=422,
            detail=f"Only users with an @{org.domain} email who are not already in '{org.name}' can be linked.",
        )

    moved = {}
    for key, model in (("jobs", Job), ("questions", Question), ("ai_credit_usage", AiCreditUsage)):
        moved[key] = (
            db.query(model)
            .filter(model.organization_id.is_(None))
            .update({model.organization_id: org.id}, synchronize_session=False)
        )

    # Resumes are unique per (email, organization): skip an email the
    # organization already has, and move only one row per email.
    already_in_org = select(ResumeMetadata.candidate_email).where(ResumeMetadata.organization_id == org.id)
    one_per_email = (
        select(func.min(ResumeMetadata.id))
        .where(
            ResumeMetadata.organization_id.is_(None),
            ResumeMetadata.candidate_email.not_in(already_in_org),
        )
        .group_by(ResumeMetadata.candidate_email)
    )
    moved["resumes"] = (
        db.query(ResumeMetadata)
        .filter(ResumeMetadata.id.in_(one_per_email))
        .update({ResumeMetadata.organization_id: org.id}, synchronize_session=False)
    )
    moved["resumes_skipped"] = (
        db.query(func.count(ResumeMetadata.id)).filter(ResumeMetadata.organization_id.is_(None)).scalar() or 0
    )

    for uid in body.user_ids:
        eligible[uid].organization_id = org.id
        eligible[uid].updated_at = datetime.utcnow()
    moved["users"] = len(body.user_ids)

    # Keep the lifetime job counter in step with the jobs the org now owns.
    org.total_jobs_used = (org.total_jobs_used or 0) + moved["jobs"]
    org.updated_at = datetime.utcnow()
    db.commit()

    logger.info(
        "Unassigned data claimed for %s (id=%d) by %s: %s",
        org.name, org.id, current_user.email, moved,
    )
    return moved


# ── Dashboard Stats ──────────────────────────────────────────────────────────

@router.get("/stats/overview")
def org_stats_overview(db: Session = Depends(get_db)):
    """Global stats for the Super Admin dashboard."""
    total_orgs = db.query(func.count(Organization.id)).scalar()
    active_orgs = db.query(func.count(Organization.id)).filter(Organization.is_active == True).scalar()
    total_users = db.query(func.count(User.id)).scalar()
    total_jobs = db.query(func.count(Job.id)).scalar()
    total_candidates = db.query(func.count(Candidate.id)).filter(~Candidate.email.like("pending_%@resume.local")).scalar()

    # Users by role
    role_counts = dict(
        db.query(User.role, func.count(User.id)).group_by(User.role).all()
    )

    return {
        "total_organizations": total_orgs,
        "active_organizations": active_orgs,
        "total_users": total_users,
        "total_jobs": total_jobs,
        "total_candidates": total_candidates,
        "users_by_role": {k.value if hasattr(k, "value") else k: v for k, v in role_counts.items()},
    }
