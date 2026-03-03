import json
import os
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, Request, UploadFile, status
from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.candidate import Candidate
from app.models.interview import (
    CandidateFeedback,
    InterviewMessage,
    InterviewSession,
    InterviewStatusLog,
    InterviewToken,
    MeetingSchedule,
    MeetingSlot,
    SessionStatus,
    Violation,
    ViolationType,
)
from app.models.job import Job
from app.database import SessionLocal
from app.schemas.interview import (
    CandidateLoginRequest,
    CandidateRegisterRequest,
    CandidateRegisterResponse,
    EvaluationRead,
    FeedbackRequest,
    FeedbackResponse,
    InterviewMessageRequest,
    InterviewMessageResponse,
    JobConfig,
    MeetingSlotRead,
    PreInterviewFieldConfig,
    RecordingUploadResponse,
    ResumeSessionResponse,
    ScheduleMeetingRequest,
    ScheduleMeetingResponse,
    ViolationRequest,
    ViolationResponse,
)
from app.services import avatar_interview_service as svc

router = APIRouter()

VIOLATION_TERMINATE_THRESHOLD = 5


# ── Helpers ───────────────────────────────────────────────────────────────────

def _get_token_or_404(token: str, db: Session) -> InterviewToken:
    obj = db.query(InterviewToken).filter(
        InterviewToken.token == token,
        InterviewToken.is_active == True,
    ).first()
    if not obj:
        raise HTTPException(status_code=404, detail="Interview link not found or no longer active")
    return obj


def _get_session_or_404(session_id: int, db: Session) -> InterviewSession:
    obj = db.query(InterviewSession).filter(InterviewSession.id == session_id).first()
    if not obj:
        raise HTTPException(status_code=404, detail="Session not found")
    return obj


# ── Public Endpoints ──────────────────────────────────────────────────────────

@router.get(
    "/interview/{token}",
    response_model=JobConfig,
    summary="Validate token & get job configuration",
    description=(
        "Validates the interview URL token and returns the full job configuration needed "
        "to render the candidate-facing interview UI.\n\n"
        "**Returns:** job title, department, location, experience range, required skills, "
        "interview settings (max questions, mode, avatar/webcam/face-match flags, special instructions).\n\n"
        "**Errors:** `404` if token is invalid or inactive."
    ),
    tags=["Interview"],
)
def get_interview_config(token: str, db: Session = Depends(get_db)):
    tok = _get_token_or_404(token, db)
    job = db.query(Job).filter(Job.id == tok.job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    settings_obj = job.interview_settings
    skills = [s.skill for s in job.skills] if job.skills else []
    pre_fields = [
        PreInterviewFieldConfig(
            field_id=f.field_id,
            label=f.label,
            type=f.type,
            is_default=f.is_default,
        )
        for f in (job.pre_interview_fields or [])
    ]

    return JobConfig(
        job_id=job.id,
        title=job.title,
        department=job.department,
        location=job.location,
        experience_level=job.experience_level,
        experience_min=job.experience_min,
        experience_max=job.experience_max,
        max_questions=settings_obj.max_questions if settings_obj else 8,
        mode=settings_obj.mode.value if settings_obj else "TRADITIONAL",
        virtual_avatar=settings_obj.virtual_avatar if settings_obj else True,
        webcam_required=settings_obj.webcam_required if settings_obj else True,
        face_match=settings_obj.face_match if settings_obj else False,
        resume_based=settings_obj.resume_based if settings_obj else True,
        special_instructions=settings_obj.special_instructions if settings_obj else None,
        skills=skills,
        pre_interview_fields=pre_fields,
    )


@router.post(
    "/interview/{token}/register",
    response_model=CandidateRegisterResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Register candidate & create session",
    description=(
        "Registers the candidate for this interview.\n\n"
        "**Email-exists logic:**\n"
        "- If the email already exists → reuses the existing candidate record.\n"
        "- If the candidate has an active (pending/in_progress) session → reuses it (can_resume=True).\n"
        "- If the candidate already completed → returns already_completed=True.\n"
        "- Otherwise creates a new session.\n\n"
        "**Returns:** `session_id`, `candidate_id`, `is_existing_user`, `can_resume`, `session_status`.\n\n"
        "**Errors:** `404` if the token is invalid."
    ),
    tags=["Interview"],
)
def register_candidate(
    token: str,
    payload: CandidateRegisterRequest,
    background_tasks: BackgroundTasks,
    request: Request,
    db: Session = Depends(get_db),
):
    tok = _get_token_or_404(token, db)
    job = db.query(Job).filter(Job.id == tok.job_id).first()

    # Resume is mandatory
    if not payload.resume_path:
        raise HTTPException(status_code=422, detail="Resume is required to register for this interview.")

    # Per-job duplicate check: match by email OR phone (if phone provided)
    email_str = str(payload.email).strip().lower()
    phone_str = (payload.phone or "").strip()

    filters = [
        (Candidate.email == email_str) & (Candidate.job_id == tok.job_id),
    ]
    if phone_str:
        filters.append(
            (Candidate.phone == phone_str) & (Candidate.job_id == tok.job_id)
        )

    existing = db.query(Candidate).filter(or_(*filters)).first()

    is_existing = bool(existing)
    extra_json = json.dumps(payload.extra_fields) if payload.extra_fields else None

    if existing:
        candidate = existing

        # Check if this candidate already completed an interview for this job
        completed_session = (
            db.query(InterviewSession)
            .filter(
                InterviewSession.candidate_id == candidate.id,
                InterviewSession.job_id == tok.job_id,
                InterviewSession.status == SessionStatus.completed,
            )
            .first()
        )
        if completed_session:
            return CandidateRegisterResponse(
                session_id=completed_session.id,
                candidate_id=candidate.id,
                is_existing_user=True,
                already_completed=True,
                message="You have already completed your interview for this position.",
            )

        # Check for resumable session (pending or in_progress)
        active_session = (
            db.query(InterviewSession)
            .filter(
                InterviewSession.candidate_id == candidate.id,
                InterviewSession.job_id == tok.job_id,
                InterviewSession.status.in_([SessionStatus.pending, SessionStatus.in_progress]),
            )
            .order_by(InterviewSession.created_at.desc())
            .first()
        )
        if active_session:
            # Update profile data but reuse session
            candidate.name = payload.name
            candidate.email = email_str
            if phone_str:
                candidate.phone = phone_str
            candidate.resume_path = payload.resume_path
            candidate.pre_interview_responses = extra_json
            db.commit()
            return CandidateRegisterResponse(
                session_id=active_session.id,
                candidate_id=candidate.id,
                is_existing_user=True,
                already_completed=False,
                can_resume=True,
                session_status=active_session.status.value,
                message="Welcome back! You have an existing interview session. Resuming where you left off.",
            )

        # Check for terminated session — don't allow re-interview, prompt to contact HR
        terminated_session = (
            db.query(InterviewSession)
            .filter(
                InterviewSession.candidate_id == candidate.id,
                InterviewSession.job_id == tok.job_id,
                InterviewSession.status == SessionStatus.terminated,
            )
            .first()
        )
        if terminated_session:
            return CandidateRegisterResponse(
                session_id=terminated_session.id,
                candidate_id=candidate.id,
                is_existing_user=True,
                already_completed=True,
                message="Your previous interview session was terminated. Please contact HR for further steps.",
            )

        # No active session — update profile and create a new one
        candidate.name = payload.name
        candidate.email = email_str
        if phone_str:
            candidate.phone = phone_str
        candidate.resume_path = payload.resume_path
        candidate.pre_interview_responses = extra_json
        db.flush()
    else:
        # Create new candidate — use try/except for race condition protection
        candidate = Candidate(
            name=payload.name,
            email=email_str,
            phone=phone_str or None,
            role=job.title if job else None,
            job_id=tok.job_id,
            resume_path=payload.resume_path,
            interview_status="Scheduled",
            pre_interview_responses=extra_json,
        )
        db.add(candidate)
        try:
            db.flush()
        except IntegrityError:
            db.rollback()
            # Race condition: another request created this candidate first — retry lookup
            existing = db.query(Candidate).filter(
                Candidate.email == email_str,
                Candidate.job_id == tok.job_id,
            ).first()
            if existing:
                # Recursively handle as existing candidate
                candidate = existing
                is_existing = True
                active_session = (
                    db.query(InterviewSession)
                    .filter(
                        InterviewSession.candidate_id == candidate.id,
                        InterviewSession.job_id == tok.job_id,
                        InterviewSession.status.in_([SessionStatus.pending, SessionStatus.in_progress]),
                    )
                    .order_by(InterviewSession.created_at.desc())
                    .first()
                )
                if active_session:
                    return CandidateRegisterResponse(
                        session_id=active_session.id,
                        candidate_id=candidate.id,
                        is_existing_user=True,
                        can_resume=True,
                        session_status=active_session.status.value,
                        message="Welcome back! Resuming your interview.",
                    )
            else:
                raise HTTPException(status_code=409, detail="Registration conflict. Please try again.")

    # Build session metadata
    metadata = json.dumps({
        "user_agent": request.headers.get("user-agent", ""),
        "ip": request.client.host if request.client else "",
    })

    # Create interview session
    settings_obj = job.interview_settings if job else None
    interview_session = InterviewSession(
        token_id=tok.id,
        candidate_id=candidate.id,
        job_id=tok.job_id,
        status=SessionStatus.pending,
        total_questions=settings_obj.max_questions if settings_obj else 8,
        session_metadata=metadata,
    )
    db.add(interview_session)
    db.commit()
    db.refresh(interview_session)

    # Compute fit score in background (non-blocking)
    background_tasks.add_task(_compute_fit_score_bg, candidate.id, tok.job_id)

    return CandidateRegisterResponse(
        session_id=interview_session.id,
        candidate_id=candidate.id,
        is_existing_user=is_existing,
        already_completed=False,
        can_resume=False,
        message="Session created. Ready to start interview.",
    )


@router.post(
    "/interview/{token}/login",
    response_model=CandidateRegisterResponse,
    summary="Login returning candidate by email",
    description=(
        "Allows a returning candidate (or admin-added candidate) to log in with just their email.\n"
        "Finds the candidate by email + job_id, then reuses or creates a session.\n\n"
        "**Errors:** `404` if no candidate found with this email for this job."
    ),
    tags=["Interview"],
)
def login_candidate(
    token: str,
    payload: CandidateLoginRequest,
    db: Session = Depends(get_db),
):
    tok = _get_token_or_404(token, db)
    job = db.query(Job).filter(Job.id == tok.job_id).first()

    candidate = db.query(Candidate).filter(
        Candidate.email == str(payload.email),
        Candidate.job_id == tok.job_id,
    ).first()

    if not candidate:
        raise HTTPException(
            status_code=404,
            detail="No candidate found with this email for this position. Please register first.",
        )

    # Check completed
    completed_session = (
        db.query(InterviewSession)
        .filter(
            InterviewSession.candidate_id == candidate.id,
            InterviewSession.job_id == tok.job_id,
            InterviewSession.status == SessionStatus.completed,
        )
        .first()
    )
    if completed_session:
        return CandidateRegisterResponse(
            session_id=completed_session.id,
            candidate_id=candidate.id,
            is_existing_user=True,
            already_completed=True,
            message="You have already completed your interview for this position.",
        )

    # Check for resumable session
    active_session = (
        db.query(InterviewSession)
        .filter(
            InterviewSession.candidate_id == candidate.id,
            InterviewSession.job_id == tok.job_id,
            InterviewSession.status.in_([SessionStatus.pending, SessionStatus.in_progress]),
        )
        .order_by(InterviewSession.created_at.desc())
        .first()
    )

    if active_session:
        return CandidateRegisterResponse(
            session_id=active_session.id,
            candidate_id=candidate.id,
            is_existing_user=True,
            already_completed=False,
            can_resume=True,
            session_status=active_session.status.value,
            message="Welcome back! Resuming your interview.",
        )

    # No active session — create a new one
    settings_obj = job.interview_settings if job else None
    new_session = InterviewSession(
        token_id=tok.id,
        candidate_id=candidate.id,
        job_id=tok.job_id,
        status=SessionStatus.pending,
        total_questions=settings_obj.max_questions if settings_obj else 8,
    )
    db.add(new_session)
    db.commit()
    db.refresh(new_session)

    return CandidateRegisterResponse(
        session_id=new_session.id,
        candidate_id=candidate.id,
        is_existing_user=True,
        already_completed=False,
        can_resume=False,
        message="Session created. Ready to start interview.",
    )


@router.get(
    "/interview/sessions/{session_id}/resume",
    response_model=ResumeSessionResponse,
    summary="Get session state for recovery",
    description="Returns the current session state including all messages for interview recovery after disconnect.",
    tags=["Interview"],
)
def resume_session(session_id: int, db: Session = Depends(get_db)):
    session = _get_session_or_404(session_id, db)

    if session.status not in (SessionStatus.pending, SessionStatus.in_progress):
        raise HTTPException(status_code=400, detail="Session cannot be resumed — it is already completed or terminated.")

    messages = (
        db.query(InterviewMessage)
        .filter(InterviewMessage.session_id == session_id)
        .order_by(InterviewMessage.created_at)
        .all()
    )

    return ResumeSessionResponse(
        session_id=session.id,
        status=session.status.value,
        current_question_index=session.current_question_index or 0,
        violation_count=session.violation_count or 0,
        messages=[
            {"role": m.role, "content": m.content, "question_index": m.question_index}
            for m in messages
        ],
    )


@router.post(
    "/interview/sessions/{session_id}/message",
    response_model=InterviewMessageResponse,
    summary="Send answer → receive next AI question",
    description=(
        "Sends the candidate's spoken/typed answer and returns the AI interviewer's next question.\n\n"
        "**Behaviour:**\n"
        "- Saves the candidate message to `interview_messages`.\n"
        "- Builds the full conversation history and calls **Claude Sonnet** with an adaptive system prompt "
        "(includes job context, resume summary, and interview rules).\n"
        "- Saves the AI reply to `interview_messages`.\n"
        "- When all questions are exhausted the AI sets `is_complete: true` in its JSON response, "
        "the session status becomes `completed`, and `is_complete` is forwarded to the client.\n\n"
        "**Errors:** `400` if session is already `completed` or `terminated`."
    ),
    tags=["Interview"],
)
def interview_message(
    session_id: int,
    payload: InterviewMessageRequest,
    db: Session = Depends(get_db),
):
    session = _get_session_or_404(session_id, db)

    if session.status == SessionStatus.terminated:
        raise HTTPException(status_code=400, detail="Interview has been terminated due to violations")
    if session.status == SessionStatus.completed:
        raise HTTPException(status_code=400, detail="Interview is already completed")

    # Mark as in_progress on first message
    if session.status == SessionStatus.pending:
        session.status = SessionStatus.in_progress
        session.started_at = datetime.utcnow()
        db.flush()

    result = svc.conduct_turn(session, payload.content, db)
    return InterviewMessageResponse(**result)


@router.post(
    "/interview/sessions/{session_id}/start",
    response_model=InterviewMessageResponse,
    summary="Start interview → AI opening message",
    description=(
        "Marks the session as `in_progress` and triggers the first AI turn with no candidate input "
        "(the AI introduces itself and asks question #1).\n\n"
        "Call this once after the candidate completes the permission/warning flow. "
        "Subsequent turns use `POST /interview/sessions/{id}/message`.\n\n"
        "**Errors:** `400` if session is not in `pending` or `in_progress` state."
    ),
    tags=["Interview"],
)
def start_interview(session_id: int, db: Session = Depends(get_db)):
    session = _get_session_or_404(session_id, db)

    if session.status not in (SessionStatus.pending, SessionStatus.in_progress):
        raise HTTPException(status_code=400, detail="Session is not in a startable state")

    session.status = SessionStatus.in_progress
    session.started_at = datetime.utcnow()
    db.flush()

    result = svc.conduct_turn(session, None, db)
    return InterviewMessageResponse(**result)


@router.post(
    "/interview/sessions/{session_id}/violation",
    response_model=ViolationResponse,
    summary="Log a cheating / integrity violation",
    description=(
        "Records a cheating event detected by the frontend monitoring system.\n\n"
        "**Violation types:**\n"
        "- `tab_switch` — candidate navigated away from the interview tab\n"
        "- `fullscreen_exit` — candidate exited fullscreen mode\n"
        "- `face_not_detected` — no face visible in webcam frame\n"
        "- `multiple_faces` — more than one person detected in frame\n\n"
        "**Auto-termination:** When `total_violations` reaches **5**, the session status is set to "
        "`terminated` and `should_terminate: true` is returned. The frontend should stop the interview immediately.\n\n"
        "**Returns:** `violation_id`, cumulative `total_violations`, and `should_terminate` flag."
    ),
    tags=["Interview"],
)
def log_violation(
    session_id: int,
    payload: ViolationRequest,
    db: Session = Depends(get_db),
):
    session = _get_session_or_404(session_id, db)

    try:
        v_type = ViolationType(payload.violation_type)
    except ValueError:
        raise HTTPException(status_code=422, detail=f"Invalid violation_type: {payload.violation_type}")

    violation = Violation(
        session_id=session_id,
        violation_type=v_type,
        details=payload.details,
        occurred_at=datetime.utcnow(),
    )
    db.add(violation)

    session.violation_count = (session.violation_count or 0) + 1
    should_terminate = session.violation_count >= VIOLATION_TERMINATE_THRESHOLD

    if should_terminate:
        session.status = SessionStatus.terminated
        session.completed_at = datetime.utcnow()
        # Mark candidate as terminated due to cheating
        candidate = db.query(Candidate).filter(Candidate.id == session.candidate_id).first()
        if candidate:
            log = InterviewStatusLog(
                candidate_id=candidate.id,
                previous_status=candidate.interview_status,
                new_status="Terminated – Cheating Detected",
                changed_by="system",
                reason=f"Auto-terminated after {session.violation_count} violations",
            )
            db.add(log)
            candidate.interview_status = "Terminated – Cheating Detected"

    db.commit()
    db.refresh(violation)

    return ViolationResponse(
        violation_id=violation.id,
        session_id=session_id,
        total_violations=session.violation_count,
        should_terminate=should_terminate,
    )


@router.post(
    "/interview/sessions/{session_id}/complete",
    response_model=EvaluationRead,
    summary="Complete interview & generate evaluation",
    description=(
        "Marks the session as `completed` and synchronously generates a structured AI evaluation "
        "using **Claude Opus**.\n\n"
        "**Evaluation scores (0–100):**\n"
        "- `technical_score` — depth and accuracy of technical knowledge\n"
        "- `communication_score` — clarity, articulation, professionalism\n"
        "- `problem_solving_score` — logical thinking and approach\n"
        "- `confidence_score` — composure under questioning\n"
        "- `overall_score` — weighted average (technical 40%, communication 25%, "
        "problem-solving 25%, confidence 10%)\n\n"
        "**Recommendation thresholds:**\n"
        "`Strong Hire` ≥ 80 · `Hire` ≥ 65 · `Maybe` ≥ 50 · `No Hire` < 50\n\n"
        "The evaluation is also saved to the `Evaluation` table and the candidate's top-level scores "
        "are mirrored back to the `candidates` row.\n\n"
        "**Errors:** `400` if session is already `completed` or `terminated`."
    ),
    tags=["Interview"],
)
def complete_interview(
    session_id: int,
    early_termination: bool = False,
    termination_reason: Optional[str] = None,
    db: Session = Depends(get_db),
):
    """
    Complete interview & generate evaluation.

    termination_reason values:
    - None / not provided → "Completed" (normal completion)
    - "ended_by_candidate" → "Ended by Candidate"
    - "network_disconnect" → "Disconnected – Network Issue"
    - "candidate_terminated" → "Candidate Terminated Prematurely"
    - any other truthy value → "Candidate Terminated Prematurely"
    """
    session = _get_session_or_404(session_id, db)

    # If session is already completed (AI set it via conduct_turn), return existing
    # evaluation or generate one — never error
    if session.status == SessionStatus.completed:
        if session.evaluation:
            return _format_evaluation(session.evaluation)
        evaluation = svc.generate_evaluation(session, db)
        return _format_evaluation(evaluation)

    if session.status == SessionStatus.terminated:
        raise HTTPException(status_code=400, detail="Session was terminated due to violations")

    session.status = SessionStatus.completed
    session.completed_at = datetime.utcnow()

    # Determine interview_status based on how the interview ended
    candidate = db.query(Candidate).filter(Candidate.id == session.candidate_id).first()
    if candidate:
        if not early_termination:
            new_status = "Completed"
        elif termination_reason == "ended_by_candidate":
            new_status = "Ended by Candidate"
        elif termination_reason == "network_disconnect":
            new_status = "Disconnected – Network Issue"
        else:
            new_status = "Candidate Terminated Prematurely"

        log = InterviewStatusLog(
            candidate_id=candidate.id,
            previous_status=candidate.interview_status,
            new_status=new_status,
            changed_by="system",
            reason=termination_reason or "Interview completed normally",
        )
        db.add(log)
        candidate.interview_status = new_status

    db.flush()

    evaluation = svc.generate_evaluation(session, db)
    return _format_evaluation(evaluation)


@router.get(
    "/interview/sessions/{session_id}/evaluation",
    response_model=EvaluationRead,
    summary="Get evaluation result",
    description=(
        "Retrieves the stored evaluation for a completed session.\n\n"
        "Returns all four dimension scores, overall score, recommendation, "
        "summary, strengths, weaknesses, and detailed feedback.\n\n"
        "**Errors:** `404` if the evaluation has not been generated yet "
        "(call `POST /complete` first)."
    ),
    tags=["Interview"],
)
def get_evaluation(session_id: int, db: Session = Depends(get_db)):
    session = _get_session_or_404(session_id, db)
    if not session.evaluation:
        raise HTTPException(status_code=404, detail="Evaluation not yet generated")
    return _format_evaluation(session.evaluation)


@router.post(
    "/interview/sessions/{session_id}/feedback",
    response_model=FeedbackResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Submit candidate experience feedback",
    description=(
        "Records the candidate's post-interview feedback.\n\n"
        "**Fields:**\n"
        "- `rating` — integer 1–5 (required)\n"
        "- `experience_comment` — optional free-text comment\n"
        "- `technical_issues` — boolean, whether the candidate experienced tech problems\n"
        "- `would_recommend` — boolean, NPS-style recommendation flag\n\n"
        "Only one feedback record is allowed per session. "
        "**Errors:** `400` if feedback was already submitted."
    ),
    tags=["Interview"],
)
def submit_feedback(
    session_id: int,
    payload: FeedbackRequest,
    db: Session = Depends(get_db),
):
    session = _get_session_or_404(session_id, db)
    if session.feedback:
        raise HTTPException(status_code=400, detail="Feedback already submitted for this session")

    fb = CandidateFeedback(
        session_id=session_id,
        rating=payload.rating,
        experience_comment=payload.experience_comment,
        technical_issues=payload.technical_issues,
        would_recommend=payload.would_recommend,
    )
    db.add(fb)
    db.commit()
    db.refresh(fb)

    return FeedbackResponse(feedback_id=fb.id, message="Thank you for your feedback!")


@router.get(
    "/interview/sessions/{session_id}/slots",
    response_model=List[MeetingSlotRead],
    summary="List available meeting slots",
    description=(
        "Returns all unbooked future meeting slots for the job associated with this session.\n\n"
        "Slots are ordered by `start_time` ascending. Only slots where `is_booked = false` "
        "and `start_time > now (UTC)` are returned.\n\n"
        "Slots are created by admins via `POST /jobs/{job_id}/meeting-slots`."
    ),
    tags=["Interview"],
)
def get_meeting_slots(session_id: int, db: Session = Depends(get_db)):
    session = _get_session_or_404(session_id, db)
    slots = (
        db.query(MeetingSlot)
        .filter(
            MeetingSlot.job_id == session.job_id,
            MeetingSlot.is_booked == False,
            MeetingSlot.start_time > datetime.utcnow(),
        )
        .order_by(MeetingSlot.start_time)
        .all()
    )
    return slots


@router.post(
    "/interview/sessions/{session_id}/schedule-meeting",
    response_model=ScheduleMeetingResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Book a post-interview meeting slot",
    description=(
        "Books one of the available meeting slots for the candidate.\n\n"
        "**Side effects:**\n"
        "- Sets `meeting_slots.is_booked = true` (atomic — prevents double-booking).\n"
        "- Creates a `MeetingSchedule` record linking the session, slot, and candidate.\n\n"
        "Only one meeting can be scheduled per session. "
        "**Errors:** `400` if already scheduled · `404` if the slot is unavailable or already booked."
    ),
    tags=["Interview"],
)
def schedule_meeting(
    session_id: int,
    payload: ScheduleMeetingRequest,
    db: Session = Depends(get_db),
):
    session = _get_session_or_404(session_id, db)
    if session.meeting_schedule:
        raise HTTPException(status_code=400, detail="Meeting already scheduled for this session")

    slot = db.query(MeetingSlot).filter(
        MeetingSlot.id == payload.slot_id,
        MeetingSlot.is_booked == False,
    ).first()
    if not slot:
        raise HTTPException(status_code=404, detail="Slot not available")

    slot.is_booked = True
    schedule = MeetingSchedule(
        session_id=session_id,
        slot_id=slot.id,
        candidate_id=session.candidate_id,
    )
    db.add(schedule)
    db.commit()
    db.refresh(schedule)

    return ScheduleMeetingResponse(
        schedule_id=schedule.id,
        slot_id=slot.id,
        start_time=slot.start_time,
        end_time=slot.end_time,
        message="Meeting scheduled successfully!",
    )


@router.post(
    "/interview/sessions/{session_id}/upload-recording",
    response_model=RecordingUploadResponse,
    summary="Upload interview recording",
    description="Stores the candidate's interview video recording and updates the candidate record with the link and duration.",
    tags=["Interview"],
)
async def upload_recording(
    session_id: int,
    file: UploadFile = File(...),
    duration: str = Form(default=""),
    db: Session = Depends(get_db),
):
    session = _get_session_or_404(session_id, db)

    os.makedirs("uploads/recordings", exist_ok=True)
    ext = ".webm"
    if file.filename and "." in file.filename:
        ext = "." + file.filename.rsplit(".", 1)[-1]
    file_path = f"uploads/recordings/{session_id}{ext}"

    content = await file.read()
    with open(file_path, "wb") as f:
        f.write(content)

    recording_link = f"/uploads/recordings/{session_id}{ext}"

    # Update candidate record
    if session.candidate_id:
        candidate = db.query(Candidate).filter(Candidate.id == session.candidate_id).first()
        if candidate:
            candidate.recording_link = recording_link
            candidate.video_duration = duration or "0:00"
            db.commit()

    return RecordingUploadResponse(
        recording_link=recording_link,
        video_duration=duration or "0:00",
        message="Recording saved successfully.",
    )


# ── Helpers ────────────────────────────────────────────────────────────────────

def _compute_fit_score_bg(candidate_id: int, job_id: int) -> None:
    """Background task: compute resume fit score after candidate registration."""
    db = SessionLocal()
    try:
        from app.services.resume_service import extract_text, calculate_fit_score
        from app.schemas.ai import FitScoreRequest

        candidate = db.query(Candidate).filter(Candidate.id == candidate_id).first()
        job = db.query(Job).filter(Job.id == job_id).first()
        if not candidate or not job or not candidate.resume_path:
            return

        full_path = candidate.resume_path
        if not full_path.startswith("/"):
            full_path = full_path.lstrip("/")
        if not os.path.exists(full_path):
            return

        resume_text = extract_text(full_path)
        skills = [s.skill for s in job.skills] if job.skills else []
        responsibilities = [r.description for r in job.responsibilities] if job.responsibilities else []

        req = FitScoreRequest(
            resume_text=resume_text,
            job_title=job.title,
            required_skills=skills,
            responsibilities=responsibilities,
            required_experience_min=job.experience_min,
            required_experience_max=job.experience_max,
        )
        result = calculate_fit_score(req)

        candidate.resume_fit_score = result.fit_level
        candidate.fit_score_reasoning = json.dumps({
            "summary": result.summary,
            "breakdown": result.breakdown.model_dump() if result.breakdown else {},
            "percentage": result.fit_percentage,
            "key_matching_skills": result.key_matching_skills,
            "relevant_experience_summary": result.relevant_experience_summary,
            "missing_weak_areas": result.missing_weak_areas,
        })
        candidate.fit_score_details = json.dumps(result.reasoning_points)
        db.commit()
    except Exception:
        pass
    finally:
        db.close()


def _format_evaluation(ev) -> EvaluationRead:
    strengths = []
    weaknesses = []
    try:
        strengths = json.loads(ev.strengths) if ev.strengths else []
    except Exception:
        pass
    try:
        weaknesses = json.loads(ev.weaknesses) if ev.weaknesses else []
    except Exception:
        pass

    return EvaluationRead(
        id=ev.id,
        session_id=ev.session_id,
        candidate_id=ev.candidate_id,
        technical_score=ev.technical_score,
        communication_score=ev.communication_score,
        problem_solving_score=ev.problem_solving_score,
        confidence_score=ev.confidence_score,
        overall_score=ev.overall_score,
        recommendation=ev.recommendation.value if ev.recommendation else None,
        summary=ev.summary,
        strengths=strengths,
        weaknesses=weaknesses,
        detailed_feedback=ev.detailed_feedback,
        meets_threshold=ev.meets_threshold or False,
        created_at=ev.created_at,
    )
