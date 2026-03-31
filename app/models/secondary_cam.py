"""Secondary Camera Verification models."""

import enum
from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import relationship

from app.database import Base


class SecondaryCamStatus(str, enum.Enum):
    pending = "pending"              # pairing code created, waiting for phone
    connected = "connected"          # phone registered
    verified = "verified"            # candidate confirmed positioning
    active = "active"                # interview in progress, phone streaming
    disconnected = "disconnected"    # heartbeat lost during interview
    completed = "completed"          # interview ended normally


class SecondaryCamSession(Base):
    __tablename__ = "secondary_cam_sessions"

    id = Column(Integer, primary_key=True, index=True)
    session_id = Column(
        Integer,
        ForeignKey("interview_sessions.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    pairing_code = Column(String(32), unique=True, nullable=False, index=True)
    status = Column(String(20), default="pending")
    device_info = Column(Text, nullable=True)
    last_heartbeat = Column(DateTime, nullable=True)
    positioning_snapshot = Column(String(500), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    verified_at = Column(DateTime, nullable=True)

    # Relationships
    session = relationship("InterviewSession", backref="secondary_cam")
    snapshots = relationship(
        "SecondaryCamSnapshot", back_populates="secondary_session",
        cascade="all, delete-orphan",
    )


class SecondaryCamSnapshot(Base):
    __tablename__ = "secondary_cam_snapshots"

    id = Column(Integer, primary_key=True, index=True)
    secondary_session_id = Column(
        Integer,
        ForeignKey("secondary_cam_sessions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    file_path = Column(String(500), nullable=False)
    captured_at = Column(DateTime, default=datetime.utcnow)

    secondary_session = relationship("SecondaryCamSession", back_populates="snapshots")
