import enum
from datetime import datetime

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import relationship

from app.database import Base


class UserRole(str, enum.Enum):
    super_admin = "super_admin"
    admin = "admin"
    recruiter = "recruiter"


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(150), nullable=False)
    email = Column(String(255), unique=True, nullable=False, index=True)
    password_hash = Column(Text, nullable=False)
    role = Column(Enum(UserRole), nullable=False, default=UserRole.recruiter)
    phone = Column(String(50), nullable=True)
    designation = Column(String(150), nullable=True)
    is_active = Column(Boolean, default=True, nullable=False)
    is_email_verified = Column(Boolean, default=False, nullable=False)
    email_verify_token = Column(String(128), nullable=True)
    email_verify_expires = Column(DateTime, nullable=True)
    # Forgot-password flow. Single-use: both are cleared the moment the
    # password is actually changed, so a reset link cannot be replayed.
    password_reset_token = Column(String(128), nullable=True)
    password_reset_expires = Column(DateTime, nullable=True)
    organization_id = Column(Integer, ForeignKey("organizations.id"), nullable=True, index=True)
    # Set for a user AskMira manages (its user UUID). Only these are switched
    # off when they drop out of AskMira's roster; users made here are left alone.
    askmira_user_id = Column(String(36), nullable=True, index=True)

    # ── Google OAuth2 (per-user, for Google Meet / Calendar integration) ─────
    google_access_token = Column(Text, nullable=True)
    google_refresh_token = Column(Text, nullable=True)
    google_email = Column(String(255), nullable=True)
    google_connected_at = Column(DateTime, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    organization = relationship("Organization", back_populates="users")
