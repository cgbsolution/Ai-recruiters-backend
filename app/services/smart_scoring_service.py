"""
Smart Scoring Service — Fit score from metadata comparison.

Compares resume metadata (skills, experience, education, etc.) against
job metadata (required skills, experience range, etc.) using weighted
feature scoring.  No AI calls — purely structural comparison.
"""

import logging
import re
from typing import Optional

from sqlalchemy.orm import Session

from app.models.candidate import Candidate
from app.models.job import Job
from app.models.job_metadata import JobMetadata
from app.models.resume_metadata import ResumeMetadata
from app.services.feature_engineering import (
    FeatureVector,
    ResumeData,
    JobData,
    extract_features,
    resume_data_from_metadata,
    job_data_from_db,
    _canonicalize_skill,
)

logger = logging.getLogger(__name__)

# ── Feature Weights (sum = 1.00) ────────────────────────────────────────────

WEIGHTS = {
    # ── Core matching (70% total) ──
    "f_skills_overlap": 0.30,       # Skills match — important but not sole factor
    "f_keyword_overlap": 0.15,      # JD keywords & responsibilities in resume
    "f_text_similarity": 0.10,      # Full resume-to-JD semantic similarity
    "f_experience_fit": 0.15,       # Experience years + domain relevance
    # ── Secondary factors (30% total) ──
    "f_tech_stack_align": 0.08,     # Related technology alignment
    "f_title_similarity": 0.06,     # Job title alignment
    "f_skills_tfidf": 0.05,         # TF-IDF similarity captures domain language
    "f_seniority_match": 0.04,      # Seniority level alignment
    "f_education_match": 0.03,      # Education requirements
    "f_certification_match": 0.02,  # Relevant certifications
    "f_industry_relevance": 0.02,   # Domain/industry match
    "f_location_match": 0.00,       # Not relevant for scoring
}

# Minimum skills overlap before penalty applies (lowered to avoid crushing domain-specific roles)
_SKILLS_GATE_THRESHOLD = 0.10


# ── Result ───────────────────────────────────────────────────────────────────

class ScoringResult:
    __slots__ = (
        "fit_percentage", "fit_level", "summary",
        "key_matching_skills", "missing_skills",
        "breakdown",
    )

    def __init__(
        self,
        fit_percentage: int,
        fit_level: str,
        summary: str,
        key_matching_skills: list[str],
        missing_skills: list[str],
        breakdown: dict,
    ):
        self.fit_percentage = fit_percentage
        self.fit_level = fit_level
        self.summary = summary
        self.key_matching_skills = key_matching_skills
        self.missing_skills = missing_skills
        self.breakdown = breakdown

    def to_dict(self) -> dict:
        return {
            "fit_percentage": self.fit_percentage,
            "fit_level": self.fit_level,
            "summary": self.summary,
            "key_matching_skills": self.key_matching_skills,
            "missing_skills": self.missing_skills,
            "breakdown": self.breakdown,
        }


# ── Core scoring logic ──────────────────────────────────────────────────────

def _score(features: FeatureVector, resume: ResumeData, job: JobData) -> ScoringResult:
    feat = features.to_dict()

    # Weighted sum → 0‑100
    raw = sum(feat[k] * w for k, w in WEIGHTS.items())

    # ── Skills Gate ─────────────────────────────────────────────────────────
    # If the candidate has very few matching skills, apply a mild penalty.
    # But don't crush scores — keyword matching misses domain synonyms.
    skills_score = feat["f_skills_overlap"]

    if skills_score < _SKILLS_GATE_THRESHOLD:
        # Mild penalty — scale down but keep at least 40% of the raw score
        # This avoids crushing scores for domain-specific roles where
        # keyword matching fails (e.g., "D365 CRM" vs "Microsoft Dynamics")
        gate_multiplier = max(0.40, skills_score / _SKILLS_GATE_THRESHOLD)
        raw = raw * gate_multiplier

    percentage = max(0, min(100, int(round(raw * 100))))

    # Soft cap: score should not exceed skills match by too much,
    # but allow generous headroom since text similarity and keywords
    # can capture domain alignment that skill keywords miss.
    skills_pct = int(round(skills_score * 100))
    max_allowed = skills_pct + 40  # allow at most +40% above skills match
    if percentage > max_allowed:
        percentage = max_allowed

    fit_level = "high" if percentage >= 75 else ("medium" if percentage >= 40 else "low")

    # Per-dimension breakdown for the hover tooltip
    breakdown = {}
    for k, w in WEIGHTS.items():
        name = k.replace("f_", "")
        val = feat[k]
        breakdown[name] = {
            "score": round(val * 100),
            "weight": round(w * 100),
            "contribution": round(val * w * 100, 1),
        }

    # Matched / missing skills — strict matching only
    r_canon = {_canonicalize_skill(s) for s in resume.skills}
    j_map = {_canonicalize_skill(s): s for s in job.skills}

    matched_canon = set()
    for c in j_map:
        if c in r_canon:
            matched_canon.add(c)
            continue
        # Check if resume has an exact multi-word skill that contains this job skill
        # But ONLY if the job skill is multi-word (avoid "java" matching "javascript")
        if len(c.split()) >= 2:
            for rskill in r_canon:
                if c == rskill:
                    matched_canon.add(c)
                    break

    # Second pass: scan raw text using WORD BOUNDARY matching
    if resume.raw_text:
        raw_lower = resume.raw_text.lower()
        for c, orig in j_map.items():
            if c not in matched_canon:
                # Use word boundary regex to avoid "java" matching inside "javascript"
                if re.search(r'\b' + re.escape(c) + r'\b', raw_lower):
                    matched_canon.add(c)
                else:
                    # Try original skill name with word boundaries
                    orig_lower = orig.lower()
                    if re.search(r'\b' + re.escape(orig_lower) + r'\b', raw_lower):
                        matched_canon.add(c)

    matched = [orig for c, orig in j_map.items() if c in matched_canon]
    missing = [orig for c, orig in j_map.items() if c not in matched_canon]

    # Human-readable summary
    parts = []
    if features.f_skills_overlap >= 0.7:
        parts.append("Strong skills match")
    elif features.f_skills_overlap >= 0.4:
        parts.append("Moderate skills alignment")
    else:
        parts.append("Limited skills overlap")

    if features.f_experience_fit >= 0.8:
        parts.append("experience fits the requirement")
    elif features.f_experience_fit >= 0.5:
        parts.append("experience is partially aligned")
    else:
        parts.append("experience gap detected")

    if features.f_title_similarity >= 0.5:
        parts.append("role title aligns well")

    summary = f"{parts[0]}, {', '.join(parts[1:])}." if len(parts) > 1 else f"{parts[0]}."

    return ScoringResult(
        fit_percentage=percentage,
        fit_level=fit_level,
        summary=summary,
        key_matching_skills=matched[:10],
        missing_skills=missing[:8],
        breakdown=breakdown,
    )


# ── Public helpers ───────────────────────────────────────────────────────────

def compute_fit_score(resume: ResumeData, job: JobData) -> ScoringResult:
    """Score a resume against a job from pre-built data objects."""
    return _score(extract_features(resume, job), resume, job)


def compute_fit_score_for_candidate(
    db: Session, candidate_id: int, job_id: int,
) -> Optional[ScoringResult]:
    """Load metadata from DB and compute fit score for candidate + job."""
    candidate = db.query(Candidate).filter(Candidate.id == candidate_id).first()
    job = db.query(Job).filter(Job.id == job_id).first()
    if not candidate or not job:
        return None

    # Find resume metadata
    meta = None
    if candidate.resume_metadata_id:
        meta = db.query(ResumeMetadata).filter(ResumeMetadata.id == candidate.resume_metadata_id).first()
    if not meta and candidate.email:
        meta = db.query(ResumeMetadata).filter(ResumeMetadata.candidate_email == candidate.email).first()
    if not meta:
        return None

    job_meta = db.query(JobMetadata).filter(JobMetadata.job_id == job_id).first()
    return compute_fit_score(resume_data_from_metadata(meta), job_data_from_db(job, job_meta))


def compute_fit_score_for_metadata(
    db: Session,
    resume_meta: ResumeMetadata,
    job: Job,
    job_meta: Optional[JobMetadata] = None,
) -> ScoringResult:
    """Score from ResumeMetadata + Job ORM objects. Used by bulk matching."""
    if not job_meta:
        job_meta = db.query(JobMetadata).filter(JobMetadata.job_id == job.id).first()
    return compute_fit_score(resume_data_from_metadata(resume_meta), job_data_from_db(job, job_meta))
