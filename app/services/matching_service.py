"""
Match candidates from the resume_metadata database against a job.

Optimized for 50k+ resumes — pure Python string matching, NO LLM calls.
Scoring breakdown:
  - Skills match:    40% weight (how many required skills does the candidate have)
  - Keyword match:   25% weight (overlap of JD keywords vs resume keywords)
  - Title match:     20% weight (job title terms in resume designation/keywords/skills)
  - Experience match: 15% weight (candidate experience within required range)

Candidates scoring >= threshold (default 60%) are returned.
"""

import json
import logging
import re
from typing import Optional

from sqlalchemy.orm import Session

from app.models.candidate import Candidate
from app.models.job import Job, JobSkill, JobResponsibility
from app.models.job_metadata import JobMetadata
from app.models.resume_metadata import ResumeMetadata

logger = logging.getLogger(__name__)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _parse_json_field(val: Optional[str]) -> list:
    """Safely parse a JSON text field to a list."""
    if not val:
        return []
    try:
        parsed = json.loads(val)
        return parsed if isinstance(parsed, list) else []
    except (json.JSONDecodeError, TypeError):
        return []


def _normalize(s: str) -> str:
    """Lowercase and strip whitespace."""
    return s.strip().lower()


def _tokenize(s: str) -> set[str]:
    """Split a string into lowercase tokens (words 2+ chars)."""
    return {w for w in re.split(r'[\s,;/\-\(\)\[\]]+', s.lower()) if len(w) >= 2}


def _fuzzy_skill_match(resume_skill: str, jd_skill: str) -> bool:
    """Check if skills match — exact or substring containment."""
    rs = _normalize(resume_skill)
    js = _normalize(jd_skill)
    if rs == js:
        return True
    # Check substring in either direction (e.g., "react" matches "react.js")
    if rs in js or js in rs:
        return True
    # Check without dots/spaces (e.g., "nodejs" matches "node.js")
    rs_clean = re.sub(r'[.\s\-]', '', rs)
    js_clean = re.sub(r'[.\s\-]', '', js)
    if rs_clean == js_clean:
        return True
    return False


def _skill_overlap(resume_skills: list[str], jd_skills: list[str]) -> list[str]:
    """Find overlapping skills using fuzzy matching."""
    matched = []
    matched_jd_indices = set()
    for rs in resume_skills:
        for idx, js in enumerate(jd_skills):
            if idx not in matched_jd_indices and _fuzzy_skill_match(rs, js):
                matched.append(rs)
                matched_jd_indices.add(idx)
                break
    return matched


def _keyword_overlap_score(resume_keywords: list[str], jd_keywords: list[str]) -> float:
    """Return 0-1 overlap ratio of keywords (case-insensitive, fuzzy)."""
    if not jd_keywords:
        return 0.0
    jd_tokens = set()
    for k in jd_keywords:
        jd_tokens.add(_normalize(k))
        jd_tokens.update(_tokenize(k))

    resume_tokens = set()
    for k in resume_keywords:
        resume_tokens.add(_normalize(k))
        resume_tokens.update(_tokenize(k))

    if not jd_tokens:
        return 0.0
    overlap = jd_tokens & resume_tokens
    return len(overlap) / len(jd_tokens)


def _title_match_score(
    job_title: str,
    resume_designation: Optional[str],
    resume_skills: list[str],
    resume_keywords: list[str],
) -> float:
    """Score 0-1 based on how well the job title matches the resume."""
    if not job_title:
        return 0.0

    title_tokens = _tokenize(job_title)
    # Remove generic words
    generic = {"junior", "senior", "lead", "principal", "staff", "intern", "associate", "manager", "developer", "engineer"}
    title_meaningful = title_tokens - generic
    title_all = title_tokens

    if not title_all:
        return 0.0

    # Build candidate text pool
    candidate_tokens = set()
    if resume_designation:
        candidate_tokens.update(_tokenize(resume_designation))
    for s in resume_skills:
        candidate_tokens.update(_tokenize(s))
    for k in resume_keywords:
        candidate_tokens.update(_tokenize(k))

    # Check meaningful title words against candidate
    if title_meaningful:
        matched = title_meaningful & candidate_tokens
        return len(matched) / len(title_meaningful)

    # Fallback: use all title tokens
    matched = title_all & candidate_tokens
    return len(matched) / len(title_all)


def _experience_match_score(
    candidate_years: Optional[float],
    required_min: int,
    required_max: int,
) -> float:
    """Score 0-1 based on experience range fit."""
    if candidate_years is None:
        return 0.3  # Unknown experience — give partial credit

    if required_min <= candidate_years <= required_max:
        return 1.0  # Perfect fit
    elif candidate_years > required_max:
        # Overqualified — slight penalty
        overshoot = candidate_years - required_max
        return max(0.4, 1.0 - (overshoot * 0.1))
    else:
        # Underqualified
        shortfall = required_min - candidate_years
        return max(0.0, 1.0 - (shortfall * 0.2))


# ── Main Matching Pipeline ───────────────────────────────────────────────────

def match_candidates_for_job(
    db: Session,
    job_id: int,
    threshold: int = 60,
) -> dict:
    """
    Match all completed resumes in the database against a job.
    Returns {total_scanned, matched_count, candidates: [...]}.

    Pure keyword/skill matching — NO LLM calls.
    Optimized for 50k+ resumes.
    """
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        return {"total_scanned": 0, "matched_count": 0, "candidates": []}

    # ── Build JD data ────────────────────────────────────────────────────────
    jd_meta = db.query(JobMetadata).filter(JobMetadata.job_id == job_id).first()

    # If no metadata exists, build it fast (no LLM)
    if not jd_meta:
        from app.services.jd_metadata_service import build_job_metadata_fast
        jd_meta = build_job_metadata_fast(db, job_id)

    # Get skills from job_skills table (always available)
    db_skills = [s.skill for s in db.query(JobSkill).filter(JobSkill.job_id == job_id).all() if s.skill]

    # Combine with metadata skills
    if jd_meta:
        meta_skills = _parse_json_field(jd_meta.required_skills)
        jd_keywords = _parse_json_field(jd_meta.keywords)
    else:
        meta_skills = []
        jd_keywords = []

    # Deduplicate skills
    seen_skills = set()
    jd_skills = []
    for s in db_skills + meta_skills:
        norm = _normalize(s)
        if norm not in seen_skills:
            seen_skills.add(norm)
            jd_skills.append(s)

    # Also add job title words as keywords for broader matching
    if job.title:
        for token in _tokenize(job.title):
            if token not in {_normalize(k) for k in jd_keywords}:
                jd_keywords.append(token)

    exp_min = job.experience_min or 0
    exp_max = job.experience_max or 99

    # ── Find existing candidates for this job (to flag "already added") ──────
    existing_emails = {
        c.email.lower() for c in db.query(Candidate.email).filter(
            Candidate.job_id == job_id
        ).all()
    }

    # ── Scan all completed resumes (exclude stale placeholders) ──────────────
    all_meta = db.query(ResumeMetadata).filter(
        ResumeMetadata.parsing_status == "completed",
        ~ResumeMetadata.candidate_email.like("%@pending.local"),
    ).all()

    results = []
    total_scanned = len(all_meta)

    for meta in all_meta:
        resume_skills = _parse_json_field(meta.skills)
        resume_keywords = _parse_json_field(meta.keywords)

        # ── Compute scores ───────────────────────────────────────────────────
        # 1. Skills match (40%)
        matched_skills = _skill_overlap(resume_skills, jd_skills)
        skill_score = len(matched_skills) / max(len(jd_skills), 1)

        # 2. Keyword match (25%)
        keyword_score = _keyword_overlap_score(resume_keywords, jd_keywords)

        # 3. Title match (20%)
        title_score = _title_match_score(
            job.title or "",
            meta.current_designation,
            resume_skills,
            resume_keywords,
        )

        # 4. Experience match (15%)
        exp_score = _experience_match_score(meta.experience_years, exp_min, exp_max)

        # ── Weighted combined score ──────────────────────────────────────────
        combined = (
            skill_score * 0.40 +
            keyword_score * 0.25 +
            title_score * 0.20 +
            exp_score * 0.15
        )
        fit_percentage = int(round(combined * 100))

        if fit_percentage >= threshold:
            results.append({
                "resume_metadata_id": meta.id,
                "candidate_email": meta.candidate_email,
                "candidate_name": meta.candidate_name,
                "current_designation": meta.current_designation,
                "experience_years": meta.experience_years,
                "location": meta.location,
                "skills_match": matched_skills[:10],  # Cap at 10 for display
                "total_skills_matched": len(matched_skills),
                "total_skills_required": len(jd_skills),
                "fit_score": fit_percentage,
                "score_breakdown": {
                    "skills": int(skill_score * 100),
                    "keywords": int(keyword_score * 100),
                    "title": int(title_score * 100),
                    "experience": int(exp_score * 100),
                },
                "already_added": (meta.candidate_email or "").lower() in existing_emails,
            })

    # Sort by fit score descending
    results.sort(key=lambda x: x["fit_score"], reverse=True)

    return {
        "total_scanned": total_scanned,
        "matched_count": len(results),
        "candidates": results,
    }
