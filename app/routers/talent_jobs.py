"""
Talent Agent — jobs: per-job settings, matching (Loop B), ranked matches with
their evidence, recruiter ratings and the precision@10 report, and the
pool backfill. Invites, approvals and interviews are added by later phases.
"""

import uuid
from datetime import datetime
from typing import Dict, List, Optional

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.auth import get_current_user
from app.authz import authorize_job
from app.database import get_db
from app.models.job import Job, JobStatus
from app.models.resume_metadata import ResumeMetadata
from app.models.talent import JobMatch, TalentJobSettings
from app.models.user import User
from app.refs import JobId, encode_ref
from app.talent import backfill, consent, heartbeat, match_loop, matching
from app.talent.access import require_talent_admin, talent_org
from app.talent.audit import audit, user_actor
from app.talent.settings import AUTONOMY_MODES, INTERVIEW_MODES, get_job_settings

router = APIRouter(dependencies=[Depends(get_current_user)])


def _emit(name: str, data: dict) -> bool:
    from app.agent.client import emit

    return emit(name, data)


def _job(db: Session, job_id: int, user: User, org_id: int) -> Job:
    job = authorize_job(db, job_id, user)
    if job.organization_id != org_id:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


def _settings_out(js: TalentJobSettings) -> dict:
    return {k: getattr(js, k) for k in (
        "autonomy_mode", "top_k", "interview_mode", "question_count", "time_limit_minutes", "slot_minutes",
        "budget_max_ctc", "max_notice_days", "relocation_ok", "locations", "must_haves", "nice_to_haves", "rubric",
        "panel_user_ids", "match_status", "match_error", "last_matched_at", "match_progress")}


STUCK_AFTER_SECONDS = 180


def _match_state(db: Session, js: TalentJobSettings) -> dict:
    """What the job page needs to show a run honestly: live progress, whether
    the worker is alive, and — if a run has sat queued for minutes — why."""
    worker = heartbeat.worker_status(db)
    hint = None
    progress = js.match_progress or {}
    if js.match_status == "queued" and progress.get("requested_at"):
        waited = (datetime.utcnow() - datetime.fromisoformat(progress["requested_at"])).total_seconds()
        if waited > STUCK_AFTER_SECONDS:
            hint = heartbeat.worker_problem(db) or (
                "The worker is checking in but hasn't picked this run up. Check that the worker is synced in Inngest "
                "and look at the match-job runs in the Inngest dashboard.")
    return {"worker": {"ok": worker["ok"], "last_seen_at": worker["last_seen_at"]}, "stuck_hint": hint}


# ── Jobs ────────────────────────────────────────────────────────────────────

@router.get("/jobs", summary="Jobs with their Talent Agent state")
def list_jobs(org_id: int = Depends(talent_org), db: Session = Depends(get_db)):
    jobs = db.query(Job).filter(Job.organization_id == org_id).order_by(Job.created_at.desc()).limit(300).all()
    ids = [j.id for j in jobs]
    counts: Dict[int, Dict[str, int]] = {}
    for job_id, status, n in db.query(JobMatch.job_id, JobMatch.status, sa.func.count()).filter(
            JobMatch.job_id.in_(ids or [0])).group_by(JobMatch.job_id, JobMatch.status):
        counts.setdefault(job_id, {})[status] = n
    settings = {s.job_id: s for s in db.query(TalentJobSettings).filter(TalentJobSettings.job_id.in_(ids or [0]))}
    out = []
    for j in jobs:
        s = settings.get(j.id)
        out.append({
            "id": j.id, "ref": encode_ref("job", j.id), "title": j.title, "location": j.location,
            "department": j.department,
            "status": j.status.value if hasattr(j.status, "value") else str(j.status),
            "autonomy_mode": s.autonomy_mode if s else None, "match_status": s.match_status if s else None,
            "last_matched_at": s.last_matched_at if s else None, "matches": counts.get(j.id, {}),
        })
    return out


@router.get("/jobs/{job_id}", summary="A job's Talent Agent settings, requirements and match counts")
def get_job(job_id: JobId, org_id: int = Depends(talent_org), user: User = Depends(get_current_user),
            db: Session = Depends(get_db)):
    job = _job(db, job_id, user, org_id)
    js = get_job_settings(db, job.id, job.organization_id)
    req = matching.requirements(db, job, js)
    counts = dict(db.query(JobMatch.status, sa.func.count()).filter(JobMatch.job_id == job.id)
                  .group_by(JobMatch.status).all())
    db.commit()
    return {"id": job.id, "ref": encode_ref("job", job.id), "title": job.title, "location": job.location,
            "department": job.department, "experience_min": job.experience_min, "experience_max": job.experience_max,
            "status": job.status.value if hasattr(job.status, "value") else str(job.status),
            "settings": _settings_out(js), "skills": req.skills, "effective_must_haves": req.must_haves,
            "matches": counts, **_match_state(db, js)}


class JobSettingsUpdate(BaseModel):
    autonomy_mode: Optional[str] = None
    top_k: Optional[int] = None
    interview_mode: Optional[str] = None
    question_count: Optional[int] = None
    time_limit_minutes: Optional[int] = None
    slot_minutes: Optional[int] = None
    budget_max_ctc: Optional[float] = None
    max_notice_days: Optional[int] = None
    relocation_ok: Optional[bool] = None
    locations: Optional[List[str]] = None
    must_haves: Optional[List[str]] = None
    nice_to_haves: Optional[List[str]] = None
    rubric: Optional[List[Dict[str, object]]] = None
    panel_user_ids: Optional[List[int]] = None


def _clean_list(values: Optional[List[str]], limit: int, length: int = 300) -> Optional[List[str]]:
    if values is None:
        return None
    out = [" ".join(str(v).split())[:length] for v in values if str(v).strip()]
    return out[:limit] or None


@router.put("/jobs/{job_id}/settings", summary="Change a job's autonomy, requirements and interview settings")
def update_job_settings(
    job_id: JobId,
    body: JobSettingsUpdate,
    org_id: int = Depends(talent_org),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    job = _job(db, job_id, user, org_id)
    js = get_job_settings(db, job.id, job.organization_id)
    changes = body.model_dump(exclude_unset=True)
    if "autonomy_mode" in changes:
        if changes["autonomy_mode"] not in AUTONOMY_MODES:
            raise HTTPException(status_code=400, detail="autonomy_mode must be autopilot or copilot")
        if changes["autonomy_mode"] == "autopilot" and user.role.value == "recruiter":
            raise HTTPException(status_code=403, detail="Only an admin can put a job on autopilot")
    if "interview_mode" in changes and changes["interview_mode"] not in INTERVIEW_MODES:
        raise HTTPException(status_code=400, detail="interview_mode must be async_text, async_voice or live_voice")
    bounds = {"top_k": (1, 200), "question_count": (8, 12), "time_limit_minutes": (25, 30), "slot_minutes": (15, 120),
              "max_notice_days": (0, 365)}
    for key, (lo, hi) in bounds.items():
        if changes.get(key) is not None and not lo <= int(changes[key]) <= hi:
            raise HTTPException(status_code=400, detail=f"{key} must be between {lo} and {hi}")
    if changes.get("budget_max_ctc") is not None and not 0 < float(changes["budget_max_ctc"]) <= 1000:
        raise HTTPException(status_code=400, detail="budget_max_ctc is in lakhs per annum (0-1000)")
    for key in ("must_haves", "nice_to_haves"):
        if key in changes:
            changes[key] = _clean_list(changes[key], 12)
    if "locations" in changes:
        changes["locations"] = _clean_list(changes["locations"], 10, 100)
    if "rubric" in changes and changes["rubric"] is not None:
        rubric = []
        for c in changes["rubric"][:10]:
            name = " ".join(str(c.get("criterion") or "").split())[:120]
            if name:
                rubric.append({"criterion": name, "description": str(c.get("description") or "")[:500],
                               "weight": max(0.0, min(5.0, float(c.get("weight") or 1)))})
        changes["rubric"] = rubric or None
    if changes.get("panel_user_ids") is not None:
        valid = {u for (u,) in db.query(User.id).filter(User.id.in_(changes["panel_user_ids"]),
                                                        User.organization_id == org_id)}
        changes["panel_user_ids"] = [u for u in changes["panel_user_ids"] if u in valid] or None
    for key, value in changes.items():
        setattr(js, key, value)
    audit(db, actor=user_actor(user), action="job_settings.updated", subject_type="job", subject_id=job.id,
          organization_id=org_id, detail={"changed": sorted(changes),
                                          "autonomy_mode": changes.get("autonomy_mode")})
    db.commit()
    if job.status == JobStatus.open and {"must_haves", "nice_to_haves", "budget_max_ctc", "max_notice_days",
                                         "relocation_ok", "locations"} & set(changes):
        _emit("talent/job.updated", {"job_id": job.id})
    return _settings_out(js)


@router.post("/jobs/{job_id}/match", summary="Run matching (Loop B) for this job now")
def run_matching(job_id: JobId, request: Request, org_id: int = Depends(talent_org),
                 user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    job = _job(db, job_id, user, org_id)
    reason = match_loop.request(db, job.id)
    if reason:  # say why now, instead of a run that never starts
        raise HTTPException(status_code=409, detail=match_loop.explain(reason))
    audit(db, actor=user_actor(user), action="match.requested", subject_type="job", subject_id=job.id,
          organization_id=org_id, commit=True)
    if not _emit("talent/match.requested", {"job_id": job.id}):
        js = get_job_settings(db, job.id, job.organization_id)
        js.match_status = "failed"
        js.match_error = heartbeat.dev_mode_problem(request.url.hostname) or (
            "The request couldn't be handed to the background worker (Inngest is unreachable or not configured).")
        js.match_progress = {**(js.match_progress or {}), "stage": "failed"}
        db.commit()
        raise HTTPException(status_code=503, detail=js.match_error)
    return {"queued": True}


# ── Matches ─────────────────────────────────────────────────────────────────

def _match_out(m: JobMatch, p: ResumeMetadata, rank: int) -> dict:
    return {
        "id": str(m.id), "rank": rank, "profile_id": p.id, "status": m.status, "status_reason": m.status_reason,
        "ai_score": m.ai_score, "model_score": m.model_score, "dimension_scores": m.dimension_scores,
        "stage1_score": m.stage1_score, "vector_score": m.vector_score, "must_haves": m.must_haves, "gaps": m.gaps,
        "strengths": m.strengths, "risk_flags": m.risk_flags, "evidence": m.evidence, "fields_sent": m.fields_sent,
        "model": m.model, "prompt_version": m.prompt_version, "recruiter_rating": m.recruiter_rating,
        "response": m.response, "confirmed": m.confirmed, "invited_at": m.invited_at,
        "profile": {
            "name": p.candidate_name, "current_designation": p.current_designation,
            "current_company": p.current_company, "experience_years": p.experience_years, "city": p.city,
            "verification_status": p.verification_status, "last_verified_at": p.last_verified_at,
            "notice_period_days": p.notice_period_days, "expected_ctc": p.expected_ctc,
            "email_status": p.email_status, "job_search_status": p.job_search_status,
        },
    }


@router.get("/jobs/{job_id}/matches", summary="Ranked matches with must-have evidence, gaps and risk flags")
def list_matches(
    job_id: JobId,
    status: Optional[str] = None,
    limit: int = Query(50, ge=1, le=500),
    org_id: int = Depends(talent_org),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    job = _job(db, job_id, user, org_id)
    rows = match_loop.ordered_matches(db, job.id)
    out = [_match_out(m, p, i + 1) for i, (m, p) in enumerate(rows)]
    if status:
        out = [o for o in out if o["status"] == status]
    page = out[:limit]
    # Whether an invite could reach them: planning skips anyone the consent gate
    # refuses, and without the reason the recruiter just saw no approvals.
    for item in page:
        decision = consent.can_contact(db, item["profile_id"], "email", "jobs")
        item["contact"] = {"allowed": decision.allowed,
                           "reason": None if decision.allowed else consent.describe(decision.reason),
                           "code": None if decision.allowed else decision.reason}
    return {"total": len(out), "items": page}


class RatingRequest(BaseModel):
    relevant: Optional[bool] = None


def _match_or_404(db: Session, org_id: int, match_id: str) -> JobMatch:
    try:
        row = db.get(JobMatch, uuid.UUID(match_id))
    except ValueError:
        row = None
    if row is None or row.organization_id != org_id:
        raise HTTPException(status_code=404, detail="Match not found")
    return row


@router.post("/matches/{match_id}/rating", summary="Rate a match relevant / not relevant (precision@10)")
def rate_match(match_id: str, body: RatingRequest, org_id: int = Depends(talent_org),
               user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    m = _match_or_404(db, org_id, match_id)
    m.recruiter_rating = None if body.relevant is None else ("relevant" if body.relevant else "not_relevant")
    m.rated_by, m.rated_at = user.id, datetime.utcnow()
    audit(db, actor=user_actor(user), action="match.rated", subject_type="job_match", subject_id=m.id,
          organization_id=org_id, profile_id=m.profile_id, detail={"rating": m.recruiter_rating, "job_id": m.job_id})
    db.commit()
    return {"recruiter_rating": m.recruiter_rating}


@router.get("/reports/precision", summary="Precision@10 of the ranking, from recruiter ratings")
def precision_report(org_id: int = Depends(talent_org), db: Session = Depends(get_db)):
    job_ids = [j for (j,) in db.query(JobMatch.job_id).filter(JobMatch.organization_id == org_id,
                                                              JobMatch.recruiter_rating.isnot(None)).distinct()]
    jobs = []
    for job_id in job_ids:
        job = db.get(Job, job_id)
        top = match_loop.ordered_matches(db, job_id, 10)
        rated = [m for m, _ in top if m.recruiter_rating]
        relevant = sum(1 for m in rated if m.recruiter_rating == "relevant")
        jobs.append({"job_id": job_id, "ref": encode_ref("job", job_id), "title": job.title if job else "",
                     "rated_in_top_10": len(rated), "relevant_in_top_10": relevant,
                     "precision_at_10": round(relevant / 10, 3) if len(rated) == 10 else None,
                     "precision_of_rated": round(relevant / len(rated), 3) if rated else None})
    complete = [j["precision_at_10"] for j in jobs if j["precision_at_10"] is not None]
    return {"jobs": jobs, "jobs_fully_rated": len(complete),
            "mean_precision_at_10": round(sum(complete) / len(complete), 3) if complete else None}


# ── Backfill ────────────────────────────────────────────────────────────────

class BackfillRun(BaseModel):
    kind: str = "embed"  # embed only: there is no bulk Claude re-parse
    limit: int = 50_000


@router.get("/backfill/estimate", summary="How many current CV versions still need an embedding")
def backfill_estimate(org_id: int = Depends(talent_org), db: Session = Depends(get_db)):
    return backfill.status(db, org_id)


@router.post("/backfill", summary="Embed every profile that has no vector yet")
def start_backfill(body: BackfillRun, org_id: int = Depends(talent_org), user: User = Depends(require_talent_admin),
                   db: Session = Depends(get_db)):
    if body.kind != "embed":
        raise HTTPException(status_code=400, detail="Only embeddings can be backfilled. Profiles keep their "
                                                    "fast-parse metadata; there is no Claude re-parse.")
    audit(db, actor=user_actor(user), action="backfill.embed_requested", subject_type="organization",
          subject_id=org_id, organization_id=org_id, detail={"limit": body.limit}, commit=True)
    return {"queued": _emit("talent/backfill.requested", {"organization_id": org_id, "kind": "embed",
                                                          "limit": int(body.limit), "actor": user_actor(user)})}
