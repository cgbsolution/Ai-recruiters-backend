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
from app.talent.settings import freshness_window, get_job_settings, get_settings, loop_enabled

REFRESH_PRIORITY = 100  # ahead of the rolling sweep's skill-overlap priorities


def _job_open(job: Job) -> bool:
    return job.status == JobStatus.open


_WHY_NOT = {
    "agent_off": "The Talent Agent is switched off on the server (TALENT_AGENT_ENABLED).",
    "loop_off": "Loop B · Match is switched off. Turn it on in Talent Agent → Settings.",
    "job_not_open": "Only open jobs are matched. Set this job's status to Open first.",
    "job_missing": "This job no longer exists.",
}


def why_not(db: Session, job: Optional[Job]) -> Optional[str]:
    """The reason this job can't be matched right now, or None."""
    from app.talent.config import talent_config

    if job is None:
        return "job_missing"
    if not talent_config.TALENT_AGENT_ENABLED:
        return "agent_off"
    if not loop_enabled(db, job.organization_id, "match"):
        return "loop_off"
    if not _job_open(job):
        return "job_not_open"
    return None


def explain(reason: str) -> str:
    return _WHY_NOT.get(reason, reason)


def _progress(js, **fields) -> None:
    """Update the run's live progress (a new dict, so the JSON column saves)."""
    js.match_progress = {**(js.match_progress or {}), **fields, "updated_at": datetime.utcnow().isoformat()}


def request(db: Session, job_id: int, now: Optional[datetime] = None) -> Optional[str]:
    """Mark a match run as queued. Returns why it can't run instead, or None."""
    job = db.get(Job, job_id)
    reason = why_not(db, job)
    if reason:
        return reason
    js = get_job_settings(db, job.id, job.organization_id)
    js.match_status, js.match_error = "queued", None
    js.match_progress = {"stage": "queued", "requested_at": (now or datetime.utcnow()).isoformat()}
    db.commit()
    return None


def start(db: Session, job_id: int) -> dict:
    from app.talent import heartbeat

    heartbeat.beat(db)  # the worker is evidently running
    job = db.get(Job, job_id)
    reason = why_not(db, job)
    if reason:
        if job is not None:  # a queued run that can no longer go ahead must not wait forever
            js = get_job_settings(db, job.id, job.organization_id)
            js.match_status, js.match_error = "skipped", explain(reason)
            _progress(js, stage="skipped")
            db.commit()
        return {"skip": reason}
    js = get_job_settings(db, job.id, job.organization_id)
    js.match_status, js.match_error = "running", None
    _progress(js, stage="filtering", started_at=datetime.utcnow().isoformat(), pool=None, total=None, scored=0)
    db.commit()
    req = matching.requirements(db, job, js)
    pool = matching.stage1(db, job, req)
    _progress(js, stage="ranking", pool=len(pool))
    db.commit()
    ranked = matching.stage2(db, job, pool)
    _progress(js, stage="scoring", total=len(ranked), scored=0)
    db.commit()
    return {"pool": len(pool), "items": [{"profile_id": r["profile_id"], "stage1_score": r["stage1_score"],
                                          "vector_score": r["vector_score"]} for r in ranked]}


def chunk_scored(db: Session, job_id: int, count: int) -> None:
    """After each Claude rerank chunk: N more of the shortlist scored."""
    job = db.get(Job, job_id)
    if job is None:
        return
    js = get_job_settings(db, job.id, job.organization_id)
    progress = js.match_progress or {}
    _progress(js, scored=min(int(progress.get("scored") or 0) + count, int(progress.get("total") or 0)))
    db.commit()


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
    window = freshness_window(get_settings(db, job.organization_id))
    asked = {pid for (pid,) in db.query(CampaignMember.profile_id)
             .filter(campaigns.asked_since(now - window), CampaignMember.profile_id.in_([p.id for _, p in top] or [0]))}
    needs = []
    for match, profile in top:
        # Already asked this cycle: no second refresh sequence — the invite
        # itself asks them to confirm salary and notice.
        if match.status == "ranked" and profile.verification_status == "stale" and profile.id not in asked:
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
    _progress(js, stage="done", matches=len(top), finished_at=now.isoformat())
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
    _progress(js, stage="failed")
    db.commit()
