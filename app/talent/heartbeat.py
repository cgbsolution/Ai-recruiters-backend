"""
Is the background worker actually running?

Matching, outreach and interviews all run in the Talent Agent worker, called
by Inngest. If the worker isn't deployed, or Inngest can't reach it, nothing
fails visibly — work just never starts. A cron function (app.agent.heartbeat)
records a check-in every 10 minutes; the Settings page and the job page warn
when it is missing or stale.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.models.talent import AgentHeartbeat

WORKER = "worker"
INTERVAL_MINUTES = 10
STALE_AFTER = timedelta(minutes=25)  # two missed check-ins


def beat(db: Session, name: str = WORKER, now: Optional[datetime] = None) -> dict:
    now = now or datetime.utcnow()
    db.execute(
        pg_insert(AgentHeartbeat.__table__).values(name=name, last_seen_at=now)
        .on_conflict_do_update(index_elements=["name"], set_={"last_seen_at": now})
    )
    db.commit()
    return {"name": name, "at": now.isoformat()}


def worker_status(db: Session, now: Optional[datetime] = None) -> dict:
    now = now or datetime.utcnow()
    row = db.get(AgentHeartbeat, WORKER)
    seen = row.last_seen_at if row else None
    return {"last_seen_at": seen, "ok": bool(seen and now - seen <= STALE_AFTER)}


def worker_problem(db: Session, now: Optional[datetime] = None) -> Optional[str]:
    """Why background work won't run, in words a recruiter can pass on — or None."""
    status = worker_status(db, now)
    if status["ok"]:
        return None
    if status["last_seen_at"] is None:
        return ("The Talent Agent worker has never checked in, so matching, emails and interviews won't run. "
                "Deploy the worker (uvicorn app.agent.worker:app) and sync it in Inngest. It checks in every "
                f"{INTERVAL_MINUTES} minutes once running.")
    return (f"The Talent Agent worker last checked in at {status['last_seen_at']:%d %b %Y, %H:%M} UTC, so background "
            "work (matching, emails, interviews) is not running. Check the worker service and its Inngest sync.")
