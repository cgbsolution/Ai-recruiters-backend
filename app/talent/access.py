"""
Who may use the Talent Agent admin API, and for which organization.

The agent works on the Resume Database, so an organization needs
`allow_resume_database`. Admins and super admins manage settings, consent and
campaigns; recruiters work on job matches, approvals and interview reviews.
A global super admin (no organization) passes ?organization_id= to act on one.
"""

from typing import Optional

from fastapi import Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.auth import get_current_user, get_org_id
from app.database import get_db
from app.models.organization import Organization
from app.models.user import User, UserRole

ADMIN_ROLES = (UserRole.admin, UserRole.super_admin)


def _org_or_404(db: Session, org_id: int) -> Organization:
    org = db.get(Organization, org_id)
    if org is None or not org.is_active:
        raise HTTPException(status_code=404, detail="Organization not found")
    return org


def resolve_org(user: User, db: Session, requested: Optional[int]) -> int:
    own = get_org_id(user)
    if own is None:  # global super admin
        if requested is None:
            raise HTTPException(status_code=400, detail="organization_id is required for a platform admin")
        _org_or_404(db, requested)
        return requested
    if requested is not None and requested != own:
        raise HTTPException(status_code=404, detail="Organization not found")
    org = _org_or_404(db, own)
    if not org.allow_resume_database and user.role != UserRole.super_admin:
        raise HTTPException(
            status_code=403,
            detail="The Talent Agent needs the Resume Database, which is not enabled for your organization.",
        )
    return own


def talent_org(
    organization_id: Optional[int] = Query(None, description="Platform admins only: the organization to act on"),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> int:
    return resolve_org(user, db, organization_id)


def require_talent_admin(user: User = Depends(get_current_user)) -> User:
    if user.role not in ADMIN_ROLES:
        raise HTTPException(status_code=403, detail="Only admins can change Talent Agent settings")
    return user
