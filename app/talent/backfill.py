"""
Backfill for the existing talent pool (run by app.agent.cv_backfill).

1. baselines   version 1 for every profile, from its stored (fast) parse — no AI.
2. embeddings  vectors for current versions, 64 per provider call.

There is no bulk Claude re-parse: profiles keep the metadata the fast parser
stored at upload (product decision, Oct 2026).
"""

from __future__ import annotations

import logging
from typing import Optional

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.models.resume_metadata import ResumeMetadata as RM
from app.models.talent import CvVersion
from app.talent import cv, embeddings

logger = logging.getLogger("app.talent.backfill")


def _scope(q, organization_id: Optional[int]):
    return q.filter(RM.organization_id == organization_id) if organization_id is not None else q


def ensure_baselines(db: Session, organization_id: Optional[int], limit: int = 2000) -> int:
    has_version = sa.exists().where(CvVersion.profile_id == RM.id)
    rows = (
        _scope(db.query(RM), organization_id)
        .filter(sa.or_(RM.parsing_status == "completed", RM.parsing_status.is_(None)), ~has_version)
        .order_by(RM.id).limit(limit).all()
    )
    for profile in rows:
        cv.ensure_baseline(db, profile)
    db.commit()
    return len(rows)


def status(db: Session, organization_id: Optional[int]) -> dict:
    """What the Matching readiness panel shows."""
    return {"embeddings_missing": len(embeddings.backfill_candidates(db, organization_id, limit=100_000))}


def embed_chunk(db: Session, organization_id: Optional[int], size: int = 500) -> int:
    ids = embeddings.backfill_candidates(db, organization_id, limit=size)
    if not ids:
        return 0
    embeddings.embed_versions(db, ids)
    return len(ids)  # processed (versions with no usable text are marked, so they don't come back)
