from datetime import datetime
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, EmailStr, field_validator


# ── Token / Job Config ────────────────────────────────────────────────────────

class InterviewTokenRead(BaseModel):
    token: str
    job_id: int
    is_active: bool
    url: str

    model_config = {"from_attributes": True}


class PreInterviewFieldConfig(BaseModel):
    field_id: str
    label: str
    type: str           # "text" | "email" | "phone" | "textarea" | "select" | "checkbox"
    is_default: bool = False


class JobConfig(BaseModel):
    job_id: int
    title: str
    department: str
    location: str
    experience_level: Optional[str]
    experience_min: int
    experience_max: int
    max_questions: int
    mode: str
    virtual_avatar: bool
    webcam_required: bool
    face_match: bool
    resume_based: bool
    special_instructions: Optional[str]
    skills: List[str]
    pre_interview_fields: List[PreInterviewFieldConfig] = []


# ── Registration ──────────────────────────────────────────────────────────────

class CandidateRegisterRequest(BaseModel):
    name: str
    email: EmailStr
    phone: Optional[str] = None
    resume_path: str                    # required — candidate cannot register without a resume
    extra_fields: Optional[Dict[str, Any]] = None  # dynamic pre-interview field responses


class CandidateRegisterResponse(BaseModel):
    session_id: int
    candidate_id: int
    is_existing_user: bool
    already_completed: bool = False
    can_resume: bool = False
    session_status: Optional[str] = None  # "pending" | "in_progress" when can_resume is True
    message: str


# ── Verify Access (invitation-based) ──────────────────────────────────────────

class VerifyAccessRequest(BaseModel):
    email: EmailStr


class VerifyAccessResponse(BaseModel):
    access: str  # "granted" | "apply" | "completed" | "terminated"
    candidate_name: Optional[str] = None
    session_id: Optional[int] = None
    candidate_id: Optional[int] = None
    can_resume: bool = False
    session_status: Optional[str] = None
    message: str


# ── Apply for Job (resume upload + basic details) ────────────────────────────

class ApplyForJobRequest(BaseModel):
    name: str
    email: EmailStr
    phone: Optional[str] = None
    resume_path: str  # path to uploaded resume file


class ApplyForJobResponse(BaseModel):
    success: bool
    message: str
    resume_metadata_id: Optional[int] = None


# ── Login (returning candidate / admin-added) ─────────────────────────────────

class CandidateLoginRequest(BaseModel):
    email: EmailStr


# ── Session Resume ────────────────────────────────────────────────────────────

class ResumeSessionResponse(BaseModel):
    session_id: int
    status: str
    current_question_index: int
    violation_count: int
    messages: List[Dict[str, Any]]


# ── Interview Turn ────────────────────────────────────────────────────────────

class InterviewMessageRequest(BaseModel):
    content: str                        # candidate's spoken/typed answer


class InterviewMessageResponse(BaseModel):
    session_id: int
    message: str                        # AI's next question or closing
    question_index: int
    is_complete: bool
    is_off_topic: bool = False
    role: str = "ai"


# ── Violation ─────────────────────────────────────────────────────────────────

class ViolationRequest(BaseModel):
    violation_type: str                 # "tab_switch" | "fullscreen_exit" | "face_not_detected" | "multiple_faces"
    details: Optional[str] = None


class ViolationResponse(BaseModel):
    violation_id: int
    session_id: int
    total_violations: int
    should_terminate: bool              # True when threshold reached


# ── Evaluation ────────────────────────────────────────────────────────────────

class EvaluationRead(BaseModel):
    id: int
    session_id: int
    candidate_id: int
    technical_score: Optional[float]
    communication_score: Optional[float]
    problem_solving_score: Optional[float]
    confidence_score: Optional[float]
    overall_score: Optional[float]
    recommendation: Optional[str]
    summary: Optional[str]
    strengths: Optional[List[str]]
    weaknesses: Optional[List[str]]
    detailed_feedback: Optional[str]
    meets_threshold: bool
    created_at: Optional[datetime]

    model_config = {"from_attributes": True}


# ── Feedback ──────────────────────────────────────────────────────────────────

class FeedbackRequest(BaseModel):
    rating: int
    experience_comment: Optional[str] = None
    technical_issues: bool = False
    would_recommend: bool = True

    @field_validator("rating")
    @classmethod
    def validate_rating(cls, v: int) -> int:
        if not 1 <= v <= 5:
            raise ValueError("Rating must be between 1 and 5")
        return v


class FeedbackResponse(BaseModel):
    feedback_id: int
    message: str


# ── Meeting Slots ─────────────────────────────────────────────────────────────

class MeetingSlotRead(BaseModel):
    id: int
    start_time: datetime
    end_time: datetime
    is_booked: bool

    model_config = {"from_attributes": True}


class MeetingSlotCreate(BaseModel):
    start_time: datetime
    end_time: datetime


class ScheduleMeetingRequest(BaseModel):
    slot_id: int


class ScheduleMeetingResponse(BaseModel):
    schedule_id: int
    slot_id: int
    start_time: datetime
    end_time: datetime
    message: str


# ── Admin: Interview URL ──────────────────────────────────────────────────────

class InterviewUrlResponse(BaseModel):
    token: str
    url: str
    job_id: int
    is_active: bool
    created_at: Optional[datetime]


# ── Admin: Session List ───────────────────────────────────────────────────────

class InterviewSessionSummary(BaseModel):
    id: int
    status: str
    candidate_id: Optional[int]
    candidate_name: Optional[str]
    candidate_email: Optional[str]
    started_at: Optional[datetime]
    completed_at: Optional[datetime]
    violation_count: int
    overall_score: Optional[float]
    recommendation: Optional[str]
    created_at: Optional[datetime]

    model_config = {"from_attributes": True}


# ── Recording Upload ───────────────────────────────────────────────────────────

class RecordingUploadResponse(BaseModel):
    recording_link: str
    video_duration: str
    message: str
