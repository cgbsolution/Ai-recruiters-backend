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
from email.utils import formataddr
from typing import TYPE_CHECKING

import httpx

from app.config import settings

if TYPE_CHECKING:
    from sqlalchemy.orm import Session
    from app.models.candidate import Candidate

logger = logging.getLogger(__name__)


def _log_email(db: "Session", candidate_id: int, subject: str, trigger_event: str, triggered_by: str = "SYSTEM", status: str = "sent"):
    """Record an email in the email_logs table and set mail_contact_history flag."""
    try:
        from app.models.candidate import EmailLog, Candidate
        log = EmailLog(
            candidate_id=candidate_id,
            subject=subject,
            triggered_by=triggered_by,
            trigger_event=trigger_event,
            status=status,
        )
        db.add(log)
        # Mark the candidate as having email contact history
        candidate = db.query(Candidate).filter(Candidate.id == candidate_id).first()
        if candidate:
            candidate.mail_contact_history = True
        db.commit()
    except Exception as e:
        logger.warning(f"Failed to log email for candidate {candidate_id}: {e}")
        try:
            db.rollback()
        except Exception:
            pass


# ── Email ─────────────────────────────────────────────────────────────────────

_TZ_LABELS = {
    "Asia/Calcutta": "India (IST)",
    "Asia/Kolkata": "India (IST)",
    "UTC": "UTC",
    "America/New_York": "US Eastern",
    "America/Los_Angeles": "US Pacific",
    "Europe/London": "UK (GMT/BST)",
    "Asia/Singapore": "Singapore",
    "Asia/Dubai": "Dubai (GST)",
    "Australia/Sydney": "Australia (AEST)",
}


def _format_interview_date(candidate) -> str:
    if not candidate.interview_date:
        return "To be confirmed"
    dt = candidate.interview_date
    raw_tz = candidate.interview_timezone or "Asia/Kolkata"
    tz_label = _TZ_LABELS.get(raw_tz, raw_tz)
    # dt is a naive datetime stored as UTC — convert to target timezone before formatting
    try:
        from datetime import timezone as _tz
        from zoneinfo import ZoneInfo
        local_dt = dt.replace(tzinfo=_tz.utc).astimezone(ZoneInfo(raw_tz))
    except Exception:
        local_dt = dt  # fallback: format as-is if tz conversion fails
    return local_dt.strftime("%a, %b %d, %Y, %I:%M %p") + f" ({tz_label})"


def _send_email_sync(msg, recipients) -> bool:
    """Blocking SMTP send — called via asyncio.to_thread."""
    host = getattr(settings, "SMTP_HOST", "") or ""
    user = getattr(settings, "SMTP_USER", "") or ""
    password = getattr(settings, "SMTP_PASS", "") or ""
    port = int(getattr(settings, "SMTP_PORT", 587))

    if not host or not user or not password:
        logger.error("SMTP not configured: SMTP_HOST=%s, SMTP_USER=%s, SMTP_PASS=%s",
                      host, user, "***" if password else "(empty)")
        return False

    try:
        logger.info("Connecting to SMTP %s:%d as %s → sending to %s", host, port, user, recipients)
        with smtplib.SMTP(host, port, timeout=15) as server:
            server.set_debuglevel(0)
            server.ehlo()
            server.starttls()
            server.ehlo()
            server.login(user, password)
            server.sendmail(msg["From"], recipients, msg.as_string())
        logger.info("Email sent successfully to %s", recipients)
        return True
    except smtplib.SMTPAuthenticationError as exc:
        logger.error("SMTP authentication failed for %s: %s — Check your App Password", user, exc)
        return False
    except smtplib.SMTPRecipientsRefused as exc:
        logger.error("SMTP recipients refused: %s", exc)
        return False
    except smtplib.SMTPException as exc:
        logger.error("SMTP error: %s", exc)
        return False
    except Exception as exc:
        logger.error("Email send failed (unexpected): %s", exc, exc_info=True)
        return False


async def send_schedule_email(candidate, db: "Session", is_reschedule: bool = False, google_meet_link: str = None) -> bool:
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
    from app.models.organization import Organization
    job_title = "the role"
    job = None
    org_name = "Our Company"
    if candidate.job_id:
        job = db.query(Job).filter(Job.id == candidate.job_id).first()
        if job:
            job_title = job.title
            if job.organization_id:
                org = db.query(Organization).filter(Organization.id == job.organization_id).first()
                if org:
                    org_name = org.name

    # Recruiter-supplied strings are interpolated into the HTML body below and
    # this mail is sent AS the platform, so escape them: unescaped, any
    # recruiter could mail arbitrary markup from "Anytimehire". Newlines are
    # stripped from the subject so it can't inject extra mail headers.
    from html import escape as _html_escape

    action_word = "Rescheduled" if is_reschedule else "Schedule"
    subject = str(template.get("subject") or "").replace("\r", " ").replace("\n", " ").strip()[:200]
    subject = subject or f"Your Interview {action_word} for {job_title}"
    # The candidate's name can come from the anonymous /apply form, and job /
    # org names are tenant-controlled: escape them for the HTML body too.
    candidate_name_html = _html_escape(candidate.name or "")
    intro_text = _html_escape(str(template.get("intro_text") or "")) or (
        f"You are shortlisted for an AI Interview round for the role of "
        f"{_html_escape(job_title)} at {_html_escape(org_name)}."
    )
    date_str = _format_interview_date(candidate)

    # Build interview URL. candidate.interview_link is set at scheduling time;
    # a personal link already carries the candidate's secret (?invite=...) and
    # is used as-is — never append ?candidate=<email> to it. Email-only links
    # are refused once a candidate has an invite, so such a candidate always
    # gets their personal link. Legacy candidates (no invite) keep the
    # ?candidate=<email> form.
    from urllib.parse import quote
    interview_url = ""
    base_link = candidate.interview_link or ""
    invite_token = getattr(candidate, "invite_token", None)

    if base_link and "invite=" in base_link:
        interview_url = base_link
    elif job and invite_token:
        from app.invites import active_job_token, build_invite_url
        token_row = active_job_token(db, job.id)
        if token_row:
            interview_url = build_invite_url(token_row.token, invite_token, origin_hint=base_link or None)
        else:
            interview_url = base_link
    elif base_link:
        # Append candidate email param if not already present
        if "?candidate=" not in base_link and "&candidate=" not in base_link:
            sep = "&" if "?" in base_link else "?"
            interview_url = f"{base_link}{sep}candidate={quote(candidate.email)}"
        else:
            interview_url = base_link
    elif job:
        from app.models.interview import InterviewToken
        token_row = (
            db.query(InterviewToken)
            .filter(InterviewToken.job_id == job.id, InterviewToken.is_active == True)
            .first()
        )
        if token_row:
            # Use last CORS origin (production URL) instead of first (localhost)
            origins = settings.cors_origins_list
            frontend_base = origins[-1] if origins else "http://localhost:3000"
            interview_url = f"{frontend_base}/interview/{token_row.token}?candidate={quote(candidate.email)}"

    extra_heading = _html_escape(str(template.get("extra_heading") or ""))
    extra_body = _html_escape(str(template.get("extra_body") or ""))
    # Bounded and shape-checked: this list went into the Cc header and the
    # recipient list unvalidated, so it could address anyone, in any number.
    import re as _re_cc

    _CC_RE = _re_cc.compile(r"^[^@\s,;<>]+@[^@\s,;<>]+\.[A-Za-z]{2,}$")
    cc_emails = [
        e.strip() for e in (template.get("cc_emails") or [])
        if isinstance(e, str) and _CC_RE.match(e.strip())
    ][:10]

    scheduled_text = "rescheduled" if is_reschedule else "scheduled"

    # Flexibility text based on candidate setting
    flexibility = getattr(candidate, "joining_flexibility", "anytime") or "anytime"
    if flexibility == "anytime":
        flexibility_text = (
            "You may take the interview at the scheduled time or at a time that works best for you. "
            "Simply click the link whenever you&#8217;re ready."
        )
    else:
        flexibility_text = (
            "Please join the interview at the scheduled time. "
            "Click the link below at the specified date and time to begin."
        )

    # Extra section HTML
    extra_section = ""
    if extra_heading or extra_body:
        extra_section = f"""
        <tr><td style="padding:0 24px 24px;">
          <div style="padding:16px 20px;background-color:#F9FAFB;border-radius:8px;border:1px solid #E5E7EB;">
            {"<h3 style='margin:0 0 8px 0;font-size:15px;color:#1F2937;'>" + extra_heading + "</h3>" if extra_heading else ""}
            {"<p style='margin:0;font-size:14px;color:#4B5563;line-height:1.6;'>" + extra_body + "</p>" if extra_body else ""}
          </div>
        </td></tr>"""

    # Interview link button (only if URL available)
    link_section = ""
    if interview_url:
        link_section = f"""
        <!-- INTERVIEW LINK box -->
        <tr><td style="padding:0 24px 16px;">
          <table cellpadding="0" cellspacing="0" border="0" width="100%" style="background-color:#EFF6FF;border:1px solid #BFDBFE;border-radius:8px;">
            <tr>
              <td style="padding:16px 20px;">
                <table cellpadding="0" cellspacing="0" border="0" width="100%">
                  <tr>
                    <td width="36" valign="top" style="padding-right:12px;">
                      <span style="font-size:20px;color:#3B82F6;">&#128279;</span>
                    </td>
                    <td>
                      <div style="font-size:11px;font-weight:700;color:#3B82F6;text-transform:uppercase;letter-spacing:1px;margin-bottom:4px;">Interview Link</div>
                      <a href="{interview_url}" style="font-size:14px;color:#2563EB;text-decoration:underline;word-break:break-all;">{interview_url}</a>
                    </td>
                  </tr>
                </table>
              </td>
            </tr>
          </table>
        </td></tr>"""

    # Google Meet section (if a Meet link was created)
    meet_section = ""
    if google_meet_link:
        meet_section = f"""
        <!-- GOOGLE MEET box -->
        <tr><td style="padding:0 24px 16px;">
          <table cellpadding="0" cellspacing="0" border="0" width="100%" style="background-color:#ECFDF5;border:1px solid #A7F3D0;border-radius:8px;">
            <tr>
              <td style="padding:16px 20px;">
                <table cellpadding="0" cellspacing="0" border="0" width="100%">
                  <tr>
                    <td width="36" valign="top" style="padding-right:12px;">
                      <span style="font-size:20px;color:#16a34a;">&#127909;</span>
                    </td>
                    <td>
                      <div style="font-size:11px;font-weight:700;color:#16a34a;text-transform:uppercase;letter-spacing:1px;margin-bottom:4px;">Google Meet</div>
                      <a href="{google_meet_link}" style="font-size:14px;color:#15803D;text-decoration:underline;word-break:break-all;">{google_meet_link}</a>
                    </td>
                  </tr>
                </table>
              </td>
            </tr>
          </table>
        </td></tr>"""

    html_body = f"""
<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1.0"/>
  <!--[if mso]><noscript><xml><o:OfficeDocumentSettings><o:PixelsPerInch>96</o:PixelsPerInch></o:OfficeDocumentSettings></xml></noscript><![endif]-->
</head>
<body style="margin:0;padding:0;background-color:#F3F4F6;font-family:'Segoe UI',Arial,Helvetica,sans-serif;-webkit-font-smoothing:antialiased;">
  <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%" style="background-color:#F3F4F6;">
    <tr><td align="center" style="padding:32px 16px;">
      <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="600" style="max-width:600px;background-color:#FFFFFF;border-radius:12px;overflow:hidden;box-shadow:0 4px 6px rgba(0,0,0,0.07);">

        <!-- Header -->
        <tr><td style="background:linear-gradient(135deg,#3B82F6,#2563EB);padding:32px 24px;text-align:center;">
          <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%">
            <tr><td align="center" style="padding-bottom:12px;">
              <div style="width:48px;height:48px;background-color:rgba(255,255,255,0.2);border-radius:50%;display:inline-block;line-height:48px;text-align:center;">
                <span style="font-size:24px;color:#FFFFFF;">&#9993;</span>
              </div>
            </td></tr>
            <tr><td>
              <h1 style="margin:0;color:#FFFFFF;font-size:22px;font-weight:600;letter-spacing:-0.3px;">
                Your Interview has been {scheduled_text}
              </h1>
            </td></tr>
          </table>
        </td></tr>

        <!-- Body -->
        <tr><td style="padding:28px 24px 6px;">
          <p style="margin:0 0 6px 0;font-size:16px;color:#1F2937;">Hello <strong>{candidate_name_html}</strong>,</p>
          <p style="margin:0 0 24px 0;font-size:14px;color:#4B5563;line-height:1.6;">
            {intro_text}
          </p>
        </td></tr>

        <!-- WHEN box -->
        <tr><td style="padding:0 24px 16px;">
          <table cellpadding="0" cellspacing="0" border="0" width="100%" style="background-color:#EFF6FF;border:1px solid #BFDBFE;border-radius:8px;">
            <tr>
              <td style="padding:16px 20px;">
                <table cellpadding="0" cellspacing="0" border="0" width="100%">
                  <tr>
                    <td width="36" valign="top" style="padding-right:12px;">
                      <span style="font-size:20px;color:#3B82F6;">&#128197;</span>
                    </td>
                    <td>
                      <div style="font-size:11px;font-weight:700;color:#3B82F6;text-transform:uppercase;letter-spacing:1px;margin-bottom:4px;">When</div>
                      <div style="font-size:15px;color:#1E3A5F;font-weight:500;">{date_str}</div>
                    </td>
                  </tr>
                </table>
              </td>
            </tr>
          </table>
        </td></tr>

        {link_section}

        {meet_section}

        <!-- Flexibility text -->
        <tr><td style="padding:0 24px 24px;">
          <p style="margin:0;font-size:13px;color:#6B7280;line-height:1.6;">
            {flexibility_text}
          </p>
        </td></tr>

        {extra_section}

        <!-- Accept / Reject buttons -->
        <tr><td style="padding:0 24px 28px;">
          <table cellpadding="0" cellspacing="0" border="0" width="100%" style="border-top:1px solid #E5E7EB;">
            <tr><td style="padding-top:20px;" align="center">
              <table cellpadding="0" cellspacing="0" border="0">
                <tr>
                  <td style="padding-right:12px;">
                    <a href="{interview_url}" style="display:inline-block;padding:10px 28px;background-color:#ECFDF5;color:#15803D;font-size:14px;font-weight:600;text-decoration:none;border-radius:8px;border:1px solid #BBF7D0;">
                      &#10003; Accept
                    </a>
                  </td>
                  <td>
                    <a href="mailto:{getattr(settings, 'EMAIL_FROM', '') or getattr(settings, 'SMTP_USER', '')}?subject=Interview%20Decline%20-%20{_html_escape(quote(candidate.name or '', safe=''), quote=True)}" style="display:inline-block;padding:10px 28px;background-color:#FEF2F2;color:#DC2626;font-size:14px;font-weight:600;text-decoration:none;border-radius:8px;border:1px solid #FECACA;">
                      &#10007; Reject
                    </a>
                  </td>
                </tr>
              </table>
            </td></tr>
          </table>
        </td></tr>

        <!-- Footer -->
        <tr><td style="padding:16px 24px;background-color:#F9FAFB;border-top:1px solid #E5E7EB;text-align:center;">
          <p style="margin:0;font-size:12px;color:#9CA3AF;">
            This is an automated email from the recruitment system
          </p>
        </td></tr>

      </table>
    </td></tr>
  </table>
</body>
</html>
"""

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = formataddr(("Anytimehire", getattr(settings, "EMAIL_FROM", settings.SMTP_USER)))
    msg["To"] = candidate.email
    if cc_emails:
        msg["Cc"] = ", ".join(cc_emails)

    msg.attach(MIMEText(html_body, "html"))

    recipients = [candidate.email] + cc_emails
    success = await asyncio.to_thread(_send_email_sync, msg, recipients)
    trigger = "reschedule" if is_reschedule else "schedule"
    _log_email(db, candidate.id, subject, trigger, status="sent" if success else "failed")
    return success


async def send_cancel_email(candidate, db: "Session") -> bool:
    """
    Send interview cancellation notification email to the candidate.
    """
    if not getattr(settings, "SMTP_HOST", None) or not getattr(settings, "SMTP_USER", None):
        return False

    from app.models.job import Job
    from app.models.organization import Organization
    job_title = "the role"
    org_name = "Our Company"
    if candidate.job_id:
        job = db.query(Job).filter(Job.id == candidate.job_id).first()
        if job:
            job_title = job.title
            if job.organization_id:
                org = db.query(Organization).filter(Organization.id == job.organization_id).first()
                if org:
                    org_name = org.name

    subject = f"Interview Cancelled – {job_title}"

    # Escaped for the HTML body: the name can come from the anonymous /apply
    # form, and the job / org names are tenant-controlled.
    from html import escape as _html_escape
    candidate_name_html = _html_escape(candidate.name or "")
    job_title = _html_escape(job_title)
    org_name = _html_escape(org_name)

    html_body = f"""
<html><body style="font-family: Arial, sans-serif; color: #333;">
  <div style="max-width:600px;margin:auto;padding:24px;border:1px solid #e0e0e0;border-radius:8px;">
    <h2 style="color:#dc2626;">Interview Cancelled</h2>
    <p>Hello {candidate_name_html},</p>
    <p>We regret to inform you that your scheduled interview for the role of
       <strong>{job_title}</strong> at {org_name} has been cancelled.</p>
    <p>If you have any questions, please reach out to the HR team.</p>
    <hr/>
    <p style="font-size:12px;color:#888;">{org_name} | HR Team</p>
  </div>
</body></html>
"""

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = formataddr(("Anytimehire", getattr(settings, "EMAIL_FROM", settings.SMTP_USER)))
    msg["To"] = candidate.email
    msg.attach(MIMEText(html_body, "html"))

    success = await asyncio.to_thread(_send_email_sync, msg, [candidate.email])
    _log_email(db, candidate.id, subject, "cancel", status="sent" if success else "failed")
    return success


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
