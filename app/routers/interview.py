import json
import logging
import os
import traceback
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
    InterviewScreenshot,
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
from app.models.organization import Organization
from app.database import SessionLocal


def _check_and_increment_candidate_quota(db, job_id: int) -> None:
    """Check lifetime candidate quota and increment counter. Raises 403 if exceeded."""
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job or not job.organization_id:
        return
    org = db.query(Organization).filter(Organization.id == job.organization_id).first()
    if not org or (org.max_candidates_per_job or 0) == 0:
        return
    if (org.total_candidates_used or 0) >= org.max_candidates_per_job:
        raise HTTPException(
            status_code=403,
            detail="You have exceeded the candidate limit. Please upgrade your plan.",
        )
    org.total_candidates_used = (org.total_candidates_used or 0) + 1
    db.flush()
from app.schemas.interview import (
    ApplyForJobRequest,
    ApplyForJobResponse,
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
    RecordingUploadResponse,
    ResumeSessionResponse,
    ScheduleMeetingRequest,
    ScheduleMeetingResponse,
    VerifyAccessRequest,
    VerifyAccessResponse,
    ViolationRequest,
    ViolationResponse,
)
from app.services import avatar_interview_service as svc
from slowapi import Limiter
from slowapi.util import get_remote_address

limiter = Limiter(key_func=get_remote_address)

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
    # Check if token has expired
    if obj.expires_at and obj.expires_at < datetime.utcnow():
        raise HTTPException(status_code=410, detail="This interview link has expired")
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
        code_editor_required=settings_obj.code_editor_required if settings_obj else True,
        allow_mobile=settings_obj.allow_mobile if settings_obj else False,
        secondary_cam=settings_obj.secondary_cam if settings_obj else False,
        manual_interview=settings_obj.manual_interview if settings_obj else False,
        special_instructions=settings_obj.special_instructions if settings_obj else None,
        auto_skip_timeout=settings_obj.auto_skip_timeout if settings_obj else 0,
        skills=skills,
    )


# ── Verify Access (invitation-based) ─────────────────────────────────────────

@router.post(
    "/interview/{token}/verify-access",
    response_model=VerifyAccessResponse,
    summary="Verify candidate access by email",
    description=(
        "Checks if the candidate's email is associated with an invited candidate for this job.\n\n"
        "Returns 'granted' if the candidate was invited (exists in candidates table for this job) "
        "AND has a resume in the database.\n"
        "Returns 'apply' if the email is not found — candidate should use Apply for Job.\n"
        "Returns 'completed' if candidate already finished the interview.\n"
        "Returns 'terminated' if candidate's session was terminated."
    ),
    tags=["Interview"],
)
@limiter.limit("10/minute")
def verify_access(
    token: str,
    payload: VerifyAccessRequest,
    request: Request,
    db: Session = Depends(get_db),
):
    from app.models.resume_metadata import ResumeMetadata

    tok = _get_token_or_404(token, db)
    email_str = str(payload.email).strip().lower()

    # 1. Check if candidate exists for this job (invited by recruiter)
    candidate = db.query(Candidate).filter(
        Candidate.email == email_str,
        Candidate.job_id == tok.job_id,
    ).first()

    if not candidate:
        # Also check resume_metadata — if resume exists but not invited for this job
        meta = db.query(ResumeMetadata).filter(
            ResumeMetadata.candidate_email == email_str
        ).first()

        if meta:
            return VerifyAccessResponse(
                access="apply",
                candidate_name=meta.candidate_name,
                message="Your resume is on file but you haven't been invited for this position. You can apply for this job.",
            )

        return VerifyAccessResponse(
            access="apply",
            message="No record found. Please apply for this job by uploading your resume.",
        )

    # 2. Check for completed session
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
        return VerifyAccessResponse(
            access="completed",
            candidate_name=candidate.name,
            session_id=completed_session.id,
            candidate_id=candidate.id,
            message="You have already completed your interview for this position.",
        )

    # 3. Check for terminated session
    terminated = (
        db.query(InterviewSession)
        .filter(
            InterviewSession.candidate_id == candidate.id,
            InterviewSession.job_id == tok.job_id,
            InterviewSession.status == SessionStatus.terminated,
        )
        .first()
    )
    if terminated:
        return VerifyAccessResponse(
            access="terminated",
            candidate_name=candidate.name,
            session_id=terminated.id,
            candidate_id=candidate.id,
            message="Your previous interview was terminated. Please contact HR.",
        )

    # 4. Check for resumable session
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
    # Common scheduling fields for all "granted" responses
    sched_fields = dict(
        interview_date=candidate.interview_date,
        interview_timezone=candidate.interview_timezone,
        joining_flexibility=candidate.joining_flexibility,
        allow_joining_early=getattr(candidate, "allow_joining_early", False),
    )

    if active_session:
        return VerifyAccessResponse(
            access="granted",
            candidate_name=candidate.name,
            session_id=active_session.id,
            candidate_id=candidate.id,
            can_resume=True,
            session_status=active_session.status.value,
            message="Welcome back! You can resume your interview.",
            **sched_fields,
        )

    # 5. Candidate is invited but has no session yet — create one
    job = db.query(Job).filter(Job.id == tok.job_id).first()
    metadata_json = json.dumps({
        "user_agent": request.headers.get("user-agent", ""),
        "ip": request.client.host if request.client else "",
    })
    settings_obj = job.interview_settings if job else None
    interview_session = InterviewSession(
        token_id=tok.id,
        candidate_id=candidate.id,
        job_id=tok.job_id,
        status=SessionStatus.pending,
        total_questions=settings_obj.max_questions if settings_obj else 8,
        session_metadata=metadata_json,
    )
    db.add(interview_session)
    db.commit()
    db.refresh(interview_session)

    return VerifyAccessResponse(
        access="granted",
        candidate_name=candidate.name,
        session_id=interview_session.id,
        candidate_id=candidate.id,
        can_resume=False,
        message="Access verified. Ready to start your interview.",
        **sched_fields,
    )


# ── Upload resume file (no candidate record — just save file) ────────────────

@router.post(
    "/interview/{token}/upload-resume",
    summary="Upload a resume file for job application",
    tags=["Interview"],
)
@limiter.limit("10/minute")
async def upload_resume_for_apply(
    token: str,
    request: Request,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    """Save a resume file to disk and return the path. No candidate record is created."""
    _get_token_or_404(token, db)  # validate token

    allowed = {
        "application/pdf",
        "application/msword",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    }
    if file.content_type not in allowed:
        raise HTTPException(status_code=400, detail="Only PDF, DOC, or DOCX files are accepted.")

    upload_dir = "uploads/resumes"
    os.makedirs(upload_dir, exist_ok=True)

    safe_name = f"{datetime.utcnow().strftime('%Y%m%d%H%M%S%f')}_{file.filename}"
    file_path = os.path.join(upload_dir, safe_name)

    with open(file_path, "wb") as f:
        content = await file.read()
        f.write(content)

    return {"resume_path": file_path}


# ── Apply for Job (upload resume without registration) ──────────────────────

@router.post(
    "/interview/{token}/apply",
    response_model=ApplyForJobResponse,
    summary="Apply for job — upload resume and basic details",
    description=(
        "Allows a candidate who hasn't been invited to submit their resume and basic details.\n"
        "The resume is stored in the database and parsed as metadata.\n"
        "The candidate will NOT be able to start the interview — only invited candidates can.\n"
        "After applying, the recruiter can review and invite the candidate."
    ),
    tags=["Interview"],
)
@limiter.limit("5/minute")
def apply_for_job(
    token: str,
    payload: ApplyForJobRequest,
    background_tasks: BackgroundTasks,
    request: Request,
    db: Session = Depends(get_db),
):
    from app.models.resume_metadata import ResumeMetadata

    tok = _get_token_or_404(token, db)
    job = db.query(Job).filter(Job.id == tok.job_id).first()
    email_str = str(payload.email).strip().lower()

    # Check if resume metadata already exists for this email
    existing_meta = db.query(ResumeMetadata).filter(
        ResumeMetadata.candidate_email == email_str
    ).first()

    if existing_meta:
        # Update the existing record with new resume
        existing_meta.candidate_name = payload.name
        existing_meta.phone = payload.phone
        existing_meta.resume_file_path = payload.resume_path
        existing_meta.parsing_status = "pending"
        db.commit()
        meta_id = existing_meta.id
    else:
        # Create new resume metadata record
        meta = ResumeMetadata(
            candidate_email=email_str,
            candidate_name=payload.name,
            phone=payload.phone,
            resume_file_path=payload.resume_path,
            parsing_status="pending",
        )
        db.add(meta)
        db.commit()
        db.refresh(meta)
        meta_id = meta.id

    # Also create/update a candidate record for this job
    existing_candidate = db.query(Candidate).filter(
        Candidate.email == email_str,
        Candidate.job_id == tok.job_id,
    ).first()

    if not existing_candidate:
        _check_and_increment_candidate_quota(db, tok.job_id)
        candidate = Candidate(
            name=payload.name,
            email=email_str,
            phone=payload.phone or None,
            role=job.title if job else None,
            job_id=tok.job_id,
            resume_path=payload.resume_path,
            interview_status="Applied",
            candidate_stage="Candidates",
            resume_metadata_id=meta_id,
        )
        db.add(candidate)
        db.commit()

    # Queue background parsing
    background_tasks.add_task(_parse_resume_metadata_bg, meta_id)

    return ApplyForJobResponse(
        success=True,
        message="Your application has been submitted. The recruiter will review your resume and may invite you for an interview.",
        resume_metadata_id=meta_id,
    )


def _parse_resume_metadata_bg(meta_id: int):
    """Background task to parse resume and update metadata."""
    from app.services.fast_resume_parser import extract_text_from_file, fast_parse_resume
    db = SessionLocal()
    try:
        from app.models.resume_metadata import ResumeMetadata
        meta = db.query(ResumeMetadata).filter(ResumeMetadata.id == meta_id).first()
        if not meta or not meta.resume_file_path:
            return

        meta.parsing_status = "processing"
        db.commit()

        raw_text = extract_text_from_file(meta.resume_file_path)
        if not raw_text:
            meta.parsing_status = "failed"
            meta.parsing_error = "Could not extract text from file"
            db.commit()
            return

        parsed = fast_parse_resume(raw_text)
        meta.raw_text = raw_text
        meta.candidate_name = parsed.get("candidate_name") or meta.candidate_name
        meta.phone = parsed.get("phone") or meta.phone
        meta.skills = json.dumps(parsed.get("skills", []))
        meta.experience_years = parsed.get("experience_years")
        meta.education = json.dumps(parsed.get("education", []))
        meta.certifications = json.dumps(parsed.get("certifications", []))
        meta.work_experience = json.dumps(parsed.get("work_experience", []))
        meta.location = parsed.get("location") or meta.location
        meta.current_designation = parsed.get("current_designation")
        meta.keywords = json.dumps(parsed.get("keywords", []))
        meta.personal_details = json.dumps(parsed.get("personal_details") or {})
        meta.summary = parsed.get("summary")
        meta.parsing_status = "completed"
        meta.parsed_at = datetime.utcnow()
        db.commit()
    except Exception as e:
        try:
            meta = db.query(ResumeMetadata).filter(ResumeMetadata.id == meta_id).first()
            if meta:
                meta.parsing_status = "failed"
                meta.parsing_error = str(e)[:500]
                db.commit()
        except:
            pass
    finally:
        db.close()


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
@limiter.limit("5/minute")
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
        db.flush()
    else:
        # Create new candidate — check quota and use try/except for race condition protection
        _check_and_increment_candidate_quota(db, tok.job_id)
        candidate = Candidate(
            name=payload.name,
            email=email_str,
            phone=phone_str or None,
            role=job.title if job else None,
            job_id=tok.job_id,
            resume_path=payload.resume_path,
            interview_status="Scheduled",
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
    "/interview/{token}/auto-register",
    response_model=CandidateRegisterResponse,
    summary="Auto-register candidate from resume database (no form required)",
    description=(
        "Automatically registers a candidate for the interview using their email.\n"
        "Looks up the resume database for candidate metadata (name, phone, resume path).\n"
        "If found, creates a Candidate record and InterviewSession without any registration form.\n\n"
        "Use this when sending interview links directly to known candidates from the resume database.\n"
        "Falls back to the candidate table if not in resume_metadata.\n\n"
        "**Query param:** `email` — the candidate's email address."
    ),
    tags=["Interview"],
)
@limiter.limit("10/minute")
def auto_register_candidate(
    token: str,
    email: str,
    background_tasks: BackgroundTasks,
    request: Request,
    db: Session = Depends(get_db),
):
    from app.models.resume_metadata import ResumeMetadata

    tok = _get_token_or_404(token, db)
    job = db.query(Job).filter(Job.id == tok.job_id).first()
    email_str = email.strip().lower()

    if not email_str:
        raise HTTPException(status_code=400, detail="Email is required")

    # Check for existing candidate for this job
    existing = db.query(Candidate).filter(
        Candidate.email == email_str,
        Candidate.job_id == tok.job_id,
    ).first()

    if existing:
        # Check completed/active sessions (same logic as register)
        completed_session = (
            db.query(InterviewSession)
            .filter(
                InterviewSession.candidate_id == existing.id,
                InterviewSession.job_id == tok.job_id,
                InterviewSession.status == SessionStatus.completed,
            )
            .first()
        )
        if completed_session:
            return CandidateRegisterResponse(
                session_id=completed_session.id,
                candidate_id=existing.id,
                is_existing_user=True,
                already_completed=True,
                message="You have already completed your interview for this position.",
            )

        active_session = (
            db.query(InterviewSession)
            .filter(
                InterviewSession.candidate_id == existing.id,
                InterviewSession.job_id == tok.job_id,
                InterviewSession.status.in_([SessionStatus.pending, SessionStatus.in_progress]),
            )
            .order_by(InterviewSession.created_at.desc())
            .first()
        )
        if active_session:
            return CandidateRegisterResponse(
                session_id=active_session.id,
                candidate_id=existing.id,
                is_existing_user=True,
                can_resume=True,
                session_status=active_session.status.value,
                message="Welcome back! Resuming your interview.",
            )

        terminated = (
            db.query(InterviewSession)
            .filter(
                InterviewSession.candidate_id == existing.id,
                InterviewSession.job_id == tok.job_id,
                InterviewSession.status == SessionStatus.terminated,
            )
            .first()
        )
        if terminated:
            return CandidateRegisterResponse(
                session_id=terminated.id,
                candidate_id=existing.id,
                is_existing_user=True,
                already_completed=True,
                message="Your previous interview was terminated. Please contact HR.",
            )

        candidate = existing
        is_existing = True
    else:
        # Look up resume database for metadata
        meta = db.query(ResumeMetadata).filter(
            ResumeMetadata.candidate_email == email_str
        ).first()

        candidate_name = email_str.split("@")[0].replace(".", " ").title()
        candidate_phone = None
        resume_path = None

        if meta:
            candidate_name = meta.candidate_name or candidate_name
            candidate_phone = meta.phone
            resume_path = meta.resume_file_path

        _check_and_increment_candidate_quota(db, tok.job_id)
        candidate = Candidate(
            name=candidate_name,
            email=email_str,
            phone=candidate_phone,
            role=job.title if job else None,
            job_id=tok.job_id,
            resume_path=resume_path,
            interview_status="Scheduled",
            resume_metadata_id=meta.id if meta else None,
        )
        db.add(candidate)
        db.commit()
        db.refresh(candidate)
        is_existing = False

    # Create session
    metadata_json = json.dumps({
        "user_agent": request.headers.get("user-agent", ""),
        "ip": request.client.host if request.client else "",
        "auto_registered": True,
    })
    settings_obj = job.interview_settings if job else None
    interview_session = InterviewSession(
        token_id=tok.id,
        candidate_id=candidate.id,
        job_id=tok.job_id,
        status=SessionStatus.pending,
        total_questions=settings_obj.max_questions if settings_obj else 8,
        session_metadata=metadata_json,
    )
    db.add(interview_session)
    db.commit()
    db.refresh(interview_session)

    # Background fit score
    if candidate.resume_path:
        background_tasks.add_task(_compute_fit_score_bg, candidate.id, tok.job_id)

    return CandidateRegisterResponse(
        session_id=interview_session.id,
        candidate_id=candidate.id,
        is_existing_user=is_existing,
        already_completed=False,
        can_resume=False,
        message="Auto-registered successfully. Ready to start interview.",
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
@limiter.limit("10/minute")
async def interview_message(
    session_id: int,
    payload: InterviewMessageRequest,
    request: Request,
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

    result = await svc.conduct_turn(session, payload.content, db)
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
@limiter.limit("5/minute")
async def start_interview(session_id: int, request: Request, db: Session = Depends(get_db)):
    session = _get_session_or_404(session_id, db)

    if session.status not in (SessionStatus.pending, SessionStatus.in_progress):
        raise HTTPException(status_code=400, detail="Session is not in a startable state")

    session.status = SessionStatus.in_progress
    session.started_at = datetime.utcnow()
    db.flush()

    result = await svc.conduct_turn(session, None, db)
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
@limiter.limit("20/minute")
def log_violation(
    session_id: int,
    payload: ViolationRequest,
    request: Request,
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


async def _run_evaluation_background(session_id: int):
    """Background task: generate evaluation using a fresh DB session."""
    db = SessionLocal()
    try:
        logging.info(f"[Evaluation] Starting background evaluation for session {session_id}")
        session = db.query(InterviewSession).filter(InterviewSession.id == session_id).first()
        if not session:
            logging.error(f"[Evaluation] Session {session_id} not found in DB")
            return
        if session.evaluation:
            logging.info(f"[Evaluation] Session {session_id} already has evaluation — skipping")
            return
        messages_count = db.query(InterviewMessage).filter(InterviewMessage.session_id == session_id).count()
        logging.info(f"[Evaluation] Session {session_id} — {messages_count} messages found, calling AI...")
        await svc.generate_evaluation(session, db)
        logging.info(f"[Evaluation] Session {session_id} — evaluation saved successfully")
    except Exception as e:
        logging.error(
            f"[Evaluation] FAILED for session {session_id}: {type(e).__name__}: {e}\n"
            f"{traceback.format_exc()}"
        )
    finally:
        db.close()


@router.post(
    "/interview/sessions/{session_id}/complete",
    status_code=202,
    summary="Complete interview & trigger evaluation",
    description=(
        "Marks the session as `completed` and triggers AI evaluation in the background.\n\n"
        "Returns `202 Accepted` immediately. Poll `GET /interview/sessions/{id}/evaluation` "
        "every 3–5 seconds until it returns `200` with the evaluation data.\n\n"
        "**Errors:** `400` if session is already `completed` or `terminated`."
    ),
    tags=["Interview"],
)
@limiter.limit("3/minute")
async def complete_interview(
    session_id: int,
    request: Request,
    background_tasks: BackgroundTasks,
    early_termination: bool = False,
    termination_reason: Optional[str] = None,
    db: Session = Depends(get_db),
):
    """
    Complete interview & trigger background evaluation.

    termination_reason values:
    - None / not provided → "Completed" (normal completion)
    - "ended_by_candidate" → "Ended by Candidate"
    - "network_disconnect" → "Disconnected – Network Issue"
    - "candidate_terminated" → "Candidate Terminated Prematurely"
    - any other truthy value → "Candidate Terminated Prematurely"
    """
    session = _get_session_or_404(session_id, db)

    # If already completed/terminated and evaluation exists, nothing to do
    if session.status in (SessionStatus.completed, SessionStatus.terminated):
        if not session.evaluation:
            background_tasks.add_task(_run_evaluation_background, session_id)
        return {"status": "processing", "session_id": session_id}

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

    db.commit()

    # Trigger evaluation in background — returns 202 immediately
    background_tasks.add_task(_run_evaluation_background, session_id)
    return {"status": "processing", "session_id": session_id}


@router.post(
    "/interview/sessions/{session_id}/drop",
    status_code=200,
    summary="Mark session as dropped (tab close / network disconnect)",
    description=(
        "Lightweight endpoint called via sendBeacon when the candidate closes the tab or disconnects.\n\n"
        "Marks the session status as `terminated` and updates the candidate's interview_status to "
        "`'Candidate Dropped Interview'`.\n\n"
        "Does NOT generate an evaluation (fast — safe for use with navigator.sendBeacon).\n\n"
        "Idempotent: silently succeeds if session is already completed or terminated."
    ),
    tags=["Interview"],
)
def drop_interview(session_id: int, db: Session = Depends(get_db)):
    session = db.query(InterviewSession).filter(InterviewSession.id == session_id).first()
    if not session:
        return {"ok": False, "reason": "not_found"}

    # Only act on active sessions
    if session.status in (SessionStatus.pending, SessionStatus.in_progress):
        session.status = SessionStatus.terminated
        session.completed_at = datetime.utcnow()

        candidate = db.query(Candidate).filter(Candidate.id == session.candidate_id).first()
        if candidate:
            new_status = "Candidate Dropped Interview"
            log = InterviewStatusLog(
                candidate_id=candidate.id,
                previous_status=candidate.interview_status,
                new_status=new_status,
                changed_by="system",
                reason="Tab closed or browser navigated away during interview",
            )
            db.add(log)
            candidate.interview_status = new_status

        db.commit()

    return {"ok": True}


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
@limiter.limit("3/minute")
async def upload_recording(
    session_id: int,
    request: Request,
    file: UploadFile = File(...),
    duration: str = Form(default=""),
    db: Session = Depends(get_db),
):
    MAX_RECORDING_SIZE = 100 * 1024 * 1024  # 100 MB

    session = _get_session_or_404(session_id, db)

    _is_serverless = bool(os.environ.get("VERCEL") or os.environ.get("AWS_LAMBDA_FUNCTION_NAME"))
    _upload_root = "/tmp/uploads" if _is_serverless else "uploads"
    os.makedirs(f"{_upload_root}/recordings", exist_ok=True)
    ext = ".webm"
    if file.filename and "." in file.filename:
        ext = "." + file.filename.rsplit(".", 1)[-1]
    file_path = f"{_upload_root}/recordings/{session_id}{ext}"

    total_size = 0
    with open(file_path, "wb") as f:
        while chunk := await file.read(8192):
            total_size += len(chunk)
            if total_size > MAX_RECORDING_SIZE:
                f.close()
                os.remove(file_path)
                raise HTTPException(status_code=413, detail="Recording too large (max 100 MB)")
            f.write(chunk)

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


@router.post(
    "/interview/sessions/{session_id}/capture-photo",
    status_code=200,
    summary="Capture and save candidate profile photo",
    description="Accepts a JPEG photo from the candidate's webcam during the pre-interview photo capture step.",
    tags=["Interview"],
)
@limiter.limit("5/minute")
async def capture_candidate_photo(
    session_id: int,
    request: Request,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    session = db.query(InterviewSession).filter(InterviewSession.id == session_id).first()
    if not session or not session.candidate_id:
        raise HTTPException(status_code=404, detail="Session or candidate not found")

    candidate = db.query(Candidate).filter(Candidate.id == session.candidate_id).first()
    if not candidate:
        raise HTTPException(status_code=404, detail="Candidate not found")

    _is_serverless = bool(os.environ.get("VERCEL") or os.environ.get("AWS_LAMBDA_FUNCTION_NAME"))
    _upload_root = "/tmp/uploads" if _is_serverless else "uploads"
    photo_dir = f"{_upload_root}/photos"
    os.makedirs(photo_dir, exist_ok=True)

    ext = ".jpg"
    if file.filename and "." in file.filename:
        ext = "." + file.filename.rsplit(".", 1)[-1].lower()
    file_path = f"{photo_dir}/{candidate.id}{ext}"

    MAX_PHOTO_SIZE = 5 * 1024 * 1024  # 5 MB
    total_size = 0
    with open(file_path, "wb") as f:
        while chunk := await file.read(8192):
            total_size += len(chunk)
            if total_size > MAX_PHOTO_SIZE:
                f.close()
                os.remove(file_path)
                raise HTTPException(status_code=413, detail="Photo too large (max 5 MB)")
            f.write(chunk)

    # Compress photo: resize to max 400x500, JPEG quality 80
    try:
        from PIL import Image
        with Image.open(file_path) as img:
            img.thumbnail((400, 500), Image.LANCZOS)
            img.save(file_path, "JPEG", quality=80, optimize=True)
    except Exception:
        pass

    relative_path = f"/uploads/photos/{candidate.id}{ext}"
    candidate.image = relative_path
    db.commit()

    return {"ok": True, "image_url": relative_path}


@router.post(
    "/interview/sessions/{session_id}/screenshot",
    status_code=200,
    summary="Upload a periodic proctoring screenshot",
    description=(
        "Accepts a JPEG snapshot from the candidate's webcam, captured every 13–15 seconds "
        "by the frontend. Stored under `uploads/screenshots/{session_id}/`. "
        "Silent — never fails the interview if this call errors."
    ),
    tags=["Interview"],
)
@limiter.limit("10/minute")
async def upload_screenshot(
    session_id: int,
    request: Request,
    file: UploadFile = File(...),
    timestamp: str = Form(default=""),
    face_count: str = Form(default=""),
    db: Session = Depends(get_db),
):
    MAX_SCREENSHOT_SIZE = 2 * 1024 * 1024  # 2 MB per screenshot

    session = db.query(InterviewSession).filter(InterviewSession.id == session_id).first()
    if not session:
        return {"ok": False}

    _is_serverless = bool(os.environ.get("VERCEL") or os.environ.get("AWS_LAMBDA_FUNCTION_NAME"))
    _upload_root = "/tmp/uploads" if _is_serverless else "uploads"
    screenshot_dir = f"{_upload_root}/screenshots/{session_id}"
    os.makedirs(screenshot_dir, exist_ok=True)

    safe_ts = (timestamp or "").replace(":", "-").replace(".", "-")[:23]
    filename = f"{safe_ts or 'snap'}.jpg"
    raw_path = f"{screenshot_dir}/{filename}"

    total_size = 0
    with open(raw_path, "wb") as f:
        while chunk := await file.read(8192):
            total_size += len(chunk)
            if total_size > MAX_SCREENSHOT_SIZE:
                f.close()
                os.remove(raw_path)
                return {"ok": False, "reason": "too_large"}
            f.write(chunk)

    # Compress screenshot: resize to max 800x600, JPEG quality 70
    try:
        from PIL import Image
        with Image.open(raw_path) as img:
            img.thumbnail((800, 600), Image.LANCZOS)
            img.save(raw_path, "JPEG", quality=70, optimize=True)
    except Exception:
        pass  # keep the original if compression fails

    relative_path = f"/uploads/screenshots/{session_id}/{filename}"

    # Use face count from frontend MediaPipe detection (much more accurate than server-side Haar cascade)
    detected_faces = 1  # default: assume single face
    if face_count.strip():
        try:
            detected_faces = int(face_count)
        except ValueError:
            detected_faces = 1

    # Persist screenshot record to DB for proctoring audit trail
    screenshot = InterviewScreenshot(
        session_id=session_id,
        file_path=relative_path,
        captured_at=datetime.utcnow(),
        face_count=detected_faces,
    )
    db.add(screenshot)
    db.commit()

    return {"ok": True, "path": relative_path, "face_count": detected_faces}


# ── Helpers ────────────────────────────────────────────────────────────────────

_bg_logger = logging.getLogger("fit_score_bg")


async def _compute_fit_score_bg(candidate_id: int, job_id: int) -> None:
    """Background task: compute resume fit score using smart scoring (no AI cost)."""
    db = SessionLocal()
    try:
        from app.services.smart_scoring_service import compute_fit_score_for_candidate

        candidate = db.query(Candidate).filter(Candidate.id == candidate_id).first()
        if not candidate:
            _bg_logger.info(f"Fit score skip: candidate={candidate_id} not found")
            return

        result = compute_fit_score_for_candidate(db, candidate_id, job_id)
        if not result:
            _bg_logger.info(f"Fit score skip: no metadata for candidate={candidate_id}")
            return

        candidate.resume_fit_score = result.fit_level
        candidate.fit_score_reasoning = json.dumps(result.to_dict())
        db.commit()
        _bg_logger.info(f"Fit score computed: candidate={candidate_id}, level={result.fit_level} ({result.fit_percentage}%%)")
    except Exception as exc:
        _bg_logger.exception(f"Fit score failed for candidate={candidate_id}: {exc}")
        db.rollback()
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


# ══════════════════════════════════════════════════════════════════════════════
# CODE EXECUTION ENDPOINTS
# ══════════════════════════════════════════════════════════════════════════════

@router.post(
    "/sessions/{session_id}/run-code",
    summary="Execute code against test cases during interview",
    description="Run candidate's code in a sandboxed environment. Returns test case results.",
)
@limiter.limit("30/minute")
async def run_code(
    request: Request,
    session_id: int,
    db: Session = Depends(get_db),
):
    """
    Execute code during an interview session.

    Body:
    {
        "language": "python",
        "code": "print(input())",
        "test_cases": [{"input": "hello", "expected_output": "hello", "description": "Echo test"}]
    }
    """
    body = await request.json()

    session = db.query(InterviewSession).filter(InterviewSession.id == session_id).first()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    if session.status not in (SessionStatus.in_progress, SessionStatus.pending):
        raise HTTPException(status_code=400, detail="Interview is not active")

    language = body.get("language", "python")
    code = body.get("code", "")
    test_cases = body.get("test_cases", [])

    if not code.strip():
        raise HTTPException(status_code=400, detail="Code cannot be empty")

    from app.services.code_execution_service import run_test_cases, execute_code

    if test_cases:
        result = await run_test_cases(language, code, test_cases, timeout=10)
    else:
        # Just run the code without test cases (free run)
        exec_result = await execute_code(language, code, stdin=body.get("stdin", ""), timeout=10)
        result = {
            "total": 0,
            "passed": 0,
            "failed": 0,
            "results": [],
            "output": exec_result,
        }

    return result


@router.post(
    "/sessions/{session_id}/submit-code",
    summary="Submit final code solution for evaluation",
    description="Submit the candidate's final code. Saves to transcript and returns test results.",
)
@limiter.limit("10/minute")
async def submit_code(
    request: Request,
    session_id: int,
    db: Session = Depends(get_db),
):
    """
    Submit final code solution. Saves code + results to interview transcript.

    Body:
    {
        "language": "python",
        "code": "...",
        "test_cases": [...],
        "question_index": 3
    }
    """
    body = await request.json()

    session = db.query(InterviewSession).filter(InterviewSession.id == session_id).first()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    if session.status not in (SessionStatus.in_progress, SessionStatus.pending):
        raise HTTPException(status_code=400, detail="Interview is not active")

    language = body.get("language", "python")
    code = body.get("code", "")
    test_cases = body.get("test_cases", [])
    question_index = body.get("question_index", session.current_question_index)

    if not code.strip():
        raise HTTPException(status_code=400, detail="Code cannot be empty")

    from app.services.code_execution_service import run_test_cases

    # Run against test cases
    test_result = await run_test_cases(language, code, test_cases, timeout=10)

    # Save candidate's code submission as a message
    code_submission_text = (
        f"[CODE SUBMISSION]\n"
        f"Language: {language}\n"
        f"Test Results: {test_result['passed']}/{test_result['total']} passed\n"
        f"```{language}\n{code}\n```"
    )

    db.add(InterviewMessage(
        session_id=session.id,
        role="candidate",
        content=code_submission_text,
        question_index=question_index,
    ))
    db.commit()

    # Now get AI's response about the code
    from app.services.avatar_interview_service import conduct_turn

    # Send code context to AI for discussion
    ai_context = (
        f"The candidate submitted code for the coding challenge.\n"
        f"Language: {language}\n"
        f"Test Results: {test_result['passed']}/{test_result['total']} test cases passed\n"
        f"Code:\n```{language}\n{code}\n```\n"
        f"Please review their solution, discuss their approach, and then continue the interview."
    )

    ai_response = await conduct_turn(session, ai_context, db)

    return {
        "test_result": test_result,
        "ai_response": ai_response,
    }


@router.get(
    "/code/languages",
    summary="Get supported programming languages",
)
async def get_languages():
    """Return list of supported languages for the code editor."""
    from app.services.code_execution_service import get_supported_languages
    return {"languages": get_supported_languages()}


@router.get(
    "/code/starter/{language}",
    summary="Get starter code template for a language",
)
async def get_starter_code(language: str):
    """Return starter code template for a given language."""
    from app.services.code_execution_service import get_starter_code as _get_starter
    code = _get_starter(language)
    return {"language": language, "code": code}
