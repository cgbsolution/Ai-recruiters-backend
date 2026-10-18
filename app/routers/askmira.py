"""
The link with AskMira, the HRMS a company may already run its people in.

AskMira is the source of truth for a linked company and its people. It calls
here server to server to create (or adopt) the company's organization and to
send who should have access; and it sends people here already signed in, with
a one-time link, so they never need a second password.

Every request carries a short-lived HS256 token signed with
ASKMIRA_SHARED_SECRET (aud "askmira:interview", iss "askmira"):

  POST /integrations/askmira/organizations   use=provision, `bh` = SHA-256 of the body
  POST /integrations/askmira/users/sync      use=provision, `bh` = SHA-256 of the body
  POST /auth/askmira/exchange                use=launch; each `jti` is accepted once

Users AskMira manages carry `askmira_user_id`. Only they are switched off when
they drop out of AskMira's roster; accounts made here are never touched.
"""
import hashlib
import hmac
import logging
import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from jose import JWTError, jwt
from pydantic import BaseModel, EmailStr, ValidationError
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.auth import create_access_token, hash_password
from app.config import settings
from app.database import SessionLocal, get_db
from app.models.askmira import AskmiraLaunch
from app.models.organization import Organization
from app.models.user import User, UserRole

logger = logging.getLogger(__name__)

integration_router = APIRouter()
auth_router = APIRouter()

AUDIENCE = "askmira:interview"
ISSUER = "askmira"
ROLES = {"admin": UserRole.admin, "recruiter": UserRole.recruiter}
_DOMAIN = re.compile(r"^[a-z0-9.-]+\.[a-z]{2,}$")


# ── Token checks ─────────────────────────────────────────────────────────────

def _secret() -> str:
    secret = settings.ASKMIRA_SHARED_SECRET or ""
    if len(secret) < 32:
        # Off (or misconfigured): behave as if the endpoints did not exist.
        raise HTTPException(status_code=404, detail="Not Found")
    return secret


def _claims(token: str, use: str) -> dict:
    try:
        claims = jwt.decode(
            token, _secret(), algorithms=["HS256"], audience=AUDIENCE, issuer=ISSUER,
        )
    except JWTError:
        raise HTTPException(status_code=401, detail="This AskMira link is not valid or has expired.")
    if claims.get("use") != use:
        raise HTTPException(status_code=401, detail="This AskMira link is not valid here.")
    return claims


async def _provision_claims(request: Request) -> tuple[dict, bytes]:
    """AskMira's server-to-server assertion, bound to this exact body."""
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("bearer "):
        _secret()
        raise HTTPException(status_code=401, detail="Missing AskMira assertion.")
    body = await request.body()
    claims = _claims(header[7:].strip(), "provision")
    if not hmac.compare_digest(str(claims.get("bh", "")), hashlib.sha256(body).hexdigest()):
        raise HTTPException(status_code=401, detail="The assertion does not match this request.")
    return claims, body


# ── Organizations ────────────────────────────────────────────────────────────

class CompanyIn(BaseModel):
    tenant_id: str
    name: str
    slug: str
    domain: Optional[str] = None


def _free_domain(db: Session, wanted: str) -> str:
    domain, n = wanted, 2
    while db.query(Organization.id).filter(Organization.domain == domain).first():
        domain = f"{n}.{wanted}"
        n += 1
    return domain


def _upsert_org(company: CompanyIn) -> dict:
    db = SessionLocal()
    try:
        org = db.query(Organization).filter(Organization.askmira_tenant_id == company.tenant_id).first()
        adopted = False
        if org is None and company.domain:
            # The company may already use this tool under its own domain: adopt
            # that organization rather than make a second one.
            existing = db.query(Organization).filter(Organization.domain == company.domain).first()
            if existing is not None:
                if existing.askmira_tenant_id and existing.askmira_tenant_id != company.tenant_id:
                    raise HTTPException(
                        status_code=409,
                        detail="That domain's organization is linked to another AskMira workspace.",
                    )
                org, adopted = existing, True
        if org is None:
            # No real domain known: a placeholder that no email can match, so
            # self-signup never lands strangers in this organization.
            org = Organization(
                name=company.name.strip()[:255],
                domain=_free_domain(db, company.domain or f"{company.slug}.askmira.invalid"),
                is_active=True,
            )
            db.add(org)
        org.askmira_tenant_id = company.tenant_id
        db.commit()
        db.refresh(org)
        logger.info(
            "[AskMira] organization %s %s for tenant %s",
            org.id, "adopted" if adopted else "linked", company.tenant_id,
        )
        return {"external_ref": str(org.id), "adopted": adopted, "domain": org.domain}
    finally:
        db.close()


@integration_router.post("/organizations")
async def link_organization(request: Request):
    claims, body = await _provision_claims(request)
    try:
        company = CompanyIn.model_validate_json(body)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.errors()[0].get("msg", "Invalid body"))
    if company.tenant_id != claims.get("sub"):
        raise HTTPException(status_code=401, detail="The assertion is for another workspace.")
    if company.domain and not _DOMAIN.match(company.domain.lower()):
        company.domain = None
    elif company.domain:
        company.domain = company.domain.lower()
    return await run_in_threadpool(_upsert_org, company)


# ── People ───────────────────────────────────────────────────────────────────

class PersonIn(BaseModel):
    id: str
    email: EmailStr
    name: str
    role: str


class RosterIn(BaseModel):
    tenant_id: str
    complete: bool = True
    users: list[PersonIn]


def _unusable_password() -> str:
    # Signs in through AskMira only, until they set a password via "forgot password".
    return hash_password(secrets.token_urlsafe(32)[:48])


def _apply_roster(roster: RosterIn) -> dict:
    db = SessionLocal()
    try:
        org = db.query(Organization).filter(Organization.askmira_tenant_id == roster.tenant_id).first()
        if org is None:
            raise HTTPException(status_code=404, detail="This workspace is not linked here yet.")
        created = updated = deactivated = 0
        skipped: list[dict] = []
        kept: set[str] = set()
        for person in roster.users:
            email = person.email.lower()
            role = ROLES.get(person.role)
            if role is None:
                skipped.append({"email": email, "reason": f"unknown role {person.role!r}"})
                continue
            user = db.query(User).filter(User.email == email).first()
            if user is None:
                db.add(User(
                    name=person.name.strip()[:150] or email,
                    email=email,
                    password_hash=_unusable_password(),
                    role=role,
                    organization_id=org.id,
                    is_active=True,
                    is_email_verified=True,
                    askmira_user_id=person.id,
                ))
                created += 1
                kept.add(email)
                continue
            if user.organization_id != org.id:
                skipped.append({"email": email, "reason": "belongs to another organization here"})
                continue
            if user.role == UserRole.super_admin:
                skipped.append({"email": email, "reason": "a platform super admin here"})
                continue
            kept.add(email)
            changed = False
            for field, value in (
                ("name", person.name.strip()[:150] or user.name),
                ("role", role),
                ("askmira_user_id", person.id),
                ("is_active", True),
            ):
                if getattr(user, field) != value:
                    setattr(user, field, value)
                    changed = True
            if changed:
                updated += 1
        if roster.complete:
            leavers = db.query(User).filter(
                User.organization_id == org.id,
                User.askmira_user_id.isnot(None),
                User.is_active == True,  # noqa: E712
            ).all()
            for user in leavers:
                if user.email not in kept:
                    user.is_active = False
                    deactivated += 1
        db.commit()
        logger.info(
            "[AskMira] roster for org %s: %s created, %s updated, %s deactivated, %s skipped",
            org.id, created, updated, deactivated, len(skipped),
        )
        return {"created": created, "updated": updated, "deactivated": deactivated, "skipped": skipped}
    finally:
        db.close()


@integration_router.post("/users/sync")
async def sync_users(request: Request):
    claims, body = await _provision_claims(request)
    try:
        roster = RosterIn.model_validate_json(body)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.errors()[0].get("msg", "Invalid body"))
    if roster.tenant_id != claims.get("sub"):
        raise HTTPException(status_code=401, detail="The assertion is for another workspace.")
    return await run_in_threadpool(_apply_roster, roster)


# ── Sign-in from AskMira ─────────────────────────────────────────────────────

class ExchangeIn(BaseModel):
    token: str


@auth_router.post("/exchange")
def exchange(body: ExchangeIn, db: Session = Depends(get_db)):
    """Trade a one-time AskMira launch link for a normal session here."""
    claims = _claims(body.token, "launch")
    jti, exp = claims.get("jti"), claims.get("exp")
    if not jti or not isinstance(exp, (int, float)):
        raise HTTPException(status_code=401, detail="This AskMira link is not valid.")

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    db.query(AskmiraLaunch).filter(AskmiraLaunch.expires_at < now - timedelta(minutes=5)).delete()
    db.add(AskmiraLaunch(jti=str(jti)[:64], expires_at=datetime.fromtimestamp(exp, timezone.utc).replace(tzinfo=None)))
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=401, detail="This sign-in link was already used. Open it again from AskMira.")

    org = db.query(Organization).filter(Organization.askmira_tenant_id == claims.get("tid")).first()
    if org is None or (claims.get("ext") and str(claims["ext"]) != str(org.id)):
        raise HTTPException(status_code=403, detail="Your company is not linked to this tool.")
    if not org.is_active:
        raise HTTPException(status_code=403, detail="Your organization's account is suspended. Contact support.")

    email = str(claims.get("email", "")).lower()
    role = ROLES.get(str(claims.get("role")))
    if not email or role is None:
        raise HTTPException(status_code=403, detail="AskMira did not give you a role in this tool.")

    user = db.query(User).filter(User.email == email).first()
    if user is None:
        # Joined since the last sync: let them in now; the next sync confirms it.
        user = User(
            name=str(claims.get("name") or email)[:150],
            email=email,
            password_hash=_unusable_password(),
            role=role,
            organization_id=org.id,
            is_active=True,
            is_email_verified=True,
            askmira_user_id=str(claims.get("sub")),
        )
        db.add(user)
    else:
        if user.organization_id != org.id or user.role == UserRole.super_admin:
            raise HTTPException(
                status_code=409,
                detail="This email already has an account with another organization here.",
            )
        if not user.is_active:
            raise HTTPException(status_code=403, detail="Your account here has been switched off.")
        user.role = role
        user.askmira_user_id = str(claims.get("sub"))
    db.commit()
    db.refresh(user)

    logger.info("[AskMira] %s signed in from AskMira (org %s)", user.email, org.id)
    return {
        "access_token": create_access_token(
            user.id, user.role.value, org_id=user.organization_id, password_hash=user.password_hash
        ),
        "token_type": "bearer",
        "expires_in": settings.SESSION_TIMEOUT_MINUTES * 60,
        "user": {
            "id": user.id,
            "name": user.name,
            "email": user.email,
            "role": user.role.value,
            "is_email_verified": user.is_email_verified,
            "organization_id": user.organization_id,
            "organization_name": org.name,
        },
    }
