"""
AI Credit Usage tracking service.

Provides:
- log_usage()     — fire-and-forget logger called after every LLM call
- get_monthly()   — aggregated monthly data for dashboard chart
- get_summary()   — totals across all time / a date range
"""

import logging
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import func, extract, cast, Date
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.models.ai_credit_usage import AiCreditUsage
from app.services.llm_provider import LLMResult

logger = logging.getLogger(__name__)


def log_usage(
    result: LLMResult,
    feature: str,
    job_id: Optional[int] = None,
    candidate_id: Optional[int] = None,
    session_id: Optional[int] = None,
) -> None:
    """Persist an AI credit usage row. Runs synchronously in a short-lived session."""
    try:
        db = SessionLocal()
        row = AiCreditUsage(
            feature=feature,
            provider=result.provider,
            model=result.model,
            tier=result.tier,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            total_tokens=result.total_tokens,
            estimated_cost=result.estimated_cost,
            job_id=job_id,
            candidate_id=candidate_id,
            session_id=session_id,
        )
        db.add(row)
        db.commit()
        db.close()
    except Exception:
        logger.exception("Failed to log AI credit usage")


async def log_usage_async(
    result: LLMResult,
    feature: str,
    job_id: Optional[int] = None,
    candidate_id: Optional[int] = None,
    session_id: Optional[int] = None,
) -> None:
    """Async wrapper that offloads the DB write to a thread."""
    import asyncio
    await asyncio.to_thread(log_usage, result, feature, job_id, candidate_id, session_id)


def _apply_org_filter(q, org_id: Optional[int]):
    """Apply organization_id filter if not None (super_admin sees all)."""
    if org_id is not None:
        q = q.filter(AiCreditUsage.organization_id == org_id)
    return q


def get_monthly_usage(db: Session, year: Optional[int] = None, org_id: Optional[int] = None):
    """
    Return per-feature monthly totals for a given year.
    Returns list of dicts: [{month, feature, total_tokens, estimated_cost, call_count}, ...]
    """
    if year is None:
        year = datetime.utcnow().year

    q = (
        db.query(
            extract("month", AiCreditUsage.created_at).label("month"),
            AiCreditUsage.feature,
            func.sum(AiCreditUsage.total_tokens).label("total_tokens"),
            func.sum(AiCreditUsage.estimated_cost).label("estimated_cost"),
            func.count(AiCreditUsage.id).label("call_count"),
        )
        .filter(extract("year", AiCreditUsage.created_at) == year)
    )
    q = _apply_org_filter(q, org_id)
    rows = (
        q.group_by(
            extract("month", AiCreditUsage.created_at),
            AiCreditUsage.feature,
        )
        .order_by(extract("month", AiCreditUsage.created_at))
        .all()
    )

    return [
        {
            "month": int(r.month),
            "feature": r.feature,
            "total_tokens": int(r.total_tokens or 0),
            "estimated_cost": round(float(r.estimated_cost or 0), 4),
            "call_count": int(r.call_count or 0),
        }
        for r in rows
    ]


def get_weekly_usage(db: Session, org_id: Optional[int] = None):
    """
    Return per-feature daily totals for the CURRENT week (Mon–Fri).
    Each row has a `day_of_week` (0=Mon … 4=Fri) and a `date` label.
    """
    now = datetime.utcnow()
    monday = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    sunday_end = monday + timedelta(days=5)

    day_col = cast(AiCreditUsage.created_at, Date).label("day")

    q = (
        db.query(
            day_col,
            AiCreditUsage.feature,
            func.sum(AiCreditUsage.total_tokens).label("total_tokens"),
            func.sum(AiCreditUsage.estimated_cost).label("estimated_cost"),
            func.count(AiCreditUsage.id).label("call_count"),
        )
        .filter(
            AiCreditUsage.created_at >= monday,
            AiCreditUsage.created_at < sunday_end,
        )
    )
    q = _apply_org_filter(q, org_id)
    rows = q.group_by(day_col, AiCreditUsage.feature).order_by(day_col).all()

    return [
        {
            "date": r.day.isoformat() if r.day else None,
            "day_of_week": r.day.weekday() if r.day else 0,  # 0=Mon
            "feature": r.feature,
            "total_tokens": int(r.total_tokens or 0),
            "estimated_cost": round(float(r.estimated_cost or 0), 4),
            "call_count": int(r.call_count or 0),
        }
        for r in rows
    ]


def get_daily_usage(db: Session, org_id: Optional[int] = None):
    """
    Return per-feature hourly totals for TODAY (0–23 hours).
    Each row has an `hour` field (0–23).
    """
    now = datetime.utcnow()
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    tomorrow_start = today_start + timedelta(days=1)

    hour_col = extract("hour", AiCreditUsage.created_at).label("hour")

    q = (
        db.query(
            hour_col,
            AiCreditUsage.feature,
            func.sum(AiCreditUsage.total_tokens).label("total_tokens"),
            func.sum(AiCreditUsage.estimated_cost).label("estimated_cost"),
            func.count(AiCreditUsage.id).label("call_count"),
        )
        .filter(
            AiCreditUsage.created_at >= today_start,
            AiCreditUsage.created_at < tomorrow_start,
        )
    )
    q = _apply_org_filter(q, org_id)
    rows = q.group_by(hour_col, AiCreditUsage.feature).order_by(hour_col).all()

    return [
        {
            "hour": int(r.hour),
            "feature": r.feature,
            "total_tokens": int(r.total_tokens or 0),
            "estimated_cost": round(float(r.estimated_cost or 0), 4),
            "call_count": int(r.call_count or 0),
        }
        for r in rows
    ]


def _get_period_bounds(period: str, now: datetime, year: Optional[int] = None):
    """Return (current_start, previous_start, previous_end) for trend comparison."""
    if period == "daily":
        current_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        previous_end = current_start
        previous_start = previous_end - timedelta(days=1)
    elif period == "weekly":
        current_start = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
        previous_end = current_start
        previous_start = previous_end - timedelta(days=7)
    elif period == "monthly":
        current_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        previous_end = current_start
        if now.month == 1:
            previous_start = datetime(now.year - 1, 12, 1)
        else:
            previous_start = datetime(now.year, now.month - 1, 1)
    else:
        return None, None, None
    return current_start, previous_start, previous_end


def _calc_change(current: float, previous: float) -> float:
    """Percentage change, rounded to 1 decimal."""
    if previous == 0:
        return 0.0
    return round(((current - previous) / previous) * 100, 1)


def _aggregate_totals(db: Session, start, end=None, org_id: Optional[int] = None):
    """Sum tokens/cost in a time range."""
    q = db.query(
        func.coalesce(func.sum(AiCreditUsage.input_tokens), 0).label("input_tokens"),
        func.coalesce(func.sum(AiCreditUsage.output_tokens), 0).label("output_tokens"),
        func.coalesce(func.sum(AiCreditUsage.total_tokens), 0).label("total_tokens"),
        func.coalesce(func.sum(AiCreditUsage.estimated_cost), 0).label("estimated_cost"),
        func.count(AiCreditUsage.id).label("call_count"),
    ).filter(AiCreditUsage.created_at >= start)
    if end:
        q = q.filter(AiCreditUsage.created_at < end)
    q = _apply_org_filter(q, org_id)
    r = q.one()
    return {
        "input_tokens": int(r.input_tokens),
        "output_tokens": int(r.output_tokens),
        "total_tokens": int(r.total_tokens),
        "estimated_cost": round(float(r.estimated_cost), 4),
        "call_count": int(r.call_count),
    }


def _get_mini_chart_data(db: Session, period: str, now: datetime, org_id: Optional[int] = None):
    """
    Return last 5 data points for the mini bar charts.
    Each point has {name, input_tokens, output_tokens, estimated_cost}.
    """
    points = []
    if period == "daily":
        # Last 5 hours
        for i in range(4, -1, -1):
            h = now.hour - i
            if h < 0:
                points.append({"name": "", "input_tokens": 0, "output_tokens": 0, "estimated_cost": 0})
                continue
            start = now.replace(hour=h, minute=0, second=0, microsecond=0)
            end = start + timedelta(hours=1)
            t = _aggregate_totals(db, start, end, org_id=org_id)
            points.append({"name": f"{h:02d}:00", **{k: t[k] for k in ["input_tokens", "output_tokens", "estimated_cost"]}})
    elif period == "weekly":
        # Last 5 days (Mon-Fri of current week)
        monday = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
        day_names = ["Mon", "Tue", "Wed", "Thu", "Fri"]
        for i in range(5):
            start = monday + timedelta(days=i)
            end = start + timedelta(days=1)
            t = _aggregate_totals(db, start, end, org_id=org_id)
            points.append({"name": day_names[i], **{k: t[k] for k in ["input_tokens", "output_tokens", "estimated_cost"]}})
    else:
        # Last 5 months
        for i in range(4, -1, -1):
            month = now.month - i
            year = now.year
            if month <= 0:
                month += 12
                year -= 1
            start = datetime(year, month, 1)
            if month == 12:
                end = datetime(year + 1, 1, 1)
            else:
                end = datetime(year, month + 1, 1)
            MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                      "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
            t = _aggregate_totals(db, start, end, org_id=org_id)
            points.append({"name": MONTHS[month - 1], **{k: t[k] for k in ["input_tokens", "output_tokens", "estimated_cost"]}})
    return points


def get_usage_summary(db: Session, period: str = "all", year: Optional[int] = None, org_id: Optional[int] = None):
    """
    Aggregated totals — per-feature AND grand totals.
    period: "all" | "monthly" | "weekly" | "daily"
    Returns { totals: {...}, features: [...], trends: {...}, chart_data: [...] }
    """
    now = datetime.utcnow()

    q = db.query(
        AiCreditUsage.feature,
        func.sum(AiCreditUsage.input_tokens).label("input_tokens"),
        func.sum(AiCreditUsage.output_tokens).label("output_tokens"),
        func.sum(AiCreditUsage.total_tokens).label("total_tokens"),
        func.sum(AiCreditUsage.estimated_cost).label("estimated_cost"),
        func.count(AiCreditUsage.id).label("call_count"),
    )

    # Apply time filter
    if period == "daily":
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        q = q.filter(AiCreditUsage.created_at >= today_start)
    elif period == "weekly":
        monday = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
        q = q.filter(AiCreditUsage.created_at >= monday)
    elif period == "monthly":
        if year:
            q = q.filter(extract("year", AiCreditUsage.created_at) == year)
        else:
            q = q.filter(extract("year", AiCreditUsage.created_at) == now.year)

    q = _apply_org_filter(q, org_id)
    rows = q.group_by(AiCreditUsage.feature).all()

    features = [
        {
            "feature": r.feature,
            "input_tokens": int(r.input_tokens or 0),
            "output_tokens": int(r.output_tokens or 0),
            "total_tokens": int(r.total_tokens or 0),
            "estimated_cost": round(float(r.estimated_cost or 0), 4),
            "call_count": int(r.call_count or 0),
        }
        for r in rows
    ]

    totals = {
        "input_tokens": sum(f["input_tokens"] for f in features),
        "output_tokens": sum(f["output_tokens"] for f in features),
        "total_tokens": sum(f["total_tokens"] for f in features),
        "estimated_cost": round(sum(f["estimated_cost"] for f in features), 4),
        "call_count": sum(f["call_count"] for f in features),
    }

    # Trend comparison with previous period
    trends = {"input_tokens": 0.0, "output_tokens": 0.0, "estimated_cost": 0.0}
    current_start, prev_start, prev_end = _get_period_bounds(period, now, year)
    if current_start is not None:
        prev_totals = _aggregate_totals(db, prev_start, prev_end, org_id=org_id)
        trends["input_tokens"] = _calc_change(totals["input_tokens"], prev_totals["input_tokens"])
        trends["output_tokens"] = _calc_change(totals["output_tokens"], prev_totals["output_tokens"])
        trends["estimated_cost"] = _calc_change(totals["estimated_cost"], prev_totals["estimated_cost"])

    # Mini chart data for summary cards
    chart_data = _get_mini_chart_data(db, period, now, org_id=org_id)

    return {"totals": totals, "features": features, "trends": trends, "chart_data": chart_data}


def get_cost_monthly(db: Session, org_id: Optional[int] = None):
    """
    Per-feature daily cost for the current month (1st to last day).
    Returns [{date, date_label, day, feature, estimated_cost}, ...]
    """
    now = datetime.utcnow()
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if now.month == 12:
        month_end = datetime(now.year + 1, 1, 1)
    else:
        month_end = datetime(now.year, now.month + 1, 1)

    day_col = cast(AiCreditUsage.created_at, Date).label("day")

    MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
              "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

    q = (
        db.query(
            day_col,
            AiCreditUsage.feature,
            func.sum(AiCreditUsage.estimated_cost).label("estimated_cost"),
        )
        .filter(
            AiCreditUsage.created_at >= month_start,
            AiCreditUsage.created_at < month_end,
        )
    )
    q = _apply_org_filter(q, org_id)
    rows = q.group_by(day_col, AiCreditUsage.feature).order_by(day_col).all()

    return [
        {
            "date": r.day.isoformat() if r.day else None,
            "date_label": f"{r.day.day:02d} {MONTHS[r.day.month - 1]}" if r.day else "",
            "day": r.day.day if r.day else 1,
            "feature": r.feature,
            "estimated_cost": round(float(r.estimated_cost or 0), 4),
        }
        for r in rows
    ]


def get_cost_weekly(db: Session, org_id: Optional[int] = None):
    """
    Per-feature daily cost for the current week (Mon–Fri).
    Returns [{date, day_label, feature, estimated_cost}, ...]
    """
    now = datetime.utcnow()
    monday = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    friday_end = monday + timedelta(days=5)

    DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri"]
    day_col = cast(AiCreditUsage.created_at, Date).label("day")

    q = (
        db.query(
            day_col,
            AiCreditUsage.feature,
            func.sum(AiCreditUsage.estimated_cost).label("estimated_cost"),
        )
        .filter(
            AiCreditUsage.created_at >= monday,
            AiCreditUsage.created_at < friday_end,
        )
    )
    q = _apply_org_filter(q, org_id)
    rows = q.group_by(day_col, AiCreditUsage.feature).order_by(day_col).all()

    return [
        {
            "date": r.day.isoformat() if r.day else None,
            "day_label": DAY_NAMES[r.day.weekday()] if r.day else "",
            "day_of_week": r.day.weekday() if r.day else 0,
            "feature": r.feature,
            "estimated_cost": round(float(r.estimated_cost or 0), 4),
        }
        for r in rows
    ]


def get_cost_daily(db: Session, org_id: Optional[int] = None):
    """
    Per-feature hourly cost for today (0–23h).
    Returns [{hour, feature, estimated_cost}, ...]
    """
    now = datetime.utcnow()
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    tomorrow_start = today_start + timedelta(days=1)

    hour_col = extract("hour", AiCreditUsage.created_at).label("hour")

    q = (
        db.query(
            hour_col,
            AiCreditUsage.feature,
            func.sum(AiCreditUsage.estimated_cost).label("estimated_cost"),
        )
        .filter(
            AiCreditUsage.created_at >= today_start,
            AiCreditUsage.created_at < tomorrow_start,
        )
    )
    q = _apply_org_filter(q, org_id)
    rows = q.group_by(hour_col, AiCreditUsage.feature).order_by(hour_col).all()

    return [
        {
            "hour": int(r.hour),
            "feature": r.feature,
            "estimated_cost": round(float(r.estimated_cost or 0), 4),
        }
        for r in rows
    ]
