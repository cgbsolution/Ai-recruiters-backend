"""
Talent Agent admin API (/talent/...): Phase 0 clean-up, settings, consent,
campaigns, job matches and approvals, interview review and the audit view.

Every route is scoped to one organization through app.talent.access.talent_org.
"""

from typing import Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.auth import get_current_user
from app.database import get_db
from app.models.user import User
from app.talent import phase0
from app.talent.access import require_talent_admin, talent_org
from app.talent.audit import user_actor

router = APIRouter(dependencies=[Depends(get_current_user)])


# ── Phase 0: inspect and clean ──────────────────────────────────────────────

class BackfillRequest(BaseModel):
    check_mx: bool = False


class MergeRequest(BaseModel):
    dry_run: bool = True


@router.get("/phase0/report", summary="Duplicate groups and email validity in the talent pool")
def phase0_report(org_id: int = Depends(talent_org), db: Session = Depends(get_db)):
    return phase0.report(db, org_id)


@router.post("/phase0/backfill", summary="Compute dedupe keys, email status and freshness for every profile")
def phase0_backfill(
    body: BackfillRequest,
    org_id: int = Depends(talent_org),
    user: User = Depends(require_talent_admin),
    db: Session = Depends(get_db),
):
    return phase0.backfill(db, org_id, check_mx=body.check_mx, actor=user_actor(user))


@router.post("/phase0/merge-duplicates", summary="Fold same-email duplicates into the newest CV (dry run by default)")
def phase0_merge(
    body: MergeRequest,
    org_id: int = Depends(talent_org),
    user: User = Depends(require_talent_admin),
    db: Session = Depends(get_db),
):
    return phase0.merge_duplicates(db, org_id, actor=user_actor(user), dry_run=body.dry_run)
