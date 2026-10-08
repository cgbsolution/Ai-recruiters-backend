"""
Loop B orchestration: start (Stages 1-2) → rerank chunks → finish.

finish() prefers fresh candidates: a strong match (in the job's top K) whose
profile is stale is marked `needs_refresh` and queued for a priority refresh
touch before Loop C may invite them.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.models.job import Job, JobStatus
from app.models.resume_metadata import ResumeMetadata
from app.models.talent import CampaignMember, JobMatch
from app.talent import campaigns, matching
from app.talent.audit import audit
from app.talent.settings import get_job_settings, loop_enabled

REFRESH_PRIORITY = 100  # ahead of the rolling sweep's skill-overlap priorities


def _job_open(job: Job) -> bool:
    return job.status == JobStatus.open


def start(db: Session, job_id: int) -> dict:
    job = db.get(Job, job_id)
    if job is None:
        return {"skip": "job_missing"}
    if not loop_enabled(db, job.organization_id, "match"):
        return {"skip": "loop_off"}
    if not _job_open(job):
        return {"skip": "job_not_open"}
    js = get_job_settings(db, job.id, job.organization_id)
    js.match_status, js.match_error = "running", None
    db.commit()
    req = matching.requirements(db, job, js)
    pool = matching.stage1(db, job, req)
    ranked = matching.stage2(db, job, pool)
    db.commit()
    return {"pool": len(pool), "items": [{"profile_id": r["profile_id"], "stage1_score": r["stage1_score"],
                                          "vector_score": r["vector_score"]} for r in ranked]}


def ordered_matches(db: Session, job_id: int, limit: Optional[int] = None):
    q = (
        db.query(JobMatch, ResumeMetadata)
        .join(ResumeMetadata, ResumeMetadata.id == JobMatch.profile_id)
        .filter(JobMatch.job_id == job_id)
        .order_by(JobMatch.ai_score.desc().nullslast(), JobMatch.model_score.desc().nullslast(),
                  (ResumeMetadata.verification_status == "fresh").desc(), JobMatch.vector_score.desc().nullslast(),
                  JobMatch.stage1_score.desc().nullslast(), JobMatch.profile_id)
    )
    return q.limit(limit).all() if limit else q.all()


def finish(db: Session, job_id: int, now: Optional[datetime] = None) -> dict:
    now = now or datetime.utcnow()
    job = db.get(Job, job_id)
    js = get_job_settings(db, job.id, job.organization_id)
    top = ordered_matches(db, job_id, js.top_k)
    needs = []
    for match, profile in top:
        if match.status == "ranked" and profile.verification_status == "stale":
            match.status, match.status_reason = "needs_refresh", "profile_stale"
            needs.append(profile.id)
    if needs:
        campaign = campaigns.get_or_create_campaign(
            db, job.organization_id, "refresh", f"job-{job.id}", f"Refresh before inviting · {job.title}",
            job_id=job.id, segment={"type": "job_refresh"},
        )
        db.flush()
        campaigns._add_members(db, campaign, [(pid, REFRESH_PRIORITY) for pid in needs])
        db.query(CampaignMember).filter(CampaignMember.campaign_id == campaign.id,
                                        CampaignMember.profile_id.in_(needs),
                                        CampaignMember.status == "queued").update(
            {"priority": REFRESH_PRIORITY}, synchronize_session=False)
    scored = db.query(sa.func.count(JobMatch.id)).filter(JobMatch.job_id == job_id, JobMatch.ai_score.isnot(None)).scalar()
    js.match_status, js.last_matched_at = "done", now
    audit(db, actor="agent", action="match.completed", subject_type="job", subject_id=job.id,
          organization_id=job.organization_id,
          detail={"matches": db.query(JobMatch).filter_by(job_id=job_id).count(), "scored": scored,
                  "top_k": js.top_k, "needs_refresh": len(needs)})
    db.commit()
    return {"top": len(top), "needs_refresh": needs, "scored": scored, "organization_id": job.organization_id}


def fail(db: Session, job_id: int, error: str) -> None:
    job = db.get(Job, job_id)
    if job is None:
        return
    js = get_job_settings(db, job.id, job.organization_id)
    js.match_status, js.match_error = "failed", error[:500]
    db.commit()
