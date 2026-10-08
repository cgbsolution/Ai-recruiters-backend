"""
Contact identity: normalization, de-duplication keys, suppression hashes and
email validity.

- dedupe_key: SHA-256 of the canonical email (Gmail dots / +tags folded), or of
  the canonical phone when there is no usable email. Same key = same person.
- suppression_hash: HMAC of "<kind>:<canonical value>" with a server key, so
  the suppression list can't be reversed into addresses by anyone holding a
  database dump.
"""

from __future__ import annotations

import hashlib
import hmac
import re
from functools import lru_cache
from typing import Optional

from app.config import settings
from app.talent.config import talent_config

PLACEHOLDER_SUFFIX = "@pending.local"
DEFAULT_COUNTRY_CODE = "91"

# Throwaway inbox providers: never worth a send.
DISPOSABLE_DOMAINS = frozenset({
    "mailinator.com", "guerrillamail.com", "10minutemail.com", "tempmail.com", "temp-mail.org",
    "yopmail.com", "trashmail.com", "sharklasers.com", "getnada.com", "dispostable.com",
    "throwawaymail.com", "maildrop.cc", "fakeinbox.com", "mailnesia.com", "mintemail.com",
})
_GMAIL = {"gmail.com", "googlemail.com"}


def normalize_email(email: Optional[str]) -> Optional[str]:
    if not email:
        return None
    e = email.strip().lower()
    return e if "@" in e else None


def canonical_email(email: Optional[str]) -> Optional[str]:
    """Address form used for identity: Gmail ignores dots and +tags."""
    e = normalize_email(email)
    if not e:
        return None
    local, _, domain = e.rpartition("@")
    if domain in _GMAIL:
        local = local.split("+", 1)[0].replace(".", "")
        domain = "gmail.com"
    return f"{local}@{domain}"


def normalize_phone(phone: Optional[str]) -> Optional[str]:
    """Digits in international form without '+': 9198xxxxxxxx. A bare 10-digit
    number (or one with a trunk 0) is taken as DEFAULT_COUNTRY_CODE."""
    if not phone:
        return None
    digits = re.sub(r"\D", "", phone)
    if digits.startswith("00"):
        digits = digits[2:]
    if len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]
    if len(digits) == 10:
        digits = DEFAULT_COUNTRY_CODE + digits
    return digits if 11 <= len(digits) <= 15 else None


def is_placeholder_email(email: Optional[str]) -> bool:
    return bool(email) and email.strip().lower().endswith(PLACEHOLDER_SUFFIX)


def dedupe_key(email: Optional[str], phone: Optional[str]) -> Optional[str]:
    e = canonical_email(email)
    if e and not is_placeholder_email(e):
        return hashlib.sha256(f"email:{e}".encode()).hexdigest()
    p = normalize_phone(phone)
    if p:
        return hashlib.sha256(f"phone:{p}".encode()).hexdigest()
    return None


def _suppression_key() -> bytes:
    key = talent_config.TALENT_SUPPRESSION_KEY or settings.SECRET_KEY
    return hmac.new(key.encode(), b"talent-suppression-v1", hashlib.sha256).digest()


def suppression_hash(kind: str, value: Optional[str]) -> Optional[str]:
    if kind == "email":
        canonical = canonical_email(value)
    elif kind == "phone":
        canonical = normalize_phone(value)
    else:
        raise ValueError(f"unknown suppression kind {kind!r}")
    if not canonical:
        return None
    return hmac.new(_suppression_key(), f"{kind}:{canonical}".encode(), hashlib.sha256).hexdigest()


# ── Email validity ───────────────────────────────────────────────────────────

EMAIL_SENDABLE = ("valid", "role", "unknown")  # statuses a send may go to


def _is_role_mailbox(email: str) -> bool:
    try:
        from app.services.fast_resume_parser import is_role_mailbox

        return bool(is_role_mailbox(email))
    except Exception:  # parser import problems must not break validation
        return email.split("@", 1)[0] in {"hr", "careers", "jobs", "info", "admin", "resumes", "recruitment"}


@lru_cache(maxsize=4096)
def domain_has_mx(domain: str) -> Optional[bool]:
    """True/False from DNS, None when DNS could not answer (treated as unknown)."""
    try:
        import dns.resolver

        try:
            dns.resolver.resolve(domain, "MX", lifetime=4.0)
            return True
        except dns.resolver.NoAnswer:
            # RFC 5321: no MX → the A record is the mail host.
            dns.resolver.resolve(domain, "A", lifetime=4.0)
            return True
        except dns.resolver.NXDOMAIN:
            return False
    except Exception:
        return None


def classify_email(email: Optional[str], check_mx: bool = False) -> str:
    """valid | invalid | placeholder | role | no_mx | unknown

    'role' (hr@, careers@…) is sendable but flagged: it is usually a vendor's
    mailbox, not the candidate. 'unknown' = syntax fine, DNS unavailable."""
    if not email or not email.strip():
        return "invalid"
    if is_placeholder_email(email):
        return "placeholder"
    try:
        from email_validator import EmailNotValidError, validate_email

        validate_email(email.strip(), check_deliverability=False)
    except EmailNotValidError:
        return "invalid"
    domain = email.strip().lower().rsplit("@", 1)[1]
    if domain in DISPOSABLE_DOMAINS:
        return "invalid"
    if check_mx:
        mx = domain_has_mx(domain)
        if mx is False:
            return "no_mx"
        if mx is None:
            return "unknown"
    if _is_role_mailbox(email.strip().lower()):
        return "role"
    return "valid"
