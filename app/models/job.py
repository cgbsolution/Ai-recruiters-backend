import enum
from datetime import datetime

from sqlalchemy import (
    Boolean, Column, DateTime, Enum, ForeignKey,
    Integer, String, Text, UniqueConstraint,
)
from sqlalchemy.orm import relationship

from app.database import Base


class JobStatus(str, enum.Enum):
    open = "Open"
    inactive = "Inactive"
    draft = "Draft"


class MixOfQuestions(str, enum.Enum):
    mix = "MIX"
    only = "ONLY"


class InterviewMode(str, enum.Enum):
    traditional = "TRADITIONAL"
    scenario = "SCENARIO"


class SkillCriteria(str, enum.Enum):
    all_mandatory = "All Mandatory"
    at_least_one = "At Least One"
    none = "None"


class Job(Base):
    __tablename__ = "jobs"

    id = Column(Integer, primary_key=True, index=True)
    title = Column(String(255), nullable=False, index=True)
    department = Column(String(255), nullable=False)
    location = Column(String(255), nullable=False)
    job_type = Column(String(100), nullable=False)
    experience_level = Column(String(100), nullable=True)
    experience_min = Column(Integer, default=0)
    experience_max = Column(Integer, default=2)
    openings = Column(Integer, default=1)
    emp_type = Column(String(100), nullable=True)
    status = Column(Enum(JobStatus), default=JobStatus.draft, index=True)
    deadline = Column(String(100), nullable=True)

    manager_name = Column(String(255), nullable=True)
    manager_phone = Column(String(50), nullable=True)
    manager_email = Column(String(255), nullable=True)

    evaluation_scoring = Column(String(255), default="Standard Evaluation")
    mix_of_questions = Column(Enum(MixOfQuestions), default=MixOfQuestions.mix)
    allow_to_contact = Column(Boolean, default=False)
    upload_jd = Column(String(500), nullable=True)

    applications = Column(Integer, default=0)
    interviews = Column(Integer, default=0)
    candidates = Column(Integer, default=0)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    responsibilities = relationship(
        "JobResponsibility", back_populates="job", cascade="all, delete-orphan"
    )
    skills = relationship(
        "JobSkill", back_populates="job", cascade="all, delete-orphan"
    )
    interview_settings = relationship(
        "InterviewSettings", uselist=False, back_populates="job", cascade="all, delete-orphan"
    )
    questions = relationship(
        "JobQuestion", back_populates="job", cascade="all, delete-orphan"
    )


class JobResponsibility(Base):
    __tablename__ = "job_responsibilities"

    id = Column(Integer, primary_key=True, index=True)
    job_id = Column(Integer, ForeignKey("jobs.id"), nullable=False, index=True)
    description = Column(Text, nullable=False)

    job = relationship("Job", back_populates="responsibilities")


class JobSkill(Base):
    __tablename__ = "job_skills"

    id = Column(Integer, primary_key=True, index=True)
    job_id = Column(Integer, ForeignKey("jobs.id"), nullable=False, index=True)
    name = Column(String(255))
    skill = Column(String(255))
    skill_criteria = Column(Enum(SkillCriteria), default=SkillCriteria.all_mandatory)

    job = relationship("Job", back_populates="skills")


class InterviewSettings(Base):
    __tablename__ = "interview_settings"

    id = Column(Integer, primary_key=True, index=True)
    job_id = Column(Integer, ForeignKey("jobs.id"), nullable=False, unique=True)
    code_editor_required = Column(Boolean, default=False)
    resume_based = Column(Boolean, default=False)
    virtual_avatar = Column(Boolean, default=False)
    allow_mobile = Column(Boolean, default=False)
    face_match = Column(Boolean, default=False)
    webcam_required = Column(Boolean, default=False)
    secondary_cam = Column(Boolean, default=False)
    mode = Column(Enum(InterviewMode), default=InterviewMode.traditional)
    max_questions = Column(Integer, default=10)
    auto_skip_timeout = Column(Integer, default=0)
    special_instructions = Column(Text, nullable=True)
    scoring = Column(String(255), default="Standard Evaluation")
    manual_interview = Column(Boolean, default=False)

    job = relationship("Job", back_populates="interview_settings")


class JobQuestion(Base):
    __tablename__ = "job_questions"

    id = Column(Integer, primary_key=True, index=True)
    job_id = Column(Integer, ForeignKey("jobs.id"), nullable=False, index=True)
    text = Column(Text, nullable=False)
    category = Column(String(255), default="general")
    difficulty = Column(String(50), default="medium")

    job = relationship("Job", back_populates="questions")


