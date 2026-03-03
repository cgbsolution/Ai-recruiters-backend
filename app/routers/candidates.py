import json
import os
import shutil
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.candidate import Candidate
from app.schemas.candidate import (
    CancelInterviewRequest,
    CandidateCreate,
    CandidateRead,
    CandidateUpdate,
    PromoteStageRequest,
    ScheduleInterviewRequest,
    SendWhatsAppRequest,
)
from app.services import candidate_service

router = APIRouter()

IS_SERVERLESS = bool(os.environ.get("VERCEL") or os.environ.get("AWS_LAMBDA_FUNCTION_NAME"))
UPLOAD_ROOT = "/tmp/uploads" if IS_SERVERLESS else "uploads"
RESUME_UPLOAD_DIR = f"{UPLOAD_ROOT}/resumes"
os.makedirs(RESUME_UPLOAD_DIR, exist_ok=True)

ALLOWED_RESUME_TYPES = {
    "application/pdf",
    "application/msword",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}


# ── Standard CRUD ─────────────────────────────────────────────────────────────

@router.get(
    "/",
    response_model=List[CandidateRead],
    summary="List candidates",
    description=(
        "Return candidates with optional filters.\n\n"
        "- `status`: Applied | Interview | Offered | Rejected\n"
        "- `job_id`: filter by opening\n"
        "- `stage`: Candidates | AI Round | Panel Round | Offer-Made | Rejected"
    ),
)
def list_candidates(
    status: Optional[str] = None,
    job_id: Optional[int] = None,
    stage: Optional[str] = None,
    db: Session = Depends(get_db),
):
    return candidate_service.get_candidates(db, status=status, job_id=job_id, stage=stage)


@router.post(
    "/",
    response_model=CandidateRead,
    status_code=status.HTTP_201_CREATED,
    summary="Create candidate manually",
)
def create_candidate(payload: CandidateCreate, db: Session = Depends(get_db)):
    return candidate_service.create_candidate(db, payload)


@router.get(
    "/{candidate_id}",
    response_model=CandidateRead,
    summary="Get candidate by ID",
)
def get_candidate(candidate_id: int, db: Session = Depends(get_db)):
    candidate = candidate_service.get_candidate_by_id(db, candidate_id)
    if not candidate:
        raise HTTPException(status_code=404, detail="Candidate not found")
    return candidate


@router.put("/{candidate_id}", response_model=CandidateRead, summary="Update candidate")
def update_candidate(
    candidate_id: int, payload: CandidateUpdate, db: Session = Depends(get_db)
):
    candidate = candidate_service.update_candidate(db, candidate_id, payload)
    if not candidate:
        raise HTTPException(status_code=404, detail="Candidate not found")
    return candidate


@router.delete("/{candidate_id}", status_code=status.HTTP_204_NO_CONTENT, summary="Delete candidate")
def delete_candidate(candidate_id: int, db: Session = Depends(get_db)):
    ok = candidate_service.delete_candidate(db, candidate_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Candidate not found")


# ── Resume Upload & Auto-Parse ────────────────────────────────────────────────

@router.post(
    "/upload-resume",
    response_model=List[CandidateRead],
    status_code=status.HTTP_201_CREATED,
    summary="Upload resumes (batch)",
    description=(
        "Upload one or more PDF/DOC/DOCX resume files. For each file:\n\n"
        "1. Text is extracted (pdfplumber / python-docx)\n"
        "2. Claude LLM parses structured data (name, email, phone, skills, experience)\n"
        "3. If `job_id` is provided, Claude calculates a fit score (low / medium / high) "
        "with breakdown reasoning\n"
        "4. A `Candidate` record is created and returned\n\n"
        "**Content-Type:** `multipart/form-data`"
    ),
)
async def upload_resumes(
    job_id: Optional[int] = Form(None),
    files: List[UploadFile] = File(...),
    db: Session = Depends(get_db),
):
    """
    Upload one or more resumes (PDF/DOC/DOCX).
    For each file: extract text → LLM parse → compute fit score → create Candidate record.
    """
    from app.services.resume_service import parse_resume_file, calculate_fit_score
    from app.schemas.ai import FitScoreRequest
    from app.models.job import Job, JobSkill, JobResponsibility

    created_candidates = []

    for file in files:
        if file.content_type not in ALLOWED_RESUME_TYPES:
            raise HTTPException(
                status_code=400,
                detail=f"Unsupported file type for {file.filename}. Allowed: PDF, DOC, DOCX",
            )

        safe_name = f"{datetime.utcnow().strftime('%Y%m%d%H%M%S%f')}_{file.filename}"
        file_path = os.path.join(RESUME_UPLOAD_DIR, safe_name)
        with open(file_path, "wb") as f:
            shutil.copyfileobj(file.file, f)

        try:
            result = parse_resume_file(file_path, job_id=job_id)
            parsed = result["parsed"]
            resume_text = result["text"]

            fit_level = None
            fit_reasoning_json = None
            fit_details_json = None

            if job_id and resume_text:
                job = db.query(Job).filter(Job.id == job_id).first()
                if job:
                    skills = [s.skill for s in db.query(JobSkill).filter(JobSkill.job_id == job_id).all() if s.skill]
                    responsibilities = [r.description for r in db.query(JobResponsibility).filter(JobResponsibility.job_id == job_id).all()]
                    fit_req = FitScoreRequest(
                        resume_text=resume_text,
                        job_title=job.title,
                        required_experience_min=job.experience_min,
                        required_experience_max=job.experience_max,
                        required_skills=skills,
                        responsibilities=responsibilities,
                    )
                    fit_result = calculate_fit_score(fit_req)
                    fit_level = fit_result.fit_level
                    fit_reasoning_json = json.dumps({
                        "summary": fit_result.summary,
                        "breakdown": fit_result.breakdown.model_dump(),
                        "percentage": fit_result.fit_percentage,
                        "key_matching_skills": fit_result.key_matching_skills,
                        "relevant_experience_summary": fit_result.relevant_experience_summary,
                        "missing_weak_areas": fit_result.missing_weak_areas,
                    })
                    fit_details_json = json.dumps(fit_result.reasoning_points)

            parsed_email = parsed.get("email") or f"unknown_{safe_name[:20]}@resume.local"
            parsed_name = parsed.get("name") or Path(file.filename).stem

            # Prevent duplicates: if same email + job already exists, update it
            existing_candidate = None
            if job_id and parsed.get("email"):
                existing_candidate = db.query(Candidate).filter(
                    Candidate.email == parsed_email,
                    Candidate.job_id == job_id,
                ).first()

            if existing_candidate:
                existing_candidate.name = parsed_name
                existing_candidate.phone = parsed.get("phone") or existing_candidate.phone
                existing_candidate.location = parsed.get("location") or existing_candidate.location
                existing_candidate.role = parsed.get("current_role") or existing_candidate.role
                existing_candidate.resume_path = file_path
                existing_candidate.resume_fit_score = fit_level
                existing_candidate.fit_score_reasoning = fit_reasoning_json
                existing_candidate.fit_score_details = fit_details_json
                db.commit()
                db.refresh(existing_candidate)
                created_candidates.append(existing_candidate)
            else:
                candidate = Candidate(
                    name=parsed_name,
                    email=parsed_email,
                    phone=parsed.get("phone") or "",
                    location=parsed.get("location") or "",
                    role=parsed.get("current_role") or "",
                    resume_path=file_path,
                    job_id=job_id,
                    resume_fit_score=fit_level,
                    fit_score_reasoning=fit_reasoning_json,
                    fit_score_details=fit_details_json,
                    interview_status="New",
                    candidate_stage="Candidates",
                )
                db.add(candidate)
                db.commit()
                db.refresh(candidate)
                created_candidates.append(candidate)

        except Exception as exc:
            if os.path.exists(file_path):
                os.remove(file_path)
            raise HTTPException(status_code=500, detail=f"Failed to process {file.filename}: {str(exc)}")

    return created_candidates


# ── Fit Score Recompute ───────────────────────────────────────────────────────

@router.post(
    "/{candidate_id}/compute-fit-score",
    response_model=CandidateRead,
    summary="Recompute resume fit score",
    description="Re-runs Claude LLM fit-score calculation for an existing candidate against their linked job. Requires resume file on disk and a linked `job_id`.",
)
def compute_fit_score(candidate_id: int, db: Session = Depends(get_db)):
    """Re-compute the LLM fit score for an existing candidate against their linked job."""
    from app.services.resume_service import extract_text, calculate_fit_score
    from app.schemas.ai import FitScoreRequest
    from app.models.job import Job, JobSkill, JobResponsibility

    candidate = db.query(Candidate).filter(Candidate.id == candidate_id).first()
    if not candidate:
        raise HTTPException(status_code=404, detail="Candidate not found")
    if not candidate.resume_path or not os.path.exists(candidate.resume_path):
        raise HTTPException(status_code=400, detail="No resume file found for this candidate")
    if not candidate.job_id:
        raise HTTPException(status_code=400, detail="Candidate is not linked to a job")

    job = db.query(Job).filter(Job.id == candidate.job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Linked job not found")

    resume_text = extract_text(candidate.resume_path)
    skills = [s.skill for s in db.query(JobSkill).filter(JobSkill.job_id == job.id).all() if s.skill]
    responsibilities = [r.description for r in db.query(JobResponsibility).filter(JobResponsibility.job_id == job.id).all()]

    fit_req = FitScoreRequest(
        resume_text=resume_text,
        job_title=job.title,
        required_experience_min=job.experience_min,
        required_experience_max=job.experience_max,
        required_skills=skills,
        responsibilities=responsibilities,
    )
    fit_result = calculate_fit_score(fit_req)

    candidate.resume_fit_score = fit_result.fit_level
    candidate.fit_score_reasoning = json.dumps({
        "summary": fit_result.summary,
        "breakdown": fit_result.breakdown.model_dump(),
        "percentage": fit_result.fit_percentage,
        "key_matching_skills": fit_result.key_matching_skills,
        "relevant_experience_summary": fit_result.relevant_experience_summary,
        "missing_weak_areas": fit_result.missing_weak_areas,
    })
    candidate.fit_score_details = json.dumps(fit_result.reasoning_points)
    db.commit()
    db.refresh(candidate)
    return candidate


# ── Interview Scheduling ──────────────────────────────────────────────────────

@router.post(
    "/{candidate_id}/schedule-interview",
    response_model=CandidateRead,
    summary="Schedule / reschedule interview",
    description=(
        "Set the interview date/time for a candidate and trigger notifications.\n\n"
        "- Saves `interview_date`, `interview_timezone`, `joining_flexibility`, `allow_candidate_reschedule`\n"
        "- Sets `interview_status` to **Scheduled**\n"
        "- Sends HTML email via SMTP (requires `SMTP_*` config in `.env`)\n"
        "- Optionally sends WhatsApp message via Meta Cloud API (`send_whatsapp: true`)\n"
        "- Saves custom email template overrides in `schedule_email_template` (JSON)"
    ),
)
def schedule_interview(
    candidate_id: int,
    payload: ScheduleInterviewRequest,
    db: Session = Depends(get_db),
):
    """Schedule (or re-schedule) an interview for a candidate and send notifications."""
    from app.services.interview_service import send_schedule_email, send_whatsapp_message
    from app.models.interview import InterviewStatusLog

    candidate = db.query(Candidate).filter(Candidate.id == candidate_id).first()
    if not candidate:
        raise HTTPException(status_code=404, detail="Candidate not found")

    old_status = candidate.interview_status
    is_reschedule = old_status == "Scheduled"

    # Log status change
    log = InterviewStatusLog(
        candidate_id=candidate.id,
        previous_status=old_status,
        new_status="Scheduled",
        changed_by="admin",
        reason="Rescheduled" if is_reschedule else "Scheduled",
    )
    db.add(log)

    candidate.interview_date = payload.interview_date
    candidate.interview_timezone = payload.interview_timezone
    candidate.joining_flexibility = payload.joining_flexibility
    candidate.allow_candidate_reschedule = payload.allow_candidate_reschedule
    candidate.interview_status = "Scheduled"

    template_data = {
        "subject": payload.email_subject,
        "intro_text": payload.email_intro_text,
        "extra_heading": payload.email_extra_heading,
        "extra_body": payload.email_extra_body,
        "cc_emails": payload.cc_emails or [],
    }
    candidate.schedule_email_template = json.dumps(template_data)
    db.commit()
    db.refresh(candidate)

    try:
        send_schedule_email(candidate, db, is_reschedule=is_reschedule)
    except Exception:
        pass

    if payload.send_whatsapp and candidate.phone:
        try:
            from app.models.job import Job
            job = db.query(Job).filter(Job.id == candidate.job_id).first()
            job_title = job.title if job else "the role"
            msg = _build_whatsapp_message(candidate, job_title, is_reschedule=is_reschedule)
            send_whatsapp_message(candidate.phone, msg)
        except Exception:
            pass

    return candidate


# ── Cancel Interview ─────────────────────────────────────────────────────────

@router.post(
    "/{candidate_id}/cancel-interview",
    response_model=CandidateRead,
    summary="Cancel a scheduled interview",
    description=(
        "Cancels a scheduled interview for a candidate.\n\n"
        "- Sets `interview_status` to **Cancelled**\n"
        "- Clears `interview_date`\n"
        "- Logs the status change to `interview_status_logs`\n"
        "- Sends cancellation email + optional WhatsApp\n\n"
        "Only works when current status is 'Scheduled'."
    ),
)
def cancel_interview(
    candidate_id: int,
    payload: CancelInterviewRequest,
    db: Session = Depends(get_db),
):
    """Cancel a scheduled interview and notify the candidate."""
    from app.services.interview_service import send_cancel_email, send_whatsapp_message
    from app.models.interview import InterviewStatusLog

    candidate = db.query(Candidate).filter(Candidate.id == candidate_id).first()
    if not candidate:
        raise HTTPException(status_code=404, detail="Candidate not found")

    if candidate.interview_status != "Scheduled":
        raise HTTPException(
            status_code=400,
            detail=f"Cannot cancel: current status is '{candidate.interview_status}', expected 'Scheduled'",
        )

    # Log status change
    log = InterviewStatusLog(
        candidate_id=candidate.id,
        previous_status=candidate.interview_status,
        new_status="Cancelled",
        changed_by="admin",
        reason=payload.reason or "Cancelled by admin",
    )
    db.add(log)

    candidate.interview_status = "Cancelled"
    candidate.interview_date = None
    db.commit()
    db.refresh(candidate)

    # Send cancellation notifications
    try:
        send_cancel_email(candidate, db)
    except Exception:
        pass

    if payload.send_whatsapp and candidate.phone:
        try:
            from app.models.job import Job
            job = db.query(Job).filter(Job.id == candidate.job_id).first()
            job_title = job.title if job else "the role"
            msg = (
                f"Hi {candidate.name},\n"
                f"Your interview for the role of {job_title} at CGBiz Solutions Private Limited "
                f"has been cancelled. Please contact HR for further details."
            )
            send_whatsapp_message(candidate.phone, msg)
        except Exception:
            pass

    return candidate


# ── Stage Promotion ───────────────────────────────────────────────────────────

@router.post(
    "/{candidate_id}/promote",
    response_model=CandidateRead,
    summary="Promote candidate to a hiring stage",
    description="Move candidate between stages: `Candidates` → `AI Round` → `Panel Round` → `Offer-Made` / `Rejected`. Also updates `status` to Offered/Rejected when appropriate.",
)
def promote_candidate(
    candidate_id: int,
    payload: PromoteStageRequest,
    db: Session = Depends(get_db),
):
    """Move candidate to a new hiring stage."""
    valid_stages = {"AI Round", "Panel Round", "Offer-Made", "Rejected", "Candidates"}
    if payload.stage not in valid_stages:
        raise HTTPException(status_code=400, detail=f"Invalid stage. Must be one of: {valid_stages}")

    candidate = db.query(Candidate).filter(Candidate.id == candidate_id).first()
    if not candidate:
        raise HTTPException(status_code=404, detail="Candidate not found")

    candidate.candidate_stage = payload.stage
    if payload.stage == "Offer-Made":
        candidate.status = "Offered"
    elif payload.stage == "Rejected":
        candidate.status = "Rejected"

    db.commit()
    db.refresh(candidate)
    return candidate


# ── Send WhatsApp ─────────────────────────────────────────────────────────────

@router.post(
    "/{candidate_id}/send-whatsapp",
    summary="Send WhatsApp message",
    description="Send a custom free-form text message to the candidate via Meta WhatsApp Business Cloud API. Requires `WHATSAPP_TOKEN` and `WHATSAPP_PHONE_NUMBER_ID` in `.env`.",
)
def send_whatsapp(
    candidate_id: int,
    payload: SendWhatsAppRequest,
    db: Session = Depends(get_db),
):
    """Send a custom WhatsApp message to a candidate."""
    from app.services.interview_service import send_whatsapp_message

    candidate = db.query(Candidate).filter(Candidate.id == candidate_id).first()
    if not candidate:
        raise HTTPException(status_code=404, detail="Candidate not found")

    phone = payload.phone or candidate.phone
    if not phone:
        raise HTTPException(status_code=400, detail="No phone number available")

    try:
        result = send_whatsapp_message(phone, payload.message)
        return {"success": True, "detail": result}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"WhatsApp send failed: {str(exc)}")


# ── Evaluation Report ─────────────────────────────────────────────────────────

@router.get(
    "/{candidate_id}/evaluation",
    summary="Get full candidate evaluation report",
    description=(
        "Returns a consolidated report for the `/evaluation/[candidateId]` page.\n\n"
        "Includes:\n"
        "- Candidate profile and stage\n"
        "- Resume file URL\n"
        "- Fit score level + reasoning breakdown (from Claude LLM)\n"
        "- Post-interview scores (grammar, confidence, clarity, trust, pronunciation, enthusiasm, conciseness, ai_cheating)\n"
        "- Recording link and duration\n\n"
        "Score fields are `null` until the interview is completed."
    ),
)
def get_evaluation(candidate_id: int, db: Session = Depends(get_db)):
    """Return full evaluation data: resume analysis + scores + interview Q&A transcript."""
    from app.models.interview import InterviewSession, InterviewMessage, SessionStatus, Evaluation

    candidate = db.query(Candidate).filter(Candidate.id == candidate_id).first()
    if not candidate:
        raise HTTPException(status_code=404, detail="Candidate not found")

    fit_reasoning = None
    fit_details = None
    if candidate.fit_score_reasoning:
        try:
            fit_reasoning = json.loads(candidate.fit_score_reasoning)
        except Exception:
            fit_reasoning = candidate.fit_score_reasoning
    if candidate.fit_score_details:
        try:
            fit_details = json.loads(candidate.fit_score_details)
        except Exception:
            fit_details = candidate.fit_score_details

    resume_url = None
    if candidate.resume_path and os.path.exists(candidate.resume_path):
        resume_url = f"/{candidate.resume_path.replace(os.sep, '/')}"

    # ── Q&A transcript from latest completed session ──────────────────────────
    transcript = []
    evaluation_detail = None

    latest_session = (
        db.query(InterviewSession)
        .filter(
            InterviewSession.candidate_id == candidate_id,
            InterviewSession.status == SessionStatus.completed,
        )
        .order_by(InterviewSession.completed_at.desc())
        .first()
    )

    if latest_session:
        messages = (
            db.query(InterviewMessage)
            .filter(InterviewMessage.session_id == latest_session.id)
            .order_by(InterviewMessage.created_at)
            .all()
        )
        # Group by question_index into Q&A pairs
        qa_map: dict = {}
        for msg in messages:
            qi = msg.question_index or 0
            if qi == 0:
                continue  # skip opening/closing system turns without index
            if qi not in qa_map:
                qa_map[qi] = {"question": None, "answer": None, "index": qi}
            if msg.role == "ai" and qa_map[qi]["question"] is None:
                qa_map[qi]["question"] = msg.content
            elif msg.role == "candidate" and qa_map[qi]["answer"] is None:
                qa_map[qi]["answer"] = msg.content
        transcript = [
            v for _, v in sorted(qa_map.items())
            if v["question"] and v["answer"]
        ]

        # ── Latest AI evaluation for this session ─────────────────────────────
        ev = (
            db.query(Evaluation)
            .filter(Evaluation.session_id == latest_session.id)
            .first()
        )
        if ev:
            try:
                strengths = json.loads(ev.strengths) if ev.strengths else []
            except Exception:
                strengths = []
            try:
                weaknesses = json.loads(ev.weaknesses) if ev.weaknesses else []
            except Exception:
                weaknesses = []
            evaluation_detail = {
                "recommendation": ev.recommendation.value if ev.recommendation else None,
                "summary": ev.summary,
                "strengths": strengths,
                "weaknesses": weaknesses,
                "detailed_feedback": ev.detailed_feedback,
                "meets_threshold": ev.meets_threshold or False,
            }

    return {
        "candidate": {
            "id": candidate.id,
            "name": candidate.name,
            "email": candidate.email,
            "phone": candidate.phone,
            "location": candidate.location,
            "role": candidate.role,
            "candidate_stage": candidate.candidate_stage,
            "interview_status": candidate.interview_status,
            "interview_date": candidate.interview_date.isoformat() if candidate.interview_date else None,
            "interview_timezone": candidate.interview_timezone,
        },
        "resume": {
            "path": candidate.resume_path,
            "url": resume_url,
        },
        "fit_score": {
            "level": candidate.resume_fit_score,
            "reasoning": fit_reasoning,
            "details": fit_details,
        },
        "scores": {
            "overall": candidate.score,
            "trust": candidate.trust_score,
            "grammar": candidate.grammar_score,
            "confidence": candidate.confidence_score,
            "clarity": candidate.clarity_score,
            "pronunciation": candidate.pronunciation_score,
            "enthusiasm": candidate.enthusiasm_score,
            "conciseness": candidate.conciseness_score,
            "ai_cheating": candidate.ai_cheating_score,
        },
        "recording": {
            "link": candidate.recording_link,
            "duration": candidate.video_duration,
        },
        "transcript": transcript,
        "evaluation": evaluation_detail,
    }


# ── Helpers ───────────────────────────────────────────────────────────────────

def _build_whatsapp_message(candidate: Candidate, job_title: str, is_reschedule: bool = False) -> str:
    action = "rescheduled" if is_reschedule else "scheduled"
    date_str = ""
    if candidate.interview_date:
        date_str = candidate.interview_date.strftime("%B %d, %Y, %I:%M %p")
        if candidate.interview_timezone:
            date_str += f" {candidate.interview_timezone}"
    link = candidate.interview_link or ""
    return (
        f"Hi {candidate.name},\n"
        f"Your Interview for the role of {job_title} by CGBiz Solutions Private limited "
        f"has been {action} on {date_str}. "
        f"Please ensure you're ready.\n\n"
        f"Interview Link: {link}"
    )
