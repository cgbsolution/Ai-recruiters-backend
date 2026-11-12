"""
AskMira's platform sets a company up here (AskMira → Platform → Connected tools).

AskMira's super admin holds this tool's platform key, the same value as
ASKMIRA_PLATFORM_SECRET here. With it — never with a workspace's own secret —
AskMira may:

  POST /integrations/askmira/platform/ping           check the key
  POST /integrations/askmira/platform/organizations  find an organization here
  POST /integrations/askmira/platform/provision      create or attach one

Each carries an HS256 token (aud "askmira:interview", iss "askmira", sub and
use "platform", `bh` = SHA-256 of the body). `provision` creates an organization
with AskMira's admin as its admin (they sign in from AskMira, or set a password
with "forgot password"), or attaches the organization the super admin picked,
and answers with that organization's own secret, exactly as Allow does
(app.routers.askmira), so every later call is checked with it.

It never makes a second organization for someone who already has an account
here, or for a domain that already has one, unasked: it answers 409 "existing"
naming them, and the super admin links that one or says "create_new".
"""
import hashlib
import hmac
import logging
import secrets
from typing import Optional
from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from jose import JWTError, jwt
from sqlalchemy import func, or_
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.config import settings
from app.database import SessionLocal
from app.models.askmira import AskmiraOrgConnection
from app.models.organization import Organization
from app.models.user import User, UserRole
from app.routers.askmira import (
    AUDIENCE,
    ISSUER,
    _DOMAIN,
    _UUID,
    _derive,
    _free_domain,
    _now,
    _spend_once,
    _unusable_password,
)

logger = logging.getLogger(__name__)

router = APIRouter()

PRODUCT = "interview"
BRAND = "AnytimeHire"


class Refused(Exception):
    """A refusal AskMira acts on: its `code` and what it names travel beside the message."""

    def __init__(self, status: int, detail: str, **extra):
        self.status, self.detail, self.extra = status, detail, extra


def _refusal(exc: Refused) -> JSONResponse:
    return JSONResponse(status_code=exc.status, content={"detail": exc.detail, **exc.extra})


async def _platform_claims(request: Request) -> tuple[dict, bytes]:
    key = settings.ASKMIRA_PLATFORM_SECRET or ""
    if len(key) < 32:
        # Off: as if these endpoints did not exist.
        raise HTTPException(status_code=404, detail="Not Found")
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing AskMira assertion.")
    body = await request.body()
    try:
        claims = jwt.decode(header[7:].strip(), key, algorithms=["HS256"], audience=AUDIENCE, issuer=ISSUER)
    except JWTError:
        raise HTTPException(status_code=401, detail="This AskMira platform key is not the one set here.")
    if claims.get("use") != "platform" or claims.get("sub") != "platform":
        raise HTTPException(status_code=401, detail="This AskMira platform key is not the one set here.")
    if not hmac.compare_digest(str(claims.get("bh", "")), hashlib.sha256(body).hexdigest()):
        raise HTTPException(status_code=401, detail="The assertion does not match this request.")
    return claims, body


def _json(body: bytes) -> dict:
    import json

    try:
        data = json.loads(body or b"{}")
    except ValueError:
        raise HTTPException(status_code=400, detail="The request is not JSON.")
    if not isinstance(data, dict):
        raise HTTPException(status_code=400, detail="The request is not a JSON object.")
    return data


def _clean(value, limit: int = 200) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()[:limit]
    return text or None


def _describe(db: Session, orgs: list[Organization]) -> list[dict]:
    out = []
    for org in orgs:
        admins = (
            db.query(User.email)
            .filter(User.organization_id == org.id, User.role == UserRole.admin, User.is_active == True)  # noqa: E712
            .order_by(User.id)
            .limit(5)
            .all()
        )
        out.append({
            "ref": str(org.id),
            "name": org.name,
            "admins": [email.lower() for (email,) in admins],
            "connected_to": org.askmira_tenant_id,
        })
    return out


def _search(q: str) -> dict:
    db = SessionLocal()
    try:
        like = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        by_person = (
            db.query(User.organization_id)
            .filter(func.lower(User.email).like(like.lower(), escape="\\"), User.organization_id.isnot(None))
            .limit(20)
            .subquery()
        )
        orgs = (
            db.query(Organization)
            .filter(
                or_(
                    Organization.name.ilike(like, escape="\\"),
                    Organization.domain.ilike(like, escape="\\"),
                    Organization.id.in_(by_person),
                )
            )
            .order_by(Organization.name)
            .limit(20)
            .all()
        )
        return {"organizations": _describe(db, orgs)}
    finally:
        db.close()


def _connect(db: Session, org: Organization, tenant_id: str, name: Optional[str], origin: Optional[str]) -> str:
    """Connect the organization to that AskMira workspace: a fresh nonce, so a fresh secret."""
    other = (
        db.query(Organization)
        .filter(Organization.askmira_tenant_id == tenant_id, Organization.id != org.id)
        .first()
    )
    if other is not None:
        # One AskMira workspace per organization here: the old link goes.
        row = db.get(AskmiraOrgConnection, other.id)
        if row is not None:
            db.delete(row)
        other.askmira_tenant_id = None
        db.flush()
    nonce = secrets.token_urlsafe(32)
    connection = db.get(AskmiraOrgConnection, org.id) or AskmiraOrgConnection(organization_id=org.id)
    connection.secret_nonce = nonce
    connection.askmira_name = name
    connection.askmira_origin = origin
    connection.connected_at = _now()
    connection.connected_by_user_id = None
    db.add(connection)
    org.askmira_tenant_id = tenant_id
    db.commit()
    return _derive(org.id, nonce)


def _answer(org: Organization, secret: str, created: bool, admin_created: bool) -> dict:
    return {
        "product": PRODUCT,
        "brand": BRAND,
        "secret": secret,
        "external_ref": str(org.id),
        "organization": org.name,
        "created": created,
        "admin_created": admin_created,
    }


def _origin(value) -> Optional[str]:
    parsed = urlparse(str(value or ""))
    return f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme in ("https", "http") and parsed.netloc else None


def _provision(data: dict, claims: dict) -> dict:
    tenant_id = str(data.get("tenant_id") or "").lower()
    company = data.get("company") if isinstance(data.get("company"), dict) else {}
    admin = data.get("admin") if isinstance(data.get("admin"), dict) else {}
    name = _clean(company.get("name")) or _clean(data.get("tenant_name"))
    email = str(admin.get("email") or "").strip().lower()
    ref = _clean(data.get("organization_ref"), 120)
    if not _UUID.match(tenant_id) or not name or "@" not in email or len(email) > 255:
        raise HTTPException(status_code=422, detail="AskMira sent an incomplete company.")
    askmira_name = _clean(data.get("tenant_name")) or name
    origin = _origin(data.get("askmira_origin"))
    domain = str(company.get("domain") or "").strip().lower()
    domain = domain if _DOMAIN.match(domain) else ""
    slug = _clean(company.get("slug"), 60) or "workspace"

    db = SessionLocal()
    try:
        if settings.secret_key_is_weak:
            raise Refused(409, "Set a strong SECRET_KEY on this server first.")
        # A captured request can't be sent again to swap the key AskMira holds.
        _spend_once(db, claims)

        # Attach the organization the super admin picked.
        if ref:
            org = db.get(Organization, int(ref)) if ref.isdigit() else None
            if org is None:
                raise HTTPException(status_code=404, detail="No such organization here.")
            if org.askmira_tenant_id and org.askmira_tenant_id != tenant_id:
                raise Refused(409, f"{org.name} is connected to another AskMira workspace.", code="taken")
            if not org.is_active:
                raise Refused(409, f"{org.name} is switched off here.", code="inactive")
            secret = _connect(db, org, tenant_id, askmira_name, origin)
            logger.info("[AskMira] platform attached org %s to workspace %s", org.id, tenant_id)
            return _answer(org, secret, False, False)

        # Already connected here (AskMira lost its key, or asked twice): the same org, a new key.
        org = db.query(Organization).filter(Organization.askmira_tenant_id == tenant_id).first()
        if org is not None:
            return _answer(org, _connect(db, org, tenant_id, askmira_name, origin), False, False)

        user = db.query(User).filter(func.lower(User.email) == email).first()
        if data.get("create_new") is not True:
            mine: list[Organization] = []
            if user is not None and user.organization_id:
                found = db.get(Organization, user.organization_id)
                if found is not None:
                    mine.append(found)
            if domain:
                for found in db.query(Organization).filter(Organization.domain == domain).all():
                    if found not in mine:
                        mine.append(found)
            if mine:
                who = f"{email} already has an account" if user is not None else f"{domain} already has an organization"
                raise Refused(409, f"{who} in {BRAND}.", code="existing", organizations=_describe(db, mine))

        org = Organization(
            name=name[:255],
            # No real domain known: a placeholder no email can match, so
            # self-signup never lands strangers in this organization.
            domain=_free_domain(db, domain or f"{slug}.askmira.invalid"),
            is_active=True,
        )
        db.add(org)
        db.flush()
        admin_created = False
        if user is None:
            db.add(User(
                name=(_clean(admin.get("name"), 150) or email),
                email=email,
                password_hash=_unusable_password(),
                role=UserRole.admin,
                organization_id=org.id,
                is_active=True,
                is_email_verified=True,
                askmira_managed=True,
            ))
            admin_created = True
        secret = _connect(db, org, tenant_id, askmira_name, origin)
        logger.info(
            "[AskMira] platform created org %s for workspace %s, admin %s%s",
            org.id, tenant_id, email, "" if admin_created else " (has an account in another organization)",
        )
        return _answer(org, secret, True, admin_created)
    finally:
        db.close()


@router.post("/platform/{action}")
async def platform(action: str, request: Request):
    claims, body = await _platform_claims(request)
    data = _json(body)
    try:
        if action == "ping":
            ready = not settings.secret_key_is_weak
            return {
                "product": PRODUCT,
                "brand": BRAND,
                "ready": ready,
                "message": f"{BRAND} is ready." if ready else "Set a strong SECRET_KEY on this server first.",
            }
        if action == "organizations":
            q = _clean(data.get("q"), 120)
            if not q or len(q) < 2:
                raise HTTPException(status_code=422, detail="Type at least two letters.")
            return await run_in_threadpool(_search, q)
        if action == "provision":
            return await run_in_threadpool(_provision, data, claims)
    except Refused as exc:
        return _refusal(exc)
    raise HTTPException(status_code=404, detail="Not Found")
