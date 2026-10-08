"""
Central resume metadata store.

Every uploaded resume is parsed once and its structured data stored here.
Keyed by candidate email — if the same person uploads again, the record is
updated (not duplicated).  This avoids repeated LLM parsing and enables
fast cross-job candidate matching.
"""

import json
from datetime import datetime

from sqlalchemy import (
    Boolean, Column, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint, event, inspect,
)
from sqlalchemy.orm import Session, relationship

from app.database import Base


class ResumeMetadata(Base):
    __tablename__ = "resume_metadata"
    __table_args__ = (
        UniqueConstraint("candidate_email", "organization_id", name="uq_resume_email_org"),
    )

    id = Column(Integer, primary_key=True, index=True)

    # ── Identity (unique per person per org) ─────────────────────────────────
    organization_id = Column(Integer, ForeignKey("organizations.id"), nullable=True, index=True)
    candidate_email = Column(String(255), nullable=False, index=True)
    candidate_name = Column(String(255), nullable=True)
    phone = Column(String(50), nullable=True)

    # ── Professional ──────────────────────────────────────────────────────────
    current_designation = Column(String(255), nullable=True)
    experience_years = Column(Float, nullable=True)
    skills = Column(Text, nullable=True)           # JSON array: ["Python", "React", ...]
    education = Column(Text, nullable=True)         # JSON array: ["B.Tech CS, XYZ Univ", ...]
    certifications = Column(Text, nullable=True)    # JSON array: ["AWS SAA", ...]
    work_experience = Column(Text, nullable=True)   # JSON array of objects

    # ── Personal / Location ───────────────────────────────────────────────────
    location = Column(String(255), nullable=True)
    annual_package = Column(Float, nullable=True)    # Annual package / CTC in LPA
    personal_details = Column(Text, nullable=True)  # JSON: {dob, gender, languages, ...}

    # ── Search / Matching ─────────────────────────────────────────────────────
    keywords = Column(Text, nullable=True)          # JSON array of extracted keywords
    summary = Column(Text, nullable=True)           # 2-3 sentence professional summary

    # ── File Reference ────────────────────────────────────────────────────────
    resume_file_path = Column(String(500), nullable=True)
    raw_text = Column(Text, nullable=True)          # full extracted text (for re-matching)
    # SHA-256 of the uploaded bytes: re-uploading the identical file replaces
    # the older copy instead of adding a second record (bulk upload only).
    file_hash = Column(String(64), nullable=True, index=True)

    # ── Bulk upload bookkeeping ───────────────────────────────────────────────
    # Lets a restarted server re-queue unfinished rows and still report
    # progress for a batch whose in-memory counters were lost.
    batch_id = Column(String(32), nullable=True, index=True)
    parse_mode = Column(String(10), nullable=True)   # "fast" | "ai"
    # fast_resume_parser.FAST_PARSER_VERSION that produced a "fast" parse: an
    # identical re-upload reuses the stored parse only if it is current.
    parser_version = Column(Integer, nullable=True)

    # ── Status ────────────────────────────────────────────────────────────────
    parsing_status = Column(
        String(20), nullable=False, default="pending"
    )  # "pending" | "processing" | "completed" | "failed"
    parsing_error = Column(Text, nullable=True)

    # ── Recruiter workflow ────────────────────────────────────────────────────
    # "new" | "shortlisted" | "contacted" | "interviewing" | "on_hold" | "rejected"
    pipeline_status = Column(String(20), nullable=False, default="new", server_default="new", index=True)
    # Where the resume came from: "bulk_upload" | "application" | "import"
    source = Column(String(20), nullable=True)

    # ── Derived for search (app.services.resume_profile — never edit directly) ─
    # Kept in sync by the before_flush hook below; filled for older rows by
    # resume_profile.backfill_profiles() at startup.
    current_company = Column(String(255), nullable=True)
    city = Column(String(100), nullable=True, index=True)
    highest_education = Column(String(20), nullable=True)
    parse_quality = Column(Integer, nullable=True)
    skills_index = Column(Text, nullable=True)       # "|Python|Django|AWS|"
    certs_index = Column(Text, nullable=True)
    search_text = Column(Text, nullable=True)        # lowercase, for keyword search
    # Stated in the resume text (NULL when it does not say)
    current_ctc = Column(Float, nullable=True)       # LPA; annual_package (typed in) wins
    expected_ctc = Column(Float, nullable=True)      # LPA
    notice_period_days = Column(Integer, nullable=True)  # 0 = immediate joiner
    open_to_relocate = Column(Boolean, nullable=True)
    derived_version = Column(Integer, nullable=True)
    # JSON list of fields whose current value a recruiter typed in (the
    # profile shows "Recruiter" instead of "From CV"). Maintained by the hook
    # below: a recruiter edit adds the field, any later CV write takes it back.
    recruiter_fields = Column(Text, nullable=True)

    # ── Timestamps ────────────────────────────────────────────────────────────
    parsed_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


# Fields a recruiter can set by hand (PATCH /resume-database/{id}).
RECRUITER_EDITABLE = (
    "candidate_name", "phone", "current_designation", "experience_years", "skills", "location",
    "annual_package", "expected_ctc", "notice_period_days", "open_to_relocate",
)


def mark_recruiter_edit(session) -> None:
    """Call before committing a recruiter's manual edit: the fields it changes
    are then recorded as recruiter-provided rather than read from the CV."""
    session.info["recruiter_edit"] = True


@event.listens_for(Session, "before_flush")
def _refresh_derived_resume_fields(session, flush_context, instances):
    """Every ORM write of a ResumeMetadata row — bulk upload, re-parse, edit,
    candidate application — refreshes its derived search columns, so no write
    path can leave the search page's filters stale."""
    from app.services.resume_profile import PROFILE_VERSION, derive_profile, json_list

    recruiter_edit = session.info.pop("recruiter_edit", False)
    with session.no_autoflush:
        for obj in list(session.new) + list(session.dirty):
            if not isinstance(obj, ResumeMetadata) or obj in session.deleted:
                continue
            state = inspect(obj)
            if not state.pending:
                changed = {f for f in RECRUITER_EDITABLE if state.attrs[f].history.has_changes()}
                if changed:
                    owned = set(json_list(obj.recruiter_fields))
                    if recruiter_edit:
                        # A value a recruiter clears is no longer theirs: the CV may fill it again.
                        cleared = {f for f in changed if getattr(obj, f) is None}
                        owned = (owned | changed) - cleared
                    else:
                        owned -= changed
                    obj.recruiter_fields = json.dumps(sorted(owned)) if owned else None
            if state.pending and obj.source is None:
                # Bulk upload and imports say so explicitly; the other writers
                # (candidate applications, the public apply form) are applications.
                obj.source = "bulk_upload" if obj.batch_id else "application"
            text_changed = (
                state.pending
                or state.attrs.raw_text.history.has_changes()
                or (obj.derived_version or 0) < PROFILE_VERSION
            )
            derive_profile(obj, text_changed=text_changed)
