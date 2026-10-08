"""
Retention and erasure.

erase_profile() — the candidate's "delete my data" (and the retention sweep):
removes the profile, every CV version and its file, Talent Agent interviews
with their recordings and transcripts, and the candidate's job applications
in this organization with their interview sessions (transcripts, evaluations,
screenshots) and recordings. The addresses go on the suppression list (as
keyed hashes) so a re-imported CV is never contacted again. The audit log
keeps ids and counts only.

Other deletion paths (the Resume Database delete endpoints, de-duplication)
delete the profile row; the listener at the bottom then removes the CV-version
and interview files that the database cascade orphaned.
"""

from __future__ import annotations

import logging
import os
import shutil
from datetime import datetime
from typing import List, Optional, Set

import sqlalchemy as sa
from sqlalchemy import event
from sqlalchemy.orm import Session, object_session

from app.media import UPLOAD_ROOT, resolve_upload_file, resume_file_in_use, safe_unlink_upload
from app.models.candidate import Candidate
from app.models.interview import InterviewSession
from app.models.job import Job
from app.models.resume_metadata import ResumeMetadata
from app.models.talent import CvVersion, JobMatch, TalentInterview
from app.talent import consent
from app.talent.audit import audit
from app.talent.schema import set_audit_actor

logger = logging.getLogger("app.talent.retention")

AUDIO_DIR = "talent_audio"


def _talent_files(conn, profile_id: int) -> Set[str]:
    files = {p for (p,) in conn.execute(
        sa.select(CvVersion.file_path).where(CvVersion.profile_id == profile_id, CvVersion.file_path.isnot(None)))}
    for (paths,) in conn.execute(
        sa.select(TalentInterview.recording_paths)
        .join(JobMatch, JobMatch.id == TalentInterview.match_id)
        .where(JobMatch.profile_id == profile_id)
    ):
        files |= set(paths or [])
    return files


def _remove_files(paths: Set[str]) -> int:
    """Delete talent files; resume files only when nothing else references them."""
    from app.database import SessionLocal

    removed = 0
    db = SessionLocal()
    try:
        for path in paths:
            if not path:
                continue
            norm = path.replace("\\", "/")
            if f"/{AUDIO_DIR}/" in f"/{norm}" or norm.startswith(f"{AUDIO_DIR}/"):
                removed += safe_unlink_upload(path, (AUDIO_DIR,))
            elif not resume_file_in_use(db, path):
                removed += safe_unlink_upload(path, ("resumes",))
    finally:
        db.close()
    return removed


def _delete_session_media(session_id: int) -> int:
    removed = 0
    for ext in (".webm", ".mp4"):
        full = resolve_upload_file(f"recordings/{session_id}{ext}", ["recordings"])
        if full and os.path.exists(full):
            try:
                os.remove(full)
                removed += 1
            except OSError as exc:
                logger.warning("Could not remove a recording during erasure: %s", exc)
    for sub in (os.path.join("recordings", ".parts", str(int(session_id))), os.path.join("screenshots", str(int(session_id)))):
        full = os.path.join(UPLOAD_ROOT, sub)
        if os.path.isdir(full):
            shutil.rmtree(full, ignore_errors=True)
            removed += 1
    return removed


def erase_profile(db: Session, profile_id: int, *, actor: str, reason: str) -> Optional[dict]:
    from app.services import candidate_service

    profile = db.get(ResumeMetadata, profile_id)
    if profile is None:
        return None
    org_id = profile.organization_id
    email = (profile.candidate_email or "").strip().lower()

    applications: List[Candidate] = db.query(Candidate).filter(Candidate.resume_metadata_id == profile.id).all()
    if email and org_id is not None:
        applications += (
            db.query(Candidate).join(Job, Job.id == Candidate.job_id)
            .filter(sa.func.lower(Candidate.email) == email, Job.organization_id == org_id,
                    Candidate.id.notin_([a.id for a in applications] or [0]))
            .all()
        )
    sessions = media = 0
    for app_row in applications:
        for session in db.query(InterviewSession).filter(InterviewSession.candidate_id == app_row.id).all():
            media += _delete_session_media(session.id)
            db.delete(session)  # messages, evaluation, screenshots, violations cascade
            sessions += 1
        db.commit()
        candidate_service.delete_candidate(db, app_row.id)  # recording link, screenshots, resume file, row

    files = _talent_files(db.connection(), profile.id)
    if profile.resume_file_path:
        files.add(profile.resume_file_path)
    consent.suppress(db, email=profile.candidate_email, phone=profile.phone, reason="erased")
    set_audit_actor(db, actor, reason)
    audit(db, actor=actor, action="profile.erased", subject_type="profile", subject_id=profile.id,
          organization_id=org_id, profile_id=profile.id,
          detail={"reason": reason, "applications": len(applications), "interview_sessions": sessions,
                  "files": len(files) + media})
    db.info["talent_skip_file_listener"] = True
    db.delete(profile)
    db.commit()
    db.info.pop("talent_skip_file_listener", None)
    removed = _remove_files(files)
    return {"applications": len(applications), "interview_sessions": sessions, "files_removed": removed + media}


def due_for_deletion(db: Session, organization_id: int, now: Optional[datetime] = None, limit: int = 500) -> List[int]:
    now = now or datetime.utcnow()
    return [pid for (pid,) in db.query(ResumeMetadata.id).filter(
        ResumeMetadata.organization_id == organization_id, ResumeMetadata.retention_until.isnot(None),
        ResumeMetadata.retention_until < now).order_by(ResumeMetadata.retention_until).limit(limit)]


# ── File cleanup for every other way a profile row is deleted ───────────────

@event.listens_for(ResumeMetadata, "before_delete")
def _collect_talent_files(mapper, connection, target):
    session = object_session(target)
    if session is None or session.info.get("talent_skip_file_listener"):
        return
    try:
        files = _talent_files(connection, target.id)
    except Exception as exc:  # e.g. tables not migrated yet: never block a delete
        logger.warning("Could not list Talent Agent files of a deleted profile: %s", type(exc).__name__)
        return
    if files:
        session.info.setdefault("talent_files_to_remove", set()).update(files)


@event.listens_for(Session, "after_commit")
def _remove_collected_files(session):
    files = session.info.pop("talent_files_to_remove", None)
    if files:
        try:
            _remove_files(files)
        except Exception as exc:
            logger.warning("Talent Agent file cleanup failed: %s", type(exc).__name__)


@event.listens_for(Session, "after_rollback")
def _forget_collected_files(session):
    session.info.pop("talent_files_to_remove", None)
