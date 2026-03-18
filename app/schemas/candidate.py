from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel


class CandidateCreate(BaseModel):
    name: str
    role: Optional[str] = ""
    email: str
    phone: Optional[str] = ""
    location: Optional[str] = ""
    status: str = "Applied"
    image: Optional[str] = ""
    job_id: Optional[int] = None
    resume_path: Optional[str] = None


class CandidateUpdate(BaseModel):
    name: Optional[str] = None
    role: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    location: Optional[str] = None
    status: Optional[str] = None
    image: Optional[str] = None
    job_id: Optional[int] = None
    resume_path: Optional[str] = None
    # Stage
    candidate_stage: Optional[str] = None
    # Interview scheduling
    interview_status: Optional[str] = None
    interview_date: Optional[datetime] = None
    interview_timezone: Optional[str] = None
    interview_link: Optional[str] = None
    allow_candidate_reschedule: Optional[bool] = None
    allow_joining_early: Optional[bool] = None
    joining_flexibility: Optional[str] = None
    schedule_email_template: Optional[str] = None
    # Fit score
    resume_fit_score: Optional[str] = None
    fit_score_reasoning: Optional[str] = None
    fit_score_details: Optional[str] = None
    # Post-interview scores
    score: Optional[float] = None
    trust_score: Optional[float] = None
    grammar_score: Optional[float] = None
    confidence_score: Optional[float] = None
    clarity_score: Optional[float] = None
    pronunciation_score: Optional[float] = None
    enthusiasm_score: Optional[float] = None
    conciseness_score: Optional[float] = None
    ai_cheating_score: Optional[float] = None
    recording_link: Optional[str] = None
    video_duration: Optional[str] = None
    mail_contact_history: Optional[bool] = None


class LinkExpiry(BaseModel):
    value: int = 7
    unit: str = "days"  # "hours" | "days"


class ScheduleInterviewRequest(BaseModel):
    interview_date: datetime
    interview_timezone: str = "Asia/Calcutta"
    joining_flexibility: str = "anytime"  # "scheduled_time" | "anytime"
    allow_candidate_reschedule: bool = False
    allow_joining_early: bool = False
    interview_url: Optional[str] = None  # frontend-provided interview link
    link_expiry: Optional[LinkExpiry] = None
    email_subject: Optional[str] = None
    email_intro_text: Optional[str] = None
    email_extra_heading: Optional[str] = None
    email_extra_body: Optional[str] = None
    cc_emails: Optional[List[str]] = None
    send_whatsapp: bool = False


class CandidateRead(BaseModel):
    id: int
    name: str
    role: Optional[str]
    email: str
    phone: Optional[str]
    location: Optional[str]
    status: str
    image: Optional[str]
    applied_date: datetime
    job_id: Optional[int]
    job_title: Optional[str] = None
    resume_path: Optional[str] = None
    # Fit score
    resume_fit_score: Optional[str] = None
    fit_score_reasoning: Optional[str] = None
    fit_score_details: Optional[str] = None
    # Stage
    candidate_stage: Optional[str] = None
    # Interview
    interview_status: Optional[str] = None
    interview_date: Optional[datetime] = None
    interview_timezone: Optional[str] = None
    interview_link: Optional[str] = None
    allow_candidate_reschedule: Optional[bool] = None
    allow_joining_early: Optional[bool] = None
    joining_flexibility: Optional[str] = None
    # Post-interview scores
    score: Optional[float] = None
    trust_score: Optional[float] = None
    grammar_score: Optional[float] = None
    confidence_score: Optional[float] = None
    clarity_score: Optional[float] = None
    pronunciation_score: Optional[float] = None
    enthusiasm_score: Optional[float] = None
    conciseness_score: Optional[float] = None
    ai_cheating_score: Optional[float] = None
    # Recording
    recording_link: Optional[str] = None
    video_duration: Optional[str] = None
    # Contact
    mail_contact_history: Optional[bool] = None
    # Resume metadata
    resume_metadata_id: Optional[int] = None
    parsing_status: Optional[str] = None

    model_config = {"from_attributes": True}


class PromoteStageRequest(BaseModel):
    stage: str  # "AI Round" | "Panel Round" | "Offer-Made" | "Rejected"


class SendWhatsAppRequest(BaseModel):
    phone: str
    message: str


class CancelInterviewRequest(BaseModel):
    reason: Optional[str] = None
    send_whatsapp: bool = False
