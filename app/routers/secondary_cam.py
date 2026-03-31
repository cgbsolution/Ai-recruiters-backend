"""
Secondary Camera Verification API.

Manages pairing between desktop interview and mobile secondary camera.
Uses short-polling for real-time status sync (consistent with existing platform patterns).
"""

import json
import logging
import os
import shutil
import uuid
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.secondary_cam import (
    SecondaryCamSession,
    SecondaryCamSnapshot,
    SecondaryCamStatus,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/secondary-cam")

UPLOAD_DIR = os.environ.get("UPLOAD_DIR", "./uploads")
SECONDARY_CAM_DIR = os.path.join(UPLOAD_DIR, "secondary_cam")
os.makedirs(SECONDARY_CAM_DIR, exist_ok=True)

# Heartbeat is considered stale after this many seconds
HEARTBEAT_STALE_SECONDS = 20


# ── Desktop Endpoints (called by interview page) ─────────────────────────────

@router.post("/{session_id}/init", summary="Initialize secondary camera pairing")
def init_pairing(session_id: int, db: Session = Depends(get_db)):
    """Create a pairing session and return the pairing code for QR generation."""
    from app.models.interview import InterviewSession

    session = db.query(InterviewSession).filter(
        InterviewSession.id == session_id
    ).first()
    if not session:
        raise HTTPException(404, "Interview session not found")

    # Check if pairing already exists
    existing = db.query(SecondaryCamSession).filter(
        SecondaryCamSession.session_id == session_id
    ).first()
    if existing:
        return {
            "pairing_code": existing.pairing_code,
            "status": existing.status.value,
        }

    pairing_code = uuid.uuid4().hex[:16]

    cam_session = SecondaryCamSession(
        session_id=session_id,
        pairing_code=pairing_code,
        status=SecondaryCamStatus.pending,
    )
    db.add(cam_session)
    db.commit()
    db.refresh(cam_session)

    logger.info("Secondary cam pairing created: session=%s code=%s", session_id, pairing_code)

    return {
        "pairing_code": pairing_code,
        "status": cam_session.status.value,
    }


@router.get("/{session_id}/status", summary="Get secondary camera status (polled by desktop)")
def get_status(session_id: int, db: Session = Depends(get_db)):
    """Return the current status of the secondary camera connection."""
    cam_session = db.query(SecondaryCamSession).filter(
        SecondaryCamSession.session_id == session_id
    ).first()
    if not cam_session:
        raise HTTPException(404, "No secondary camera session found")

    # Check if heartbeat is stale
    heartbeat_stale = False
    seconds_since_heartbeat = None
    if cam_session.last_heartbeat:
        delta = datetime.utcnow() - cam_session.last_heartbeat
        seconds_since_heartbeat = int(delta.total_seconds())
        heartbeat_stale = seconds_since_heartbeat > HEARTBEAT_STALE_SECONDS

        # Auto-mark as disconnected if heartbeat is very stale and was active
        if heartbeat_stale and cam_session.status == SecondaryCamStatus.active:
            cam_session.status = SecondaryCamStatus.disconnected
            db.commit()

    return {
        "status": cam_session.status.value,
        "heartbeat_stale": heartbeat_stale,
        "seconds_since_heartbeat": seconds_since_heartbeat,
        "verified_at": cam_session.verified_at.isoformat() if cam_session.verified_at else None,
        "device_info": json.loads(cam_session.device_info) if cam_session.device_info else None,
    }


# ── Mobile Endpoints (called by phone page) ──────────────────────────────────

@router.get("/connect/{pairing_code}", summary="Validate pairing code (mobile page load)")
def validate_pairing(pairing_code: str, db: Session = Depends(get_db)):
    """Validate a pairing code and return session info for the mobile page."""
    cam_session = db.query(SecondaryCamSession).filter(
        SecondaryCamSession.pairing_code == pairing_code
    ).first()
    if not cam_session:
        raise HTTPException(404, "Invalid or expired pairing code")

    # Check if code is too old (expire after 30 minutes)
    if cam_session.created_at and (datetime.utcnow() - cam_session.created_at) > timedelta(minutes=30):
        raise HTTPException(410, "Pairing code has expired. Please generate a new one.")

    from app.models.interview import InterviewSession
    session = db.query(InterviewSession).filter(
        InterviewSession.id == cam_session.session_id
    ).first()

    job_title = ""
    candidate_name = ""
    if session:
        if session.candidate_id:
            from app.models.candidate import Candidate as CandModel
            cand = db.query(CandModel).filter(CandModel.id == session.candidate_id).first()
            if cand:
                candidate_name = cand.name or ""
        if session.job_id:
            from app.models.job import Job
            job = db.query(Job).filter(Job.id == session.job_id).first()
            if job:
                job_title = job.title

    return {
        "valid": True,
        "pairing_code": pairing_code,
        "status": cam_session.status.value,
        "job_title": job_title,
        "candidate_name": candidate_name,
    }


@router.post("/connect/{pairing_code}/register", summary="Register mobile device")
def register_device(
    pairing_code: str,
    device_info: str = Form(default="{}"),
    db: Session = Depends(get_db),
):
    """Register the mobile device and mark as connected."""
    cam_session = db.query(SecondaryCamSession).filter(
        SecondaryCamSession.pairing_code == pairing_code
    ).first()
    if not cam_session:
        raise HTTPException(404, "Invalid pairing code")

    cam_session.status = SecondaryCamStatus.connected
    cam_session.device_info = device_info
    cam_session.last_heartbeat = datetime.utcnow()
    db.commit()

    logger.info("Secondary device registered: code=%s", pairing_code)

    return {"status": "connected"}


@router.post("/connect/{pairing_code}/confirm-position", summary="Confirm phone positioning")
async def confirm_position(
    pairing_code: str,
    snapshot: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    """Upload positioning snapshot and mark as verified."""
    cam_session = db.query(SecondaryCamSession).filter(
        SecondaryCamSession.pairing_code == pairing_code
    ).first()
    if not cam_session:
        raise HTTPException(404, "Invalid pairing code")

    # Save snapshot
    filename = f"{pairing_code}_position_{datetime.utcnow().strftime('%Y%m%d%H%M%S')}.jpg"
    filepath = os.path.join(SECONDARY_CAM_DIR, filename)
    with open(filepath, "wb") as f:
        shutil.copyfileobj(snapshot.file, f)

    cam_session.status = SecondaryCamStatus.verified
    cam_session.positioning_snapshot = filepath
    cam_session.verified_at = datetime.utcnow()
    cam_session.last_heartbeat = datetime.utcnow()
    db.commit()

    logger.info("Secondary cam position verified: code=%s", pairing_code)

    return {"status": "verified"}


@router.post("/connect/{pairing_code}/heartbeat", summary="Mobile heartbeat")
def heartbeat(pairing_code: str, db: Session = Depends(get_db)):
    """Update the heartbeat timestamp to prove the phone is still connected."""
    cam_session = db.query(SecondaryCamSession).filter(
        SecondaryCamSession.pairing_code == pairing_code
    ).first()
    if not cam_session:
        raise HTTPException(404, "Invalid pairing code")

    cam_session.last_heartbeat = datetime.utcnow()

    # Transition from verified → active on first heartbeat after verification
    if cam_session.status == SecondaryCamStatus.verified:
        cam_session.status = SecondaryCamStatus.active

    # Reconnection: disconnected → active
    if cam_session.status == SecondaryCamStatus.disconnected:
        cam_session.status = SecondaryCamStatus.active
        logger.info("Secondary cam reconnected: code=%s", pairing_code)

    db.commit()

    return {"status": cam_session.status.value}


@router.post("/connect/{pairing_code}/snapshot", summary="Upload periodic snapshot")
async def upload_snapshot(
    pairing_code: str,
    snapshot: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    """Upload a periodic camera snapshot during the interview."""
    cam_session = db.query(SecondaryCamSession).filter(
        SecondaryCamSession.pairing_code == pairing_code
    ).first()
    if not cam_session:
        raise HTTPException(404, "Invalid pairing code")

    filename = f"{pairing_code}_{datetime.utcnow().strftime('%Y%m%d%H%M%S%f')}.jpg"
    filepath = os.path.join(SECONDARY_CAM_DIR, filename)
    with open(filepath, "wb") as f:
        shutil.copyfileobj(snapshot.file, f)

    snap = SecondaryCamSnapshot(
        secondary_session_id=cam_session.id,
        file_path=filepath,
    )
    db.add(snap)
    cam_session.last_heartbeat = datetime.utcnow()
    db.commit()

    return {"status": "ok"}


@router.post("/{session_id}/complete", summary="Mark secondary cam session as completed")
def complete_session(session_id: int, db: Session = Depends(get_db)):
    """Mark the secondary camera session as completed when interview ends."""
    cam_session = db.query(SecondaryCamSession).filter(
        SecondaryCamSession.session_id == session_id
    ).first()
    if cam_session:
        cam_session.status = SecondaryCamStatus.completed
        db.commit()
    return {"status": "completed"}
