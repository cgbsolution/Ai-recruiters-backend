"""
Loop B — find the best-fitting profiles for a job.

Stage 1  SQL filter over the org's talent pool: required skills, experience
         range, location / relocation, notice period, budget, freshness
         (fresh, stale or being refreshed) and not "not looking".
Stage 2  Vector similarity between the job and each profile's current CV
         version (pgvector, or numpy without it); keeps TALENT_RERANK_POOL
         (200). Without embeddings: the Stage 1 score orders the pool.
Stage 3  Claude rerank of the redacted profiles (app.talent.rerank).

Unknown facts (no experience figure, no city, no stated salary) never
exclude a profile — they only lower its Stage 1 score. A filter excludes
only what the profile positively contradicts.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.models.job import Job, SkillCriteria
from app.models.job_metadata import JobMetadata
from app.models.resume_metadata import ResumeMetadata as RM
from app.models.talent import TalentJobSettings
from app.services.resume_profile import normalize_city
from app.talent import embeddings, llm, vectors
from app.talent.config import talent_config
from app.talent.settings import get_job_settings

STAGE1_LIMIT = 3000
MATCHABLE_STATUSES = ("fresh", "stale", "refreshing")


@dataclass
class Requirements:
    title: str
    skills: List[str]
    mandatory_skills: List[str]
    must_haves: List[str]
    nice_to_haves: List[str]
    experience_min: Optional[int]
    experience_max: Optional[int]
    locations: List[str]
    remote: bool
    relocation_ok: bool
    max_notice_days: Optional[int]
    budget_max_ctc: Optional[float]
    responsibilities: List[str] = field(default_factory=list)
    summary: Optional[str] = None

    def prompt_payload(self) -> dict:
        """What the rerank prompt sees about the job (no budget / location:
        those are decided by the SQL filter, not the model)."""
        out = {"title": self.title, "must_haves": self.must_haves}
        if self.nice_to_haves:
            out["nice_to_haves"] = self.nice_to_haves
        if self.experience_min or self.experience_max:
            out["experience_years"] = f"{self.experience_min or 0}-{self.experience_max or ''}".rstrip("-")
        if self.responsibilities:
            out["responsibilities"] = self.responsibilities[:10]
        if self.summary:
            out["summary"] = self.summary[:1500]
        return out


def _json_list(value) -> list:
    try:
        data = json.loads(value) if isinstance(value, str) else value
    except (TypeError, ValueError):
        return []
    return [str(x).strip() for x in data or [] if str(x).strip()]


def requirements(db: Session, job: Job, js: Optional[TalentJobSettings] = None) -> Requirements:
    js = js or get_job_settings(db, job.id, job.organization_id)
    jm = db.query(JobMetadata).filter(JobMetadata.job_id == job.id).first()
    skills, mandatory = [], []
    for s in job.skills:
        name = (s.skill or s.name or "").strip()
        if not name or name.lower() in {x.lower() for x in skills}:
            continue
        skills.append(name)
        if s.skill_criteria in (None, SkillCriteria.all_mandatory):
            mandatory.append(name)
    for name in _json_list(jm.required_skills if jm else None):
        if name.lower() not in {x.lower() for x in skills}:
            skills.append(name)
    must = list(js.must_haves or [])
    if not must:
        must = [f"Hands-on experience with {s}" for s in (mandatory or skills)[:8]]
        if job.experience_min:
            must.append(f"At least {job.experience_min} years of relevant experience")
    locations = [loc for loc in (js.locations or ([job.location] if job.location else [])) if loc]
    remote = any("remote" in loc.lower() for loc in locations)
    return Requirements(
        title=job.title, skills=skills[:25], mandatory_skills=mandatory[:25], must_haves=must[:12],
        nice_to_haves=list(js.nice_to_haves or [])[:8], experience_min=job.experience_min or None,
        experience_max=job.experience_max or None, locations=locations, remote=remote,
        relocation_ok=bool(js.relocation_ok), max_notice_days=js.max_notice_days, budget_max_ctc=js.budget_max_ctc,
        responsibilities=[r.description for r in job.responsibilities if r.description][:10],
        summary=jm.responsibilities_summary if jm else None,
    )


def _pg_word(skill: str) -> str:
    """A Postgres regex matching the skill as a whole word in search_text."""
    escaped = re.sub(r"([.^$*+?()\[\]{}|\\])", r"\\\1", skill.lower())
    return rf"\m{escaped}\M" if re.match(r"^\w.*\w$|^\w$", skill) else escaped


def _skill_hit(skill: str):
    idx = sa.func.lower(sa.func.coalesce(RM.skills_index, ""))
    text = sa.func.coalesce(RM.search_text, "")
    return sa.or_(idx.contains(f"|{skill.lower()}|", autoescape=True), text.op("~*")(_pg_word(skill)))


def stage1(db: Session, job: Job, req: Requirements, limit: int = STAGE1_LIMIT) -> List[dict]:
    hits = sum(sa.case((_skill_hit(s), 1), else_=0) for s in req.skills) if req.skills else sa.literal(0)
    ctc = sa.func.coalesce(RM.expected_ctc, RM.current_ctc, RM.annual_package)
    q = db.query(
        RM.id, RM.experience_years, RM.city, RM.open_to_relocate, RM.verification_status,
        RM.notice_period_days, ctc.label("ctc"), hits.label("skill_hits"),
    ).filter(
        RM.organization_id == job.organization_id,
        sa.or_(RM.parsing_status == "completed", RM.parsing_status.is_(None)),
        RM.verification_status.in_(MATCHABLE_STATUSES),
        sa.or_(RM.job_search_status.is_(None), RM.job_search_status != "not_looking"),
    )
    if req.experience_min:
        q = q.filter(sa.or_(RM.experience_years.is_(None), RM.experience_years >= req.experience_min - 1))
    if req.experience_max:
        q = q.filter(sa.or_(RM.experience_years.is_(None), RM.experience_years <= req.experience_max + 3))
    if req.max_notice_days is not None:
        q = q.filter(sa.or_(RM.notice_period_days.is_(None), RM.notice_period_days <= req.max_notice_days))
    if req.budget_max_ctc:
        q = q.filter(sa.or_(ctc.is_(None), ctc <= req.budget_max_ctc * 1.1))
    cities = sorted({c.lower() for c in (normalize_city(loc) for loc in req.locations) if c})
    if cities and not req.remote:
        allowed = [sa.func.lower(RM.city).in_(cities), RM.city.is_(None)]
        if req.relocation_ok:
            allowed.append(RM.open_to_relocate.is_(True))
        q = q.filter(sa.or_(*allowed))
    if req.skills:
        q = q.filter(hits >= 1)
    rows = q.order_by(sa.desc("skill_hits"), RM.id).limit(limit).all()

    out = []
    n_skills = max(len(req.skills), 1)
    for r in rows:
        exp_ok = r.experience_years is not None and (
            (not req.experience_min or r.experience_years >= req.experience_min)
            and (not req.experience_max or r.experience_years <= req.experience_max + 1))
        city_ok = bool(r.city and r.city.lower() in cities) or req.remote or not cities
        score = (0.7 * min(r.skill_hits / n_skills, 1.0) + 0.1 * (r.verification_status == "fresh")
                 + 0.1 * exp_ok + 0.1 * city_ok)
        out.append({"profile_id": r.id, "stage1_score": round(score, 4), "skill_hits": int(r.skill_hits),
                    "fresh": r.verification_status == "fresh"})
    return out


def stage2(db: Session, job: Job, pool: List[dict], keep: Optional[int] = None) -> List[dict]:
    """Order the Stage 1 pool by semantic similarity; keep the top `keep`."""
    keep = keep or llm.rerank_pool()
    by_id: Dict[int, dict] = {p["profile_id"]: p for p in pool}
    if not by_id:
        return []
    query = None
    if embeddings.enabled():
        try:
            query = embeddings.embed_job(db, job)
        except embeddings.EmbeddingsOff:
            query = None
    if query is None:
        ranked = sorted(pool, key=lambda p: (p["stage1_score"], p["fresh"]), reverse=True)[:keep]
        return [{**p, "vector_score": None} for p in ranked]
    near = vectors.nearest_profiles(db.connection(), query, by_id.keys(), keep, embeddings.model_name())
    ranked = [{**by_id[pid], "vector_score": round(sim, 4)} for pid, sim in near]
    if len(ranked) < keep:  # profiles with no vector yet still get a chance, by Stage 1 score
        seen = {p["profile_id"] for p in ranked}
        rest = sorted((p for p in pool if p["profile_id"] not in seen), key=lambda p: p["stage1_score"], reverse=True)
        ranked += [{**p, "vector_score": None} for p in rest[: keep - len(ranked)]]
    return ranked
