"""
Message transports for the Talent Agent. Only app.talent.outbox calls these,
and every `send()` demands a ContactPermit from app.talent.consent.can_contact
(checked again here, so a caller can't skip the consent gate).

Failure semantics, which keep "no duplicate sends" true over flaky networks:
    TransientSendError  — the message certainly did NOT leave; safe to retry.
    PermanentSendError  — the provider refused it; don't retry (hard_bounce
                          when the recipient address itself was rejected).
    UnknownSendError    — it may have been delivered (the connection died
                          during DATA); recorded as `unknown`, never retried.

Email transports: smtp (SMTP_* settings) | memory (tests) | disabled.
WhatsApp: Meta Cloud API *template* messages — business-initiated messages
outside a 24-hour customer window must use a template Meta has approved, so a
kind with no configured template is never sent on WhatsApp.
"""

from __future__ import annotations

import json
import logging
import smtplib
import socket
import uuid
from dataclasses import dataclass, field
from email.message import EmailMessage
from email.utils import formataddr, make_msgid
from typing import Dict, List, Optional

from app.config import settings
from app.talent.config import talent_config
from app.talent.consent import ContactPermit

logger = logging.getLogger("app.talent.transports")


class SendError(Exception):
    pass


class TransientSendError(SendError):
    def __init__(self, message: str, retry_after_seconds: int = 900):
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


class PermanentSendError(SendError):
    def __init__(self, message: str, hard_bounce: bool = False):
        super().__init__(message)
        self.hard_bounce = hard_bounce


class UnknownSendError(SendError):
    pass


@dataclass
class OutgoingEmail:
    subject: str
    text: str
    html: str
    from_name: str
    message_id_hint: str  # usually the touch id
    unsubscribe_url: Optional[str] = None
    headers: Dict[str, str] = field(default_factory=dict)


@dataclass
class OutgoingWhatsApp:
    template: str
    language: str
    parameters: List[str]
    message_id_hint: str


def sender_address() -> str:
    return (settings.EMAIL_FROM or settings.SMTP_USER or "no-reply@localhost").strip()


def sender_domain() -> str:
    addr = sender_address()
    return addr.rsplit("@", 1)[1].lower() if "@" in addr else "localhost"


def build_mime(permit: ContactPermit, msg: OutgoingEmail) -> EmailMessage:
    em = EmailMessage()
    em["From"] = formataddr((msg.from_name, sender_address()))
    em["To"] = permit.address
    em["Subject"] = msg.subject
    em["Message-ID"] = make_msgid(idstring=msg.message_id_hint.replace("-", "")[:32], domain=sender_domain())
    if msg.unsubscribe_url:
        # RFC 8058 one-click unsubscribe — required by Gmail / Yahoo for bulk senders.
        em["List-Unsubscribe"] = f"<{msg.unsubscribe_url}>"
        em["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"
    for k, v in msg.headers.items():
        em[k] = v
    em.set_content(msg.text)
    em.add_alternative(msg.html, subtype="html")
    return em


# ── Email ────────────────────────────────────────────────────────────────────

class SmtpTransport:
    name = "smtp"

    def send(self, permit: ContactPermit, msg: OutgoingEmail) -> str:
        permit.check("email")
        host, user, password = settings.SMTP_HOST, settings.SMTP_USER, settings.SMTP_PASS
        if not (host and user and password):
            raise TransientSendError("SMTP is not configured", retry_after_seconds=3600)
        mime = build_mime(permit, msg)
        try:
            server = smtplib.SMTP(host, int(settings.SMTP_PORT), timeout=20)
        except (OSError, smtplib.SMTPException) as exc:
            raise TransientSendError(f"connect failed: {type(exc).__name__}") from exc
        try:
            try:
                server.ehlo()
                server.starttls()
                server.ehlo()
                server.login(user, password)
                code, resp = server.mail(sender_address())
                if code >= 400:
                    raise TransientSendError(f"MAIL FROM refused ({code})")
                code, resp = server.rcpt(permit.address)
                if code >= 500:
                    raise PermanentSendError(f"recipient refused ({code})", hard_bounce=code in (550, 551, 553))
                if code >= 400:
                    raise TransientSendError(f"recipient deferred ({code})")
            except SendError:
                raise
            except smtplib.SMTPAuthenticationError as exc:
                raise TransientSendError("SMTP authentication failed", retry_after_seconds=3600) from exc
            except (OSError, smtplib.SMTPException) as exc:
                raise TransientSendError(f"before DATA: {type(exc).__name__}") from exc
            # From here the server may have the message: never report "not sent" for a dropped connection.
            try:
                code, resp = server.data(mime.as_bytes())
            except smtplib.SMTPDataError as exc:
                if exc.smtp_code >= 500:
                    raise PermanentSendError(f"message refused ({exc.smtp_code})") from exc
                raise TransientSendError(f"message deferred ({exc.smtp_code})") from exc
            except (OSError, socket.timeout, smtplib.SMTPException) as exc:
                raise UnknownSendError(f"connection lost during DATA: {type(exc).__name__}") from exc
            if code >= 500:
                raise PermanentSendError(f"message refused ({code})")
            if code >= 400:
                raise TransientSendError(f"message deferred ({code})")
            return str(mime["Message-ID"])
        finally:
            try:
                server.quit()
            except Exception:
                pass


class MemoryTransport:
    """Test transport: records messages; `fail_next` injects failures."""

    name = "memory"

    def __init__(self):
        self.fail_next: List[SendError] = []

    def send(self, permit: ContactPermit, msg: OutgoingEmail) -> str:
        permit.check("email")
        if self.fail_next:
            raise self.fail_next.pop(0)
        mime = build_mime(permit, msg)
        message_id = str(mime["Message-ID"])
        memory_outbox.append({
            "channel": "email", "to": permit.address, "profile_id": permit.profile_id, "purpose": permit.purpose,
            "subject": msg.subject, "text": msg.text, "html": msg.html, "message_id": message_id,
            "headers": {k: v for k, v in mime.items()},
        })
        return message_id


class DisabledTransport:
    name = "disabled"

    def send(self, permit: ContactPermit, msg) -> str:
        permit.check(permit.channel)
        raise TransientSendError("sending is disabled (TALENT_EMAIL_TRANSPORT=disabled)", retry_after_seconds=3600)


memory_outbox: List[dict] = []
_memory_transport = MemoryTransport()


def email_transport():
    kind = (talent_config.TALENT_EMAIL_TRANSPORT or "smtp").strip().lower()
    if kind == "memory":
        return _memory_transport
    if kind == "disabled":
        return DisabledTransport()
    return SmtpTransport()


# ── WhatsApp ─────────────────────────────────────────────────────────────────

def whatsapp_template_for(kind: str) -> Optional[str]:
    """Approved template name for a message kind, from TALENT_WHATSAPP_TEMPLATES
    (JSON: {"refresh_touch_3": "profile_refresh_v1", ...})."""
    import os

    raw = os.environ.get("TALENT_WHATSAPP_TEMPLATES", "")
    try:
        return (json.loads(raw) if raw else {}).get(kind)
    except json.JSONDecodeError:
        logger.warning("TALENT_WHATSAPP_TEMPLATES is not valid JSON")
        return None


class WhatsAppTransport:
    name = "whatsapp"

    def send(self, permit: ContactPermit, msg: OutgoingWhatsApp) -> str:
        permit.check("whatsapp")
        import httpx

        token, phone_id = settings.WHATSAPP_TOKEN, settings.WHATSAPP_PHONE_NUMBER_ID
        if not token or not phone_id:
            raise TransientSendError("WhatsApp is not configured", retry_after_seconds=3600)
        payload = {
            "messaging_product": "whatsapp", "to": permit.address, "type": "template",
            "template": {
                "name": msg.template, "language": {"code": msg.language},
                "components": [{"type": "body", "parameters": [{"type": "text", "text": p} for p in msg.parameters]}],
            },
        }
        try:
            resp = httpx.post(
                f"https://graph.facebook.com/v19.0/{phone_id}/messages",
                headers={"Authorization": f"Bearer {token}"}, json=payload, timeout=15,
            )
        except httpx.ConnectError as exc:
            raise TransientSendError("WhatsApp connect failed") from exc
        except httpx.HTTPError as exc:  # sent, but no answer: may have been delivered
            raise UnknownSendError(f"WhatsApp request failed: {type(exc).__name__}") from exc
        if resp.status_code == 429 or resp.status_code >= 500:
            raise TransientSendError(f"WhatsApp {resp.status_code}")
        if resp.status_code >= 400:
            raise PermanentSendError(f"WhatsApp refused ({resp.status_code})")
        return (resp.json().get("messages") or [{}])[0].get("id") or f"wa-{uuid.uuid4().hex}"


class MemoryWhatsAppTransport:
    name = "memory"

    def send(self, permit: ContactPermit, msg: OutgoingWhatsApp) -> str:
        permit.check("whatsapp")
        message_id = f"wa-{uuid.uuid4().hex}"
        memory_outbox.append({"channel": "whatsapp", "to": permit.address, "profile_id": permit.profile_id,
                              "purpose": permit.purpose, "template": msg.template, "parameters": msg.parameters,
                              "message_id": message_id})
        return message_id


def whatsapp_transport():
    kind = (talent_config.TALENT_EMAIL_TRANSPORT or "smtp").strip().lower()
    if kind == "memory":
        return MemoryWhatsAppTransport()
    if kind == "disabled":
        return DisabledTransport()
    return WhatsAppTransport()
