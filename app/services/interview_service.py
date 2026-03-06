"""
Interview scheduling notifications.

Email: uses smtplib with SMTP credentials from settings.
WhatsApp: uses Meta WhatsApp Business Cloud API (httpx async).

Configure credentials in .env:
  SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS, EMAIL_FROM
  WHATSAPP_TOKEN, WHATSAPP_PHONE_NUMBER_ID
"""

import asyncio
import json
import logging
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import TYPE_CHECKING

import httpx

from app.config import settings

if TYPE_CHECKING:
    from sqlalchemy.orm import Session
    from app.models.candidate import Candidate

logger = logging.getLogger(__name__)


# ── Email ─────────────────────────────────────────────────────────────────────

def _format_interview_date(candidate) -> str:
    if not candidate.interview_date:
        return "To be confirmed"
    dt = candidate.interview_date
    tz = candidate.interview_timezone or "IST"
    return dt.strftime(f"%a, %b %d, %Y, %I:%M %p") + f" ({tz})"


def _send_email_sync(msg, recipients) -> bool:
    """Blocking SMTP send — called via asyncio.to_thread."""
    try:
        port = int(getattr(settings, "SMTP_PORT", 587))
        with smtplib.SMTP(settings.SMTP_HOST, port) as server:
            server.starttls()
            server.login(settings.SMTP_USER, settings.SMTP_PASS)
            server.sendmail(msg["From"], recipients, msg.as_string())
        return True
    except Exception as exc:
        logger.warning(f"Email send failed: {exc}")
        return False


async def send_schedule_email(candidate, db: "Session", is_reschedule: bool = False) -> bool:
    """
    Send interview schedule/reschedule notification email to the candidate.
    Reads SMTP settings from config; returns True on success.
    """
    if not getattr(settings, "SMTP_HOST", None) or not getattr(settings, "SMTP_USER", None):
        return False

    template = {}
    if candidate.schedule_email_template:
        try:
            template = json.loads(candidate.schedule_email_template)
        except Exception:
            pass

    from app.models.job import Job
    job_title = "the role"
    if candidate.job_id:
        job = db.query(Job).filter(Job.id == candidate.job_id).first()
        if job:
            job_title = job.title

    action_word = "Rescheduled" if is_reschedule else "Schedule"
    subject = template.get("subject") or f"Your Interview {action_word} for {job_title}"
    intro_text = template.get("intro_text") or (
        f"You are shortlisted for an AI Interview round for the role "
        f"{job_title} at company CGBiz Solutions Private limited"
    )
    date_str = _format_interview_date(candidate)
    interview_url = candidate.interview_link or ""
    extra_heading = template.get("extra_heading") or ""
    extra_body = template.get("extra_body") or ""
    cc_emails = template.get("cc_emails") or []

    html_body = f"""
<html><body style="font-family: Arial, sans-serif; color: #333;">
  <div style="max-width:600px;margin:auto;padding:24px;border:1px solid #e0e0e0;border-radius:8px;">
    <h2 style="color:#1a73e8;">Your Interview has been {"rescheduled" if is_reschedule else "scheduled"}</h2>
    <p>Hello {candidate.name},</p>
    <p>{intro_text}</p>
    <hr/>
    <p><strong>When is the interview?</strong></p>
    <p>{date_str}</p>
    <p><strong>Interview URL</strong></p>
    <p><a href="{interview_url}">{interview_url}</a></p>
    <p>You may take the interview at the scheduled time or at a time that works best for you.
       Simply click the link whenever you're ready.</p>
    {"<h3>" + extra_heading + "</h3>" if extra_heading else ""}
    {"<p>" + extra_body + "</p>" if extra_body else ""}
    <hr/>
    <p style="font-size:12px;color:#888;">CGBiz Solutions Private Limited | HR Team</p>
  </div>
</body></html>
"""

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = getattr(settings, "EMAIL_FROM", settings.SMTP_USER)
    msg["To"] = candidate.email
    if cc_emails:
        msg["Cc"] = ", ".join(cc_emails)

    msg.attach(MIMEText(html_body, "html"))

    recipients = [candidate.email] + cc_emails
    return await asyncio.to_thread(_send_email_sync, msg, recipients)


async def send_cancel_email(candidate, db: "Session") -> bool:
    """
    Send interview cancellation notification email to the candidate.
    """
    if not getattr(settings, "SMTP_HOST", None) or not getattr(settings, "SMTP_USER", None):
        return False

    from app.models.job import Job
    job_title = "the role"
    if candidate.job_id:
        job = db.query(Job).filter(Job.id == candidate.job_id).first()
        if job:
            job_title = job.title

    subject = f"Interview Cancelled – {job_title}"

    html_body = f"""
<html><body style="font-family: Arial, sans-serif; color: #333;">
  <div style="max-width:600px;margin:auto;padding:24px;border:1px solid #e0e0e0;border-radius:8px;">
    <h2 style="color:#dc2626;">Interview Cancelled</h2>
    <p>Hello {candidate.name},</p>
    <p>We regret to inform you that your scheduled interview for the role of
       <strong>{job_title}</strong> at CGBiz Solutions Private Limited has been cancelled.</p>
    <p>If you have any questions, please reach out to the HR team.</p>
    <hr/>
    <p style="font-size:12px;color:#888;">CGBiz Solutions Private Limited | HR Team</p>
  </div>
</body></html>
"""

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = getattr(settings, "EMAIL_FROM", settings.SMTP_USER)
    msg["To"] = candidate.email
    msg.attach(MIMEText(html_body, "html"))

    return await asyncio.to_thread(_send_email_sync, msg, [candidate.email])


# ── WhatsApp Business Cloud API ───────────────────────────────────────────────

async def send_whatsapp_message(phone: str, message: str) -> str:
    """
    Send a free-form text message via Meta WhatsApp Business Cloud API.

    Requires in .env:
      WHATSAPP_TOKEN=<permanent token from Meta Business>
      WHATSAPP_PHONE_NUMBER_ID=<phone number ID from WhatsApp app>

    The phone number must be in E.164 format without '+', e.g. 919876543210.
    """
    token = getattr(settings, "WHATSAPP_TOKEN", "")
    phone_number_id = getattr(settings, "WHATSAPP_PHONE_NUMBER_ID", "")

    if not token or not phone_number_id:
        raise RuntimeError(
            "WhatsApp not configured. Set WHATSAPP_TOKEN and WHATSAPP_PHONE_NUMBER_ID in .env"
        )

    clean_phone = "".join(c for c in phone if c.isdigit())

    url = f"https://graph.facebook.com/v19.0/{phone_number_id}/messages"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": clean_phone,
        "type": "text",
        "text": {"preview_url": False, "body": message},
    }

    async with httpx.AsyncClient() as client:
        resp = await client.post(url, headers=headers, json=payload, timeout=15)

    if resp.status_code >= 400:
        raise RuntimeError(f"WhatsApp API error {resp.status_code}: {resp.text}")

    return resp.json().get("messages", [{}])[0].get("id", "sent")
