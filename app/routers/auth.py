"""
Authentication router — signup, login, me, change-password, email verification, user management.
"""
import secrets
import logging
import threading
import time
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from jose import JWTError, jwt
from pydantic import BaseModel, EmailStr
from slowapi import Limiter
from slowapi.util import get_remote_address
from sqlalchemy.orm import Session

from app.auth import (
    ALGORITHM,
    create_access_token,
    get_current_user,
    get_org_id,
    hash_password,
    require_roles,
    verify_password,
)
from app.config import settings
from app.database import get_db
from app.models.organization import Organization
from app.models.user import User, UserRole
from app.refs import OrgId

logger = logging.getLogger(__name__)

router = APIRouter()

# Per-IP throttling of the unauthenticated credential endpoints (same slowapi
# pattern as candidates.py). RateLimitExceeded is an HTTPException, so a hit
# is answered with 429 and the error_message given on each route.
limiter = Limiter(key_func=get_remote_address)


# ── Schemas ──────────────────────────────────────────────────────────────────

class SignUpRequest(BaseModel):
    name: str
    email: EmailStr
    password: str
    role: UserRole = UserRole.recruiter


class CreateUserRequest(SignUpRequest):
    # Super admins only: the organization to create the user in (ref or id).
    # When omitted, the organization is matched by the email's domain.
    organization_id: Optional[OrgId] = None


class SignInRequest(BaseModel):
    email: EmailStr
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int = 0  # seconds until token expires
    user: dict


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str


class ForgotPasswordRequest(BaseModel):
    email: EmailStr


class ResetPasswordRequest(BaseModel):
    token: str
    new_password: str


class UserOut(BaseModel):
    id: int
    name: str
    email: str
    role: str
    is_active: bool
    is_email_verified: bool
    organization_id: int | None = None
    organization_name: str | None = None
    created_at: datetime | None = None

    class Config:
        from_attributes = True


class UpdateUserRequest(BaseModel):
    name: str | None = None
    role: UserRole | None = None
    is_active: bool | None = None


# ── Sign Up ──────────────────────────────────────────────────────────────────

@router.post("/signup", response_model=TokenResponse, status_code=201)
@limiter.limit("5/minute", error_message="Too many sign-up attempts. Please wait a minute and try again.")
def signup(request: Request, body: SignUpRequest, db: Session = Depends(get_db)):
    """Register a new user. First user is auto-promoted to super_admin."""
    existing = db.query(User).filter(User.email == body.email.lower()).first()
    if existing:
        raise HTTPException(status_code=409, detail="Email already registered")

    if len(body.password) < 6:
        raise HTTPException(status_code=422, detail="Password must be at least 6 characters")

    # First user → super_admin
    user_count = db.query(User).count()
    role = UserRole.super_admin if user_count == 0 else body.role

    # Only super_admin can create admin/super_admin accounts (checked later via protected endpoint)
    # Direct signup always creates recruiter unless it's the first user
    if user_count > 0:
        role = UserRole.recruiter

    # Domain-based organization matching
    email_domain = body.email.lower().split("@")[1]
    org = db.query(Organization).filter(
        Organization.domain == email_domain,
        Organization.is_active == True,
    ).first()

    # For non-super_admin users, require a whitelisted organization domain
    organization_id = None
    if role != UserRole.super_admin:
        if not org:
            raise HTTPException(
                status_code=403,
                detail=f"Your email domain '{email_domain}' is not associated with any registered organization. Please contact your administrator.",
            )
        organization_id = org.id

    # First user (super_admin) is active immediately; others need admin approval
    is_first_user = user_count == 0

    user = User(
        name=body.name.strip(),
        email=body.email.lower().strip(),
        password_hash=hash_password(body.password),
        role=role,
        organization_id=organization_id,
        is_active=is_first_user,  # New signups are inactive until admin approves
        is_email_verified=is_first_user,
    )

    # Generate email verification token
    if not is_first_user:
        user.email_verify_token = secrets.token_urlsafe(48)
        user.email_verify_expires = datetime.utcnow() + timedelta(hours=24)

    db.add(user)
    db.commit()
    db.refresh(user)

    logger.info("User registered: %s (role=%s, org=%s, active=%s)", user.email, user.role.value, org.name if org else "N/A", user.is_active)

    # First user gets auto-logged in; others must wait for admin approval
    if is_first_user:
        token = create_access_token(user.id, user.role.value, org_id=organization_id)
        return TokenResponse(
            access_token=token,
            expires_in=settings.SESSION_TIMEOUT_MINUTES * 60,
            user={
                "id": user.id,
                "name": user.name,
                "email": user.email,
                "role": user.role.value,
                "is_email_verified": user.is_email_verified,
                "organization_id": organization_id,
                "organization_name": org.name if org else None,
            },
        )

    # Return a pending response (no token) for non-first users
    from fastapi.responses import JSONResponse
    return JSONResponse(
        status_code=202,
        content={
            "pending_approval": True,
            "message": "Your account has been created. Please wait for admin approval.",
        },
    )


# ── Sign In ──────────────────────────────────────────────────────────────────

@router.post("/login", response_model=TokenResponse)
@limiter.limit("10/minute", error_message="Too many login attempts. Please wait a minute and try again.")
def login(request: Request, body: SignInRequest, db: Session = Depends(get_db)):
    """Authenticate and return a JWT token."""
    user = db.query(User).filter(User.email == body.email.lower()).first()
    if not user or not verify_password(body.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Invalid email or password")

    if not user.is_active:
        raise HTTPException(
            status_code=403,
            detail="Your account is pending approval by your organization admin.",
        )

    org = None
    if user.organization_id:
        try:
            org = db.query(Organization).filter(Organization.id == user.organization_id).first()
        except Exception:
            if user.role != UserRole.super_admin:
                raise
            # org columns may not exist yet — don't crash a super admin's login

    # Tenant users need an ACTIVE organization, the same rule get_current_user
    # applies to every request. Without this, users of a deleted or suspended
    # organization were still issued a fresh token.
    if user.role != UserRole.super_admin:
        if org is None:
            raise HTTPException(
                status_code=403,
                detail="Your account is not assigned to an organization. Contact your administrator.",
            )
        if not org.is_active:
            raise HTTPException(
                status_code=403,
                detail="Your organization's account is suspended. Contact support.",
            )

    token = create_access_token(user.id, user.role.value, org_id=user.organization_id)
    logger.info("User logged in: %s", user.email)

    org_name = org.name if org else None

    return TokenResponse(
        access_token=token,
        expires_in=settings.SESSION_TIMEOUT_MINUTES * 60,
        user={
            "id": user.id,
            "name": user.name,
            "email": user.email,
            "role": user.role.value,
            "is_email_verified": user.is_email_verified,
            "organization_id": user.organization_id,
            "organization_name": org_name,
        },
    )


# ── Me ───────────────────────────────────────────────────────────────────────

@router.get("/me")
def get_me(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Return the current authenticated user's info."""
    org_name = None
    allow_code_editor = False
    allow_whatsapp = False
    allow_fit_score = False
    if user.organization_id:
        org = db.query(Organization).filter(Organization.id == user.organization_id).first()
        org_name = org.name if org else None
        allow_code_editor = getattr(org, "allow_code_editor", False) if org else False
        allow_whatsapp = getattr(org, "allow_whatsapp", False) if org else False
        allow_fit_score = getattr(org, "allow_fit_score", False) if org else False

    return {
        "id": user.id,
        "name": user.name,
        "email": user.email,
        "role": user.role.value,
        "phone": user.phone,
        "designation": user.designation,
        "is_active": user.is_active,
        "is_email_verified": user.is_email_verified,
        "organization_id": user.organization_id,
        "organization_name": org_name,
        "allow_code_editor": allow_code_editor,
        "allow_whatsapp": allow_whatsapp,
        "allow_fit_score": allow_fit_score,
        "google_connected": bool(getattr(user, "google_refresh_token", None)),
        "google_email": getattr(user, "google_email", None),
        "created_at": user.created_at,
    }


# ── Update Own Profile ──────────────────────────────────────────────────────

@router.patch("/profile")
def update_profile(
    body: dict,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Update the current user's own profile fields (name, phone, designation)."""
    if "name" in body and body["name"].strip():
        user.name = body["name"].strip()
    if "phone" in body:
        user.phone = body["phone"] or None
    if "designation" in body:
        user.designation = body["designation"] or None
    user.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(user)

    org_name = None
    if user.organization_id:
        from app.models.organization import Organization
        org = db.query(Organization).filter(Organization.id == user.organization_id).first()
        org_name = org.name if org else None

    return {
        "id": user.id,
        "name": user.name,
        "email": user.email,
        "role": user.role.value,
        "phone": user.phone,
        "designation": user.designation,
        "is_active": user.is_active,
        "is_email_verified": user.is_email_verified,
        "organization_id": user.organization_id,
        "organization_name": org_name,
    }


# ── Change Password ─────────────────────────────────────────────────────────

@router.post("/change-password")
def change_password(
    body: ChangePasswordRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Change the current user's password."""
    if not verify_password(body.current_password, user.password_hash):
        raise HTTPException(status_code=400, detail="Current password is incorrect")

    if len(body.new_password) < 6:
        raise HTTPException(status_code=422, detail="New password must be at least 6 characters")

    user.password_hash = hash_password(body.new_password)
    user.updated_at = datetime.utcnow()
    db.commit()

    return {"message": "Password updated successfully"}


# ── Forgot / Reset Password ─────────────────────────────────────────────────

# How long a reset link stays usable.
RESET_TOKEN_TTL = timedelta(hours=1)

# Returned no matter what, so the endpoint cannot be used to discover which
# email addresses have accounts.
_FORGOT_PASSWORD_REPLY = {
    "message": "If an account exists for that email, a reset link has been sent."
}


def _send_password_reset_email(user: User, token: str) -> bool:
    """Compose and send the reset link. Returns False if SMTP is unconfigured."""
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText

    # Reuse the SMTP sender the interview emails already go through rather than
    # standing up a third copy of the connection/retry logic.
    from app.services.interview_service import _send_email_sync

    link = f"{settings.frontend_base_url}/reset-password?token={token}"

    msg = MIMEMultipart("alternative")
    msg["Subject"] = "Reset your password"
    msg["From"] = f"{settings.EMAIL_FROM or 'No Reply'} <{settings.SMTP_USER}>"
    msg["To"] = user.email

    hours = int(RESET_TOKEN_TTL.total_seconds() // 3600)
    msg.attach(MIMEText(
        f"Hi {user.name},\n\n"
        f"We received a request to reset your password.\n\n"
        f"Reset it here (the link expires in {hours} hour(s)):\n{link}\n\n"
        f"If you did not request this, you can ignore this email — your "
        f"password will not change.\n",
        "plain",
    ))
    msg.attach(MIMEText(
        f"""
        <div style="font-family:Arial,sans-serif;font-size:15px;color:#1f2937">
          <p>Hi {user.name},</p>
          <p>We received a request to reset your password.</p>
          <p style="margin:24px 0">
            <a href="{link}" style="background:#465fff;color:#fff;padding:12px 24px;
               border-radius:8px;text-decoration:none;font-weight:600">Reset Password</a>
          </p>
          <p style="color:#6b7280;font-size:13px">
            This link expires in {hours} hour(s). If you did not request a reset,
            you can ignore this email — your password will not change.
          </p>
        </div>
        """,
        "html",
    ))

    return _send_email_sync(msg, [user.email])


@router.post("/forgot-password")
@limiter.limit("5/minute", error_message="Too many password reset requests. Please wait a minute and try again.")
def forgot_password(request: Request, body: ForgotPasswordRequest, db: Session = Depends(get_db)):
    """Email a single-use password reset link.

    Always returns the same message. Reporting whether the address was found
    would turn this into an account-enumeration oracle.
    """
    email = body.email.strip().lower()
    user = db.query(User).filter(User.email == email).first()

    if not user or not user.is_active:
        logger.info("Password reset requested for unknown/inactive address: %s", email)
        return _FORGOT_PASSWORD_REPLY

    user.password_reset_token = secrets.token_urlsafe(32)
    user.password_reset_expires = datetime.utcnow() + RESET_TOKEN_TTL
    db.commit()

    try:
        sent = _send_password_reset_email(user, user.password_reset_token)
    except Exception:
        logger.exception("Failed to send password reset email to %s", email)
        sent = False

    if not sent:
        # Leave the token in place — the user can retry — but make the failure
        # visible in the logs instead of silently pretending the mail went out.
        logger.error("Password reset email could NOT be delivered to %s", email)

    return _FORGOT_PASSWORD_REPLY


@router.post("/reset-password")
def reset_password(body: ResetPasswordRequest, db: Session = Depends(get_db)):
    """Set a new password using a token from the reset email."""
    if len(body.new_password) < 6:
        raise HTTPException(status_code=422, detail="Password must be at least 6 characters")

    token = (body.token or "").strip()
    if not token:
        raise HTTPException(status_code=400, detail="Reset link is invalid")

    user = db.query(User).filter(User.password_reset_token == token).first()
    if not user:
        raise HTTPException(status_code=400, detail="Reset link is invalid or has already been used")

    if not user.password_reset_expires or user.password_reset_expires < datetime.utcnow():
        raise HTTPException(status_code=410, detail="Reset link has expired. Please request a new one.")

    if not user.is_active:
        raise HTTPException(status_code=403, detail="This account is inactive")

    user.password_hash = hash_password(body.new_password)
    # Single use — clear the token so the same link cannot be replayed.
    user.password_reset_token = None
    user.password_reset_expires = None
    user.updated_at = datetime.utcnow()
    db.commit()

    logger.info("Password reset completed for %s", user.email)
    return {"message": "Password updated successfully. You can now sign in."}


# ── Email Verification ───────────────────────────────────────────────────────

@router.get("/verify-email")
def verify_email(token: str, uid: int, db: Session = Depends(get_db)):
    """Verify a user's email with their verification token."""
    user = db.query(User).filter(User.id == uid).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    if user.is_email_verified:
        return {"message": "Email is already verified", "already_verified": True}

    if user.email_verify_token != token:
        raise HTTPException(status_code=400, detail="Invalid verification token")

    if user.email_verify_expires and user.email_verify_expires < datetime.utcnow():
        raise HTTPException(status_code=410, detail="Verification link has expired. Please sign in to request a new one.")

    user.is_email_verified = True
    user.email_verify_token = None
    user.email_verify_expires = None
    user.updated_at = datetime.utcnow()
    db.commit()

    return {"message": "Email verified successfully"}


# ── User Management (Super Admin / Admin only) ──────────────────────────────

@router.get("/users")
def list_users(
    db: Session = Depends(get_db),
    user: User = Depends(require_roles(UserRole.super_admin, UserRole.admin)),
):
    """List all users. Admin and Super Admin only, scoped by organization."""
    q = db.query(User)

    if user.role == UserRole.admin:
        # Admins see only users in their own organization (recruiters + admins).
        # get_org_id refuses an admin with no organization; filtering on
        # organization_id == NULL used to list every org-less user.
        q = q.filter(
            User.organization_id == get_org_id(user),
            User.role.in_([UserRole.recruiter, UserRole.admin]),
        )
    # Super admin sees everyone

    users = q.all()
    # Enrich with org names
    org_ids = {u.organization_id for u in users if u.organization_id}
    orgs = {}
    if org_ids:
        orgs = {o.id: o.name for o in db.query(Organization).filter(Organization.id.in_(org_ids)).all()}

    return [
        {
            "id": u.id,
            "name": u.name,
            "email": u.email,
            "role": u.role.value,
            "is_active": u.is_active,
            "is_email_verified": u.is_email_verified,
            "organization_id": u.organization_id,
            "organization_name": orgs.get(u.organization_id),
            "created_at": u.created_at,
        }
        for u in users
    ]


@router.post("/users", response_model=UserOut, status_code=201)
def create_user(
    body: CreateUserRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles(UserRole.super_admin, UserRole.admin)),
):
    """Create a new user with a specific role. Admin/Super Admin only."""
    existing = db.query(User).filter(User.email == body.email.lower()).first()
    if existing:
        raise HTTPException(status_code=409, detail="Email already registered")

    if len(body.password) < 6:
        raise HTTPException(status_code=422, detail="Password must be at least 6 characters")

    # Admins cannot create super_admin users
    if current_user.role == UserRole.admin and body.role == UserRole.super_admin:
        raise HTTPException(status_code=403, detail="Admins cannot create super admin users")

    # Only super_admin can create super_admin
    if body.role == UserRole.super_admin and current_user.role != UserRole.super_admin:
        raise HTTPException(status_code=403, detail="Only super admins can create super admin users")

    # Determine organization. An admin or recruiter must always land in an
    # existing, active organization: a NULL organization_id used to be read as
    # "global" by every scoped query, so it is refused instead of defaulted.
    email_domain = body.email.lower().split("@")[1]
    org = None
    if current_user.role == UserRole.admin:
        # Admins create users only in their own organization, on its domain
        admin_org_id = get_org_id(current_user)
        if body.organization_id is not None and body.organization_id != admin_org_id:
            raise HTTPException(status_code=403, detail="Admins can only create users in their own organization")
        org = db.query(Organization).filter(Organization.id == admin_org_id).first()
        if not org:
            raise HTTPException(
                status_code=403,
                detail="Your account is not assigned to an organization. Contact your administrator.",
            )
        if email_domain != (org.domain or "").strip().lower():
            raise HTTPException(
                status_code=422,
                detail=f"The email address must use your organization's domain ({org.domain}).",
            )
    else:
        # Super admin: explicit organization, otherwise match by email domain
        if body.organization_id is not None:
            org = db.query(Organization).filter(Organization.id == body.organization_id).first()
            if not org:
                raise HTTPException(status_code=422, detail="Organization not found")
        else:
            org = db.query(Organization).filter(Organization.domain == email_domain).first()
        if body.role != UserRole.super_admin:
            if not org:
                raise HTTPException(
                    status_code=422,
                    detail=(
                        f"No organization is registered for the domain '{email_domain}'. "
                        "Create the organization first, or choose the organization explicitly."
                    ),
                )
            if not org.is_active:
                raise HTTPException(
                    status_code=422,
                    detail=f"Organization '{org.name}' is inactive. Activate it before adding users to it.",
                )
    org_id = org.id if org else None

    new_user = User(
        name=body.name.strip(),
        email=body.email.lower().strip(),
        password_hash=hash_password(body.password),
        role=body.role,
        organization_id=org_id,
        is_email_verified=True,  # Created by admin — skip verification
    )
    db.add(new_user)
    db.commit()
    db.refresh(new_user)

    logger.info("User created by %s: %s (role=%s, org_id=%s)", current_user.email, new_user.email, new_user.role.value, org_id)
    return {
        "id": new_user.id,
        "name": new_user.name,
        "email": new_user.email,
        "role": new_user.role.value,
        "is_active": new_user.is_active,
        "is_email_verified": new_user.is_email_verified,
        "organization_id": new_user.organization_id,
        "organization_name": org.name if org else None,
        "created_at": new_user.created_at,
    }


@router.patch("/users/{user_id}", response_model=UserOut)
def update_user(
    user_id: int,
    body: UpdateUserRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles(UserRole.super_admin, UserRole.admin)),
):
    """Update a user's role or active status. Admin/Super Admin only."""
    target = db.query(User).filter(User.id == user_id).first()
    if not target:
        raise HTTPException(status_code=404, detail="User not found")

    # Admin can only modify users in their own organization. get_org_id refuses
    # an org-less admin (None != None used to let one manage every org-less user).
    if current_user.role == UserRole.admin and target.organization_id != get_org_id(current_user):
        raise HTTPException(status_code=403, detail="Cannot modify users outside your organization")

    # Cannot modify a super_admin unless you are super_admin
    if target.role == UserRole.super_admin and current_user.role != UserRole.super_admin:
        raise HTTPException(status_code=403, detail="Cannot modify super admin users")

    # Only a super admin can grant the super admin role
    if body.role == UserRole.super_admin and current_user.role != UserRole.super_admin:
        raise HTTPException(status_code=403, detail="Admins cannot assign super admin role")

    # An admin/recruiter needs an organization: demoting an org-less super
    # admin would create a tenant user with organization_id NULL.
    if body.role is not None and body.role != UserRole.super_admin and target.organization_id is None:
        raise HTTPException(
            status_code=422,
            detail="This user has no organization, so they can only be a super admin. Create the user inside an organization instead.",
        )

    if body.name is not None:
        target.name = body.name.strip()
    if body.role is not None:
        target.role = body.role
    if body.is_active is not None:
        target.is_active = body.is_active

    target.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(target)

    return target


@router.delete("/users/{user_id}", status_code=204)
def delete_user(
    user_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles(UserRole.super_admin, UserRole.admin)),
):
    """Delete a user. Admin/Super Admin only."""
    target = db.query(User).filter(User.id == user_id).first()
    if not target:
        raise HTTPException(status_code=404, detail="User not found")

    # Cannot delete yourself
    if target.id == current_user.id:
        raise HTTPException(status_code=400, detail="Cannot delete your own account")

    # Admin can only delete users in their own organization (see update_user)
    if current_user.role == UserRole.admin and target.organization_id != get_org_id(current_user):
        raise HTTPException(status_code=403, detail="Cannot delete users outside your organization")

    # Cannot delete a super_admin unless you are super_admin
    if target.role == UserRole.super_admin and current_user.role != UserRole.super_admin:
        raise HTTPException(status_code=403, detail="Cannot delete super admin users")

    db.delete(target)
    db.commit()

    logger.info("User deleted by %s: %s (id=%d)", current_user.email, target.email, user_id)


# ── Google OAuth2 (Connect / Callback / Disconnect) ─────────────────────────

# The OAuth `state` is a dedicated token, NOT the user's 24h access JWT (which
# then sat in Google/redirect URLs and logs and worked as an API credential).
# It has no `sub` claim, so get_current_user never accepts it; it expires after
# 10 minutes and its nonce is single-use within this process.
GOOGLE_OAUTH_STATE_TYP = "google_oauth_state"
GOOGLE_OAUTH_STATE_TTL = timedelta(minutes=10)
_used_google_state_nonces: dict = {}  # nonce -> expiry (unix seconds)
_google_state_lock = threading.Lock()


def _create_google_oauth_state(user_id: int) -> str:
    now = datetime.utcnow()
    return jwt.encode(
        {
            "typ": GOOGLE_OAUTH_STATE_TYP,
            "uid": str(user_id),
            "nonce": secrets.token_urlsafe(16),
            "iat": now,
            "exp": now + GOOGLE_OAUTH_STATE_TTL,
        },
        settings.SECRET_KEY,
        algorithm=ALGORITHM,
    )


def _consume_google_oauth_state(state: str) -> int:
    """User id from a valid, unexpired, not-yet-used OAuth state; 400 otherwise."""
    invalid = HTTPException(status_code=400, detail="Invalid or expired state token")
    try:
        payload = jwt.decode(state, settings.SECRET_KEY, algorithms=[ALGORITHM])
        user_id = int(payload["uid"])
        nonce = str(payload["nonce"])
        expires = float(payload["exp"])
    except (JWTError, KeyError, TypeError, ValueError):
        raise invalid
    if payload.get("typ") != GOOGLE_OAUTH_STATE_TYP:
        raise invalid

    with _google_state_lock:
        now = time.time()
        for used, used_until in list(_used_google_state_nonces.items()):
            if used_until < now:
                del _used_google_state_nonces[used]
        if nonce in _used_google_state_nonces:
            raise invalid
        _used_google_state_nonces[nonce] = expires
    return user_id


@router.get("/google/connect")
def google_connect(user: User = Depends(get_current_user)):
    """
    Return the Google OAuth2 authorization URL.
    Frontend opens this URL in a popup window.
    """
    if not settings.GOOGLE_CLIENT_ID or not settings.GOOGLE_CLIENT_SECRET:
        raise HTTPException(status_code=501, detail="Google OAuth is not configured on this server")

    from app.services.google_calendar_service import build_oauth_url

    # Dedicated short-lived state token (see _create_google_oauth_state)
    state_token = _create_google_oauth_state(user.id)
    auth_url = build_oauth_url(state=state_token)

    return {"url": auth_url}


@router.get("/google/callback")
def google_callback(code: str, state: str, db: Session = Depends(get_db)):
    """
    Google redirects here after user authorizes.
    Exchanges the code for tokens, stores them on the user, and returns
    an HTML page that notifies the opener window and closes itself.
    """
    from app.services.google_calendar_service import exchange_code_for_tokens

    # Verify state: only a google_oauth_state token, each usable once
    user_id = _consume_google_oauth_state(state)

    user = db.query(User).filter(User.id == user_id, User.is_active == True).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    try:
        tokens = exchange_code_for_tokens(code)
    except Exception as e:
        logger.error("Google OAuth token exchange failed: %s", e)
        raise HTTPException(status_code=400, detail="Failed to connect Google account. Please try again.")

    user.google_access_token = tokens["access_token"]
    user.google_refresh_token = tokens["refresh_token"]
    user.google_email = tokens["email"]
    user.google_connected_at = datetime.utcnow()
    user.updated_at = datetime.utcnow()
    db.commit()

    logger.info("Google account connected for user %s: %s", user.email, tokens["email"])

    # Return HTML that notifies the opener window and closes the popup
    from fastapi.responses import HTMLResponse
    return HTMLResponse("""
    <!DOCTYPE html>
    <html>
    <head><title>Google Connected</title></head>
    <body style="display:flex;align-items:center;justify-content:center;height:100vh;font-family:system-ui;background:#f9fafb;">
      <div style="text-align:center;padding:40px;background:white;border-radius:12px;box-shadow:0 4px 12px rgba(0,0,0,0.1);">
        <div style="font-size:48px;margin-bottom:16px;">&#10003;</div>
        <h2 style="color:#16a34a;margin:0 0 8px;">Google Account Connected!</h2>
        <p style="color:#6b7280;margin:0;">You can close this window now.</p>
      </div>
      <script>
        if (window.opener) {
          window.opener.postMessage({ type: "google-connected" }, "*");
        }
        setTimeout(() => window.close(), 2000);
      </script>
    </body>
    </html>
    """)


@router.post("/google/disconnect")
def google_disconnect(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Disconnect the user's Google account by clearing stored tokens."""
    # Optionally revoke the token with Google
    if user.google_access_token:
        try:
            import requests as req
            req.post("https://oauth2.googleapis.com/revoke", params={"token": user.google_access_token})
        except Exception:
            pass  # Best-effort revocation

    user.google_access_token = None
    user.google_refresh_token = None
    user.google_email = None
    user.google_connected_at = None
    user.updated_at = datetime.utcnow()
    db.commit()

    logger.info("Google account disconnected for user %s", user.email)
    return {"message": "Google account disconnected"}
