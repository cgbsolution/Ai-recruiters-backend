"""
AI Credit Usage analytics endpoints for the admin dashboard.
"""

from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.auth import get_current_user, get_org_id
from app.database import get_db
from app.models.user import User
from app.services.credit_service import (
    get_cost_daily,
    get_cost_monthly,
    get_cost_weekly,
    get_daily_usage,
    get_monthly_usage,
    get_usage_summary,
    get_weekly_usage,
)

router = APIRouter(dependencies=[Depends(get_current_user)])


def _resolve_org_id(user: User, org_id_override: Optional[int]) -> Optional[int]:
    """Super admins may pass ?org_id=X to filter by a specific org."""
    if org_id_override is not None and user.role == "super_admin":
        return org_id_override
    return get_org_id(user)


@router.get("/monthly")
def credits_monthly(
    year: Optional[int] = Query(None, description="Year to query (default: current)"),
    org_id: Optional[int] = Query(None, description="Filter by org (super_admin only)"),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Per-feature monthly token usage (Jan-Dec) for the line chart."""
    return get_monthly_usage(db, year=year, org_id=_resolve_org_id(user, org_id))


@router.get("/weekly")
def credits_weekly(
    org_id: Optional[int] = Query(None, description="Filter by org (super_admin only)"),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Per-feature daily token usage for the current week (Mon-Fri)."""
    return get_weekly_usage(db, org_id=_resolve_org_id(user, org_id))


@router.get("/daily")
def credits_daily(
    org_id: Optional[int] = Query(None, description="Filter by org (super_admin only)"),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Per-feature hourly token usage for today (0-23h)."""
    return get_daily_usage(db, org_id=_resolve_org_id(user, org_id))


@router.get("/summary")
def credits_summary(
    period: str = Query("monthly", description="all | monthly | weekly | daily"),
    year: Optional[int] = Query(None),
    org_id: Optional[int] = Query(None, description="Filter by org (super_admin only)"),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Totals + per-feature breakdown, filtered by time period."""
    return get_usage_summary(db, period=period, year=year, org_id=_resolve_org_id(user, org_id))


@router.get("/cost/monthly")
def cost_monthly(
    org_id: Optional[int] = Query(None, description="Filter by org (super_admin only)"),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Per-feature daily cost for the current month."""
    return get_cost_monthly(db, org_id=_resolve_org_id(user, org_id))


@router.get("/cost/weekly")
def cost_weekly(
    org_id: Optional[int] = Query(None, description="Filter by org (super_admin only)"),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Per-feature daily cost for current week (Mon-Fri)."""
    return get_cost_weekly(db, org_id=_resolve_org_id(user, org_id))


@router.get("/cost/daily")
def cost_daily(
    org_id: Optional[int] = Query(None, description="Filter by org (super_admin only)"),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Per-feature hourly cost for today."""
    return get_cost_daily(db, org_id=_resolve_org_id(user, org_id))
