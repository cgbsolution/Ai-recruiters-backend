"""
What the candidate sees on their "my data" page, and the full export.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from app.models.job import Job
from app.models.organization import Organization
from app.models.resume_metadata import ResumeMetadata
from app.models.talent import AuditLog, CvVersion, JobMatch, TalentInterview, Touch
from app.talent.consent import consent_summary


def _plain(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    if hasattr(value, "hex") and not isinstance(value, (str, bytes)):
        return str(value)
    return value


def _json(text):
    try:
        return json.loads(text) if text else None
    except (TypeError, ValueError):
        return text


def summary(db: Session, profile: ResumeMetadata) -> dict:
    org = db.get(Organization, profile.organization_id)
    versions = db.query(CvVersion).filter(CvVersion.profile_id == profile.id).order_by(CvVersion.version_no).all()
    touches = db.query(Touch).filter(Touch.profile_id == profile.id, Touch.sent_at.isnot(None)) \
        .order_by(Touch.sent_at.desc()).limit(50).all()
    matches = (
        db.query(JobMatch, Job.title).join(Job, Job.id == JobMatch.job_id)
        .filter(JobMatch.profile_id == profile.id, JobMatch.status != "ranked").all()
    )
    interviews = (
        db.query(TalentInterview, Job.title).join(JobMatch, JobMatch.id == TalentInterview.match_id)
        .join(Job, Job.id == JobMatch.job_id).filter(JobMatch.profile_id == profile.id).all()
    )
    return _plain({
        "organization": org.name if org else "",
        "profile": {
            "name": profile.candidate_name, "email": profile.candidate_email, "phone": profile.phone,
            "current_designation": profile.current_designation, "current_company": profile.current_company,
            "location": profile.location, "current_ctc": profile.current_ctc, "expected_ctc": profile.expected_ctc,
            "notice_period_days": profile.notice_period_days, "job_search_status": profile.job_search_status,
            "last_verified_at": profile.last_verified_at, "keep_until": profile.retention_until,
        },
        "consents": consent_summary(db, profile.id),
        "cv_versions": [{"version": v.version_no, "received_at": v.received_at, "source": v.source,
                         "current": v.is_current} for v in versions],
        "messages": [{"kind": t.template, "channel": t.channel, "sent_at": t.sent_at} for t in touches],
        "applications": [{"job": title, "status": m.status, "updated_at": m.updated_at} for m, title in matches],
        "interviews": [{"job": title, "mode": i.mode, "status": i.status, "completed_at": i.completed_at,
                        "recorded": bool(i.recording_paths)} for i, title in interviews],
    })


def full_export(db: Session, profile: ResumeMetadata) -> dict:
    columns = {c.name: getattr(profile, c.key) for c in ResumeMetadata.__table__.columns
               if c.name not in ("search_text", "skills_index", "certs_index", "derived_version", "batch_id")}
    for key in ("skills", "education", "certifications", "work_experience", "personal_details", "keywords",
                "recruiter_fields", "candidate_fields"):
        columns[key] = _json(columns.get(key))
    data = summary(db, profile)
    data["profile_record"] = columns
    data["cv_versions"] = [
        {"version": v.version_no, "received_at": v.received_at, "source": v.source, "parsed": v.parsed,
         "field_confidence": v.field_confidence, "changes": v.diff}
        for v in db.query(CvVersion).filter(CvVersion.profile_id == profile.id).order_by(CvVersion.version_no)
    ]
    data["matches"] = [
        {"job_id": m.job_id, "status": m.status, "ai_score": m.ai_score, "must_haves": m.must_haves,
         "gaps": m.gaps, "strengths": m.strengths, "risk_flags": m.risk_flags, "created_at": m.created_at}
        for m in db.query(JobMatch).filter(JobMatch.profile_id == profile.id)
    ]
    data["interview_records"] = [
        {"mode": i.mode, "status": i.status, "questions": i.questions, "transcript": i.transcript,
         "scores": i.scores, "summary": i.summary, "completed_at": i.completed_at}
        for i in db.query(TalentInterview).join(JobMatch, JobMatch.id == TalentInterview.match_id)
        .filter(JobMatch.profile_id == profile.id)
    ]
    data["activity_log"] = [
        {"at": a.at, "action": a.action, "actor": a.actor.split(":")[0]}
        for a in db.query(AuditLog).filter(AuditLog.profile_id == profile.id).order_by(AuditLog.at)
    ]
    return _plain(data)
