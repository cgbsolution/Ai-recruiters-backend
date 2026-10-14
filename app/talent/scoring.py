"""
Loop D — scoring an async interview, recruiter overrides, and the shortlist.

score_interview()  Claude scores each rubric criterion 1-5 with verbatim
                   quotes from the candidate's answers and a confidence. The
                   answers it sees are scrubbed like CVs are (names, contact
                   details, protected terms). Every quote is checked against
                   what the candidate actually said; a criterion scored ≥3
                   with no traceable quote is marked low-confidence for review.
                   The overall score is the weighted mean, computed here.
override()         a recruiter changes one criterion's score, with a reason.
                   The AI score is kept next to it; both are audited.
shortlist()        sort order only: interview score (overrides applied), match
                   score, minus a small penalty per risk flag. It changes no
                   status and sends nothing.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime
from typing import List, Optional

from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from app.models.job import Job
from app.models.resume_metadata import ResumeMetadata
from app.models.talent import JobMatch, TalentInterview, TalentJobSettings
from app.talent import claude, llm
from app.talent.audit import audit, user_actor
from app.talent.config import talent_config
from app.talent.matching import requirements
from app.talent.prompts import SCORER_SCHEMA, SCORER_SYSTEM, SCORER_VERSION
from app.talent.redaction import name_tokens, scrub_text
from app.talent.settings import get_job_settings


def rubric_for(db: Session, job: Job) -> List[dict]:
    js = get_job_settings(db, job.id, job.organization_id)
    if js.rubric:
        return [{"criterion": c["criterion"], "description": c.get("description") or "",
                 "weight": float(c.get("weight") or 1)} for c in js.rubric]
    req = requirements(db, job, js)
    crits = [{"criterion": m, "description": "Concrete evidence from the candidate's own work", "weight": 1.0}
             for m in req.must_haves[:8]]
    crits.append({"criterion": "Depth and specificity of examples",
                  "description": "Explains what they personally did, how, and the outcome", "weight": 1.0})
    return crits


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s%+#.]", " ", (text or "").lower())).strip()


def scrubbed_transcript(iv: TalentInterview, candidate_name: Optional[str]) -> List[dict]:
    names = name_tokens(candidate_name)
    out = []
    for t in iv.transcript or []:
        text = t.get("text") or ""
        if t["turn"] == "answer":
            text = scrub_text(text, names) or ""
        out.append({"turn": t["turn"], "question_id": t["question_id"], "text": text})
    return out


def _verify(quotes, answers_norm: List[str]) -> List[dict]:
    out = []
    for q in quotes or []:
        n = _norm(q)
        idx = next((i for i, a in enumerate(answers_norm) if len(n) >= 3 and n in a), None)
        out.append({"quote": q, "verified": idx is not None, "answer_index": idx})
    return out


def finalize(data: dict, rubric: List[dict], transcript: List[dict]) -> dict:
    answers_norm = [_norm(t["text"]) for t in transcript if t["turn"] == "answer"]
    by_name = {_norm(c.get("criterion")): c for c in data.get("criteria") or []}
    scores = []
    for crit in rubric:
        got = by_name.get(_norm(crit["criterion"])) or next(
            (c for k, c in by_name.items() if _norm(crit["criterion"])[:25] in k), None)
        score = max(1, min(5, int(round(float((got or {}).get("score") or 1)))))
        confidence = max(0.0, min(1.0, float((got or {}).get("confidence") or 0)))
        evidence = _verify((got or {}).get("evidence"), answers_norm)
        verified = [e for e in evidence if e["verified"]]
        notes = []
        if got is None:
            notes.append("not scored by the model")
            confidence = 0.0
        if score >= 3 and not verified:
            notes.append("no quote traceable to the transcript — check by hand")
            confidence = min(confidence, 0.3)
        scores.append({"criterion": crit["criterion"], "weight": crit["weight"], "score": score,
                       "confidence": round(confidence, 2), "evidence": evidence,
                       "rationale": ((got or {}).get("rationale") or "")[:1000], "notes": notes, "override": None})
    return {"scores": scores, "summary": (data.get("summary") or "")[:4000],
            "integrity_observations": [str(o)[:500] for o in data.get("integrity_observations") or []][:10]}


def overall(scores: List[dict]) -> Optional[float]:
    if not scores:
        return None
    total = sum(s["weight"] for s in scores) or 1.0
    value = sum((s["override"]["score"] if s.get("override") else s["score"]) * s["weight"] for s in scores) / total
    return round(value, 2)


async def score_interview(interview_id: str) -> dict:
    from app.agent.common import run_db

    def _context(db: Session):
        iv = db.get(TalentInterview, uuid.UUID(interview_id))
        if iv is None or iv.status not in ("completed", "scored"):
            return None
        if iv.scores and iv.prompt_version == SCORER_VERSION:
            return {"skip": True}
        match = db.get(JobMatch, iv.match_id)
        job = db.get(Job, match.job_id)
        profile = db.get(ResumeMetadata, match.profile_id)
        return {"rubric": rubric_for(db, job), "transcript": scrubbed_transcript(iv, profile.candidate_name),
                "organization_id": job.organization_id, "job_id": job.id, "title": job.title}

    ctx = await run_db(_context)
    if ctx is None or ctx.get("skip"):
        return {"status": "skipped"}
    result = None
    if llm.available() and any(t["turn"] == "answer" and t["text"] for t in ctx["transcript"]):
        user = claude.joined([
            claude.untrusted("untrusted_rubric", {"job": ctx["title"], "criteria": ctx["rubric"]}),
            claude.untrusted("untrusted_transcript", ctx["transcript"]),
            "Score every rubric criterion, using the criterion names exactly as given.",
        ])
        result = await llm.structured(
            feature="talent_interview_score", system=SCORER_SYSTEM, user=user, schema=SCORER_SCHEMA,
            model=talent_config.TALENT_SCORER_MODEL, effort=talent_config.TALENT_SCORER_EFFORT,
            prompt_version=SCORER_VERSION, max_tokens=12000, organization_id=ctx["organization_id"],
            job_id=ctx["job_id"],
        )

    def _store(db: Session):
        iv = db.get(TalentInterview, uuid.UUID(interview_id))
        match = db.get(JobMatch, iv.match_id)
        iv.human_review_status = "pending"
        if result is None or result.refused or not result.data:
            reason = "model_declined" if result is not None else "ai_unavailable_or_no_answers"
            iv.summary = "Not scored automatically — please review the transcript."
            audit(db, actor="agent", action="interview.score_unavailable", subject_type="interview", subject_id=iv.id,
                  organization_id=match.organization_id, profile_id=match.profile_id, detail={"reason": reason})
            db.commit()
            return {"status": "unscored", "reason": reason}
        scored = finalize(result.data, ctx["rubric"], ctx["transcript"])
        iv.scores, iv.summary = scored["scores"], scored["summary"]
        iv.integrity_observations = scored["integrity_observations"]
        iv.overall_score = overall(scored["scores"])
        iv.model, iv.prompt_version, iv.status = result.model, result.prompt_version, "scored"
        audit(db, actor="agent", action="interview.scored", subject_type="interview", subject_id=iv.id,
              organization_id=match.organization_id, profile_id=match.profile_id,
              detail={"overall": iv.overall_score, "model": result.model, "prompt_version": result.prompt_version,
                      "criteria": [{"criterion": s["criterion"], "score": s["score"], "confidence": s["confidence"],
                                    "verified_quotes": sum(e["verified"] for e in s["evidence"])} for s in scored["scores"]]})
        db.commit()
        return {"status": "scored", "overall": iv.overall_score, "job_id": match.job_id}

    return await run_db(_store)


def override(db: Session, iv: TalentInterview, criterion: str, score: int, reason: str, user) -> TalentInterview:
    if not 1 <= int(score) <= 5:
        raise ValueError("score must be 1 to 5")
    if len((reason or "").strip()) < 5:
        raise ValueError("say why you are changing the score")
    scores = [dict(s) for s in iv.scores or []]
    target = next((s for s in scores if s["criterion"] == criterion), None)
    if target is None:
        raise ValueError("unknown criterion")
    before = target["override"]["score"] if target.get("override") else target["score"]
    target["override"] = {"score": int(score), "by": user.id, "at": datetime.utcnow().isoformat(),
                          "reason": reason.strip()[:1000]}
    iv.scores = scores
    flag_modified(iv, "scores")
    iv.overall_score = overall(scores)
    match = db.get(JobMatch, iv.match_id)
    audit(db, actor=user_actor(user), action="interview.score_overridden", subject_type="interview", subject_id=iv.id,
          organization_id=match.organization_id, profile_id=match.profile_id,
          detail={"criterion": criterion, "ai_score": target["score"], "from": before, "to": int(score),
                  "overall": iv.overall_score})
    db.commit()
    return iv


def mark_reviewed(db: Session, iv: TalentInterview, user) -> TalentInterview:
    iv.human_review_status, iv.reviewed_by, iv.reviewed_at = "reviewed", user.id, datetime.utcnow()
    match = db.get(JobMatch, iv.match_id)
    audit(db, actor=user_actor(user), action="interview.reviewed", subject_type="interview", subject_id=iv.id,
          organization_id=match.organization_id, profile_id=match.profile_id,
          detail={"overall": iv.overall_score,
                  "overrides": sum(1 for s in iv.scores or [] if s.get("override"))})
    db.commit()
    return iv


# ── Shortlist (sort order only) ─────────────────────────────────────────────

def shortlist(db: Session, job_id: int) -> List[dict]:
    rows = (
        db.query(JobMatch, TalentInterview)
        .join(TalentInterview, TalentInterview.match_id == JobMatch.id)
        .filter(JobMatch.job_id == job_id, TalentInterview.overall_score.isnot(None))
        .populate_existing()  # a session holding older copies must not read stale scores
        .all()
    )
    out = []
    for m, iv in rows:
        if iv.overall_score is None:
            continue
        interview_pct = (iv.overall_score - 1) / 4 * 100
        flags = len(m.risk_flags or [])
        composite = round(0.6 * interview_pct + 0.4 * (m.ai_score or 0) - min(flags, 3) * 3, 1)
        out.append({"match_id": str(m.id), "interview_id": str(iv.id), "profile_id": m.profile_id,
                    "composite": composite, "interview_score": iv.overall_score, "ai_score": m.ai_score,
                    "risk_flags": flags, "reviewed": iv.human_review_status == "reviewed", "status": m.status})
    out.sort(key=lambda r: (r["composite"], r["ai_score"] or 0), reverse=True)
    for i, r in enumerate(out):
        r["rank"] = i + 1
    return out


def build_shortlist(db: Session, job_id: int) -> dict:
    rows = shortlist(db, job_id)
    job = db.get(Job, job_id)
    js = db.get(TalentJobSettings, job_id)
    if js is not None:
        js.shortlist = [{k: r[k] for k in ("match_id", "composite", "rank")} for r in rows]
    audit(db, actor="agent", action="shortlist.built", subject_type="job", subject_id=job_id,
          organization_id=job.organization_id if job else None, detail={"candidates": len(rows)})
    db.commit()
    return {"candidates": len(rows)}

