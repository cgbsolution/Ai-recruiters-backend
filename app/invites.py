"""
Personal interview invite links.

Interview links used to be per JOB (/interview/<job token>), and anyone who had
that link and knew a candidate's email could open — or take over — that
candidate's interview session. Each scheduled candidate now gets a secret
`invite_token`, and their link is /interview/<job token>?invite=<secret>.

Grace period: candidates scheduled before this change have no invite_token and
already hold emailed links of the form ?candidate=<email>. Those links keep
working (email verification) until the candidate is rescheduled, cancelled or
retakes — at which point a token is issued and email-only access stops.
"""
import secrets
import uuid
from typing import Optional
from urllib.parse import quote, urlsplit

from app.config import settings

INVITE_QUERY_PARAM = "invite"


def new_invite_token() -> str:
    return secrets.token_urlsafe(24)


def rotate_invite_token(candidate) -> str:
    """Issue a fresh secret, invalidating any previously sent personal link."""
    candidate.invite_token = new_invite_token()
    return candidate.invite_token


def ensure_invite_token(candidate) -> str:
    return candidate.invite_token or rotate_invite_token(candidate)


def email_access_allowed(candidate) -> bool:
    """Legacy email-only verification is allowed only for candidates who were
    scheduled before personal links existed (no invite_token yet).

    The evidence of that is the stored interview_link: scheduling wrote it and
    emailed it. The status alone is not enough — a recruiter moving a candidate
    through the pipeline (PUT /candidates/{id} with status "Interview" maps to
    "Scheduled") changes interview_status without ever sending a link, which
    let anyone with the job link and that candidate's email open a session in
    their name."""
    status = (candidate.interview_status or "New").strip()
    was_sent_a_link = bool((candidate.interview_link or "").strip())
    return not candidate.invite_token and status != "New" and was_sent_a_link


def active_job_token(db, job_id: int):
    from app.models.interview import InterviewToken

    return (
        db.query(InterviewToken)
        .filter(InterviewToken.job_id == job_id, InterviewToken.is_active == True)  # noqa: E712
        .order_by(InterviewToken.created_at.desc())
        .first()
    )


def get_or_create_job_token(db, job_id: int):
    from app.models.interview import InterviewToken

    tok = active_job_token(db, job_id)
    if tok is None:
        tok = InterviewToken(job_id=job_id, token=uuid.uuid4().hex, is_active=True)
        db.add(tok)
        db.flush()
    return tok


def frontend_origin(hint: Optional[str] = None) -> str:
    """Origin for candidate-facing links. A client-provided hint (the
    recruiter's app origin) is used only when it is an allowed CORS origin."""
    if hint:
        parts = urlsplit(hint.strip())
        if parts.scheme in ("http", "https") and parts.netloc:
            origin = f"{parts.scheme}://{parts.netloc}"
            allowed = settings.cors_origins_list
            if "*" in allowed or origin in allowed:
                return origin
    base = settings.frontend_base_url
    if base:
        return base
    origins = [o for o in settings.cors_origins_list if o != "*"]
    return origins[-1] if origins else "http://localhost:3000"


def build_invite_url(job_token: str, invite_token: str, origin_hint: Optional[str] = None) -> str:
    return (
        f"{frontend_origin(origin_hint)}/interview/{quote(job_token, safe='')}"
        f"?{INVITE_QUERY_PARAM}={quote(invite_token, safe='')}"
    )
