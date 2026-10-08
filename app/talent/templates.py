"""
Outbound message templates. Every email carries: who is writing and why, the
action link, a one-click unsubscribe and a "your data" link. All dynamic
values are HTML-escaped.

render(kind, ctx) -> (subject, text, html)
"""

from __future__ import annotations

from html import escape
from typing import Callable, Dict, List, Tuple

from app.talent.config import talent_config

Rendered = Tuple[str, str, str]


def _button(url: str, label: str) -> str:
    return (
        f'<p style="margin:24px 0"><a href="{escape(url)}" style="background:#465fff;color:#fff;'
        f'padding:12px 20px;border-radius:8px;text-decoration:none;font-weight:600">{escape(label)}</a></p>'
    )


def _footer_text(ctx: dict) -> str:
    lines = [
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
    return (
        '<div style="font-family:Arial,Helvetica,sans-serif;color:#101828;font-size:15px;line-height:22px;'
        f'max-width:560px;margin:0 auto;padding:24px">{body_html}{_footer_html(ctx)}</div>'
    )


def _greeting(ctx: dict) -> str:
    return f"Hi {ctx['first_name']}," if ctx.get("first_name") else "Hello,"


# ── Loop A: refresh ─────────────────────────────────────────────────────────

_REFRESH_COPY = {
    1: ("Is your profile with {org} still up to date?",
        "{org} keeps your CV on file so we can tell you about roles that fit. Could you take a minute to "
        "confirm your current role, location, salary expectations and notice period? It keeps the roles we "
        "send you relevant."),
    2: ("Quick check: are you still open to new roles?",
        "A short follow-up: confirming your profile takes about a minute, and you can upload a newer CV if "
        "you have one."),
    3: ("Last reminder: confirm your profile with {org}",
        "This is the last reminder for now. If you'd rather not hear from us, use the link at the bottom and "
        "we will stop."),
}


def _refresh(touch_no: int) -> Callable[[dict], Rendered]:
    def render(ctx: dict) -> Rendered:
        subject_t, body_t = _REFRESH_COPY[touch_no]
        org = ctx["org_name"]
        subject = subject_t.format(org=org)
        body = body_t.format(org=org)
        text = f"{_greeting(ctx)}\n\n{body}\n\nConfirm your profile: {ctx['action_url']}\n{_footer_text(ctx)}"
        html = _wrap(
            f"<p>{escape(_greeting(ctx))}</p><p>{escape(body)}</p>{_button(ctx['action_url'], 'Confirm my profile')}"
            f'<p style="color:#667085;font-size:13px">The link works once and expires in '
            f"{talent_config.TALENT_REFRESH_LINK_DAYS} days.</p>",
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
            + f"<p>{escape(ask)}</p>{_button(ctx['action_url'], 'Interested? Respond')}",
            ctx,
        )
        return subject, text, html

    return render


# ── Loop D: interviews ──────────────────────────────────────────────────────

def _slots_lines(ctx: dict) -> List[str]:
    return [f"• {s}" for s in ctx.get("slot_labels", [])]


def _interview_offer(ctx: dict) -> Rendered:
    role = ctx["job_title"]
    subject = f"Choose a time for your {role} interview"
    disclosure = ("This first interview is conducted by an AI interviewer. You'll be told this again before it "
                  "starts, asked for consent to record, and you can ask to speak to a person instead.")
    slots = _slots_lines(ctx)
    text = (f"{_greeting(ctx)}\n\nThanks for your interest in the {role} role. Please pick one of these times:\n"
            + "\n".join(slots) + f"\n\n{disclosure}\n\nPick a time: {ctx['action_url']}\n{_footer_text(ctx)}")
    html = _wrap(
        f"<p>{escape(_greeting(ctx))}</p><p>Thanks for your interest in the {escape(role)} role. "
        "Please pick one of these times:</p><ul>"
        + "".join(f"<li>{escape(s)}</li>" for s in ctx.get("slot_labels", []))
        + f"</ul><p>{escape(disclosure)}</p>{_button(ctx['action_url'], 'Pick a time')}",
        ctx,
    )
    return subject, text, html


def _interview_confirmed(ctx: dict) -> Rendered:
    role = ctx["job_title"]
    subject = f"Confirmed: {role} interview on {ctx['when_label']}"
    text = (f"{_greeting(ctx)}\n\nYour interview is booked for {ctx['when_label']}.\n"
            f"Join here at that time: {ctx['action_url']}\n{_footer_text(ctx)}")
    html = _wrap(
        f"<p>{escape(_greeting(ctx))}</p><p>Your interview is booked for <b>{escape(ctx['when_label'])}</b>.</p>"
        f"{_button(ctx['action_url'], 'Open interview page')}",
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
            f"<b>{escape(ctx['when_label'])}</b>.</p>{_button(ctx['action_url'], 'Open interview page')}",
            ctx,
        )
        return subject, text, html

    return render


def _interview_missed(ctx: dict) -> Rendered:
    subject = f"We missed you — pick a new time for the {ctx['job_title']} interview"
    text = (f"{_greeting(ctx)}\n\nIt looks like the interview didn't happen. Choose another time:\n"
            + "\n".join(_slots_lines(ctx)) + f"\n\n{ctx['action_url']}\n{_footer_text(ctx)}")
    html = _wrap(
        f"<p>{escape(_greeting(ctx))}</p><p>It looks like the interview didn't happen. Choose another time:</p><ul>"
        + "".join(f"<li>{escape(s)}</li>" for s in ctx.get("slot_labels", []))
        + f"</ul>{_button(ctx['action_url'], 'Pick a new time')}",
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
    "interview_offer": "interview", "interview_confirmed": "interview", "interview_reminder_24h": "interview",
    "interview_reminder_1h": "interview", "interview_missed": "interview",
}


def render(kind: str, ctx: dict) -> Rendered:
    return TEMPLATES[kind](ctx)
