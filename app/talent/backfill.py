"""
Backfill for the existing talent pool (run by app.agent.cv_backfill).

1. baselines   version 1 for every profile, from its stored parse (no AI).
2. embeddings  vectors for current versions, 64 per provider call.
3. re-parse    optional: every CV through the Claude parser via the Message
               Batches API (50% cheaper, results within 24 h). Batches hold
               at most PARSE_BATCH_SIZE requests to stay under the 256 MB limit.
               estimate() prices it first — starting it is an admin decision.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import List, Optional

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.config import settings
from app.models.resume_metadata import ResumeMetadata as RM
from app.models.talent import CvVersion, TalentBatchRun
from app.talent import claude, cv, cv_pipeline, embeddings, parsing
from app.talent.audit import audit
from app.talent.config import talent_config
from app.talent.prompts import CV_PARSE_SCHEMA, CV_PARSE_SYSTEM, CV_PARSE_VERSION

logger = logging.getLogger("app.talent.backfill")

PARSE_BATCH_SIZE = 5000
MIN_TEXT_CHARS = 200
EST_OUTPUT_TOKENS = 1500
EST_SYSTEM_TOKENS = 700


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


def _parse_query(db: Session, organization_id: Optional[int]):
    current = sa.orm.aliased(CvVersion)
    pending = sa.exists().where(CvVersion.profile_id == RM.id, CvVersion.source == "reparse",
                                CvVersion.parse_status == "pending")
    return (
        _scope(db.query(RM.id, sa.func.length(RM.raw_text).label("chars")), organization_id)
        .outerjoin(current, sa.and_(current.profile_id == RM.id, current.is_current.is_(True)))
        .filter(sa.func.length(RM.raw_text) >= MIN_TEXT_CHARS, ~pending,
                sa.or_(current.prompt_version.is_(None), current.prompt_version != CV_PARSE_VERSION))
    )


def estimate(db: Session, organization_id: Optional[int]) -> dict:
    rows = _parse_query(db, organization_id).all()
    model = talent_config.TALENT_PARSE_MODEL
    chars = sum(min(int(r.chars or 0), parsing.MAX_CV_CHARS) for r in rows)
    input_tokens = chars // 4 + EST_SYSTEM_TOKENS * len(rows)
    output_tokens = EST_OUTPUT_TOKENS * len(rows)
    rate_in, rate_out = claude._COST.get(model, (0.0, 0.0))
    cost = (input_tokens * rate_in + output_tokens * rate_out) / 1_000_000 * 0.5  # Batch API: half price
    return {"profiles": len(rows), "model": model, "estimated_input_tokens": input_tokens,
            "estimated_output_tokens": output_tokens, "estimated_cost_usd": round(cost, 2),
            "batches": -(-len(rows) // PARSE_BATCH_SIZE), "prompt_version": CV_PARSE_VERSION,
            "embeddings_missing": len(embeddings.backfill_candidates(db, organization_id, limit=100_000))}


def create_parse_batch(db: Session, organization_id: Optional[int], actor: str,
                       limit: int = PARSE_BATCH_SIZE) -> Optional[str]:
    """Submit up to `limit` CVs as one Message Batch. Returns the run id."""
    import anthropic
    from anthropic.types.messages.batch_create_params import Request

    ids = [r.id for r in _parse_query(db, organization_id).order_by(RM.id).limit(min(limit, PARSE_BATCH_SIZE))]
    if not ids:
        return None
    requests, version_ids = [], []
    for profile in db.query(RM).filter(RM.id.in_(ids)):
        previous = cv.current_version(db, profile.id)
        version = cv.new_version(db, profile, source="reparse", file_path=profile.resume_file_path,
                                 file_hash=profile.file_hash)
        params = claude.request_params(
            system=CV_PARSE_SYSTEM, user=parsing.user_content(profile.raw_text, previous.parsed if previous else None),
            schema=CV_PARSE_SCHEMA, model=talent_config.TALENT_PARSE_MODEL, effort=talent_config.TALENT_PARSE_EFFORT,
            max_tokens=8000, for_batch=True,
        )
        requests.append(Request(custom_id=str(version.id), params=params))
        version_ids.append(version.id)
    client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY, max_retries=3, timeout=600)
    batch = client.messages.batches.create(requests=requests)
    run = TalentBatchRun(organization_id=organization_id, kind="parse", provider_batch_id=batch.id,
                         status="in_progress", total=len(requests), created_by=actor)
    db.add(run)
    audit(db, actor=actor, action="backfill.parse_batch_created", subject_type="batch_run", subject_id=batch.id,
          organization_id=organization_id, detail={"requests": len(requests), "model": talent_config.TALENT_PARSE_MODEL,
                                                   "prompt_version": CV_PARSE_VERSION})
    db.commit()
    return str(run.id)


def batch_status(db: Session, run_id: str) -> str:
    import anthropic

    run = db.get(TalentBatchRun, uuid.UUID(run_id))
    if run is None or run.status in ("ended", "collected"):
        return run.status if run else "missing"
    client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY, max_retries=3)
    batch = client.messages.batches.retrieve(run.provider_batch_id)
    if batch.processing_status == "ended":
        run.status = "ended"
        db.commit()
    return run.status


def batch_statuses(db: Session, run_ids: List[str]) -> List[str]:
    return [batch_status(db, r) for r in run_ids]


def collect_parse_batch(db: Session, run_id: str) -> dict:
    """Apply every result (idempotent: parsed versions are skipped)."""
    import anthropic

    run = db.get(TalentBatchRun, uuid.UUID(run_id))
    if run is None or run.status == "collected":
        return {"status": run.status if run else "missing"}
    client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY, max_retries=3)
    ok = failed = 0
    model = talent_config.TALENT_PARSE_MODEL
    for result in client.messages.batches.results(run.provider_batch_id):
        version_id = result.custom_id
        if result.result.type == "succeeded":
            try:
                parsed = parsing.from_result(claude.parse_message(result.result.message, model, CV_PARSE_VERSION))
            except ValueError:
                parsed = None
            if parsed is not None:
                cv_pipeline.apply_parse(db, version_id, None, parsed.parsed, field_confidence=parsed.field_confidence,
                                        parse_confidence=parsed.confidence, model=parsed.model,
                                        prompt_version=parsed.prompt_version)
                ok += 1
                continue
        cv_pipeline._mark_failed(db, version_id, f"batch result {result.result.type}")
        failed += 1
    run.status, run.succeeded, run.failed, run.ended_at = "collected", ok, failed, datetime.utcnow()
    audit(db, actor="agent", action="backfill.parse_batch_collected", subject_type="batch_run",
          subject_id=run.provider_batch_id, organization_id=run.organization_id,
          detail={"succeeded": ok, "failed": failed})
    db.commit()
    return {"status": "collected", "succeeded": ok, "failed": failed}


def embed_chunk(db: Session, organization_id: Optional[int], size: int = 500) -> int:
    ids = embeddings.backfill_candidates(db, organization_id, limit=size)
    if not ids:
        return 0
    embeddings.embed_versions(db, ids)
    return len(ids)  # processed (versions with no usable text are marked, so they don't come back)


def runs(db: Session, organization_id: Optional[int]) -> List[dict]:
    q = db.query(TalentBatchRun)
    if organization_id is not None:
        q = q.filter(TalentBatchRun.organization_id == organization_id)
    return [{"id": str(r.id), "kind": r.kind, "status": r.status, "total": r.total, "succeeded": r.succeeded,
             "failed": r.failed, "created_at": r.created_at, "ended_at": r.ended_at}
            for r in q.order_by(TalentBatchRun.created_at.desc()).limit(50)]
