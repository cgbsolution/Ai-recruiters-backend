"""
Is the background worker actually running?

Matching, outreach and interviews all run in the Talent Agent worker, called
by Inngest. If the worker isn't deployed, or Inngest can't reach it, nothing
fails visibly — work just never starts. The worker records a check-in when
Inngest syncs it (app.agent.worker) and every 10 minutes after (cron function
app.agent.heartbeat); the Settings page and the job page warn when it is
missing or stale, naming the URL to sync.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.models.talent import AgentHeartbeat
from app.talent.config import talent_config

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


def sync_url() -> str:
    """The URL to sync in Inngest: the API's own /api/inngest unless the worker
    is deployed as a separate service (see app.agent.proxy)."""
    if talent_config.TALENT_EMBEDDED_WORKER or talent_config.TALENT_SERVE_IN_API:
        base = talent_config.TALENT_PUBLIC_API_URL.rstrip("/") or "https://<your API address>"
        return f"{base}/api/inngest"
    return "https://<your worker address>/api/inngest"


def worker_problem(db: Session, now: Optional[datetime] = None) -> Optional[str]:
    """Why background work won't run, in words a recruiter can pass on — or None."""
    status = worker_status(db, now)
    if status["ok"]:
        return None
    if status["last_seen_at"] is None:
        return ("The Talent Agent worker has never checked in, so matching, emails and interviews won't run. "
                f"In Inngest Cloud, open Apps → Sync new app and enter {sync_url()} — it checks in as soon as "
                f"the sync succeeds, then every {INTERVAL_MINUTES} minutes.")
    return (f"The Talent Agent worker last checked in at {status['last_seen_at']:%d %b %Y, %H:%M} UTC, so background "
            f"work (matching, emails, interviews) is not running. In Inngest Cloud, check that the app at "
            f"{sync_url()} is synced and not paused, then resync it.")
