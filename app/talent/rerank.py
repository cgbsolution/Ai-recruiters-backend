"""
Loop B Stage 3 — Claude reranks the Stage 2 shortlist.

The model sees the job's requirements and ONE redacted profile
(app.talent.redaction.build_rank_input: allowlisted fields only, free text
scrubbed). The ranking number is computed here, not taken from the model:

- Each must-have's evidence quote is checked against the text the model was
  given; a "yes" whose quote isn't there is downgraded to "unclear".
- must_have_coverage = (verified yes + ½ unclear) / must-haves, server-side.
- ai_score = weighted sum of the dimension scores (prompts.RERANK_WEIGHTS);
  the model's own overall `score` is kept as model_score, a tie-breaker.
  (LLM single percentages are modal — similar candidates collapse onto the
  same few values — so they make a poor sort key on their own.)

A match is re-scored only when the job, the profile payload, the model or the
prompt version changed (rank_input_hash). Every score — and every refusal —
is written to audit_log with the exact list of fields that were sent.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import uuid
from datetime import datetime
from typing import List, Optional

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.models.job import Job
from app.models.resume_metadata import ResumeMetadata
from app.models.talent import JobMatch
from app.talent import claude, cv
from app.talent.audit import audit
from app.talent.config import talent_config
from app.talent.matching import requirements
from app.talent.prompts import RERANK_SCHEMA, RERANK_SYSTEM, RERANK_VERSION, RERANK_WEIGHTS
from app.talent.redaction import build_rank_input

logger = logging.getLogger("app.talent.rerank")


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s%+#.]", " ", text.lower())).strip()


def user_content(job_payload: dict, candidate_payload: dict) -> str:
    return claude.joined([
        claude.untrusted("untrusted_job", job_payload),
        claude.untrusted("untrusted_candidate", candidate_payload),
        "Score this candidate for this job. Evaluate every must-have listed in the job.",
    ])


def input_hash(job_payload: dict, candidate_payload: dict, model: str) -> str:
    blob = json.dumps({"job": job_payload, "candidate": candidate_payload, "model": model, "v": RERANK_VERSION},
                      sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()


def _clamp(value, lo: int = 0, hi: int = 100) -> int:
    try:
        return max(lo, min(hi, int(round(float(value)))))
    except (TypeError, ValueError):
        return lo


def finalize(data: dict, candidate_payload: dict, must_haves: List[str]) -> dict:
    """Verify evidence, recompute coverage, compute the composite."""
    corpus = _norm(json.dumps(candidate_payload, ensure_ascii=False))
    checked, verified_quotes, unverified = [], 0, 0
    for item in data.get("must_haves") or []:
        quote = (item.get("evidence") or "").strip()
        met = item.get("met") if item.get("met") in ("yes", "no", "unclear") else "unclear"
        ok = len(_norm(quote)) >= 3 and _norm(quote) in corpus
        if quote:
            verified_quotes += ok
            unverified += not ok
        if met == "yes" and not ok:
            met = "unclear"  # a claim with no traceable evidence is not a "yes"
        checked.append({"requirement": item.get("requirement"), "met": met, "evidence": quote,
                        "evidence_verified": ok})
    dims = {k: _clamp((data.get("dimension_scores") or {}).get(k)) for k in RERANK_WEIGHTS}
    n = len(checked) or len(must_haves)
    if checked:
        yes = sum(1 for c in checked if c["met"] == "yes")
        unclear = sum(1 for c in checked if c["met"] == "unclear")
        dims["must_have_coverage"] = round(100 * (yes + 0.5 * unclear) / max(n, 1))
    composite = round(sum(RERANK_WEIGHTS[k] * dims[k] for k in RERANK_WEIGHTS))
    strengths = [{"strength": s.get("strength"), "evidence": s.get("evidence"),
                  "evidence_verified": _norm(s.get("evidence") or "") in corpus if s.get("evidence") else False}
                 for s in data.get("strengths") or []]
    return {
        "ai_score": _clamp(composite), "model_score": _clamp(data.get("score")), "dimension_scores": dims,
        "must_haves": checked, "gaps": [g for g in data.get("gaps") or [] if g], "strengths": strengths,
        "risk_flags": data.get("risk_flags") or [],
        "evidence": {"verified_quotes": verified_quotes, "unverified_quotes": unverified},
    }


# ── Preparation and storage (database side) ────────────────────────────────

def prepare(db: Session, job_id: int, items: List[dict]) -> dict:
    """Upsert the matches with their Stage 1/2 scores and build each
    redacted payload. Returns the job payload and the items to score."""
    job = db.get(Job, job_id)
    req = requirements(db, job)
    job_payload = req.prompt_payload()
    model = talent_config.TALENT_RERANK_MODEL
    out = []
    for item in items:
        profile = db.get(ResumeMetadata, item["profile_id"])
        if profile is None:
            continue
        version = cv.ensure_baseline(db, profile)
        payload, fields, counts = build_rank_input(version.parsed or cv.snapshot(profile), profile.candidate_name)
        h = input_hash(job_payload, payload, model)
        db.execute(
            pg_insert(JobMatch.__table__).values(
                id=uuid.uuid4(), job_id=job.id, profile_id=profile.id, organization_id=job.organization_id,
                stage1_score=item.get("stage1_score"), vector_score=item.get("vector_score"), status="ranked",
                created_at=datetime.utcnow(), updated_at=datetime.utcnow(),
            ).on_conflict_do_update(
                constraint="uq_job_matches_job_profile",
                set_={"stage1_score": item.get("stage1_score"), "vector_score": item.get("vector_score"),
                      "updated_at": datetime.utcnow()},
            )
        )
        match = db.query(JobMatch).filter_by(job_id=job.id, profile_id=profile.id).populate_existing().one()
        skip = None
        if match.rank_input_hash == h and match.ai_score is not None:
            skip = "unchanged"
        elif len(payload) < 2 or not (payload.get("skills") or payload.get("work_history")):
            skip = "not_enough_cv_data"
            match.status_reason = skip
        out.append({"match_id": str(match.id), "profile_id": profile.id, "payload": payload, "fields": fields,
                    "redactions": counts, "hash": h, "skip": skip})
    db.commit()
    return {"job_payload": job_payload, "must_haves": req.must_haves, "organization_id": job.organization_id,
            "items": out}


def store(db: Session, job_id: int, organization_id: Optional[int], item: dict, result: Optional[claude.ClaudeResult],
          must_haves: List[str], error: Optional[str] = None) -> None:
    match = db.get(JobMatch, uuid.UUID(item["match_id"]))
    if match is None:
        return
    match.fields_sent = item["fields"]
    if result is None or result.refused or not result.data:
        reason = error or ("model_declined" if result is not None and result.refused else "ai_unavailable")
        match.status_reason, match.ai_score = reason, None
        audit(db, actor="agent", action="match.score_unavailable", subject_type="job_match", subject_id=match.id,
              organization_id=organization_id, profile_id=match.profile_id,
              detail={"job_id": job_id, "reason": reason, "fields_sent": item["fields"],
                      "model": result.model if result else None, "prompt_version": RERANK_VERSION})
        db.commit()
        return
    scored = finalize(result.data, item["payload"], must_haves)
    for key, value in scored.items():
        setattr(match, key, value)
    match.model, match.prompt_version, match.rank_input_hash = result.model, result.prompt_version, item["hash"]
    match.status_reason = None
    audit(db, actor="agent", action="match.scored", subject_type="job_match", subject_id=match.id,
          organization_id=organization_id, profile_id=match.profile_id,
          detail={"job_id": job_id, "ai_score": scored["ai_score"], "model_score": scored["model_score"],
                  "dimension_scores": scored["dimension_scores"], "model": result.model,
                  "prompt_version": result.prompt_version, "fields_sent": item["fields"],
                  "redactions": item["redactions"], "input_hash": item["hash"],
                  "unverified_quotes": scored["evidence"]["unverified_quotes"]})
    db.commit()


# ── Scoring (model side) ───────────────────────────────────────────────────

def _retryable(exc: Exception) -> bool:
    import anthropic

    if isinstance(exc, (anthropic.RateLimitError, anthropic.APIConnectionError, anthropic.APITimeoutError)):
        return True
    return isinstance(exc, anthropic.APIStatusError) and exc.status_code >= 500


async def score_chunk(job_id: int, items: List[dict]) -> dict:
    """One durable step: score up to ~10 profiles, TALENT_RERANK_CONCURRENCY at
    a time. Transient API errors fail the step (Inngest retries it; finished
    profiles are skipped on the retry by their input hash)."""
    from app.agent.common import run_db

    prepared = await run_db(prepare, job_id, items)
    todo = [i for i in prepared["items"] if not i["skip"]]
    if not claude.available():
        for item in todo:
            await run_db(store, job_id, prepared["organization_id"], item, None, prepared["must_haves"])
        return {"scored": 0, "skipped": len(prepared["items"]), "reason": "ai_unavailable"}

    sem = asyncio.Semaphore(max(1, int(talent_config.TALENT_RERANK_CONCURRENCY)))
    transient: List[Exception] = []

    async def one(item: dict) -> None:
        async with sem:
            try:
                result = await claude.structured(
                    feature="talent_rerank", system=RERANK_SYSTEM,
                    user=user_content(prepared["job_payload"], item["payload"]), schema=RERANK_SCHEMA,
                    model=talent_config.TALENT_RERANK_MODEL, effort=talent_config.TALENT_RERANK_EFFORT,
                    prompt_version=RERANK_VERSION, max_tokens=6000, organization_id=prepared["organization_id"],
                    job_id=job_id,
                )
            except Exception as exc:  # noqa: BLE001
                if _retryable(exc):
                    transient.append(exc)
                    return
                logger.warning("Rerank failed for a profile (%s)", type(exc).__name__)
                await run_db(store, job_id, prepared["organization_id"], item, None, prepared["must_haves"],
                             f"ai_error:{type(exc).__name__}")
                return
        await run_db(store, job_id, prepared["organization_id"], item, result, prepared["must_haves"])

    await asyncio.gather(*(one(i) for i in todo))
    if transient:
        raise transient[0]
    return {"scored": len(todo), "skipped": len(prepared["items"]) - len(todo)}
