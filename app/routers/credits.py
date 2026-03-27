"""
AI Credit Usage analytics endpoints for the admin dashboard.
"""

from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app.services.credit_service import (
    get_cost_daily,
    get_cost_monthly,
    get_cost_weekly,
    get_daily_usage,
    get_monthly_usage,
    get_usage_summary,
    get_weekly_usage,
)

router = APIRouter()


@router.get("/monthly")
def credits_monthly(
    year: Optional[int] = Query(None, description="Year to query (default: current)"),
    db: Session = Depends(get_db),
):
    """Per-feature monthly token usage (Jan-Dec) for the line chart."""
    return get_monthly_usage(db, year=year)


@router.get("/weekly")
def credits_weekly(db: Session = Depends(get_db)):
    """Per-feature daily token usage for the current week (Mon-Fri)."""
    return get_weekly_usage(db)


@router.get("/daily")
def credits_daily(db: Session = Depends(get_db)):
    """Per-feature hourly token usage for today (0-23h)."""
    return get_daily_usage(db)


@router.get("/summary")
def credits_summary(
    period: str = Query("monthly", description="all | monthly | weekly | daily"),
    year: Optional[int] = Query(None),
    db: Session = Depends(get_db),
):
    """Totals + per-feature breakdown, filtered by time period."""
    return get_usage_summary(db, period=period, year=year)


@router.get("/cost/monthly")
def cost_monthly(db: Session = Depends(get_db)):
    """Per-feature daily cost for the current month."""
    return get_cost_monthly(db)


@router.get("/cost/weekly")
def cost_weekly(db: Session = Depends(get_db)):
    """Per-feature daily cost for current week (Mon-Fri)."""
    return get_cost_weekly(db)


@router.get("/cost/daily")
def cost_daily(db: Session = Depends(get_db)):
    """Per-feature hourly cost for today."""
    return get_cost_daily(db)
