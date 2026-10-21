"""
The link with AskMira, the HRMS a company may already run its people in.

AskMira is the source of truth for a linked company and its people. It calls
here server to server to create (or adopt) the company's organization and to
send who should have access; and it sends people here already signed in, with
a one-time link, so they never need a second password.

Every request carries a short-lived HS256 token signed with the shared secret
(aud "askmira:interview", iss "askmira"). The secret comes from the connection
the platform super admin made with a connection key ("Connection key" below),
or else from ASKMIRA_SHARED_SECRET:

  POST /integrations/askmira/organizations   use=provision, `bh` = SHA-256 of the body
  POST /integrations/askmira/users/sync      use=provision, `bh` = SHA-256 of the body
  POST /integrations/askmira/query           use=query, `bh` = SHA-256 of the body
  POST /integrations/askmira/handshake       use=handshake, `bh`; accepts a pending key
  POST /auth/askmira/exchange                use=launch; each `jti` is accepted once

`query` is read-only: AskMira's companion asks it on behalf of one person (see
"Read-only queries" below).

Users AskMira manages carry `askmira_user_id`. Only they are switched off when
they drop out of AskMira's roster; accounts made here are never touched.
"""
import base64
import hashlib
import hmac
import json
import logging
import re
import secrets
import time
from urllib.parse import urlparse
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from jose import JWTError, jwt
from pydantic import BaseModel, EmailStr, Field, ValidationError
from sqlalchemy import case, func, or_
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.auth import create_access_token, get_current_user, hash_password, is_global_admin
from app.config import settings
from app.database import SessionLocal, get_db
from app.models.askmira import AskmiraConnection, AskmiraLaunch
from app.models.candidate import Candidate, CandidateStatus
from app.models.interview import InterviewSession, SessionStatus
from app.models.job import Job, JobStatus
from app.models.organization import Organization
from app.models.user import User, UserRole
from app.refs import encode_ref

logger = logging.getLogger(__name__)

integration_router = APIRouter()
auth_router = APIRouter()

AUDIENCE = "askmira:interview"
ISSUER = "askmira"
ROLES = {"admin": UserRole.admin, "recruiter": UserRole.recruiter}
_DOMAIN = re.compile(r"^[a-z0-9.-]+\.[a-z]{2,}$")


# ── Token checks ─────────────────────────────────────────────────────────────

# ── Connection key ───────────────────────────────────────────────────────────
#
# The platform super admin connects AskMira from the app, with no .env edit:
# "Generate connection key" stores a random nonce as pending and hands out
#
#     amk1.<base64url JSON {"p": "interview", "api", "app", "s": secret, "exp"}>
#
# where the secret is derived from SECRET_KEY and the nonce, so the database
# never holds it. AskMira's super admin pastes the key there; AskMira proves it
# holds the secret with a signed handshake, and the pending nonce becomes the
# active one. A new key replaces the connection only once its handshake lands.

KEY_PREFIX = "amk1."
KEY_LIFETIME = timedelta(minutes=30)
_CONNECTION_ID = 1
#: The active secret is looked up at most this often per process.
_CACHE_SECONDS = 30.0
_cache: dict = {"at": float("-inf"), "secret": None}


def _derive(nonce: str) -> str:
    message = f"askmira-connection:v1:{nonce}".encode()
    return hmac.new((settings.SECRET_KEY or "").encode(), message, hashlib.sha256).hexdigest()


def _forget() -> None:
    _cache.update(at=float("-inf"), secret=None)


def _connected_secret() -> Optional[str]:
    """The secret of the connection made with a key, if there is one."""
    now = time.monotonic()
    if now - _cache["at"] < _CACHE_SECONDS:
        return _cache["secret"]
    db = SessionLocal()
    try:
        row = db.get(AskmiraConnection, _CONNECTION_ID)
        secret = _derive(row.active_nonce) if row is not None and row.active_nonce else None
    except SQLAlchemyError:
        # Before the migration has run: no connection made here.
        secret = None
    finally:
        db.close()
    _cache.update(at=now, secret=secret)
    return secret


def _secret() -> str:
    secret = _connected_secret() or settings.ASKMIRA_SHARED_SECRET or ""
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


async def _provision_claims(request: Request, use: str = "provision") -> tuple[dict, bytes]:
    """AskMira's server-to-server assertion, bound to this exact body."""
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("bearer "):
        _secret()
        raise HTTPException(status_code=401, detail="Missing AskMira assertion.")
    body = await request.body()
    claims = _claims(header[7:].strip(), use)
    if not hmac.compare_digest(str(claims.get("bh", "")), hashlib.sha256(body).hexdigest()):
        raise HTTPException(status_code=401, detail="The assertion does not match this request.")
    return claims, body


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _iso_or_none(value: Optional[datetime]) -> Optional[str]:
    return value.replace(tzinfo=timezone.utc).isoformat() if value else None


def _platform_admin(user: User = Depends(get_current_user)) -> User:
    if not is_global_admin(user):
        raise HTTPException(status_code=403, detail="Only the platform super admin connects AskMira.")
    return user


def _address(value: str, what: str) -> str:
    text = (value or "").strip().rstrip("/")
    parsed = urlparse(text)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise HTTPException(status_code=422, detail=f"The {what} must be a web address.")
    if parsed.scheme == "http" and parsed.hostname not in ("localhost", "127.0.0.1"):
        raise HTTPException(status_code=422, detail=f"The {what} must use https.")
    return text


def _connection_read(row: Optional[AskmiraConnection]) -> dict:
    by_key = row is not None and bool(row.active_nonce)
    by_env = len(settings.ASKMIRA_SHARED_SECRET or "") >= 32
    pending = bool(
        row is not None and row.pending_nonce and row.pending_expires_at
        and row.pending_expires_at > _now()
    )
    return {
        "connected": by_key or by_env,
        "source": "key" if by_key else "environment" if by_env else None,
        "connected_at": _iso_or_none(row.connected_at) if by_key else None,
        "pending_until": _iso_or_none(row.pending_expires_at) if pending else None,
    }


class ConnectionKeyIn(BaseModel):
    #: This server's API, as AskMira will call it.
    api_url: str = Field(min_length=8, max_length=500)
    #: This product's web app, where AskMira sends people signed in.
    app_url: str = Field(min_length=8, max_length=500)


@integration_router.get("/connection")
def connection_status(_: User = Depends(_platform_admin), db: Session = Depends(get_db)):
    """Whether AskMira is connected to this server, and how."""
    return _connection_read(db.get(AskmiraConnection, _CONNECTION_ID))


@integration_router.post("/connection-key")
def generate_connection_key(
    payload: ConnectionKeyIn, user: User = Depends(_platform_admin), db: Session = Depends(get_db),
):
    """A key, good for 30 minutes, for AskMira's super admin to paste in its console."""
    if settings.secret_key_is_weak:
        raise HTTPException(
            status_code=409,
            detail="Set a strong SECRET_KEY on this server before connecting AskMira.",
        )
    api_url = _address(payload.api_url, "server address")
    app_url = _address(payload.app_url, "app address")
    nonce = secrets.token_urlsafe(32)
    expires = _now() + KEY_LIFETIME
    row = db.get(AskmiraConnection, _CONNECTION_ID)
    if row is None:
        row = AskmiraConnection(id=_CONNECTION_ID)
        db.add(row)
    row.pending_nonce = nonce
    row.pending_expires_at = expires
    row.generated_by_user_id = user.id
    db.commit()
    body = {
        "p": "interview",
        "api": api_url,
        "app": app_url,
        "s": _derive(nonce),
        "exp": int(expires.replace(tzinfo=timezone.utc).timestamp()),
    }
    encoded = base64.urlsafe_b64encode(json.dumps(body, separators=(",", ":")).encode()).decode()
    logger.info("[AskMira] connection key generated by user %s", user.id)
    return {"key": KEY_PREFIX + encoded.rstrip("="), "expires_at": _iso_or_none(expires)}


@integration_router.delete("/connection")
def disconnect(user: User = Depends(_platform_admin), db: Session = Depends(get_db)):
    """Forget the connection made with a key. ASKMIRA_SHARED_SECRET, if set, takes over."""
    row = db.get(AskmiraConnection, _CONNECTION_ID)
    if row is not None:
        row.active_nonce = None
        row.connected_at = None
        row.pending_nonce = None
        row.pending_expires_at = None
        db.commit()
    _forget()
    logger.info("[AskMira] connection removed by user %s", user.id)
    return _connection_read(row)


def _handshake(token: str, body: bytes) -> dict:
    db = SessionLocal()
    try:
        row = db.get(AskmiraConnection, _CONNECTION_ID)
        candidates: list[tuple[str, str]] = []
        if (
            row is not None and row.pending_nonce and row.pending_expires_at
            and row.pending_expires_at > _now()
        ):
            candidates.append(("pending", _derive(row.pending_nonce)))
        _forget()
        current = _connected_secret() or settings.ASKMIRA_SHARED_SECRET or ""
        if len(current) >= 32:
            candidates.append(("current", current))
        if not candidates:
            raise HTTPException(status_code=404, detail="Not Found")
        for kind, secret in candidates:
            try:
                claims = jwt.decode(token, secret, algorithms=["HS256"], audience=AUDIENCE, issuer=ISSUER)
            except JWTError:
                continue
            if claims.get("use") != "handshake" or not hmac.compare_digest(
                str(claims.get("bh", "")), hashlib.sha256(body).hexdigest()
            ):
                raise HTTPException(status_code=401, detail="The assertion does not match this request.")
            if kind == "pending":
                row.active_nonce = row.pending_nonce
                row.connected_at = _now()
                row.pending_nonce = None
                row.pending_expires_at = None
                db.commit()
                _forget()
                logger.info("[AskMira] connected with a connection key")
            return {"product": "interview", "brand": "AnytimeHire"}
        raise HTTPException(status_code=401, detail="This AskMira link is not valid or has expired.")
    finally:
        db.close()


@integration_router.post("/handshake")
async def handshake(request: Request):
    """AskMira proves it holds a connection key's secret; a pending key becomes active."""
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing AskMira assertion.")
    body = await request.body()
    return await run_in_threadpool(_handshake, header[7:].strip(), body)


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


# ── Read-only queries for AskMira's companion ───────────────────────────────
#
# Ask Mira, AskMira's AI companion, asks on behalf of one person and shows the
# answer to that person only; no model reads it. An answer covers only the
# organization linked to the workspace in the token. It is given only to
# someone who is an active admin or recruiter of that organization here AND
# whom AskMira gives one of those roles: the people who see the same data when
# they open this tool, where every admin and recruiter sees the whole
# organization.
#
# Never returned: contact details, resumes, AI scores, recommendations, fit
# scores or proctoring results. AskMira shows no AI score without the signals
# behind it and a confidence, and this tool keeps no confidence. A proctoring
# outcome is for a person to review here, so interview outcomes are given in
# neutral words ("Ended early"), never as a finding.

# Auto-created resume-upload placeholders, not real applicants (as in jobs.list_jobs).
_PLACEHOLDER = "pending_%@resume.local"
# Interviews that took place, whatever the ending (as in jobs.list_jobs).
_HELD = (
    "Completed", "Terminated Prematurely", "Terminated – Cheating Detected",
    "Ended by Candidate", "Candidate Terminated Prematurely",
)
_INTERVIEW_LABEL = {
    "New": "Not scheduled",
    "Applied": "Not scheduled",
    "Added from Database": "Not scheduled",
    "Scheduled": "Scheduled",
    "Completed": "Completed",
    "Cancelled": "Cancelled",
    "Ended by Candidate": "Ended by the candidate",
    "Candidate Dropped Interview": "Ended by the candidate",
    "Disconnected – Network Issue": "Disconnected",
    "Disconnected – Awaiting Rejoin": "Disconnected, may rejoin",
    "Candidate Terminated Prematurely": "Ended early",
    "Terminated Prematurely": "Ended early",
    "Terminated – Cheating Detected": "Ended early",
}
_NOT_SCHEDULED = ("New", "Applied", "Added from Database")
_JOB_STATUS = {status.value.lower(): status for status in JobStatus}
_CANDIDATE_STATUS = {status.value.lower(): status for status in CandidateStatus}
_MAX_WINDOW = timedelta(days=100)


class ActorIn(BaseModel):
    id: str
    email: EmailStr
    role: str


class QueryIn(BaseModel):
    tenant_id: str
    actor: ActorIn
    kind: str
    params: dict = Field(default_factory=dict)


def _interview_label(value: Optional[str]) -> str:
    if not value:
        return "Not scheduled"
    return _INTERVIEW_LABEL.get(value, "See the interview tool")


def _iso(value: Optional[datetime]) -> Optional[str]:
    """Stored times are naive UTC; say so."""
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc).isoformat()


def _limit(params: dict) -> int:
    try:
        return min(max(int(params.get("limit") or 25), 1), 50)
    except (TypeError, ValueError):
        return 25


def _search(params: dict) -> Optional[str]:
    """A LIKE pattern for the search text, with its wildcards taken literally."""
    text = str(params.get("query") or "").strip()[:100]
    if not text:
        return None
    text = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{text}%"


def _instant(value) -> datetime:
    try:
        moment = datetime.fromisoformat(str(value))
    except ValueError:
        raise HTTPException(status_code=422, detail="start and end must be ISO 8601 times.")
    if moment.tzinfo is not None:
        moment = moment.astimezone(timezone.utc).replace(tzinfo=None)
    return moment


def _window(params: dict) -> tuple[datetime, datetime]:
    start, end = _instant(params.get("start")), _instant(params.get("end"))
    if end <= start or end - start > _MAX_WINDOW:
        raise HTTPException(
            status_code=422, detail="The window must end after it starts, within 100 days."
        )
    return start, end


def _org_candidates(db: Session, org: Organization):
    """The organization's real applicants, each with their job's title."""
    return (
        db.query(Candidate, Job.title)
        .join(Job, Job.id == Candidate.job_id)
        .filter(Job.organization_id == org.id, ~Candidate.email.like(_PLACEHOLDER))
    )


def _q_overview(db: Session, org: Organization, params: dict) -> dict:
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    jobs = {
        (status.value if status else "Draft"): count
        for status, count in db.query(Job.status, func.count(Job.id))
        .filter(Job.organization_id == org.id)
        .group_by(Job.status)
    }
    open_positions = (
        db.query(func.coalesce(func.sum(Job.openings), 0))
        .filter(Job.organization_id == org.id, Job.status == JobStatus.open)
        .scalar()
    )
    in_org = (
        db.query(Candidate)
        .join(Job, Job.id == Candidate.job_id)
        .filter(Job.organization_id == org.id, ~Candidate.email.like(_PLACEHOLDER))
    )
    candidates = {
        (status.value if status else "Applied"): count
        for status, count in in_org.with_entities(Candidate.status, func.count(Candidate.id))
        .group_by(Candidate.status)
    }
    upcoming = in_org.filter(
        Candidate.interview_status == "Scheduled", Candidate.interview_date >= now
    ).count()
    held = (
        db.query(func.count(InterviewSession.id))
        .join(Job, Job.id == InterviewSession.job_id)
        .filter(
            Job.organization_id == org.id,
            InterviewSession.status == SessionStatus.completed,
            InterviewSession.completed_at >= now - timedelta(days=30),
        )
        .scalar()
    )
    return {
        "jobs": jobs,
        "open_positions": int(open_positions or 0),
        "candidates": candidates,
        "upcoming_interviews": upcoming,
        "completed_interviews_30d": int(held or 0),
    }


def _q_jobs(db: Session, org: Organization, params: dict) -> dict:
    query = db.query(Job).filter(Job.organization_id == org.id)
    status = _JOB_STATUS.get(str(params.get("status") or "").lower())
    if status is not None:
        query = query.filter(Job.status == status)
    pattern = _search(params)
    if pattern:
        query = query.filter(or_(
            Job.title.ilike(pattern, escape="\\"), Job.department.ilike(pattern, escape="\\"),
        ))
    total = query.count()
    jobs = query.order_by(Job.created_at.desc(), Job.id.desc()).limit(_limit(params)).all()
    counts: dict[int, tuple[int, int]] = {}
    if jobs:
        rows = (
            db.query(
                Candidate.job_id,
                func.count(Candidate.id),
                func.count(case((Candidate.interview_status.in_(_HELD), 1))),
            )
            .filter(
                Candidate.job_id.in_([job.id for job in jobs]),
                ~Candidate.email.like(_PLACEHOLDER),
            )
            .group_by(Candidate.job_id)
            .all()
        )
        counts = {job_id: (applicants, held) for job_id, applicants, held in rows}
    return {
        "total": total,
        "jobs": [
            {
                "title": job.title,
                "department": job.department,
                "location": job.location,
                "status": job.status.value if job.status else None,
                "openings": job.openings or 0,
                "applicants": counts.get(job.id, (0, 0))[0],
                "interviews_held": counts.get(job.id, (0, 0))[1],
                "deadline": job.deadline,
                "created_at": _iso(job.created_at),
                # Opaque, as in this app's own URLs: AskMira links to the job's candidates.
                "ref": encode_ref("job", job.id),
            }
            for job in jobs
        ],
    }


def _q_candidates(db: Session, org: Organization, params: dict) -> dict:
    query = _org_candidates(db, org)
    status = _CANDIDATE_STATUS.get(str(params.get("status") or "").lower())
    if status is not None:
        query = query.filter(Candidate.status == status)
    pattern = _search(params)
    if pattern:
        query = query.filter(or_(
            Candidate.name.ilike(pattern, escape="\\"), Job.title.ilike(pattern, escape="\\"),
        ))
    total = query.count()
    rows = (
        query.order_by(Candidate.applied_date.desc().nullslast(), Candidate.id.desc())
        .limit(_limit(params))
        .all()
    )
    return {
        "total": total,
        "candidates": [
            {
                "name": candidate.name,
                "job": title,
                "status": candidate.status.value if candidate.status else None,
                "stage": candidate.candidate_stage,
                "interview": _interview_label(candidate.interview_status),
                "interview_at": None
                if not candidate.interview_status or candidate.interview_status in _NOT_SCHEDULED
                else _iso(candidate.interview_date),
                "applied_at": _iso(candidate.applied_date),
                "job_ref": encode_ref("job", candidate.job_id),
            }
            for candidate, title in rows
        ],
    }


def _q_interviews(db: Session, org: Organization, params: dict) -> dict:
    start, end = _window(params)
    query = _org_candidates(db, org).filter(
        Candidate.interview_date >= start,
        Candidate.interview_date < end,
        Candidate.interview_status.isnot(None),
        Candidate.interview_status.notin_(_NOT_SCHEDULED),
    )
    pattern = _search(params)
    if pattern:
        query = query.filter(or_(
            Candidate.name.ilike(pattern, escape="\\"), Job.title.ilike(pattern, escape="\\"),
        ))
    total = query.count()
    rows = query.order_by(Candidate.interview_date, Candidate.id).limit(_limit(params)).all()
    return {
        "total": total,
        "interviews": [
            {
                "candidate": candidate.name,
                "job": title,
                "at": _iso(candidate.interview_date),
                "status": _interview_label(candidate.interview_status),
            }
            for candidate, title in rows
        ],
    }


_QUERIES = {
    "overview": _q_overview,
    "jobs": _q_jobs,
    "candidates": _q_candidates,
    "interviews": _q_interviews,
}


def _answer(question: QueryIn) -> dict:
    handler = _QUERIES.get(question.kind)
    if handler is None:
        raise HTTPException(status_code=422, detail="Unknown query.")
    db = SessionLocal()
    try:
        org = (
            db.query(Organization)
            .filter(Organization.askmira_tenant_id == question.tenant_id)
            .first()
        )
        if org is None:
            raise HTTPException(status_code=404, detail="This workspace is not linked here yet.")
        if not org.is_active:
            raise HTTPException(
                status_code=403, detail="Your organization's account here is suspended."
            )
        email = question.actor.email.lower()
        user = db.query(User).filter(func.lower(User.email) == email).first()
        if (
            ROLES.get(question.actor.role) is None
            or user is None
            or user.organization_id != org.id
            or not user.is_active
            or user.role not in (UserRole.admin, UserRole.recruiter)
        ):
            raise HTTPException(
                status_code=403,
                detail="You don't have access to hiring data in the interview tool yet. "
                "Open it once from AskMira, then ask again.",
            )
        answer = handler(db, org, question.params or {})
        logger.info("[AskMira] query %s for org %s by %s", question.kind, org.id, email)
        return {"kind": question.kind, **answer}
    finally:
        db.close()


@integration_router.post("/query")
async def query(request: Request):
    claims, body = await _provision_claims(request, use="query")
    try:
        question = QueryIn.model_validate_json(body)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.errors()[0].get("msg", "Invalid body"))
    if question.tenant_id != claims.get("sub"):
        raise HTTPException(status_code=401, detail="The assertion is for another workspace.")
    return await run_in_threadpool(_answer, question)


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
