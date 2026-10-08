"""
Candidate-facing Talent Agent API (/talent/public/...). No login: each route
takes the magic-link token from the email (app.talent.tokens). Unknown,
expired and used links all get the same 404, so links can't be probed.

  refresh/{token}        GET page data · POST confirm · POST cv (optional new CV)
  unsubscribe/{token}    GET state · POST opt out (also RFC 8058 one-click)
  my-data/{token}        GET summary · GET export · POST consent · POST delete
"""

from datetime import datetime
from typing import Dict, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.organization import Organization
from app.models.resume_metadata import ResumeMetadata
from app.ratelimit import SlidingWindowCounter
from app.talent import consent, data_export, deliverability, refresh, retention, tokens
from app.talent.audit import audit

router = APIRouter()

# Writes per link per hour (submit, upload, consent changes).
_link_writes = SlidingWindowCounter(limit=20, window_seconds=3600)


def _not_found() -> HTTPException:
    return HTTPException(status_code=404, detail="This link is invalid or has expired.")


def _throttle(raw: str) -> None:
    key = tokens.hash_token(raw)
    wait = _link_writes.blocked_for(key)
    if wait:
        raise HTTPException(status_code=429, detail="Too many attempts. Try again later.",
                            headers={"Retry-After": str(wait)})
    _link_writes.hit(key)


# ── Refresh ─────────────────────────────────────────────────────────────────

class RefreshSubmit(BaseModel):
    fields: Dict[str, Optional[object]] = Field(default_factory=dict)
    consents: Dict[str, bool] = Field(default_factory=dict)


@router.get("/refresh/{token}")
def refresh_page(token: str, db: Session = Depends(get_db)):
    try:
        row = refresh.open_link(db, token)
        return refresh.page_data(db, row)
    except refresh.LinkError:
        raise _not_found()


@router.post("/refresh/{token}/cv")
def refresh_upload(token: str, file: UploadFile = File(...), db: Session = Depends(get_db)):
    row = tokens.lookup(db, token, "refresh")
    if row is None:
        raise _not_found()
    _throttle(token)
    try:
        version = refresh.attach_cv(db, row, file)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True, "cv_version_id": str(version.id)}


@router.post("/refresh/{token}")
def refresh_submit(token: str, body: RefreshSubmit, db: Session = Depends(get_db)):
    row = tokens.lookup(db, token, "refresh")
    if row is None:
        raise _not_found()
    _throttle(token)
    try:
        return refresh.submit(db, row, body.model_dump())
    except refresh.LinkError:
        raise _not_found()
    except (ValueError, TypeError) as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc))


# ── Invite (Loop C) ─────────────────────────────────────────────────────────

class InviteAnswer(BaseModel):
    interested: bool
    current_ctc: Optional[float] = None
    expected_ctc: Optional[float] = None
    notice_period_days: Optional[int] = None


@router.get("/invite/{token}")
def invite_page(token: str, db: Session = Depends(get_db)):
    from app.talent import invites

    row = tokens.lookup(db, token, "invite")
    if row is None:
        raise _not_found()
    try:
        return invites.page_data(db, row)
    except invites.LinkError:
        raise _not_found()


@router.post("/invite/{token}")
def invite_answer(token: str, body: InviteAnswer, db: Session = Depends(get_db)):
    from app.talent import invites

    row = tokens.lookup(db, token, "invite")
    if row is None:
        raise _not_found()
    _throttle(token)
    try:
        return invites.respond(db, row, body.model_dump())
    except invites.LinkError:
        raise _not_found()
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc))


# ── Interview (Loop D) ──────────────────────────────────────────────────────

class BookRequest(BaseModel):
    slot_id: str


def _interview(db: Session, token: str):
    from app.talent import interview_page

    try:
        return interview_page.resolve(db, token)
    except interview_page.LinkError:
        raise _not_found()


@router.get("/interview/{token}")
def interview_state(token: str, db: Session = Depends(get_db)):
    from app.talent import interview_page

    return interview_page.state(db, _interview(db, token))


@router.post("/interview/{token}/book")
def interview_book(token: str, body: BookRequest, db: Session = Depends(get_db)):
    from app.talent import interview_page, scheduling

    interview = _interview(db, token)
    _throttle(token)
    try:
        scheduling.book(db, interview.id, body.slot_id)
    except (scheduling.SlotTaken, ValueError) as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc) or "That time is no longer available.")
    return interview_page.state(db, _interview(db, token))


@router.post("/interview/{token}/reschedule")
def interview_reschedule(token: str, db: Session = Depends(get_db)):
    from app.talent import interview_page, scheduling

    interview = _interview(db, token)
    _throttle(token)
    try:
        scheduling.reschedule(db, interview.id, actor="candidate")
    except scheduling.NotReschedulable as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return interview_page.state(db, _interview(db, token))


class StartRequest(BaseModel):
    disclosure_acknowledged: bool
    consent_to_record: bool
    answer_mode: str = "text"  # text | audio


@router.post("/interview/{token}/start")
def interview_start(token: str, body: StartRequest, db: Session = Depends(get_db)):
    """Explicit AI disclosure and recording consent first — nothing is asked or stored before."""
    from app.talent import interviews

    iv = _interview(db, token)
    if iv.mode == "live_voice":
        from app.talent import live_voice

        try:  # same disclosure and consent step; the live interview link is revealed only after it
            return live_voice.start(db, iv, disclosure_acknowledged=body.disclosure_acknowledged,
                                    consent_to_record=body.consent_to_record)
        except live_voice.LiveVoiceError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
    try:
        return interviews.start(db, iv, disclosure_acknowledged=body.disclosure_acknowledged,
                                consent_to_record=body.consent_to_record, answer_mode=body.answer_mode)
    except interviews.InterviewError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@router.get("/interview/{token}/progress")
def interview_progress(token: str, db: Session = Depends(get_db)):
    from app.talent import interviews

    return interviews.progress(db, _interview(db, token))


@router.post("/interview/{token}/answer")
async def interview_answer(
    token: str,
    question_id: str = Form(...),
    text: Optional[str] = Form(None),
    audio: Optional[UploadFile] = File(None),
    db: Session = Depends(get_db),
):
    from app.talent import interviews

    iv = _interview(db, token)
    interview_id = str(iv.id)
    db.close()  # the answer step manages its own sessions around the transcription / follow-up calls
    try:
        return await interviews.answer(interview_id, question_id, text, audio)
    except interviews.InterviewError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@router.post("/interview/{token}/finish")
def interview_finish(token: str, db: Session = Depends(get_db)):
    from app.talent import interview_page, interviews

    iv = _interview(db, token)
    interviews.finish(db, iv, reason="candidate_finished")
    return interview_page.state(db, _interview(db, token))


@router.post("/interview/{token}/human")
def interview_human(token: str, db: Session = Depends(get_db)):
    """"Talk to a person instead" — always available, never penalised."""
    from app.talent import interview_page, scheduling

    interview = _interview(db, token)
    scheduling.human_requested(db, interview)
    from app.agent.client import emit

    emit("talent/interview.rescheduled", {"interview_id": str(interview.id)})  # stops reminders
    return interview_page.state(db, _interview(db, token))


# ── Unsubscribe ─────────────────────────────────────────────────────────────

def _profile_for(db: Session, raw: str, purpose: str) -> ResumeMetadata:
    row = tokens.lookup(db, raw, purpose)
    profile = db.get(ResumeMetadata, row.profile_id) if row else None
    if profile is None:
        raise _not_found()
    return profile


@router.get("/unsubscribe/{token}")
def unsubscribe_state(token: str, db: Session = Depends(get_db)):
    profile = _profile_for(db, token, "unsubscribe")
    org = db.get(Organization, profile.organization_id)
    return {"organization": org.name if org else "", "suppressed": consent.is_suppressed(
        db, email=profile.candidate_email)}


@router.post("/unsubscribe/{token}")
async def unsubscribe(token: str, request: Request, db: Session = Depends(get_db)):
    """One tap, immediate: suppression, every consent withdrawn, sequences
    stopped. Also the RFC 8058 one-click target (List-Unsubscribe-Post)."""
    profile = _profile_for(db, token, "unsubscribe")
    deliverability.opt_out(db, profile, source="unsubscribe_link", actor="candidate", suppress_reason="unsubscribe")
    db.commit()
    from app.agent.client import emit

    emit("talent/member.stopped", {"profile_id": profile.id, "campaign_id": None, "reason": "opted_out"})
    return JSONResponse({"ok": True, "unsubscribed": True})


# ── My data ─────────────────────────────────────────────────────────────────

class ConsentChange(BaseModel):
    channel: str
    purpose: str
    granted: bool


class DeleteRequest(BaseModel):
    confirm: str


@router.get("/my-data/{token}")
def my_data(token: str, db: Session = Depends(get_db)):
    profile = _profile_for(db, token, "my_data")
    return data_export.summary(db, profile)


@router.get("/my-data/{token}/export")
def my_data_export(token: str, db: Session = Depends(get_db)):
    profile = _profile_for(db, token, "my_data")
    audit(db, actor="candidate", action="profile.exported", subject_type="profile", subject_id=profile.id,
          organization_id=profile.organization_id, profile_id=profile.id, commit=True)
    return JSONResponse(
        data_export.full_export(db, profile),
        headers={"Content-Disposition": f'attachment; filename="my-data-{datetime.utcnow():%Y%m%d}.json"'},
    )


@router.post("/my-data/{token}/consent")
def my_data_consent(token: str, body: ConsentChange, db: Session = Depends(get_db)):
    profile = _profile_for(db, token, "my_data")
    _throttle(token)
    try:
        consent.set_consent(db, profile.id, body.channel, body.purpose, body.granted, source="my_data_page",
                            actor="candidate", organization_id=profile.organization_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if body.granted and consent.lift_own_suppression(db, email=profile.candidate_email, phone=profile.phone):
        audit(db, actor="candidate", action="suppression.lifted", subject_type="profile", subject_id=profile.id,
              organization_id=profile.organization_id, profile_id=profile.id, detail={"via": "my_data_page"})
    db.commit()
    if not body.granted:
        from app.agent.client import emit

        emit("talent/member.stopped", {"profile_id": profile.id, "campaign_id": None, "reason": "consent_withdrawn"})
    return {"ok": True, "consents": consent.consent_summary(db, profile.id)}


@router.post("/my-data/{token}/withdraw-all")
def my_data_withdraw_all(token: str, db: Session = Depends(get_db)):
    profile = _profile_for(db, token, "my_data")
    _throttle(token)
    deliverability.opt_out(db, profile, source="my_data_page", actor="candidate", suppress_reason="withdrawn")
    db.commit()
    from app.agent.client import emit

    emit("talent/member.stopped", {"profile_id": profile.id, "campaign_id": None, "reason": "consent_withdrawn"})
    return {"ok": True}


@router.post("/webhooks/deliverability")
async def deliverability_webhook(request: Request, db: Session = Depends(get_db)):
    """Bounces / complaints / unsubscribes from the mail provider, in a
    normalized form (an adapter in front of SES / SendGrid / Postmark maps its
    payload to this):

        {"events": [{"id": "evt_1", "type": "bounce", "bounce_type": "hard",
                     "message_id": "<...@domain>", "timestamp": "2026-10-09T10:00:00Z"}]}

    Signed: X-Talent-Signature = hex HMAC-SHA256 of the raw body with
    TALENT_DELIVERABILITY_WEBHOOK_SECRET. Idempotent on the event id."""
    import hashlib
    import hmac

    from app.talent.config import talent_config

    secret = talent_config.TALENT_DELIVERABILITY_WEBHOOK_SECRET
    raw = await request.body()
    expected = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest() if secret else ""
    if not secret or not hmac.compare_digest(expected, request.headers.get("x-talent-signature", "")):
        raise HTTPException(status_code=401, detail="bad signature")
    try:
        events = (await request.json()).get("events") or []
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid JSON")
    kinds = {"delivered": "delivered", "complaint": "complaint", "unsubscribe": "unsubscribe"}
    recorded = 0
    for ev in events[:1000]:
        kind = kinds.get(ev.get("type"))
        if ev.get("type") == "bounce":
            kind = "bounce_hard" if ev.get("bounce_type", "hard") == "hard" else "bounce_soft"
        if kind is None or not ev.get("message_id"):
            continue
        occurred = None
        if ev.get("timestamp"):
            try:
                occurred = datetime.fromisoformat(str(ev["timestamp"]).replace("Z", "+00:00")).replace(tzinfo=None)
            except ValueError:
                occurred = None
        if deliverability.record_event(db, kind=kind, provider_message_id=ev["message_id"],
                                       provider=str(ev.get("provider") or "webhook")[:30],
                                       provider_event_id=str(ev.get("id"))[:255] if ev.get("id") else None,
                                       occurred_at=occurred) is not None:
            recorded += 1
    return {"recorded": recorded}


@router.post("/my-data/{token}/delete")
def my_data_delete(token: str, body: DeleteRequest, db: Session = Depends(get_db)):
    profile = _profile_for(db, token, "my_data")
    if body.confirm.strip().upper() != "DELETE":
        raise HTTPException(status_code=400, detail='Type DELETE to confirm.')
    _throttle(token)
    profile_id = profile.id
    result = retention.erase_profile(db, profile_id, actor="candidate", reason="candidate_request")
    from app.agent.client import emit

    emit("talent/member.stopped", {"profile_id": profile_id, "campaign_id": None, "reason": "erased"})
    return {"ok": True, "deleted": result}
