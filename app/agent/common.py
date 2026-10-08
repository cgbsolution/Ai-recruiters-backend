"""
Helpers shared by the Inngest functions.

Function bodies stay thin: each durable step calls one service function in
app.talent inside its own database session, on a worker thread (the services
are synchronous SQLAlchemy code), and returns plain JSON.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, Callable, List

import inngest

from app.database import SessionLocal
from app.talent.config import talent_config


def _with_session(fn: Callable, *args, **kwargs):
    db = SessionLocal()
    try:
        return fn(db, *args, **kwargs)
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


async def run_db(fn: Callable, *args, **kwargs) -> Any:
    """Run `fn(db, *args)` on a thread with a fresh session."""
    return await asyncio.to_thread(_with_session, fn, *args, **kwargs)


def as_utc(naive_utc: datetime) -> datetime:
    """The schema stores naive UTC; Inngest reads a naive datetime as LOCAL time."""
    return naive_utc.replace(tzinfo=timezone.utc) if naive_utc.tzinfo is None else naive_utc


def parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value)


def enabled_orgs(db, loop: str) -> List[int]:
    """Organizations whose `loop` flag is on (empty when the master switch is off)."""
    if not talent_config.TALENT_AGENT_ENABLED:
        return []
    from app.models.talent import TalentSettings

    return [org_id for (org_id,) in db.query(TalentSettings.organization_id).filter(
        TalentSettings.flags[loop].as_boolean().is_(True)).order_by(TalentSettings.organization_id)]


def flag_on(db, organization_id: int, flag: str) -> bool:
    from app.talent.settings import get_settings

    return bool((get_settings(db, organization_id).flags or {}).get(flag, False))


async def step_until_settled(ctx: inngest.Context, step_name: str, call: Callable[[], Any],
                             max_rounds: int = 60) -> dict:
    """Run a send step; while it reports deferred/paused, sleep until its
    retry_at and run it again (each round has its own durable step ids)."""
    result: dict = {}
    for round_no in range(max_rounds):
        suffix = "" if round_no == 0 else f"-retry-{round_no}"
        result = await ctx.step.run(f"{step_name}{suffix}", call)
        if result.get("status") not in ("deferred", "paused"):
            return result
        until = parse_iso(result["retry_at"]) if result.get("retry_at") else None
        if until is None:
            return result
        await ctx.step.sleep_until(f"{step_name}-wait-{round_no}", as_utc(until))
    return {**result, "status": "stopped", "reason": "gave_up_waiting"}


def stop_filter(profile_id: int) -> str:
    """wait_for_event expression matching a stop for this profile."""
    return f"async.data.profile_id == {int(profile_id)}"


def jsonable(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(v) for v in value]
    if hasattr(value, "hex") and not isinstance(value, (bytes, str)):
        return str(value)
    return value

