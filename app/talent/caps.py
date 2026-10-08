"""
Daily send caps per sender domain, ramped from the day the domain first sent
(TALENT_SEND_RAMP, default 500 → 2,000 → 5,000 a day, one step a week).

A send reserves one unit with a single conditional UPDATE, so any number of
concurrent workers can never push a day past its cap. Bulk refresh mail may
use only TALENT_BULK_SHARE of the cap; the rest is held back for invites,
interview reminders and replies to candidates ("transactional"), so a large
refresh backlog can't starve them.

Days are UTC days.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Optional

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.models.talent import SendCounter, SenderDomain
from app.talent.config import talent_config

BULK = "bulk"
TRANSACTIONAL = "transactional"


def default_ramp() -> list:
    return [{"from_day": d, "cap": c} for d, c in talent_config.send_ramp]


def get_domain(db: Session, domain: str, today: Optional[date] = None) -> SenderDomain:
    row = db.get(SenderDomain, domain)
    if row is None:
        db.execute(
            pg_insert(SenderDomain.__table__)
            .values(domain=domain, ramp=default_ramp(), ramp_started_on=today or datetime.utcnow().date(), paused=False)
            .on_conflict_do_nothing(index_elements=["domain"])
        )
        db.flush()
        row = db.get(SenderDomain, domain)
    return row


def cap_for(row: SenderDomain, day: date) -> int:
    age = max(0, (day - row.ramp_started_on).days)
    cap = 0
    for step in sorted(row.ramp or default_ramp(), key=lambda s: s["from_day"]):
        if age >= int(step["from_day"]):
            cap = int(step["cap"])
    return cap


def bulk_cap_for(row: SenderDomain, day: date) -> int:
    return int(cap_for(row, day) * float(talent_config.TALENT_BULK_SHARE))


def usage(db: Session, domain: str, day: date) -> dict:
    row = db.get(SendCounter, (domain, day))
    dom = get_domain(db, domain)
    return {
        "domain": domain, "day": day.isoformat(), "cap": cap_for(dom, day), "bulk_cap": bulk_cap_for(dom, day),
        "total": row.total if row else 0, "bulk": row.bulk if row else 0, "paused": dom.paused,
        "paused_reason": dom.paused_reason,
    }


def reserve(db: Session, domain: str, priority: str, now: Optional[datetime] = None) -> bool:
    """Take one send from today's budget. True if granted."""
    day = (now or datetime.utcnow()).date()
    dom = get_domain(db, domain, day)
    if dom.paused:
        return False
    cap, bulk_cap = cap_for(dom, day), bulk_cap_for(dom, day)
    db.execute(
        pg_insert(SendCounter.__table__).values(domain=domain, day=day, total=0, bulk=0)
        .on_conflict_do_nothing(index_elements=["domain", "day"])
    )
    t = SendCounter.__table__
    stmt = t.update().where(t.c.domain == domain, t.c.day == day, t.c.total < cap)
    if priority == BULK:
        stmt = stmt.where(t.c.bulk < bulk_cap).values(total=t.c.total + 1, bulk=t.c.bulk + 1)
    else:
        stmt = stmt.values(total=t.c.total + 1)
    return db.execute(stmt).rowcount == 1


def release(db: Session, domain: str, priority: str, now: Optional[datetime] = None) -> None:
    """Give back a reservation whose message certainly did not leave."""
    day = (now or datetime.utcnow()).date()
    t = SendCounter.__table__
    values = {"total": sa.func.greatest(t.c.total - 1, 0)}
    if priority == BULK:
        values["bulk"] = sa.func.greatest(t.c.bulk - 1, 0)
    db.execute(t.update().where(t.c.domain == domain, t.c.day == day).values(**values))


def remaining(db: Session, domain: str, priority: str, now: Optional[datetime] = None) -> int:
    day = (now or datetime.utcnow()).date()
    u = usage(db, domain, day)
    if u["paused"]:
        return 0
    left = u["cap"] - u["total"]
    if priority == BULK:
        left = min(left, u["bulk_cap"] - u["bulk"])
    return max(0, left)


def next_day_start(now: Optional[datetime] = None) -> datetime:
    now = now or datetime.utcnow()
    return datetime(now.year, now.month, now.day) + timedelta(days=1, minutes=5)
