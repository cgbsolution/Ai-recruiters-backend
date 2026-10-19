"""
Outbound message templates. Every email carries: who is writing and why, the
action link, a one-click unsubscribe and a "your data" link. All dynamic
values are HTML-escaped.

Each organization brands its emails (Talent Agent → Settings → Email
branding): sender name, logo, button colour, sign-off, and its own opening
paragraph for the profile-update email — see brand_context().

render(kind, ctx) -> (subject, text, html)
"""

from __future__ import annotations

import re
from html import escape
from typing import Callable, Dict, List, Optional, Tuple

from app.talent.config import talent_config

Rendered = Tuple[str, str, str]

DEFAULT_COLOR = "#465fff"
_HEX = re.compile(r"^#[0-9a-fA-F]{6}$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def valid_logo_url(url: Optional[str]) -> bool:
    """A logo must be an https image URL: it's embedded in every email."""
    return bool(url) and url.startswith("https://") and len(url) <= 500 and not re.search(r"[\s\"'<>\\]", url)


def valid_color(color: Optional[str]) -> bool:
    return bool(color) and bool(_HEX.match(color))


def clean_line(value: Optional[str], limit: int) -> Optional[str]:
    """One line of plain text (it ends up in a From header and subjects)."""
    value = " ".join(_CONTROL.sub(" ", value or "").split())[:limit]
    return value or None


def brand_context(org_name: str, *, name: Optional[str] = None, logo_url: Optional[str] = None,
                  color: Optional[str] = None, signature: Optional[str] = None,
                  message: Optional[str] = None) -> dict:
    """The organization's branding with safe fallbacks: anything invalid is
    replaced by the default rather than reaching an email."""
    name = clean_line(name, 120) or org_name
    return {
        "name": name,
        "logo_url": logo_url if valid_logo_url(logo_url) else None,
        "color": color if valid_color(color) else DEFAULT_COLOR,
        "signature": (signature or "").strip()[:300] or f"The {name} talent team",
        "message": (message or "").strip()[:800] or None,
    }


def _brand(ctx: dict) -> dict:
    return ctx.get("brand") or brand_context(ctx.get("org_name") or "Our recruitment team")


def _text_color(background: str) -> str:
    """White or dark text, whichever reads on the brand colour."""
    r, g, b = (int(background[i:i + 2], 16) / 255 for i in (1, 3, 5))
    lum = 0.2126 * r ** 2.2 + 0.7152 * g ** 2.2 + 0.0722 * b ** 2.2
    return "#101828" if lum > 0.4 else "#ffffff"


def _button(url: str, label: str, ctx: Optional[dict] = None) -> str:
    color = _brand(ctx or {})["color"]
    return (
        f'<p style="margin:24px 0"><a href="{escape(url)}" style="background:{color};color:{_text_color(color)};'
        f'padding:12px 20px;border-radius:8px;text-decoration:none;font-weight:600;display:inline-block">'
        f"{escape(label)}</a></p>"
    )


def _footer_text(ctx: dict) -> str:
    lines = [
        "",
        _brand(ctx)["signature"],
        "",
        "--",
        f"You are receiving this because {ctx['org_name']} has your CV in its candidate database.",
        f"Your data, export or delete: {ctx['my_data_url']}",
        f"Stop all messages: {ctx['unsubscribe_url']}",
    ]
    if talent_config.TALENT_SENDER_POSTAL_ADDRESS:
        lines.append(talent_config.TALENT_SENDER_POSTAL_ADDRESS)
    return "\n".join(lines)


def _footer_html(ctx: dict) -> str:
    postal = (
        f"<br>{escape(talent_config.TALENT_SENDER_POSTAL_ADDRESS)}" if talent_config.TALENT_SENDER_POSTAL_ADDRESS else ""
    )
    return (
        '<hr style="border:none;border-top:1px solid #e4e7ec;margin:32px 0 16px">'
        f'<p style="color:#667085;font-size:12px;line-height:18px">You are receiving this because '
        f"{escape(ctx['org_name'])} has your CV in its candidate database.<br>"
        f'<a href="{escape(ctx["my_data_url"])}" style="color:#667085">See, export or delete your data</a> · '
        f'<a href="{escape(ctx["unsubscribe_url"])}" style="color:#667085">Stop all messages</a>{postal}</p>'
    )


def _wrap(body_html: str, ctx: dict) -> str:
    b = _brand(ctx)
    header = (
        f'<img src="{escape(b["logo_url"])}" alt="{escape(b["name"])}" height="36" '
        'style="display:block;height:36px;max-width:220px;border:0">'
        if b["logo_url"] else
        f'<div style="font-size:18px;font-weight:700;color:{b["color"]}">{escape(b["name"])}</div>'
    )
    signature = "<br>".join(escape(line) for line in b["signature"].splitlines())
    return (
        '<div style="font-family:Arial,Helvetica,sans-serif;color:#101828;font-size:15px;line-height:22px;'
        'max-width:560px;margin:0 auto;padding:24px">'
        f'<div style="padding-bottom:14px;margin-bottom:24px;border-bottom:3px solid {b["color"]}">{header}</div>'
        f'{body_html}<p style="margin-top:28px">{signature}</p>{_footer_html(ctx)}</div>'
    )


def _greeting(ctx: dict) -> str:
    return f"Hi {ctx['first_name']}," if ctx.get("first_name") else "Hello,"


# ── Loop A: refresh (every 3 months by default) ─────────────────────────────

def _snapshot(ctx: dict) -> List[Tuple[str, str]]:
    """What we hold today, so the candidate sees at a glance what's out of date."""
    rows = []
    if ctx.get("profile_role"):
        rows.append(("Current role", ctx["profile_role"]))
    if ctx.get("profile_skills"):
        rows.append(("Skills", ", ".join(ctx["profile_skills"])))
    if ctx.get("profile_updated"):
        rows.append(("Last updated", ctx["profile_updated"]))
    return rows


def _snapshot_html(rows: List[Tuple[str, str]]) -> str:
    if not rows:
        return ""
    cells = "".join(
        f'<tr><td style="color:#667085;padding:3px 16px 3px 0;vertical-align:top;white-space:nowrap">{escape(k)}</td>'
        f'<td style="padding:3px 0">{escape(v)}</td></tr>' for k, v in rows)
    return ('<div style="background:#f9fafb;border:1px solid #eaecf0;border-radius:8px;padding:12px 16px;'
            f'margin:16px 0;font-size:14px"><table role="presentation" cellpadding="0" cellspacing="0">{cells}</table></div>')


def _refresh_copy(touch_no: int, ctx: dict) -> Tuple[str, str, str, str]:
    """(subject, opening, ask, small print) — f-strings only: a brand name is
    the organization's own text and must never be treated as a template."""
    org, first = _brand(ctx)["name"], ctx.get("first_name")
    if touch_no == 1:
        subject = (f"{first}, is your profile with {org} still up to date?" if first
                   else f"Is your profile with {org} still up to date?")
        opening = _brand(ctx)["message"] or (
            f"It's been a few months since your profile with {org} was last updated. Keeping it current means we "
            "only contact you about roles that fit where you are now.")
        ask = ("Has anything changed: a new role, new skills or certifications, your notice period or salary "
               "expectations? Updating takes about a minute, and you can upload your latest CV (PDF or Word).")
        small = "Nothing has changed? Open the page and confirm in one click."
    elif touch_no == 2:
        subject = f"Quick reminder: update your profile with {org}"
        opening = f"A quick reminder about your profile with {org}."
        ask = ("If you've picked up new skills or moved roles, updating takes about a minute, and it keeps the "
               "roles we send you relevant.")
        small = ""
    else:
        subject = f"Last reminder: keep your {org} profile current"
        opening = "This is our last reminder for now."
        ask = ("If you'd like roles that match your experience today, take a minute to update your profile. "
               "Not looking at the moment? You can say so on the same page, or stop all messages with the link below.")
        small = ""
    return subject, opening, ask, small


def _refresh(touch_no: int) -> Callable[[dict], Rendered]:
    def render(ctx: dict) -> Rendered:
        subject, opening, ask, small = _refresh_copy(touch_no, ctx)
        rows = _snapshot(ctx) if touch_no < 3 else []
        expiry = f"This link is personal and expires in {talent_config.TALENT_REFRESH_LINK_DAYS} days."
        text = "\n\n".join(filter(None, [
            _greeting(ctx), opening,
            ("What we have on file:\n" + "\n".join(f"  {k}: {v}" for k, v in rows)) if rows else "",
            ask, f"Update your profile: {ctx['action_url']}", " ".join(filter(None, [small, expiry])),
        ])) + "\n" + _footer_text(ctx)
        html = _wrap(
            f"<p>{escape(_greeting(ctx))}</p><p>{escape(opening)}</p>"
            + (f'<p style="margin-bottom:0">What we have on file:</p>{_snapshot_html(rows)}' if rows else "")
            + f"<p>{escape(ask)}</p>{_button(ctx['action_url'], 'Update my profile', ctx)}"
            + f'<p style="color:#667085;font-size:13px">{escape(" ".join(filter(None, [small, expiry])))}</p>',
            ctx,
        )
        return subject, text, html

    return render


# ── Loop C: invites ─────────────────────────────────────────────────────────

def _invite(reminder: bool) -> Callable[[dict], Rendered]:
    def render(ctx: dict) -> Rendered:
        role = ctx["job_title"]
        subject = (f"Reminder: are you interested in the {role} role?" if reminder
                   else f"{role} at {ctx['org_name']} — interested?")
        summary = ctx.get("role_summary") or ""
        intro = (f"Following up on the {role} role we wrote about." if reminder else
                 f"We think your background fits a {role} opening ({ctx.get('job_location') or 'location on request'}).")
        ask = ("If you're interested, tell us on the page below and confirm your current and expected salary and "
               "your notice period. Not interested? One click and we'll close it.")
        text = f"{_greeting(ctx)}\n\n{intro}\n\n{summary}\n\n{ask}\n\nRespond: {ctx['action_url']}\n{_footer_text(ctx)}"
        html = _wrap(
            f"<p>{escape(_greeting(ctx))}</p><p>{escape(intro)}</p>"
            + (f"<p>{escape(summary)}</p>" if summary else "")
            + f"<p>{escape(ask)}</p>{_button(ctx['action_url'], 'Interested? Respond', ctx)}",
            ctx,
        )
        return subject, text, html

    return render


def _consent_request(reminder: bool) -> Callable[[dict], Rendered]:
    """A matched candidate we have no consent for: one ask (and one reminder)
    for permission to email them about this role, then interested or not."""
    def render(ctx: dict) -> Rendered:
        role, org = ctx["job_title"], ctx["org_name"]
        subject = (f"Reminder: may {org} tell you about the {role} role?" if reminder
                   else f"{role} at {org}: may we tell you about it?")
        summary = ctx.get("role_summary") or ""
        intro = (f"Following up on the {role} role we wrote about." if reminder else
                 f"We think your background fits a {role} opening "
                 f"({ctx.get('job_location') or 'location on request'}).")
        ask = ("Before we send you anything more, we need your permission. Open the page below to agree to hear "
               "from us by email about this role and its interview, then tell us whether you're interested.")
        small = ("Don't want to hear from us? Ignore this email and we won't write about this role again"
                 + (" after one reminder." if not reminder else "."))
        text = (f"{_greeting(ctx)}\n\n{intro}\n\n{summary}\n\n{ask}\n\nRespond: {ctx['action_url']}\n\n{small}\n"
                + _footer_text(ctx))
        html = _wrap(
            f"<p>{escape(_greeting(ctx))}</p><p>{escape(intro)}</p>"
            + (f"<p>{escape(summary)}</p>" if summary else "")
            + f"<p>{escape(ask)}</p>{_button(ctx['action_url'], 'Give consent and respond', ctx)}"
            + f'<p style="color:#667085;font-size:13px">{escape(small)}</p>',
            ctx,
        )
        return subject, text, html

    return render


# ── Loop D: interviews ──────────────────────────────────────────────────────

def _pick_window(ctx: dict) -> str:
    days = ctx.get("booking_days") or 7
    return f"Pick any date and time that suits you in the next {days} days."


def _interview_offer(ctx: dict) -> Rendered:
    role = ctx["job_title"]
    subject = f"Choose a time for your {role} interview"
    disclosure = ("This first interview is conducted by an AI interviewer. You'll be told this again before it "
                  "starts, asked for consent to record, and you can ask to speak to a person instead.")
    text = (f"{_greeting(ctx)}\n\nThanks for your interest in the {role} role. {_pick_window(ctx)}"
            f"\n\n{disclosure}\n\nPick a time: {ctx['action_url']}\n{_footer_text(ctx)}")
    html = _wrap(
        f"<p>{escape(_greeting(ctx))}</p><p>Thanks for your interest in the {escape(role)} role. "
        f"{escape(_pick_window(ctx))}</p>"
        f"<p>{escape(disclosure)}</p>{_button(ctx['action_url'], 'Pick a time', ctx)}",
        ctx,
    )
    return subject, text, html


def _interview_confirmed(ctx: dict) -> Rendered:
    role = ctx["job_title"]
    subject = f"Confirmed: {role} interview on {ctx['when_label']}"
    text = (f"{_greeting(ctx)}\n\nYour interview is booked for {ctx['when_label']}.\n"
            f"Your interview link (it opens 15 minutes before): {ctx['action_url']}\n{_footer_text(ctx)}")
    html = _wrap(
        f"<p>{escape(_greeting(ctx))}</p><p>Your interview is booked for <b>{escape(ctx['when_label'])}</b>.</p>"
        "<p>Use the button below at that time to start your interview. It opens 15 minutes before.</p>"
        f"{_button(ctx['action_url'], 'Open interview link', ctx)}",
        ctx,
    )
    return subject, text, html


def _interview_reminder(lead: str) -> Callable[[dict], Rendered]:
    def render(ctx: dict) -> Rendered:
        subject = f"Reminder: your {ctx['job_title']} interview {lead}"
        text = (f"{_greeting(ctx)}\n\nA reminder that your interview is at {ctx['when_label']}.\n"
                f"Interview page: {ctx['action_url']}\n{_footer_text(ctx)}")
        html = _wrap(
            f"<p>{escape(_greeting(ctx))}</p><p>A reminder that your interview is at "
            f"<b>{escape(ctx['when_label'])}</b>.</p>{_button(ctx['action_url'], 'Open interview page', ctx)}",
            ctx,
        )
        return subject, text, html

    return render


def _interview_missed(ctx: dict) -> Rendered:
    subject = f"We missed you — pick a new time for the {ctx['job_title']} interview"
    text = (f"{_greeting(ctx)}\n\nIt looks like the interview didn't happen. {_pick_window(ctx)}"
            f"\n\n{ctx['action_url']}\n{_footer_text(ctx)}")
    html = _wrap(
        f"<p>{escape(_greeting(ctx))}</p><p>It looks like the interview didn't happen. "
        f"{escape(_pick_window(ctx))}</p>{_button(ctx['action_url'], 'Pick a new time', ctx)}",
        ctx,
    )
    return subject, text, html


# ── Decisions (recruiter-approved only) ─────────────────────────────────────

def _not_moving_forward(ctx: dict) -> Rendered:
    role = ctx["job_title"]
    subject = f"Update on the {role} role"
    body = (f"Thank you for your time and interest in the {role} role. After review, the hiring team has decided "
            "not to move forward with your application for this role. We'll keep your profile on file and may "
            "contact you about other roles, unless you tell us otherwise.")
    text = f"{_greeting(ctx)}\n\n{body}\n{_footer_text(ctx)}"
    return subject, text, _wrap(f"<p>{escape(_greeting(ctx))}</p><p>{escape(body)}</p>", ctx)


TEMPLATES: Dict[str, Callable[[dict], Rendered]] = {
    "refresh_touch_1": _refresh(1),
    "refresh_touch_2": _refresh(2),
    "refresh_touch_3": _refresh(3),
    "invite": _invite(False),
    "invite_reminder": _invite(True),
    "consent_request": _consent_request(False),
    "consent_request_reminder": _consent_request(True),
    "interview_offer": _interview_offer,
    "interview_confirmed": _interview_confirmed,
    "interview_reminder_24h": _interview_reminder("tomorrow"),
    "interview_reminder_1h": _interview_reminder("in one hour"),
    "interview_missed": _interview_missed,
    "not_moving_forward": _not_moving_forward,
}

# Which purpose each kind needs consent for — the outbox enforces it.
TEMPLATE_PURPOSE = {
    "refresh_touch_1": "refresh", "refresh_touch_2": "refresh", "refresh_touch_3": "refresh",
    "invite": "jobs", "invite_reminder": "jobs", "not_moving_forward": "jobs",
    # Asks for the "jobs" consent it lacks: the outbox checks can_request_consent instead.
    "consent_request": "consent_request", "consent_request_reminder": "consent_request",
    "interview_offer": "interview", "interview_confirmed": "interview", "interview_reminder_24h": "interview",
    "interview_reminder_1h": "interview", "interview_missed": "interview",
}


def render(kind: str, ctx: dict) -> Rendered:
    return TEMPLATES[kind](ctx)
