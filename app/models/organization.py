from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, Integer, String
from sqlalchemy.orm import relationship

from app.database import Base


class Organization(Base):
    __tablename__ = "organizations"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(255), nullable=False)
    domain = Column(String(255), unique=True, nullable=False, index=True)
    is_active = Column(Boolean, default=True, nullable=False)
    # Quota limits — 0 means unlimited
    max_jobs = Column(Integer, default=0, nullable=False)
    max_candidates_per_job = Column(Integer, default=0, nullable=False)
    # Lifetime usage counters — never decremented on delete, reset only by super admin
    total_jobs_used = Column(Integer, default=0, nullable=False, server_default="0")
    total_candidates_used = Column(Integer, default=0, nullable=False, server_default="0")
    # Feature permissions — controlled by super admin
    allow_resume_database = Column(Boolean, default=False, nullable=False, server_default="false")
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    users = relationship("User", back_populates="organization")
    jobs = relationship("Job", back_populates="organization")
