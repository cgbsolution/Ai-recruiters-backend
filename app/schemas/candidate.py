from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, computed_field, field_serializer

from app.media import sign_media_url
from app.refs import JobId, encode_ref


class CandidateCreate(BaseModel):
    name: str
    # These default to None, not "" / "Applied": re-adding an existing email to
    # the same job upserts the row with model_dump(exclude_none=True), so
    # non-None defaults silently wiped the candidate's phone, location and role
    # and reset their status.
    role: Optional[str] = None
    email: str
    phone: Optional[str] = None
    location: Optional[str] = None
    status: Optional[str] = None
    job_id: Optional[JobId] = None
    # `image` and `resume_path` are server-managed (photo capture / resume
    # upload) and no longer accepted from clients: they are file paths that
    # reach file readers and deletion. Unknown keys are ignored.


class CandidateUpdate(BaseModel):
    name: Optional[str] = None
    role: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    location: Optional[str] = None
    status: Optional[str] = None
    job_id: Optional[JobId] = None
    # Stage
    candidate_stage: Optional[str] = None
    # Interview scheduling
    interview_status: Optional[str] = None
    interview_date: Optional[datetime] = None
    interview_timezone: Optional[str] = None
    allow_candidate_reschedule: Optional[bool] = None
    allow_joining_early: Optional[bool] = None
    joining_flexibility: Optional[str] = None
    mail_contact_history: Optional[bool] = None
    # Server-managed and no longer client-writable (unknown keys are ignored):
    #   image, resume_path, recording_link, video_duration — file paths that
    #     reach file readers and os.remove;
    #   interview_link, schedule_email_template — built by schedule-interview
    #     (the personal invite link and the email template);
    #   resume_fit_score, fit_score_* and the post-interview scores — computed
    #     by the scorers / evaluation.


class HireCandidateRequest(BaseModel):
    # Hire into this job (added if the candidate isn't linked to it yet). It must
    # belong to the caller's organization.
    job_id: Optional[JobId] = None


class LinkExpiry(BaseModel):
    value: int = 7
    unit: str = "days"  # "hours" | "days"


class ScheduleInterviewRequest(BaseModel):
    interview_date: datetime
    interview_timezone: str = "Asia/Calcutta"
    joining_flexibility: str = "anytime"  # "scheduled_time" | "anytime"
    allow_candidate_reschedule: bool = False
    allow_joining_early: bool = False
    # Recruiter's app URL for the job link. Only its origin is used (and only if it
    # is an allowed origin): the stored link is the candidate's personal invite.
    interview_url: Optional[str] = None
    link_expiry: Optional[LinkExpiry] = None
    email_subject: Optional[str] = None
    email_intro_text: Optional[str] = None
    email_extra_heading: Optional[str] = None
    email_extra_body: Optional[str] = None
    cc_emails: Optional[List[str]] = None
    send_whatsapp: bool = False
    create_google_meet: bool = False


class CandidateRead(BaseModel):
    id: int
    name: str
    role: Optional[str]
    email: str
    phone: Optional[str]
    location: Optional[str]
    status: str
    image: Optional[str]
    applied_date: Optional[datetime] = None
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
    # Added by
    added_by: Optional[str] = None
    # Resume metadata
    resume_metadata_id: Optional[int] = None
    parsing_status: Optional[str] = None
    parsing_error: Optional[str] = None

    model_config = {"from_attributes": True}

    # ── Derived, response-only fields ────────────────────────────────────────
    # CandidateRead is only returned by endpoints that already authorized the
    # caller for this candidate, so signing its media here is safe. The raw
    # image / resume_path / recording_link fields are kept unchanged.
    @computed_field
    @property
    def ref(self) -> str:
        """Opaque id for page URLs (app.refs)."""
        return encode_ref("candidate", self.id)

    @computed_field
    @property
    def job_ref(self) -> Optional[str]:
        return encode_ref("job", self.job_id)

    @computed_field
    @property
    def image_url(self) -> Optional[str]:
        """Signed, expiring API-relative URL for the photo (app.media)."""
        return sign_media_url(self.image)

    @computed_field
    @property
    def resume_url(self) -> Optional[str]:
        return sign_media_url(self.resume_path)

    @computed_field
    @property
    def recording_url(self) -> Optional[str]:
        return sign_media_url(self.recording_link)

    @field_serializer("interview_date")
    def _serialize_interview_date(self, v: Optional[datetime]) -> Optional[str]:
        """Naive datetime from DB is stored as UTC — append Z so clients parse it correctly."""
        if v is None:
            return None
        return v.isoformat() + "Z"


class PromoteStageRequest(BaseModel):
    stage: str  # "AI Round" | "Panel Round" | "Offer-Made" | "Rejected"


class SendWhatsAppRequest(BaseModel):
    phone: str
    message: str


class CancelInterviewRequest(BaseModel):
    reason: Optional[str] = None
    send_whatsapp: bool = False
