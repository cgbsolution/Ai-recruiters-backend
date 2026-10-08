"""
Phase 5 — live voice interviews (behind the `live_voice` switch).

The product already has a real-time voice interviewer (the AI avatar
interview: streaming speech-to-text, text-to-speech, recording). Rather than
build a second one, a live_voice Talent Agent interview bridges to it:

1. At booking, the candidate gets a job application (`candidates` row) with a
   personal invite token for that interview, scheduled at the booked time.
2. The candidate opens their Talent Agent interview link first. The SAME AI
   disclosure and recording-consent step runs there; only after consent is
   recorded is the live interview link revealed.
3. When the live session completes, its transcript (interview_messages) is
   imported into the Talent Agent interview and scored by the SAME rubric
   pipeline (app.talent.scoring), with the same human review.

Without the switch, a job set to live_voice interviews in async_voice mode.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from app.models.candidate import Candidate
from app.models.interview import InterviewMessage, InterviewSession, SessionStatus
from app.models.job import Job
from app.models.resume_metadata import ResumeMetadata
from app.models.talent import JobMatch, TalentInterview
from app.talent.audit import audit


class LiveVoiceError(Exception):
    pass


def ensure_application(db: Session, interview: TalentInterview) -> Candidate:
    """The job application the live interviewer works from (created once)."""
    from app import invites

    match = db.get(JobMatch, interview.match_id)
    job = db.get(Job, match.job_id)
    profile = db.get(ResumeMetadata, match.profile_id)
    app_row = db.get(Candidate, match.application_id) if match.application_id else None
    if app_row is None:
        app_row = db.query(Candidate).filter(Candidate.job_id == job.id,
                                             Candidate.email == profile.candidate_email).first()
    if app_row is None:
        app_row = Candidate(
            name=profile.candidate_name or profile.candidate_email.split("@")[0], email=profile.candidate_email,
            phone=profile.phone or "", location=profile.location or "", role=profile.current_designation or "",
            resume_path=profile.resume_file_path, job_id=job.id, resume_metadata_id=profile.id,
            parsing_status="completed", candidate_stage="AI Round", added_by="talent-agent",
        )
        db.add(app_row)
        db.flush()
    invites.get_or_create_job_token(db, job.id)
    invites.ensure_invite_token(app_row)
    app_row.interview_status = "Scheduled"
    app_row.interview_date = interview.scheduled_at
    app_row.joining_flexibility = "scheduled_time"
    app_row.allow_joining_early = True
    match.application_id = app_row.id
    db.flush()
    return app_row


def live_url(db: Session, interview: TalentInterview) -> str:
    from app import invites

    match = db.get(JobMatch, interview.match_id)
    app_row = db.get(Candidate, match.application_id)
    token = invites.get_or_create_job_token(db, match.job_id)
    return invites.build_invite_url(token.token, invites.ensure_invite_token(app_row))


def start(db: Session, interview: TalentInterview, *, disclosure_acknowledged: bool, consent_to_record: bool) -> dict:
    """The consent step for a live interview; returns the live link only after it."""
    if interview.mode != "live_voice":
        raise LiveVoiceError("not a live interview")
    if not disclosure_acknowledged or not consent_to_record:
        raise LiveVoiceError("Please confirm you understand this is an AI interview and agree to it being recorded.")
    if interview.status not in ("scheduled", "in_progress"):
        raise LiveVoiceError("This interview can't be started now.")
    match = db.get(JobMatch, interview.match_id)
    now = datetime.utcnow()
    ensure_application(db, interview)
    if interview.status == "scheduled":
        interview.ai_disclosed_at, interview.consent_recorded_at = now, now
        interview.started_at, interview.status = now, "in_progress"
        audit(db, actor="candidate", action="interview.consent_recorded", subject_type="interview",
              subject_id=interview.id, organization_id=match.organization_id, profile_id=match.profile_id,
              detail={"ai_disclosure_acknowledged": True, "recording_consent": True, "answer_mode": "live_voice"})
    url = live_url(db, interview)
    db.commit()
    return {"status": interview.status, "live_url": url}


def sync(db: Session, interview_id: str) -> dict:
    """Import a finished live session's transcript, then hand it to scoring."""
    interview = db.get(TalentInterview, uuid.UUID(interview_id))
    if interview is None or interview.mode != "live_voice" or interview.status != "in_progress":
        return {"status": "skipped"}
    match = db.get(JobMatch, interview.match_id)
    if not match.application_id:
        return {"status": "no_application"}
    session = (
        db.query(InterviewSession)
        .filter(InterviewSession.candidate_id == match.application_id,
                InterviewSession.status.in_((SessionStatus.completed, SessionStatus.terminated)))
        .order_by(InterviewSession.completed_at.desc().nullslast())
        .first()
    )
    if session is None:
        return {"status": "waiting"}
    turns, q_index = [], 0
    for msg in db.query(InterviewMessage).filter(InterviewMessage.session_id == session.id).order_by(
            InterviewMessage.created_at):
        if msg.role == "ai":
            q_index += 1
            turns.append({"turn": "question", "question_id": f"q{q_index}", "text": msg.content,
                          "at": msg.created_at.isoformat() if msg.created_at else None})
        elif msg.role == "candidate" and q_index:
            turns.append({"turn": "answer", "question_id": f"q{q_index}", "text": msg.content, "kind": "audio",
                          "audio_path": None, "transcription": "ok",
                          "at": msg.created_at.isoformat() if msg.created_at else None})
    interview.transcript = turns
    flag_modified(interview, "transcript")
    interview.legacy_session_id = session.id
    interview.questions = [{"id": t["question_id"], "text": t["text"], "must_have": "", "source": "live"}
                           for t in turns if t["turn"] == "question"]
    from app.talent import interviews

    interviews.finish(db, interview, reason="live_session_completed")
    return {"status": "imported", "turns": len(turns)}


def open_live_interviews(db: Session, limit: int = 200) -> list:
    return [str(i) for (i,) in db.query(TalentInterview.id).filter(
        TalentInterview.mode == "live_voice", TalentInterview.status == "in_progress").limit(limit)]


def recording_link(db: Session, interview: TalentInterview) -> Optional[str]:
    match = db.get(JobMatch, interview.match_id)
    app_row = db.get(Candidate, match.application_id) if match.application_id else None
    return app_row.recording_link if app_row else None
