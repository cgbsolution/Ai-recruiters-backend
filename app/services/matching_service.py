"""
Match candidates from the resume_metadata database against a job.

Optimized for 100K+ resumes:
  1. SQL pre-filter: only load resumes with ≥1 matching skill (ILIKE on JSON)
  2. Batch scoring: process in chunks of 500, track progress
  3. Background task: non-blocking — poll for progress/results
  4. Skip raw_text in initial load for speed; only load if needed

Scoring uses the Smart Scoring 12-dimension system (no LLM calls).
"""

import json
import logging
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional

from sqlalchemy.orm import Session
from sqlalchemy import or_, func, text

from app.models.candidate import Candidate
from app.models.job import Job, JobSkill, JobResponsibility
from app.models.job_metadata import JobMetadata
from app.models.resume_metadata import ResumeMetadata
from app.database import SessionLocal

logger = logging.getLogger(__name__)

BATCH_SIZE = 200  # DB fetch batch; progress updates per-resume

# ── In-memory task store ──────────────────────────────────────────────────────

@dataclass
class MatchTask:
    task_id: str
    job_id: int
    status: str = "running"          # running | completed | failed
    total_resumes: int = 0
    filtered_count: int = 0          # after SQL pre-filter
    processed: int = 0
    matched_count: int = 0
    candidates: list = field(default_factory=list)
    error: str | None = None
    started_at: float = field(default_factory=time.time)
    scoring_started_at: float = 0.0   # when actual scoring began (after pre-filter)
    eta_seconds: float = 0.0          # estimated time remaining
    threshold: int = 30

_tasks: dict[str, MatchTask] = {}
_tasks_lock = threading.Lock()

# Auto-cleanup tasks older than 30 minutes
def _cleanup_old_tasks():
    cutoff = time.time() - 1800
    with _tasks_lock:
        stale = [k for k, v in _tasks.items() if v.started_at < cutoff]
        for k in stale:
            del _tasks[k]

def get_task(task_id: str) -> MatchTask | None:
    _cleanup_old_tasks()
    return _tasks.get(task_id)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _parse_json_field(val: Optional[str]) -> list:
    if not val:
        return []
    try:
        parsed = json.loads(val)
        return parsed if isinstance(parsed, list) else []
    except (json.JSONDecodeError, TypeError):
        return []


def _normalize(s: str) -> str:
    return s.strip().lower()


# ── SQL Pre-filter ───────────────────────────────────────────────────────────

def _build_skill_filters(jd_skills: list[str]):
    """Build SQL ILIKE conditions to pre-filter resumes by skills JSON field."""
    from app.services.feature_engineering import _canonicalize_skill
    # Canonicalize and deduplicate
    canon_skills = list({_canonicalize_skill(s) for s in jd_skills if s})
    # Take the top 20 most specific skills (longer = more specific)
    canon_skills.sort(key=len, reverse=True)
    canon_skills = canon_skills[:20]

    conditions = []
    for skill in canon_skills:
        # Escape SQL LIKE wildcards
        escaped = skill.replace("%", "\\%").replace("_", "\\_")
        conditions.append(ResumeMetadata.skills.ilike(f"%{escaped}%"))
        # Also check keywords field
        conditions.append(ResumeMetadata.keywords.ilike(f"%{escaped}%"))

    return or_(*conditions) if conditions else None


# ── Background scoring worker ────────────────────────────────────────────────

def _run_matching(task_id: str, job_id: int, threshold: int):
    """Background thread: score all matching resumes."""
    from app.services.smart_scoring_service import compute_fit_score_for_metadata
    from app.services.feature_engineering import _canonicalize_skill, _safe_json

    task = _tasks[task_id]
    db = SessionLocal()
    try:
        job = db.query(Job).filter(Job.id == job_id).first()
        if not job:
            task.status = "failed"
            task.error = "Job not found"
            return

        # Build JD data
        jd_meta = db.query(JobMetadata).filter(JobMetadata.job_id == job_id).first()
        if not jd_meta:
            from app.services.jd_metadata_service import build_job_metadata_fast
            jd_meta = build_job_metadata_fast(db, job_id)

        db_skills = [s.skill for s in db.query(JobSkill).filter(JobSkill.job_id == job_id).all() if s.skill]
        meta_skills = _parse_json_field(jd_meta.required_skills) if jd_meta else []

        seen_skills = set()
        jd_skills = []
        for s in db_skills + meta_skills:
            norm = _normalize(s)
            if norm not in seen_skills:
                seen_skills.add(norm)
                jd_skills.append(s)

        # Existing candidates for this job
        existing_emails = {
            c.email.lower() for c in db.query(Candidate.email).filter(
                Candidate.job_id == job_id
            ).all()
        }

        # ── SQL pre-filter: only resumes with at least 1 matching skill ──
        base_q = db.query(ResumeMetadata).filter(
            ResumeMetadata.parsing_status == "completed",
            ~ResumeMetadata.candidate_email.like("%@pending.local"),
        )

        skill_filter = _build_skill_filters(jd_skills)
        if skill_filter is not None:
            filtered_q = base_q.filter(skill_filter)
            total_filtered = filtered_q.count()
        else:
            filtered_q = base_q
            total_filtered = filtered_q.count()

        total_all = base_q.count()
        task.total_resumes = total_all
        task.filtered_count = total_filtered

        logger.info(
            "Match task %s: job=%s, total=%d, pre-filtered=%d, skills=%d",
            task_id, job_id, total_all, total_filtered, len(jd_skills)
        )

        # ── Process in batches ───────────────────────────────────────────
        results = []
        offset = 0
        scored_count = 0
        task.scoring_started_at = time.time()

        while True:
            batch = (
                filtered_q
                .with_entities(
                    ResumeMetadata.id,
                    ResumeMetadata.candidate_email,
                    ResumeMetadata.candidate_name,
                    ResumeMetadata.current_designation,
                    ResumeMetadata.experience_years,
                    ResumeMetadata.location,
                    ResumeMetadata.skills,
                    ResumeMetadata.keywords,
                    ResumeMetadata.education,
                    ResumeMetadata.certifications,
                    ResumeMetadata.work_experience,
                    ResumeMetadata.summary,
                    ResumeMetadata.raw_text,
                )
                .order_by(ResumeMetadata.id)
                .offset(offset)
                .limit(BATCH_SIZE)
                .all()
            )

            if not batch:
                break

            for row in batch:
                try:
                    meta = _RowProxy(row)
                    score_result = compute_fit_score_for_metadata(db, meta, job, jd_meta)
                    fit_pct = score_result.fit_percentage

                    if fit_pct >= threshold:
                        results.append({
                            "resume_metadata_id": row.id,
                            "candidate_email": row.candidate_email,
                            "candidate_name": row.candidate_name,
                            "current_designation": row.current_designation,
                            "experience_years": row.experience_years,
                            "location": row.location,
                            "skills_match": score_result.key_matching_skills[:10],
                            "total_skills_matched": len(score_result.key_matching_skills),
                            "total_skills_required": len(jd_skills),
                            "fit_score": fit_pct,
                            "fit_level": score_result.fit_level,
                            "score_breakdown": score_result.breakdown,
                            "already_added": (row.candidate_email or "").lower() in existing_emails,
                        })
                except Exception:
                    continue

                # Update progress per resume for smooth tracking
                scored_count += 1
                task.processed = scored_count
                task.matched_count = len(results)

                # Compute ETA every 10 resumes
                if scored_count % 10 == 0 and scored_count > 0:
                    elapsed = time.time() - task.scoring_started_at
                    rate = scored_count / elapsed if elapsed > 0 else 0
                    remaining = total_filtered - scored_count
                    task.eta_seconds = remaining / rate if rate > 0 else 0

            offset += BATCH_SIZE

        # Sort by score descending
        results.sort(key=lambda x: x["fit_score"], reverse=True)

        task.candidates = results
        task.matched_count = len(results)
        task.processed = total_filtered
        task.status = "completed"

        logger.info(
            "Match task %s completed: scanned=%d, matched=%d",
            task_id, total_filtered, len(results)
        )

    except Exception as e:
        logger.error("Match task %s failed: %s", task_id, e, exc_info=True)
        task.status = "failed"
        task.error = str(e)
    finally:
        db.close()


class _RowProxy:
    """Lightweight proxy that mimics ResumeMetadata ORM object from a query row."""
    __slots__ = (
        "id", "candidate_email", "candidate_name", "current_designation",
        "experience_years", "location", "skills", "keywords", "education",
        "certifications", "work_experience", "summary", "raw_text",
        "resume_metadata_id",
    )

    def __init__(self, row):
        self.id = row.id
        self.resume_metadata_id = row.id
        self.candidate_email = row.candidate_email
        self.candidate_name = row.candidate_name
        self.current_designation = row.current_designation
        self.experience_years = row.experience_years
        self.location = row.location
        self.skills = row.skills
        self.keywords = row.keywords
        self.education = row.education
        self.certifications = row.certifications
        self.work_experience = row.work_experience
        self.summary = row.summary
        self.raw_text = row.raw_text


# ── Public API ───────────────────────────────────────────────────────────────

def start_match_task(job_id: int, threshold: int = 30) -> str:
    """Start a background matching task. Returns task_id for polling."""
    _cleanup_old_tasks()
    task_id = uuid.uuid4().hex[:12]
    task = MatchTask(task_id=task_id, job_id=job_id, threshold=threshold)
    with _tasks_lock:
        _tasks[task_id] = task

    thread = threading.Thread(
        target=_run_matching,
        args=(task_id, job_id, threshold),
        daemon=True,
    )
    thread.start()
    return task_id


def match_candidates_for_job(
    db: Session,
    job_id: int,
    threshold: int = 30,
) -> dict:
    """
    Synchronous matching — kept for backward compatibility.
    For large databases, use start_match_task() + polling instead.
    """
    from app.services.smart_scoring_service import compute_fit_score_for_metadata
    from app.services.feature_engineering import _canonicalize_skill, _safe_json

    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        return {"total_scanned": 0, "matched_count": 0, "candidates": []}

    jd_meta = db.query(JobMetadata).filter(JobMetadata.job_id == job_id).first()
    if not jd_meta:
        from app.services.jd_metadata_service import build_job_metadata_fast
        jd_meta = build_job_metadata_fast(db, job_id)

    db_skills = [s.skill for s in db.query(JobSkill).filter(JobSkill.job_id == job_id).all() if s.skill]
    meta_skills = _parse_json_field(jd_meta.required_skills) if jd_meta else []

    seen_skills = set()
    jd_skills = []
    for s in db_skills + meta_skills:
        norm = _normalize(s)
        if norm not in seen_skills:
            seen_skills.add(norm)
            jd_skills.append(s)

    existing_emails = {
        c.email.lower() for c in db.query(Candidate.email).filter(
            Candidate.job_id == job_id
        ).all()
    }

    # SQL pre-filter
    base_q = db.query(ResumeMetadata).filter(
        ResumeMetadata.parsing_status == "completed",
        ~ResumeMetadata.candidate_email.like("%@pending.local"),
    )
    skill_filter = _build_skill_filters(jd_skills)
    if skill_filter is not None:
        base_q = base_q.filter(skill_filter)

    all_meta = base_q.all()
    results = []

    for meta in all_meta:
        try:
            score_result = compute_fit_score_for_metadata(db, meta, job, jd_meta)
            fit_percentage = score_result.fit_percentage

            if fit_percentage >= threshold:
                matched_skills = score_result.key_matching_skills
                results.append({
                    "resume_metadata_id": meta.id,
                    "candidate_email": meta.candidate_email,
                    "candidate_name": meta.candidate_name,
                    "current_designation": meta.current_designation,
                    "experience_years": meta.experience_years,
                    "location": meta.location,
                    "skills_match": matched_skills[:10],
                    "total_skills_matched": len(matched_skills),
                    "total_skills_required": len(jd_skills),
                    "fit_score": fit_percentage,
                    "fit_level": score_result.fit_level,
                    "score_breakdown": score_result.breakdown,
                    "already_added": (meta.candidate_email or "").lower() in existing_emails,
                })
        except Exception:
            continue

    results.sort(key=lambda x: x["fit_score"], reverse=True)

    return {
        "total_scanned": len(all_meta),
        "matched_count": len(results),
        "candidates": results,
    }
