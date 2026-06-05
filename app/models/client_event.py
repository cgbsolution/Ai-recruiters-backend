from datetime import datetime

from sqlalchemy import Column, DateTime, Float, Integer, String, Text

from app.database import Base


class ClientEvent(Base):
    """Candidate-side telemetry beacon.

    The interview runs entirely in the candidate's browser, so failures there
    (a recording that never uploaded, a tab crash, rising memory) are invisible
    to the server unless the client tells us. The frontend emits best-effort
    `navigator.sendBeacon` events to POST /client-events; this table is where we
    land them so an operator can answer "why is this candidate's video missing?"
    without waiting for a complaint.

    Mirrors the payload sent by the frontend's clientTelemetry.ts.
    """

    __tablename__ = "client_events"

    id = Column(Integer, primary_key=True, index=True)
    # Correlates the event to an interview session when the client knows it
    # (pulled from the beacon's context.sessionId). Nullable: some events fire
    # before a session exists or outside a session.
    session_id = Column(Integer, nullable=True, index=True)
    event_type = Column(String(60), nullable=False, index=True)
    message = Column(Text, nullable=True)
    # Full JSON context blob from the client (kept verbatim for debugging).
    context = Column(Text, nullable=True)
    url = Column(String(1000), nullable=True)
    user_agent = Column(String(500), nullable=True)
    device_memory_gb = Column(Float, nullable=True)
    hardware_concurrency = Column(Integer, nullable=True)
    # Timestamp the client stamped (ms since epoch), kept for ordering.
    client_at = Column(String(40), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
