"""
Processing a new CV version: extract text (out of process), parse, diff
against the previous version, apply to the profile, make it current.

Fields a recruiter typed in or the candidate confirmed are never overwritten
by a parse (resume_metadata.recruiter_fields / candidate_fields).
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy.orm import Session

from app.models.resume_metadata import ResumeMetadata
from app.models.talent import CvVersion
from app.services.resume_profile import json_list
from app.talent import cv
from app.talent.audit import audit

logger = logging.getLogger("app.talent.cv_pipeline")

# Profile columns a parse may write (JSON list columns are stored as text).
_JSON_COLUMNS = ("skills", "work_experience", "education", "certifications")
_SCALAR_COLUMNS = ("current_designation", "experience_years", "location", "summary")


def load(db: Session, version_id: str) -> Optional[dict]:
    version = db.get(CvVersion, uuid.UUID(version_id))
    if version is None:
        return None
    return {"id": str(version.id), "profile_id": version.profile_id, "source": version.source,
            "file_path": version.file_path, "parse_status": version.parse_status}


def diff(previous: Optional[dict], current: dict) -> dict:
    """What changed between two parsed versions (for recruiters and the audit)."""
    previous = previous or {}
    out: dict = {}
    for key in ("current_designation", "current_company", "experience_years", "location",
                "current_ctc", "expected_ctc", "notice_period_days"):
        if previous.get(key) != current.get(key) and current.get(key) is not None:
            out[key] = {"from": previous.get(key), "to": current.get(key)}

    def names(items, key=None):
        result = set()
        for item in items or []:
            value = item.get(key) if (key and isinstance(item, dict)) else item
            if isinstance(value, dict):
                value = value.get("name")
            if value:
                result.add(str(value).strip().lower())
        return result

    old_skills, new_skills = names(previous.get("skills"), "name"), names(current.get("skills"), "name")
    if old_skills != new_skills:
        out["skills"] = {"added": sorted(new_skills - old_skills), "removed": sorted(old_skills - new_skills)}
    old_jobs = {(str(w.get("company") or "").lower(), str(w.get("title") or "").lower())
                for w in previous.get("work_experience") or [] if isinstance(w, dict)}
    new_jobs = [w for w in current.get("work_experience") or []
                if isinstance(w, dict) and (str(w.get("company") or "").lower(), str(w.get("title") or "").lower()) not in old_jobs]
    if new_jobs:
        out["new_roles"] = [{"company": w.get("company"), "title": w.get("title")} for w in new_jobs][:10]
    return out


def apply_parse(db: Session, version_id: str, raw_text: Optional[str], parsed: dict, *,
                field_confidence: Optional[dict] = None, parse_confidence: Optional[float] = None,
                model: Optional[str] = None, prompt_version: Optional[str] = None) -> dict:
    version = db.get(CvVersion, uuid.UUID(version_id))
    if version is None or version.parse_status == "parsed":
        return {"status": "skipped"}
    profile = db.get(ResumeMetadata, version.profile_id)
    if profile is None:
        return {"status": "skipped"}
    previous = cv.current_version(db, profile.id)
    changes = diff(previous.parsed if previous else None, parsed)

    version.parsed, version.diff = parsed, changes
    version.field_confidence, version.parse_confidence = field_confidence, parse_confidence
    version.model, version.prompt_version = model, prompt_version
    version.parse_status = "parsed"
    cv.make_current(db, version)

    if version.source == "refresh_upload":
        owned = set(json_list(profile.recruiter_fields)) | set(json_list(profile.candidate_fields))
        if raw_text:
            profile.raw_text = raw_text
        profile.resume_file_path, profile.file_hash = version.file_path, version.file_hash
        for column in _SCALAR_COLUMNS:
            if column not in owned and parsed.get(column) not in (None, ""):
                setattr(profile, column, parsed[column])
        for column in _JSON_COLUMNS:
            if column not in owned and parsed.get(column):
                value = parsed[column]
                if column == "skills":
                    value = [s.get("name") if isinstance(s, dict) else s for s in value]
                setattr(profile, column, json.dumps(value))
        profile.parsed_at = datetime.utcnow()
    audit(db, actor="agent", action="cv.parsed", subject_type="cv_version", subject_id=version.id,
          organization_id=profile.organization_id, profile_id=profile.id,
          detail={"source": version.source, "changed": sorted(changes), "model": model,
                  "prompt_version": prompt_version, "parse_confidence": parse_confidence})
    db.commit()
    return {"status": "parsed", "changed": sorted(changes)}


def fast_parsed(fast: Optional[dict]) -> dict:
    """Map the regex parser's output onto the version schema."""
    fast = fast or {}

    def lst(v):
        return v if isinstance(v, list) else json_list(v)

    return {
        "current_designation": fast.get("current_designation"),
        "current_company": None,
        "experience_years": fast.get("experience_years"),
        "skills": [{"name": s} for s in lst(fast.get("skills"))],
        "work_experience": lst(fast.get("work_experience")),
        "education": lst(fast.get("education")),
        "certifications": lst(fast.get("certifications")),
        "location": fast.get("location"),
        "summary": fast.get("summary"),
    }


async def process(version_id: str) -> dict:
    """Parse one pending version. Text extraction runs in the extraction
    process pool; the parse is Claude's when configured (app.talent.parsing),
    else the regex parser's."""
    from app.agent.common import run_db
    from app.services.extraction_pool import extract_text_isolated

    info = await run_db(load, version_id)
    if info is None or info["parse_status"] != "pending":
        return {"status": "skipped"}

    if info["source"] != "refresh_upload" or not info["file_path"]:
        # A confirmation without a new file: the stored parse plus what the candidate confirmed.
        def _confirm(db: Session):
            profile = db.get(ResumeMetadata, info["profile_id"])
            if profile is None:
                return {"status": "skipped"}
            return apply_parse(db, version_id, None, cv.snapshot(profile))

        return await run_db(_confirm)

    from app.media import resolve_upload_file

    full = resolve_upload_file(info["file_path"], ["resumes"])
    if not full:
        await run_db(_mark_failed, version_id, "file missing")
        return {"status": "failed"}
    text, fast = await extract_text_isolated(full, fast_parse=True)
    if not text or len(text.strip()) < 50:
        await run_db(_mark_failed, version_id, "no readable text")
        return {"status": "failed"}

    from app.talent import parsing

    result = await parsing.parse_cv(text, previous=await run_db(_previous_parsed, info["profile_id"]),
                                    profile_id=info["profile_id"])
    if result is None:
        return await run_db(apply_parse, version_id, text, fast_parsed(fast))
    return await run_db(apply_parse, version_id, text, result.parsed, field_confidence=result.field_confidence,
                        parse_confidence=result.confidence, model=result.model, prompt_version=result.prompt_version)


def _previous_parsed(db: Session, profile_id: int) -> Optional[dict]:
    current = cv.current_version(db, profile_id)
    return current.parsed if current else None


def _mark_failed(db: Session, version_id: str, reason: str) -> None:
    version = db.get(CvVersion, uuid.UUID(version_id))
    if version is not None:
        version.parse_status = "failed"
        audit(db, actor="agent", action="cv.parse_failed", subject_type="cv_version", subject_id=version.id,
              profile_id=version.profile_id, detail={"reason": reason})
        db.commit()


def pending_versions(db: Session, older_than_minutes: int = 30, limit: int = 200) -> list:
    """Versions whose processing event was lost — picked up by the sweep."""
    from datetime import timedelta

    cutoff = datetime.utcnow() - timedelta(minutes=older_than_minutes)
    return [str(v) for (v,) in db.query(CvVersion.id).filter(
        CvVersion.parse_status == "pending", CvVersion.received_at < cutoff).limit(limit)]
