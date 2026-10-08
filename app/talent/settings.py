"""
Per-organization Talent Agent settings and loop flags.

A loop runs only when BOTH the global TALENT_AGENT_ENABLED switch is on and
the organization's flag for that loop is on. Organizations without a
talent_settings row get the defaults below, with every loop off.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from app.models.talent import TalentJobSettings, TalentSettings
from app.talent.config import talent_config

LOOPS = ("refresh", "match", "invite", "interview", "live_voice", "retention")
# Sub-switches that are not loops of their own:
#   refresh_rolling — the daily sweep enrolls every due profile (off: only pilot campaigns run)
EXTRA_FLAGS = ("refresh_rolling",)
AUTONOMY_MODES = ("autopilot", "copilot")
INTERVIEW_MODES = ("async_text", "async_voice", "live_voice")

DEFAULT_CONSENT_TEXT = (
    "You can be contacted by email about keeping your profile up to date, about job openings "
    "that match it, and about interviews you agree to. You can withdraw this at any time from "
    "the link in every message."
)


def get_settings(db: Session, organization_id: Optional[int]) -> TalentSettings:
    """The org's settings row, or an unsaved one holding the defaults."""
    row = None
    if organization_id is not None:
        row = db.query(TalentSettings).filter(TalentSettings.organization_id == organization_id).first()
    if row is None:
        row = TalentSettings(
            organization_id=organization_id, flags={}, timezone="Asia/Kolkata", quiet_hours_start=21,
            quiet_hours_end=9, refresh_after_days=180, retention_months=24, default_autonomy="copilot",
            consent_text=None,
        )
    return row


def get_or_create_settings(db: Session, organization_id: int) -> TalentSettings:
    row = db.query(TalentSettings).filter(TalentSettings.organization_id == organization_id).first()
    if row is None:
        row = get_settings(db, None)
        row.organization_id = organization_id
        db.add(row)
        db.flush()
    return row


def loop_enabled(db: Session, organization_id: Optional[int], loop: str) -> bool:
    if loop not in LOOPS:
        raise ValueError(f"unknown loop {loop!r}")
    if not talent_config.TALENT_AGENT_ENABLED or organization_id is None:
        return False
    flags = get_settings(db, organization_id).flags or {}
    return bool(flags.get(loop, False))


def consent_text(row: TalentSettings) -> str:
    return row.consent_text or DEFAULT_CONSENT_TEXT


def org_zone(row: TalentSettings) -> ZoneInfo:
    try:
        return ZoneInfo(row.timezone or "Asia/Kolkata")
    except Exception:
        return ZoneInfo("Asia/Kolkata")


def next_send_window(row: TalentSettings, now_utc: Optional[datetime] = None) -> Optional[datetime]:
    """None if `now` is inside the sending window; otherwise the naive-UTC time
    the window next opens. Quiet hours run from quiet_hours_start (local) to
    quiet_hours_end (local) and may wrap midnight (21 → 9)."""
    now_utc = now_utc or datetime.utcnow()
    zone = org_zone(row)
    local = now_utc.replace(tzinfo=ZoneInfo("UTC")).astimezone(zone)
    start, end = int(row.quiet_hours_start), int(row.quiet_hours_end)
    if start == end:  # no quiet hours
        return None
    hour = local.hour
    quiet = (start <= hour or hour < end) if start > end else (start <= hour < end)
    if not quiet:
        return None
    opens = local.replace(hour=end, minute=0, second=0, microsecond=0)
    if opens <= local:
        opens += timedelta(days=1)
    return opens.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)


def freshness_window(row: TalentSettings) -> timedelta:
    return timedelta(days=int(row.refresh_after_days or 180))


def get_job_settings(db: Session, job_id: int, organization_id: Optional[int] = None) -> TalentJobSettings:
    row = db.get(TalentJobSettings, job_id)
    if row is None:
        default_autonomy = "copilot"
        if organization_id is not None:
            default_autonomy = get_settings(db, organization_id).default_autonomy or "copilot"
        row = TalentJobSettings(
            job_id=job_id, autonomy_mode=default_autonomy, top_k=25, interview_mode="async_text",
            question_count=10, time_limit_minutes=30, slot_minutes=45, relocation_ok=True,
        )
        db.add(row)
        db.flush()
    return row
