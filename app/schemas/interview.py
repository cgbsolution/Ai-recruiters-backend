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


class JobConfig(BaseModel):
    job_id: int
    title: str
    department: str
    location: str
    experience_level: Optional[str]
    experience_min: int
    experience_max: int
    max_questions: int                  # verbal-only count
    max_coding_questions: int = 0       # coding turns appended after verbal
    mode: str
    virtual_avatar: bool
    webcam_required: bool
    face_match: bool
    resume_based: bool
    code_editor_required: bool
    allow_mobile: bool
    secondary_cam: bool
    manual_interview: bool
    special_instructions: Optional[str]
    auto_skip_timeout: int
    skills: List[str]
    organization_name: Optional[str] = None
    manager_name: Optional[str] = None
    manager_email: Optional[str] = None


# ── Registration ──────────────────────────────────────────────────────────────

class CandidateRegisterRequest(BaseModel):
    name: str
    email: EmailStr
    phone: Optional[str] = None
    resume_path: str                    # required — candidate cannot register without a resume


class CandidateRegisterResponse(BaseModel):
    session_id: int
    candidate_id: int
    is_existing_user: bool
    already_completed: bool = False
    can_resume: bool = False
    session_status: Optional[str] = None  # "pending" | "in_progress" when can_resume is True
    message: str
    # Per-session bearer credential. The client must echo it back as the
    # X-Session-Token header (or ?st= query for sendBeacon) on every
    # /interview/sessions/{id}/* call — closes the sequential-id IDOR hole.
    session_token: Optional[str] = None


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
    # Scheduling settings for frontend enforcement
    interview_date: Optional[datetime] = None
    interview_timezone: Optional[str] = None
    joining_flexibility: Optional[str] = None  # "scheduled_time" | "anytime"
    allow_joining_early: Optional[bool] = None
    # Per-session bearer credential (see CandidateRegisterResponse.session_token)
    session_token: Optional[str] = None


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
    # Client-generated idempotency key. The client retries /message after a
    # network timeout while the original request may still complete server-side;
    # replaying the same turn_id returns the stored response instead of running
    # a duplicate AI turn (double question advance, duplicated answer).
    turn_id: Optional[str] = None


class InterviewMessageResponse(BaseModel):
    session_id: int
    message: str                        # AI's next question or closing
    question_index: int
    is_complete: bool
    is_off_topic: bool = False
    role: str = "ai"
    # When the AI (or server-side backstop) attaches a coding challenge, this
    # object carries the problem + test cases to the candidate UI. The
    # frontend's InterviewRoom reads `coding_question` and surfaces the
    # "Switch to Coding Test" button when it's present.
    coding_question: Optional[dict] = None


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
    coding_score: Optional[float] = None
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
