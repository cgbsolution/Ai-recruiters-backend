"""
Talent Agent — the recruiter's side of Loops C and D: the approval queue,
inviting, interview scheduling, interview review (scores, evidence,
overrides) and the human-only outcomes (submit to client, not-moving-forward).
"""

import uuid
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.auth import get_current_user
from app.database import get_db
from app.models.job import Job
from app.models.resume_metadata import ResumeMetadata
from app.models.talent import JobMatch, TalentInterview
from app.models.user import User
from app.refs import JobId, encode_ref
from app.routers.talent_jobs import _emit, _job, _match_or_404, _match_out
from app.talent import approvals as approvals_service
from app.talent import consent, invites, match_loop, scheduling
from app.talent.access import talent_org
from app.talent.audit import user_actor

router = APIRouter(dependencies=[Depends(get_current_user)])


# ── Approvals and invites (Loop C) ──────────────────────────────────────────

class DecideRequest(BaseModel):
    approval_ids: List[str]
    approve: bool
    note: Optional[str] = None


@router.get("/approvals", summary="Decisions waiting for a recruiter")
def list_approvals(
    job_id: Optional[JobId] = None,
    kind: Optional[str] = None,
    org_id: int = Depends(talent_org),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if job_id is not None:
        _job(db, job_id, user, org_id)
    ranks = {m.id: i + 1 for i, (m, _) in enumerate(match_loop.ordered_matches(db, job_id))} if job_id else {}
    out = []
    for a in approvals_service.queue(db, org_id, job_id, kind):
        m = db.get(JobMatch, a.match_id) if a.match_id else None
        p = db.get(ResumeMetadata, m.profile_id) if m else None
        out.append({"id": str(a.id), "kind": a.kind, "job_id": a.job_id, "requested_at": a.requested_at,
                    "match": _match_out(m, p, ranks.get(m.id, 0)) if m and p else None})
    return out


@router.post("/approvals/decide", summary="Approve or decline waiting decisions (approved invites start at once)")
def decide_approvals(body: DecideRequest, org_id: int = Depends(talent_org), user: User = Depends(get_current_user),
                     db: Session = Depends(get_db)):
    to_invite, decided = [], 0
    for aid in body.approval_ids[:500]:
        row = approvals_service.get(db, aid)
        if row is None or row.organization_id != org_id or row.status != "pending" or row.kind != "invite":
            continue  # submit / reject decisions have their own endpoints below
        approvals_service.decide(db, row, user, body.approve, body.note)
        m = db.get(JobMatch, row.match_id) if row.match_id else None
        if m is not None:
            if body.approve:
                to_invite.append(str(m.id))
            elif m.status == "approval_pending":
                m.status, m.status_reason = "closed", "recruiter_declined_invite"
        decided += 1
    db.commit()
    for match_id in to_invite:
        _emit("talent/invite.send", {"match_id": match_id})
    return {"decided": decided, "invites_started": len(to_invite)}


@router.post("/jobs/{job_id}/invites/plan", summary="Work out who to invite now (copilot: creates approvals)")
def plan_invites(job_id: JobId, org_id: int = Depends(talent_org), user: User = Depends(get_current_user),
                 db: Session = Depends(get_db)):
    job = _job(db, job_id, user, org_id)
    return {"queued": _emit("talent/invite.plan.requested", {"job_id": job.id})}


@router.post("/matches/{match_id}/invite", summary="Invite this candidate now (records your approval)")
def invite_one(match_id: str, org_id: int = Depends(talent_org), user: User = Depends(get_current_user),
               db: Session = Depends(get_db)):
    m = _match_or_404(db, org_id, match_id)
    if m.status not in invites.INVITABLE + ("approval_pending",):
        raise HTTPException(status_code=409, detail="This candidate has already been invited or closed")
    decision = consent.can_contact(db, m.profile_id, "email", "jobs")
    if not decision.allowed and decision.reason == "no_consent":  # the invite goes out as a consent request
        decision = consent.can_request_consent(db, m.profile_id)
    if not decision.allowed:  # the send would be refused anyway: say why now
        raise HTTPException(status_code=409, detail=f"Can't invite: {consent.describe(decision.reason)}.")
    invites.invite_now(db, m, user)
    return {"queued": _emit("talent/invite.send", {"match_id": str(m.id)})}


# ── Interviews (Loop D) ─────────────────────────────────────────────────────

def _interview_row(db: Session, iv: TalentInterview) -> dict:
    m = db.get(JobMatch, iv.match_id)
    p = db.get(ResumeMetadata, m.profile_id)
    job = db.get(Job, m.job_id)
    return {"id": str(iv.id), "match_id": str(m.id), "job_id": job.id, "job_ref": encode_ref("job", job.id),
            "job_title": job.title, "candidate": p.candidate_name, "profile_id": p.id, "mode": iv.mode,
            "status": iv.status, "scheduled_at": iv.scheduled_at, "completed_at": iv.completed_at,
            "reschedule_count": iv.reschedule_count, "no_show_count": iv.no_show_count,
            "needs_recruiter": iv.needs_recruiter, "overall_score": iv.overall_score,
            "human_review_status": iv.human_review_status, "ai_score": m.ai_score, "match_status": m.status}


def interview_or_404(db: Session, org_id: int, interview_id: str) -> TalentInterview:
    try:
        iv = db.get(TalentInterview, uuid.UUID(interview_id))
    except ValueError:
        iv = None
    m = db.get(JobMatch, iv.match_id) if iv else None
    if iv is None or m is None or m.organization_id != org_id:
        raise HTTPException(status_code=404, detail="Interview not found")
    return iv


@router.get("/interviews", summary="Talent Agent interviews")
def list_interviews(
    job_id: Optional[JobId] = None,
    needs_recruiter: Optional[bool] = None,
    org_id: int = Depends(talent_org),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    q = db.query(TalentInterview).join(JobMatch, JobMatch.id == TalentInterview.match_id).filter(
        JobMatch.organization_id == org_id)
    if job_id is not None:
        _job(db, job_id, user, org_id)
        q = q.filter(JobMatch.job_id == job_id)
    if needs_recruiter is not None:
        q = q.filter(TalentInterview.needs_recruiter.is_(needs_recruiter))
    return [_interview_row(db, iv) for iv in q.order_by(TalentInterview.updated_at.desc()).limit(300)]


@router.get("/interviews/{interview_id}", summary="One interview: transcript, scores with evidence, summary")
def get_interview(interview_id: str, org_id: int = Depends(talent_org), db: Session = Depends(get_db)):
    from app.media import sign_media_url
    from app.talent.scoring import rubric_for

    iv = interview_or_404(db, org_id, interview_id)
    m = db.get(JobMatch, iv.match_id)
    p = db.get(ResumeMetadata, m.profile_id)
    job = db.get(Job, m.job_id)
    transcript = [{**t, "audio_url": sign_media_url(t["audio_path"]) if t.get("audio_path") else None}
                  for t in iv.transcript or []]
    return {
        **_interview_row(db, iv),
        "questions": iv.questions, "transcript": transcript, "scores": iv.scores, "summary": iv.summary,
        "integrity_observations": iv.integrity_observations, "model": iv.model, "prompt_version": iv.prompt_version,
        "consent_recorded_at": iv.consent_recorded_at, "ai_disclosed_at": iv.ai_disclosed_at,
        "started_at": iv.started_at, "reviewed_at": iv.reviewed_at, "rubric": rubric_for(db, job),
        "match": _match_out(m, p, 0),
    }


class OverrideRequest(BaseModel):
    criterion: str
    score: int
    reason: str


@router.post("/interviews/{interview_id}/override", summary="Change one criterion's score (audited)")
def override_score(interview_id: str, body: OverrideRequest, org_id: int = Depends(talent_org),
                   user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    from app.talent import scoring

    iv = interview_or_404(db, org_id, interview_id)
    try:
        scoring.override(db, iv, body.criterion, body.score, body.reason, user)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    m = db.get(JobMatch, iv.match_id)
    _emit("talent/shortlist.requested", {"job_id": m.job_id})
    return {"scores": iv.scores, "overall_score": iv.overall_score}


@router.post("/interviews/{interview_id}/review", summary="Mark the interview reviewed")
def review_interview(interview_id: str, org_id: int = Depends(talent_org), user: User = Depends(get_current_user),
                     db: Session = Depends(get_db)):
    from app.talent import scoring

    iv = interview_or_404(db, org_id, interview_id)
    scoring.mark_reviewed(db, iv, user)
    return _interview_row(db, iv)


@router.get("/jobs/{job_id}/shortlist", summary="Interviewed candidates in shortlist order (a sort order only)")
def get_shortlist(job_id: JobId, org_id: int = Depends(talent_org), user: User = Depends(get_current_user),
                  db: Session = Depends(get_db)):
    from app.talent import scoring

    job = _job(db, job_id, user, org_id)
    rows = scoring.shortlist(db, job.id)
    for r in rows:
        p = db.get(ResumeMetadata, r["profile_id"])
        r["candidate"] = p.candidate_name if p else None
        r["current_designation"] = p.current_designation if p else None
    return rows


class DecisionRequest(BaseModel):
    note: Optional[str] = None
    notify: bool = True


@router.post("/matches/{match_id}/submit", summary="Submit this candidate to the client (your decision, recorded)")
def submit_match(match_id: str, body: DecisionRequest, org_id: int = Depends(talent_org),
                 user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    from app.talent import decisions

    m = _match_or_404(db, org_id, match_id)
    try:
        decisions.submit_to_client(db, m, user, body.note)
    except decisions.DecisionError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return {"status": m.status, "packet": decisions.client_packet(db, m)}


@router.post("/matches/{match_id}/reject", summary="Close this candidate's process; optionally tell them")
def reject_match(match_id: str, body: DecisionRequest, org_id: int = Depends(talent_org),
                 user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    from app.talent import decisions

    m = _match_or_404(db, org_id, match_id)
    if m.status in ("closed", "declined", "submitted"):
        raise HTTPException(status_code=409, detail="This candidate's process is already closed or submitted")
    return decisions.reject(db, m, user, body.note, notify=body.notify)


@router.get("/matches/{match_id}/packet", summary="What to share with the client about this candidate")
def match_packet(match_id: str, org_id: int = Depends(talent_org), db: Session = Depends(get_db)):
    from app.talent import decisions

    return decisions.client_packet(db, _match_or_404(db, org_id, match_id))


@router.post("/interviews/{interview_id}/reschedule", summary="Offer the candidate new times")
def recruiter_reschedule(interview_id: str, org_id: int = Depends(talent_org), user: User = Depends(get_current_user),
                         db: Session = Depends(get_db)):
    iv = interview_or_404(db, org_id, interview_id)
    try:
        scheduling.reschedule(db, iv.id, actor=user_actor(user), by_recruiter=True)
    except scheduling.NotReschedulable as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return _interview_row(db, db.get(TalentInterview, iv.id))
