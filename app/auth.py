"""
Authentication utilities: JWT tokens, password hashing, org scoping, and dependency injection.
"""
import hashlib
import hmac
import time
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
PASSWORD_MIN_LENGTH = 8
# bcrypt only reads the first 72 bytes, and bcrypt 5 raises on anything longer
# (which surfaced as an unhandled 500 on signup / change / reset).
PASSWORD_MAX_BYTES = 72


def password_problem(plain: str) -> Optional[str]:
    """Why a NEW password is unacceptable, or None when it is fine."""
    if len(plain or "") < PASSWORD_MIN_LENGTH:
        return f"Password must be at least {PASSWORD_MIN_LENGTH} characters"
    if len(plain.encode("utf-8")) > PASSWORD_MAX_BYTES:
        return f"Password must be at most {PASSWORD_MAX_BYTES} bytes long"
    return None


def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))
    except Exception:
        return False


# A real hash to check against when the email is unknown, so a login attempt
# for a non-existent account costs the same bcrypt time as a wrong password —
# otherwise the response time says which emails have accounts.
_DUMMY_HASH = bcrypt.hashpw(b"timing-equaliser", bcrypt.gensalt()).decode("utf-8")


def burn_password_check(plain: str) -> None:
    verify_password(plain, _DUMMY_HASH)


# ── JWT ──────────────────────────────────────────────────────────────────────
ALGORITHM = "HS256"


def password_fingerprint(password_hash: Optional[str]) -> str:
    """Short keyed digest of the user's CURRENT password hash, carried in the
    login token. Changing or resetting the password changes the hash, so every
    token issued before that stops matching and is refused — a stolen token no
    longer outlives the password change meant to lock the thief out."""
    return hmac.new(
        settings.SECRET_KEY.encode("utf-8"), (password_hash or "").encode("utf-8"), hashlib.sha256
    ).hexdigest()[:24]


def create_access_token(
    user_id: int, role: str, org_id: Optional[int] = None, password_hash: Optional[str] = None,
    auth_time: Optional[int] = None,
) -> str:
    """`auth_time` (epoch seconds) is when the password was entered. A renewed
    token (POST /auth/refresh) carries the original one forward, which is what
    SESSION_MAX_LIFETIME_DAYS is measured from."""
    expire = datetime.utcnow() + timedelta(minutes=settings.SESSION_TIMEOUT_MINUTES)
    payload = {
        "sub": str(user_id),
        "role": role,
        "exp": expire,
        "auth_time": int(auth_time if auth_time is not None else time.time()),
    }
    if org_id:
        payload["org_id"] = org_id
    if password_hash:
        payload["pwf"] = password_fingerprint(password_hash)
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
    # Tokens issued before the fingerprint existed carry none; they are still
    # accepted and age out within SESSION_TIMEOUT_MINUTES.
    pwf = payload.get("pwf")
    if pwf is not None and not hmac.compare_digest(str(pwf), password_fingerprint(user.password_hash)):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Your password was changed. Please sign in again.",
            headers={"WWW-Authenticate": "Bearer"},
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
