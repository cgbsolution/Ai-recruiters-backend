import enum
from datetime import datetime

from sqlalchemy import (
    Boolean, Column, DateTime, Enum, Float, ForeignKey,
    Integer, String, Text, UniqueConstraint,
)
from sqlalchemy.orm import relationship
from sqlalchemy.types import TypeDecorator

from app.database import Base


class SessionStatus(str, enum.Enum):
    pending = "pending"
    in_progress = "in_progress"
    completed = "completed"
    terminated = "terminated"


class ViolationType(str, enum.Enum):
    tab_switch = "tab_switch"
    fullscreen_exit = "fullscreen_exit"
    face_not_detected = "face_not_detected"
    multiple_faces = "multiple_faces"


class Recommendation(str, enum.Enum):
    strong_hire = "Strong Hire"
    hire = "Hire"
    maybe = "Maybe"
    no_hire = "No Hire"


class RecommendationType(TypeDecorator):
    # Column type for Evaluation.recommendation.
    # Writes canonical .value ("Strong Hire" / "Hire" / "Maybe" / "No Hire").
    # Reads tolerate legacy rows that were stored as the Python enum .name
    # ("hire", "strong_hire", "no_hire", "maybe") before values_callable was
    # added, and any case variation.
    impl = String(32)
    cache_ok = True

    _aliases = {
        "strong_hire": Recommendation.strong_hire,
        "strong hire": Recommendation.strong_hire,
        "hire": Recommendation.hire,
        "maybe": Recommendation.maybe,
        "no_hire": Recommendation.no_hire,
        "no hire": Recommendation.no_hire,
    }

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if isinstance(value, Recommendation):
            return value.value
        if isinstance(value, str):
            member = self._aliases.get(value.strip().lower())
            if member is not None:
                return member.value
        return value

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        if isinstance(value, Recommendation):
            return value
        if isinstance(value, str):
            return self._aliases.get(value.strip().lower())
        return None


class MeetingScheduleStatus(str, enum.Enum):
    scheduled = "scheduled"
    cancelled = "cancelled"


class InterviewToken(Base):
    __tablename__ = "interview_tokens"

    id = Column(Integer, primary_key=True, index=True)
    job_id = Column(Integer, ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False, index=True)
    token = Column(String(64), unique=True, nullable=False, index=True)
    is_active = Column(Boolean, default=True, nullable=False)
    expires_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    job = relationship("Job")
    sessions = relationship("InterviewSession", back_populates="token", cascade="all, delete-orphan")


class InterviewSession(Base):
    __tablename__ = "interview_sessions"

    id = Column(Integer, primary_key=True, index=True)
    token_id = Column(Integer, ForeignKey("interview_tokens.id", ondelete="CASCADE"), nullable=False, index=True)
    candidate_id = Column(Integer, ForeignKey("candidates.id", ondelete="SET NULL"), nullable=True, index=True)
    job_id = Column(Integer, ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False, index=True)
    status = Column(Enum(SessionStatus), default=SessionStatus.pending, nullable=False, index=True)
    started_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)
    total_questions = Column(Integer, default=0)
    current_question_index = Column(Integer, default=0)
    violation_count = Column(Integer, default=0)
    session_metadata = Column(Text, nullable=True)  # JSON: device info, user agent
    interview_language = Column(String(20), nullable=True, default="en")  # "en" | "hi" | "ar" | "hinglish"
    created_at = Column(DateTime, default=datetime.utcnow)

    token = relationship("InterviewToken", back_populates="sessions")
    candidate = relationship("Candidate")
    job = relationship("Job")
    messages = relationship("InterviewMessage", back_populates="session", cascade="all, delete-orphan", order_by="InterviewMessage.created_at")
    violations = relationship("Violation", back_populates="session", cascade="all, delete-orphan")
    evaluation = relationship("Evaluation", back_populates="session", uselist=False, cascade="all, delete-orphan")
    feedback = relationship("CandidateFeedback", back_populates="session", uselist=False, cascade="all, delete-orphan")
    meeting_schedule = relationship("MeetingSchedule", back_populates="session", uselist=False, cascade="all, delete-orphan")
    screenshots = relationship("InterviewScreenshot", back_populates="session", cascade="all, delete-orphan", order_by="InterviewScreenshot.captured_at")


class InterviewMessage(Base):
    __tablename__ = "interview_messages"

    id = Column(Integer, primary_key=True, index=True)
    session_id = Column(Integer, ForeignKey("interview_sessions.id", ondelete="CASCADE"), nullable=False, index=True)
    role = Column(String(20), nullable=False)  # "ai" | "candidate"
    content = Column(Text, nullable=False)
    question_index = Column(Integer, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    session = relationship("InterviewSession", back_populates="messages")


class Violation(Base):
    __tablename__ = "violations"

    id = Column(Integer, primary_key=True, index=True)
    session_id = Column(Integer, ForeignKey("interview_sessions.id", ondelete="CASCADE"), nullable=False, index=True)
    violation_type = Column(Enum(ViolationType), nullable=False)
    details = Column(Text, nullable=True)
    occurred_at = Column(DateTime, default=datetime.utcnow)

    session = relationship("InterviewSession", back_populates="violations")


class Evaluation(Base):
    __tablename__ = "evaluations"

    id = Column(Integer, primary_key=True, index=True)
    session_id = Column(Integer, ForeignKey("interview_sessions.id", ondelete="CASCADE"), nullable=False, unique=True)
    candidate_id = Column(Integer, ForeignKey("candidates.id", ondelete="CASCADE"), nullable=False, index=True)
    technical_score = Column(Float, nullable=True)
    communication_score = Column(Float, nullable=True)
    problem_solving_score = Column(Float, nullable=True)
    confidence_score = Column(Float, nullable=True)
    coding_score = Column(Float, nullable=True)  # 0-100, only set when coding submissions exist
    overall_score = Column(Float, nullable=True)
    recommendation = Column(RecommendationType(), nullable=True)
    summary = Column(Text, nullable=True)
    strengths = Column(Text, nullable=True)     # JSON array of strings
    weaknesses = Column(Text, nullable=True)    # JSON array of strings
    detailed_feedback = Column(Text, nullable=True)
    per_question_evaluations = Column(Text, nullable=True)  # JSON: [{index, score, positives:[], negatives:[]}]
    skills_ratings = Column(Text, nullable=True)            # JSON: [{skill, rating, evaluation}]
    meets_threshold = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    session = relationship("InterviewSession", back_populates="evaluation")
    candidate = relationship("Candidate")


class CandidateFeedback(Base):
    __tablename__ = "candidate_feedback"

    id = Column(Integer, primary_key=True, index=True)
    session_id = Column(Integer, ForeignKey("interview_sessions.id", ondelete="CASCADE"), nullable=False, unique=True)
    rating = Column(Integer, nullable=False)        # 1-5
    experience_comment = Column(Text, nullable=True)
    technical_issues = Column(Boolean, default=False)
    would_recommend = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    session = relationship("InterviewSession", back_populates="feedback")


class MeetingSlot(Base):
    __tablename__ = "meeting_slots"

    id = Column(Integer, primary_key=True, index=True)
    job_id = Column(Integer, ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False, index=True)
    start_time = Column(DateTime, nullable=False)
    end_time = Column(DateTime, nullable=False)
    is_booked = Column(Boolean, default=False, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    job = relationship("Job")
    meeting_schedule = relationship("MeetingSchedule", back_populates="slot", uselist=False)


class MeetingSchedule(Base):
    __tablename__ = "meeting_schedules"

    id = Column(Integer, primary_key=True, index=True)
    session_id = Column(Integer, ForeignKey("interview_sessions.id", ondelete="CASCADE"), nullable=False, unique=True)
    slot_id = Column(Integer, ForeignKey("meeting_slots.id", ondelete="CASCADE"), nullable=False)
    candidate_id = Column(Integer, ForeignKey("candidates.id", ondelete="CASCADE"), nullable=False, index=True)
    status = Column(Enum(MeetingScheduleStatus), default=MeetingScheduleStatus.scheduled)
    created_at = Column(DateTime, default=datetime.utcnow)

    session = relationship("InterviewSession", back_populates="meeting_schedule")
    slot = relationship("MeetingSlot", back_populates="meeting_schedule")
    candidate = relationship("Candidate")


class InterviewStatusLog(Base):
    __tablename__ = "interview_status_logs"

    id = Column(Integer, primary_key=True, index=True)
    candidate_id = Column(Integer, ForeignKey("candidates.id", ondelete="CASCADE"), nullable=False, index=True)
    previous_status = Column(String(60), nullable=True)
    new_status = Column(String(60), nullable=False)
    changed_by = Column(String(100), nullable=True)  # "system" | "admin" | "candidate"
    reason = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    candidate = relationship("Candidate")


class InterviewScreenshot(Base):
    """Periodic webcam snapshots captured every ~14 seconds for proctoring."""
    __tablename__ = "interview_screenshots"

    id = Column(Integer, primary_key=True, index=True)
    session_id = Column(Integer, ForeignKey("interview_sessions.id", ondelete="CASCADE"), nullable=False, index=True)
    file_path = Column(String(500), nullable=False)   # relative path under uploads/
    captured_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    face_count = Column(Integer, nullable=True)  # 0=no face, 1=single face, 2+=multiple faces

    session = relationship("InterviewSession", back_populates="screenshots")


class CodingSubmission(Base):
    """Structured record of a candidate's code answer for a coding challenge.

    Each row is a single submission. We keep both the raw code and the
    per-test-case execution results so the recruiter UI can show pass/fail
    breakdowns and the evaluator LLM can score code separately from verbal
    answers.
    """
    __tablename__ = "coding_submissions"

    id = Column(Integer, primary_key=True, index=True)
    session_id = Column(Integer, ForeignKey("interview_sessions.id", ondelete="CASCADE"), nullable=False, index=True)
    question_index = Column(Integer, nullable=True)
    title = Column(String(255), nullable=True)
    description = Column(Text, nullable=True)
    difficulty = Column(String(20), nullable=True)  # easy | medium | hard
    language = Column(String(40), nullable=False)
    code = Column(Text, nullable=False)
    test_cases_total = Column(Integer, nullable=False, default=0)
    test_cases_passed = Column(Integer, nullable=False, default=0)
    test_results = Column(Text, nullable=True)   # JSON: full per-test results array
    execution_time_ms = Column(Integer, nullable=True)
    submitted_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    session = relationship("InterviewSession")
