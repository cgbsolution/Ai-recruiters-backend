"""
Structured metadata extracted from a Job Description.

When a JD is uploaded or generated, we use Claude to extract
searchable metadata (skills, keywords, education requirements, etc.)
that enables fast candidate-to-JD matching without re-reading the JD.
"""

from datetime import datetime

from sqlalchemy import Column, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import relationship

from app.database import Base


class JobMetadata(Base):
    __tablename__ = "job_metadata"

    id = Column(Integer, primary_key=True, index=True)
    job_id = Column(Integer, ForeignKey("jobs.id"), nullable=False, unique=True, index=True)

    # ── Extracted Fields ──────────────────────────────────────────────────────
    parsed_title = Column(String(255), nullable=True)
    required_skills = Column(Text, nullable=True)           # JSON array
    experience_range = Column(String(100), nullable=True)   # e.g. "2-5 years"
    location = Column(String(255), nullable=True)
    education_requirements = Column(Text, nullable=True)    # JSON array
    designation = Column(String(255), nullable=True)
    keywords = Column(Text, nullable=True)                  # JSON array
    responsibilities_summary = Column(Text, nullable=True)  # concise summary

    # ── Raw Source ────────────────────────────────────────────────────────────
    raw_jd_text = Column(Text, nullable=True)

    # ── Timestamps ────────────────────────────────────────────────────────────
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # ── Relationship ──────────────────────────────────────────────────────────
    job = relationship("Job", backref="job_metadata")
