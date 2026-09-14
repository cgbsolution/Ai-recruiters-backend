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
    ResumeData,  # noqa: F401  — also re-used by feature_score_from_text below
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

# Single source of truth for the level bands. The frontend badge and the LLM
# scorer must use these same numbers — they previously disagreed (the badge
# called anything >= 40 "MEDIUM FIT" while the backend stored it as "low"), so a
# 45% candidate was saved one way and displayed another.
FIT_LEVEL_HIGH = 70
FIT_LEVEL_MEDIUM = 50


def level_for_percentage(percentage: int) -> str:
    """Map a 0-100 fit percentage onto high/medium/low."""
    if percentage >= FIT_LEVEL_HIGH:
        return "high"
    if percentage >= FIT_LEVEL_MEDIUM:
        return "medium"
    return "low"


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
            # Alias: the LLM path calls this `missing_weak_areas`. Emitting both
            # keys from both paths keeps the tooltip working regardless of which
            # scorer produced the row.
            "missing_weak_areas": self.missing_skills,
            "breakdown": self.breakdown,
            "score_source": "features",
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

    # NOTE: a `max_allowed = skills_pct + 40` soft cap used to run here. It was a
    # second clustering source: whenever it bound, the result became a function
    # of keyword skills overlap ALONE and the other 70% of the weights were
    # thrown away — so every candidate sharing a skills-overlap bucket collapsed
    # onto the identical percentage. Skills overlap already carries the largest
    # single weight (0.30) plus the gate penalty below it, so the cap was mostly
    # double-counting the same signal. Removed.

    fit_level = level_for_percentage(percentage)

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


def resume_metadata_for_candidate(
    db: Session, candidate: Candidate, organization_id: Optional[int],
) -> Optional[ResumeMetadata]:
    """The candidate's parsed resume, restricted to the tenant that owns the job.

    ResumeMetadata is per organization (unique on email + organization_id), and
    matching on email alone used to pull ANOTHER tenant's parsed resume into
    this candidate's score and fit reasoning. The linked row is used only when
    it belongs to the same organization, and the email fallback is always
    filtered by it — a job with no organization only matches rows with none.
    """
    if candidate.resume_metadata_id:
        meta = db.query(ResumeMetadata).filter(
            ResumeMetadata.id == candidate.resume_metadata_id
        ).first()
        if meta is not None and meta.organization_id == organization_id:
            return meta
    if not candidate.email:
        return None
    q = db.query(ResumeMetadata).filter(ResumeMetadata.candidate_email == candidate.email)
    if organization_id is None:
        q = q.filter(ResumeMetadata.organization_id.is_(None))
    else:
        q = q.filter(ResumeMetadata.organization_id == organization_id)
    return q.first()


def compute_fit_score_for_candidate(
    db: Session, candidate_id: int, job_id: int,
) -> Optional[ScoringResult]:
    """Load metadata from DB and compute fit score for candidate + job."""
    candidate = db.query(Candidate).filter(Candidate.id == candidate_id).first()
    job = db.query(Job).filter(Job.id == job_id).first()
    if not candidate or not job:
        return None

    # Find resume metadata (same tenant as the job only)
    meta = resume_metadata_for_candidate(db, candidate, job.organization_id)
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


# ── Blending with the LLM scorer ─────────────────────────────────────────────

# The feature score adjusts the LLM composite; it does not average with it.
#
# A straight weighted average was tried first and was measurably worse. The
# feature score is NOT a calibrated 0-100: several of its twelve features
# (education, certifications, seniority, title similarity) score a hard zero
# whenever the underlying structured field is absent, yet their weight is still
# counted in the denominator. Resumes we only have raw TEXT for therefore land
# around 25 almost regardless of quality. At a 0.35 weight that dragged an
# excellent candidate from 96 to 72 and squeezed the top three into a 2-point
# band — trading one distortion for another.
#
# So it is applied as a BOUNDED nudge instead. The LLM's per-dimension composite
# sets the level; the feature score, which is genuinely continuous (TF-IDF
# cosine and overlap ratios essentially never tie), separates near-ties and
# pulls back candidates the LLM liked on narrative but who show little literal
# evidence in the text. Capped so it can never move a candidate across a band on
# its own.
FEATURE_NUDGE_MAX = 8.0      # max points the feature score may move the result
FEATURE_NUDGE_PIVOT = 50.0   # feature score that means "no adjustment"
FEATURE_NUDGE_SLOPE = 0.16   # points of adjustment per point of feature score


def blend_with_features(llm_percentage: int, feature_result: Optional[ScoringResult]) -> int:
    """Adjust the LLM composite by a bounded amount driven by the feature score."""
    base = max(0, min(100, int(llm_percentage)))
    if feature_result is None:
        return base
    delta = (feature_result.fit_percentage - FEATURE_NUDGE_PIVOT) * FEATURE_NUDGE_SLOPE
    delta = max(-FEATURE_NUDGE_MAX, min(FEATURE_NUDGE_MAX, delta))
    return max(0, min(100, int(round(base + delta))))


def feature_score_from_text(
    db: Session,
    resume_text: str,
    job: Job,
    job_meta: Optional[JobMetadata] = None,
) -> Optional[ScoringResult]:
    """Feature score for a candidate we only have raw resume TEXT for.

    The LLM path runs on extracted text and often has no ResumeMetadata row to
    read structured skills from. The feature extractors all accept `raw_text` as
    a fallback (skills overlap, keyword overlap, tech-stack alignment and the
    TF-IDF/text similarity all scan it), so an otherwise-empty ResumeData still
    yields a usable continuous score. Returns None if there is nothing to score.
    """
    if not resume_text or not resume_text.strip():
        return None
    try:
        if not job_meta:
            job_meta = db.query(JobMetadata).filter(JobMetadata.job_id == job.id).first()
        resume = ResumeData(
            skills=[],
            keywords=[],
            experience_years=None,
            education=[],
            certifications=[],
            work_experience=[],
            current_designation=None,
            location=None,
            summary=None,
            raw_text=resume_text,
        )
        return compute_fit_score(resume, job_data_from_db(job, job_meta))
    except Exception:
        logger.exception("feature_score_from_text failed — falling back to LLM-only score")
        return None


def feature_score_for_candidate(
    db: Session,
    candidate: Candidate,
    job: Job,
    resume_text: Optional[str] = None,
) -> Optional[ScoringResult]:
    """Best available feature score for a candidate.

    Prefers a real ResumeMetadata row — structured skills, years and designation
    make every feature measurable, so the score is fair. Falls back to scanning
    raw resume text, which is systematically lower (see FEATURE_NUDGE_MAX) but
    still monotone enough to be useful as a bounded nudge.
    """
    meta = None
    try:
        meta = resume_metadata_for_candidate(db, candidate, job.organization_id)
    except Exception:
        logger.exception("feature_score_for_candidate: metadata lookup failed")

    if meta is not None:
        try:
            return compute_fit_score_for_metadata(db, meta, job)
        except Exception:
            logger.exception("feature_score_for_candidate: metadata scoring failed")

    return feature_score_from_text(db, resume_text or "", job)
