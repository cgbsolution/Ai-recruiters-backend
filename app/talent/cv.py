"""
CV versions: every CV a profile has had, with its parse, per-field confidence,
the diff against the version before, and (in cv_versions.embedding) its vector.

Exactly one version per profile is current (partial unique index). A new
version becomes current once it is parsed; until then matching keeps using
the previous one.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Optional

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.models.resume_metadata import ResumeMetadata
from app.models.talent import CvVersion
from app.talent.settings import get_settings


def _json(value):
    if value in (None, ""):
        return None
    if isinstance(value, (list, dict)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return None


def snapshot(profile: ResumeMetadata) -> dict:
    """The structured profile as stored today (the 'parsed' of a backfilled version)."""
    return {
        "current_designation": profile.current_designation,
        "current_company": profile.current_company,
        "experience_years": profile.experience_years,
        "skills": _json(profile.skills) or [],
        "work_experience": _json(profile.work_experience) or [],
        "education": _json(profile.education) or [],
        "certifications": _json(profile.certifications) or [],
        "location": profile.location,
        "summary": profile.summary,
        "current_ctc": profile.current_ctc,
        "expected_ctc": profile.expected_ctc,
        "notice_period_days": profile.notice_period_days,
        "open_to_relocate": profile.open_to_relocate,
    }


def retention_date(db: Session, profile: ResumeMetadata, start: Optional[datetime] = None) -> datetime:
    months = int(get_settings(db, profile.organization_id).retention_months or 24)
    return (start or datetime.utcnow()) + timedelta(days=30 * months)


def current_version(db: Session, profile_id: int) -> Optional[CvVersion]:
    return db.query(CvVersion).filter(CvVersion.profile_id == profile_id, CvVersion.is_current.is_(True)).first()


def ensure_baseline(db: Session, profile: ResumeMetadata) -> CvVersion:
    """Version 1 for a profile that predates the agent: the stored parse."""
    existing = current_version(db, profile.id)
    if existing is not None:
        return existing
    any_version = db.query(CvVersion).filter(CvVersion.profile_id == profile.id).order_by(CvVersion.version_no.desc()).first()
    if any_version is not None:
        return any_version
    version = CvVersion(
        profile_id=profile.id, version_no=1, is_current=True, file_path=profile.resume_file_path,
        file_hash=profile.file_hash, source="backfill", parse_status="parsed", parsed=snapshot(profile),
        received_at=profile.created_at or datetime.utcnow(),
        retention_until=profile.retention_until or retention_date(db, profile, profile.created_at),
    )
    db.add(version)
    db.flush()
    return version


def new_version(db: Session, profile: ResumeMetadata, *, source: str, file_path: Optional[str] = None,
                file_hash: Optional[str] = None) -> CvVersion:
    ensure_baseline(db, profile)
    last = db.query(sa.func.max(CvVersion.version_no)).filter(CvVersion.profile_id == profile.id).scalar() or 0
    version = CvVersion(
        profile_id=profile.id, version_no=last + 1, is_current=False, file_path=file_path, file_hash=file_hash,
        source=source, parse_status="pending", received_at=datetime.utcnow(),
        retention_until=retention_date(db, profile),
    )
    db.add(version)
    db.flush()
    return version


def make_current(db: Session, version: CvVersion) -> None:
    db.query(CvVersion).filter(CvVersion.profile_id == version.profile_id, CvVersion.id != version.id,
                               CvVersion.is_current.is_(True)).update({"is_current": False}, synchronize_session=False)
    db.flush()
    version.is_current = True
    db.flush()
