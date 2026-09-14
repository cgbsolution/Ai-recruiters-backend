"""
Authentication utilities: JWT tokens, password hashing, org scoping, and dependency injection.
"""
from datetime import datetime, timedelta
from typing import Annotated, List, Optional

import bcrypt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.models.user import User, UserRole

# ── Password Hashing (using bcrypt directly — passlib has Python 3.14 issues)
def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))
    except Exception:
        return False


# ── JWT ──────────────────────────────────────────────────────────────────────
ALGORITHM = "HS256"


def create_access_token(user_id: int, role: str, org_id: Optional[int] = None) -> str:
    expire = datetime.utcnow() + timedelta(minutes=settings.SESSION_TIMEOUT_MINUTES)
    payload = {
        "sub": str(user_id),
        "role": role,
        "exp": expire,
    }
    if org_id:
        payload["org_id"] = org_id
    return jwt.encode(payload, settings.SECRET_KEY, algorithm=ALGORITHM)


def decode_access_token(token: str) -> dict:
    return jwt.decode(token, settings.SECRET_KEY, algorithms=[ALGORITHM])


# ── FastAPI Dependencies ─────────────────────────────────────────────────────
security = HTTPBearer(auto_error=False)


def get_current_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(security)],
    db: Session = Depends(get_db),
) -> User:
    """Extract and validate the current user from the Bearer token."""
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        payload = decode_access_token(credentials.credentials)
        user_id = int(payload["sub"])
    except (JWTError, KeyError, ValueError):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    user = db.query(User).filter(User.id == user_id, User.is_active == True).first()
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found or inactive",
        )
    # Tenant users must belong to an ACTIVE organization. Suspending a tenant
    # (Organization.is_active=False) previously had no effect on live tokens,
    # and deleting an org left its users with organization_id=NULL — which
    # every scoped query then treated as GLOBAL, cross-tenant access.
    if user.role != UserRole.super_admin:
        org = user.organization
        if org is None:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Your account is not assigned to an organization. Contact your administrator.",
            )
        if not org.is_active:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Your organization's account is suspended. Contact support.",
            )
    return user


def require_roles(*roles: UserRole):
    """
    Dependency factory — returns a dependency that checks whether
    the current user has one of the allowed roles.
    """
    allowed: List[UserRole] = list(roles)

    def _checker(user: User = Depends(get_current_user)) -> User:
        if user.role not in allowed:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You do not have permission to access this resource",
            )
        return user

    return _checker


# ── Organization Scoping ─────────────────────────────────────────────────────

def is_global_admin(user: User) -> bool:
    """A super admin who is not attached to an organization sees every tenant."""
    return user.role == UserRole.super_admin and user.organization_id is None


def get_org_id(user: User) -> Optional[int]:
    """The organization the caller's queries must be scoped to.

    Returns None ONLY for a global super admin (see is_global_admin). Callers
    treat None as "no filter", so returning it for an admin or recruiter whose
    organization_id is NULL silently granted them every tenant's data — that
    state is now refused instead."""
    if user.organization_id is None:
        if user.role == UserRole.super_admin:
            return None
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Your account is not assigned to an organization. Contact your administrator.",
        )
    return user.organization_id


def scoped_query(db: Session, model, user: User):
    """
    Returns a query filtered by the caller's organization (see get_org_id).
    Only a global super admin gets unfiltered access.
    The model must have an `organization_id` column.
    """
    q = db.query(model)
    org_id = get_org_id(user)
    if org_id is not None:
        q = q.filter(model.organization_id == org_id)
    return q
