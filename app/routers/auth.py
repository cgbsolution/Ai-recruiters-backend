"""
Authentication router — signup, login, me, change-password, email verification, user management.
"""
import secrets
import logging
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, EmailStr
from sqlalchemy.orm import Session

from app.auth import (
    create_access_token,
    get_current_user,
    get_org_id,
    hash_password,
    require_roles,
    verify_password,
)
from app.database import get_db
from app.models.organization import Organization
from app.models.user import User, UserRole

logger = logging.getLogger(__name__)

router = APIRouter()


# ── Schemas ──────────────────────────────────────────────────────────────────

class SignUpRequest(BaseModel):
    name: str
    email: EmailStr
    password: str
    role: UserRole = UserRole.recruiter


class SignInRequest(BaseModel):
    email: EmailStr
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: dict


class ChangePasswordRequest(BaseModel):
    current_password: str
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
def signup(body: SignUpRequest, db: Session = Depends(get_db)):
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
def login(body: SignInRequest, db: Session = Depends(get_db)):
    """Authenticate and return a JWT token."""
    user = db.query(User).filter(User.email == body.email.lower()).first()
    if not user or not verify_password(body.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Invalid email or password")

    if not user.is_active:
        raise HTTPException(
            status_code=403,
            detail="Your account is pending approval by your organization admin.",
        )

    token = create_access_token(user.id, user.role.value, org_id=user.organization_id)
    logger.info("User logged in: %s", user.email)

    # Fetch org name if user belongs to one
    org_name = None
    if user.organization_id:
        org = db.query(Organization).filter(Organization.id == user.organization_id).first()
        org_name = org.name if org else None

    return TokenResponse(
        access_token=token,
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
    if user.organization_id:
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
        # Admins see only users in their own organization (recruiters + admins)
        q = q.filter(
            User.organization_id == user.organization_id,
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
    body: SignUpRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles(UserRole.super_admin, UserRole.admin)),
):
    """Create a new user with a specific role. Admin/Super Admin only."""
    existing = db.query(User).filter(User.email == body.email.lower()).first()
    if existing:
        raise HTTPException(status_code=409, detail="Email already registered")

    # Admins cannot create super_admin users
    if current_user.role == UserRole.admin and body.role == UserRole.super_admin:
        raise HTTPException(status_code=403, detail="Admins cannot create super admin users")

    # Only super_admin can create super_admin
    if body.role == UserRole.super_admin and current_user.role != UserRole.super_admin:
        raise HTTPException(status_code=403, detail="Only super admins can create super admin users")

    # Determine organization: admin creates in own org, super_admin can specify via email domain
    org_id = None
    if current_user.role == UserRole.admin:
        org_id = current_user.organization_id
    else:
        # Super admin: match org by email domain
        email_domain = body.email.lower().split("@")[1]
        org = db.query(Organization).filter(Organization.domain == email_domain).first()
        if org:
            org_id = org.id

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
        "organization_name": org.name if org_id and current_user.role == UserRole.super_admin and org else None,
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

    # Admin can only modify users in their own organization
    if current_user.role == UserRole.admin and target.organization_id != current_user.organization_id:
        raise HTTPException(status_code=403, detail="Cannot modify users outside your organization")

    # Cannot modify a super_admin unless you are super_admin
    if target.role == UserRole.super_admin and current_user.role != UserRole.super_admin:
        raise HTTPException(status_code=403, detail="Cannot modify super admin users")

    # Admins cannot promote to super_admin
    if body.role and current_user.role == UserRole.admin and body.role == UserRole.super_admin:
        raise HTTPException(status_code=403, detail="Admins cannot assign super admin role")

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

    # Admin can only delete users in their own organization
    if current_user.role == UserRole.admin and target.organization_id != current_user.organization_id:
        raise HTTPException(status_code=403, detail="Cannot delete users outside your organization")

    # Cannot delete a super_admin unless you are super_admin
    if target.role == UserRole.super_admin and current_user.role != UserRole.super_admin:
        raise HTTPException(status_code=403, detail="Cannot delete super admin users")

    db.delete(target)
    db.commit()

    logger.info("User deleted by %s: %s (id=%d)", current_user.email, target.email, user_id)
