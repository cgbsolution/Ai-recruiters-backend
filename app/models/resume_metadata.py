"""
Central resume metadata store.

Every uploaded resume is parsed once and its structured data stored here.
Keyed by candidate email — if the same person uploads again, the record is
updated (not duplicated).  This avoids repeated LLM parsing and enables
fast cross-job candidate matching.
"""

from datetime import datetime

from sqlalchemy import Column, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import relationship

from app.database import Base


class ResumeMetadata(Base):
    __tablename__ = "resume_metadata"
    __table_args__ = (
        UniqueConstraint("candidate_email", "organization_id", name="uq_resume_email_org"),
    )

    id = Column(Integer, primary_key=True, index=True)

    # ── Identity (unique per person per org) ─────────────────────────────────
    organization_id = Column(Integer, ForeignKey("organizations.id"), nullable=True, index=True)
    candidate_email = Column(String(255), nullable=False, index=True)
    candidate_name = Column(String(255), nullable=True)
    phone = Column(String(50), nullable=True)

    # ── Professional ──────────────────────────────────────────────────────────
    current_designation = Column(String(255), nullable=True)
    experience_years = Column(Float, nullable=True)
    skills = Column(Text, nullable=True)           # JSON array: ["Python", "React", ...]
    education = Column(Text, nullable=True)         # JSON array: ["B.Tech CS, XYZ Univ", ...]
    certifications = Column(Text, nullable=True)    # JSON array: ["AWS SAA", ...]
    work_experience = Column(Text, nullable=True)   # JSON array of objects

    # ── Personal / Location ───────────────────────────────────────────────────
    location = Column(String(255), nullable=True)
    annual_package = Column(Float, nullable=True)    # Annual package / CTC in LPA
    personal_details = Column(Text, nullable=True)  # JSON: {dob, gender, languages, ...}

    # ── Search / Matching ─────────────────────────────────────────────────────
    keywords = Column(Text, nullable=True)          # JSON array of extracted keywords
    summary = Column(Text, nullable=True)           # 2-3 sentence professional summary

    # ── File Reference ────────────────────────────────────────────────────────
    resume_file_path = Column(String(500), nullable=True)
    raw_text = Column(Text, nullable=True)          # full extracted text (for re-matching)

    # ── Status ────────────────────────────────────────────────────────────────
    parsing_status = Column(
        String(20), nullable=False, default="pending"
    )  # "pending" | "processing" | "completed" | "failed"
    parsing_error = Column(Text, nullable=True)

    # ── Timestamps ────────────────────────────────────────────────────────────
    parsed_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
