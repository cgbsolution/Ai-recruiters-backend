import enum
from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, Enum, Float, ForeignKey, Integer, String, Text, UniqueConstraint

from app.database import Base


class CandidateStatus(str, enum.Enum):
    applied = "Applied"
    interview = "Interview"
    offered = "Offered"
    rejected = "Rejected"


class Candidate(Base):
    __tablename__ = "candidates"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(255), nullable=False)
    role = Column(String(255), nullable=True)
    email = Column(String(255), nullable=False, index=True)
    phone = Column(String(50), nullable=True)
    location = Column(String(255), nullable=True)
    status = Column(Enum(CandidateStatus), default=CandidateStatus.applied, index=True)
    image = Column(String(500), nullable=True)
    resume_path = Column(String(500), nullable=True)
    job_id = Column(Integer, ForeignKey("jobs.id"), nullable=True, index=True)
    applied_date = Column(DateTime, default=datetime.utcnow)
    pre_interview_responses = Column(Text, nullable=True)

    # ── Resume Fit Score ─────────────────────────────────────────────────────
    resume_fit_score = Column(String(20), nullable=True)      # "low" | "medium" | "high"
    fit_score_reasoning = Column(Text, nullable=True)         # JSON: {summary, skills_match, exp_match, ...}
    fit_score_details = Column(Text, nullable=True)           # JSON: detailed breakdown points

    # ── Stage Tracking ───────────────────────────────────────────────────────
    # "Candidates" | "AI Round" | "Panel Round" | "Offer-Made" | "Rejected"
    candidate_stage = Column(String(50), nullable=True, default="Candidates")

    # ── Interview Scheduling ─────────────────────────────────────────────────
    # "New" | "Scheduled" | "Completed" | "Cancelled" | "Ended by Candidate"
    # "Disconnected – Network Issue" | "Candidate Terminated Prematurely"
    # "Terminated – Cheating Detected"
    interview_status = Column(String(60), nullable=True, default="New")
    interview_date = Column(DateTime, nullable=True)
    interview_timezone = Column(String(100), nullable=True)
    interview_link = Column(String(500), nullable=True)
    allow_candidate_reschedule = Column(Boolean, nullable=True, default=False)
    # "scheduled_time" | "anytime"
    joining_flexibility = Column(String(30), nullable=True, default="anytime")
    schedule_email_template = Column(Text, nullable=True)    # JSON with subject + body overrides

    # ── Post-Interview Scores ────────────────────────────────────────────────
    score = Column(Float, nullable=True)
    trust_score = Column(Float, nullable=True)
    grammar_score = Column(Float, nullable=True)
    confidence_score = Column(Float, nullable=True)
    clarity_score = Column(Float, nullable=True)
    pronunciation_score = Column(Float, nullable=True)
    enthusiasm_score = Column(Float, nullable=True)
    conciseness_score = Column(Float, nullable=True)
    ai_cheating_score = Column(Float, nullable=True)

    # ── Recording ────────────────────────────────────────────────────────────
    recording_link = Column(String(500), nullable=True)
    video_duration = Column(String(50), nullable=True)

    # ── Contact History ──────────────────────────────────────────────────────
    mail_contact_history = Column(Boolean, nullable=True, default=False)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint('email', 'job_id', name='uq_candidate_email_job'),
    )
