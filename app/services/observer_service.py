"""Observer-link minting and creator-email helpers."""

from __future__ import annotations

import asyncio
import logging
import secrets
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from html import escape as _esc
from email.mime.text import MIMEText
from email.utils import formataddr
from typing import Optional

from sqlalchemy.orm import Session

from app.config import settings
from app.models.candidate import Candidate
from app.models.observer import ObserverToken
from app.services.interview_service import _format_interview_date, _log_email, _send_email_sync

logger = logging.getLogger(__name__)


def _build_observer_url(token: str) -> str:
    base = (settings.OBSERVER_LINK_BASE_URL or "").rstrip("/")
    return f"{base}/observe/{token}"


# Expiry for a link minted without an interview date (e.g. resent for a
# candidate who was never scheduled, or after a cancel). Such links used to be
# minted with expires_at=None and never expired.
DEFAULT_OBSERVER_LINK_TTL = timedelta(days=7)


def revoke_observer_tokens(db: Session, candidate_id: int) -> int:
    """Revoke every live observer link for a candidate (cancel / retake), so a
    link once handed to an outside panelist doesn't carry over to the next
    interview. Open observer sockets notice within a few seconds. The caller
    commits."""
    now = datetime.utcnow()
    rows = (
        db.query(ObserverToken)
        .filter(ObserverToken.candidate_id == candidate_id, ObserverToken.revoked_at.is_(None))
        .all()
    )
    for row in rows:
        row.revoked_at = now
    return len(rows)


def get_or_create_observer_token(
    db: Session,
    candidate: Candidate,
    creator_user_id: Optional[int],
    creator_email: Optional[str],
    expires_at: Optional[datetime] = None,
) -> ObserverToken:
    """Return the active observer token for this candidate, or mint a new one.

    A candidate has at most one active observer token at a time. Re-scheduling
    just refreshes the expiry of the existing token instead of creating a new
    URL — that way recruiters who already shared the link don't have to
    re-share it after a reschedule.
    """
    if expires_at is None:
        expires_at = datetime.utcnow() + DEFAULT_OBSERVER_LINK_TTL
    existing = (
        db.query(ObserverToken)
        .filter(
            ObserverToken.candidate_id == candidate.id,
            ObserverToken.revoked_at.is_(None),
        )
        .order_by(ObserverToken.created_at.desc())
        .first()
    )
    if existing is not None:
        if expires_at is not None:
            existing.expires_at = expires_at
        if creator_user_id is not None and existing.created_by_user_id is None:
            existing.created_by_user_id = creator_user_id
            existing.created_by_email = creator_email
        db.commit()
        return existing

    obs = ObserverToken(
        token=secrets.token_urlsafe(32)[:64],
        candidate_id=candidate.id,
        job_id=candidate.job_id,
        session_id=None,
        room_name=f"interview-{candidate.id}-{secrets.token_hex(4)}",
        created_by_user_id=creator_user_id,
        created_by_email=creator_email,
        expires_at=expires_at,
    )
    db.add(obs)
    db.commit()
    db.refresh(obs)
    return obs


def _observer_email_html(
    candidate: Candidate,
    job_title: str,
    observer_url: str,
    when_str: str,
    is_reschedule: bool,
) -> str:
    title = "Interview Observer Link (Updated)" if is_reschedule else "Your Interview Observer Link"
    # The candidate's name comes from the public /apply form — escape it (and
    # everything else interpolated) so an applicant can't put links or markup
    # into an email the platform sends from its own domain.
    candidate_name = _esc(candidate.name or "")
    job_title = _esc(job_title or "")
    when_str = _esc(when_str or "")
    observer_url = _esc(observer_url or "", quote=True)
    return f"""\
<!DOCTYPE html>
<html><body style="margin:0;padding:0;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif;background:#f7f7fb;">
  <div style="max-width:600px;margin:0 auto;padding:32px 24px;">
    <div style="background:#fff;border-radius:12px;padding:32px;box-shadow:0 1px 3px rgba(0,0,0,.05);">
      <h2 style="margin:0 0 8px 0;color:#111;font-size:22px;">{title}</h2>
      <p style="margin:0 0 24px 0;color:#555;font-size:14px;">
        You scheduled an AI interview with <strong>{candidate_name}</strong> for <strong>{job_title}</strong>.
        Use the private link below to silently watch it live — the candidate is not notified, the
        interview continues without any interruption, and nothing is recorded on your end.
      </p>
      <div style="background:#f3f4f6;border-radius:8px;padding:16px;margin-bottom:24px;">
        <div style="font-size:12px;color:#6b7280;text-transform:uppercase;letter-spacing:0.05em;margin-bottom:4px;">When</div>
        <div style="font-size:15px;color:#111;font-weight:600;">{when_str}</div>
      </div>
      <div style="text-align:center;margin:24px 0;">
        <a href="{observer_url}" style="display:inline-block;padding:14px 28px;background:#4f46e5;color:#fff;text-decoration:none;border-radius:8px;font-weight:600;font-size:15px;">
          Open observer link
        </a>
      </div>
      <p style="margin:16px 0 0 0;color:#6b7280;font-size:12px;word-break:break-all;">
        Or copy: {observer_url}
      </p>
      <hr style="margin:32px 0 16px 0;border:none;border-top:1px solid #e5e7eb;" />
      <p style="margin:0;color:#9ca3af;font-size:12px;line-height:1.6;">
        Anyone with this link can watch the interview. Share only with people authorised to evaluate
        the candidate. The link can be revoked any time from the candidate page in your dashboard.
      </p>
    </div>
  </div>
</body></html>
"""


async def send_observer_link_email(
    db: Session,
    candidate: Candidate,
    creator_email: str,
    observer_token: ObserverToken,
    job_title: str,
    is_reschedule: bool = False,
) -> bool:
    """Email the observer link to the recruiter who scheduled the interview."""
    if not getattr(settings, "SMTP_HOST", None) or not creator_email:
        logger.warning("Skipping observer link email — SMTP not configured or no recipient")
        return False

    observer_url = _build_observer_url(observer_token.token)
    when_str = _format_interview_date(candidate)

    subject = (
        f"Observer link (updated) — {candidate.name} interview"
        if is_reschedule
        else f"Observer link — {candidate.name} interview"
    )

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = formataddr(("AI Recruiters", settings.EMAIL_FROM or settings.SMTP_USER))
    msg["To"] = creator_email

    html = _observer_email_html(candidate, job_title, observer_url, when_str, is_reschedule)
    text = (
        f"Observer link for {candidate.name}'s interview ({job_title})\n"
        f"When: {when_str}\n\n"
        f"Watch live: {observer_url}\n\n"
        "Anyone with this link can watch the interview live (read-only). "
        "Revoke it any time from the candidate page."
    )
    msg.attach(MIMEText(text, "plain", "utf-8"))
    msg.attach(MIMEText(html, "html", "utf-8"))

    ok = await asyncio.to_thread(_send_email_sync, msg, [creator_email])
    try:
        _log_email(
            db,
            candidate_id=candidate.id,
            subject=subject,
            trigger_event="observer-link",
            triggered_by=creator_email,
            status="sent" if ok else "failed",
        )
    except Exception:
        pass
    return ok
