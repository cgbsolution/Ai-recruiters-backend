"""
Talent Agent tables (app.talent / app.agent).

The talent pool is `resume_metadata` (one row per person per organization);
every table here that talks about "a candidate" points at it as `profile_id`,
with ON DELETE CASCADE so deleting a profile anywhere removes its consent,
CV versions, outreach, matches and interviews with it. (`candidates` is a
per-job application row and is only linked from job_matches.)

Times are naive UTC, like the rest of the schema.

The embedding columns (cv_versions.embedding, talent_job_settings.embedding)
are deliberately NOT mapped: their type is `vector(1024)` when pgvector is
installed and `double precision[]` otherwise, so they are created and read by
app.talent.vectors with raw SQL.
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Column,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    PrimaryKeyConstraint,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB

from app.database import Base

# JSONB on Postgres, plain JSON elsewhere (tests/conftest.py builds every
# model on SQLite).
JsonB = JSON().with_variant(JSONB(), "postgresql")


def _uuid_pk():
    return Column(Uuid, primary_key=True, default=uuid.uuid4, server_default=text("gen_random_uuid()"))


class TalentSettings(Base):
    """Per-organization switches and limits. Missing row = defaults from
    app.talent.config (every loop OFF)."""

    __tablename__ = "talent_settings"

    id = Column(Integer, primary_key=True)
    organization_id = Column(
        Integer, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    # {"refresh": bool, "match": bool, "invite": bool, "interview": bool, "live_voice": bool}
    flags = Column(JsonB, nullable=False, default=dict)
    timezone = Column(String(64), nullable=False, default="Asia/Kolkata")
    quiet_hours_start = Column(Integer, nullable=False, default=21)  # local hour sending stops
    quiet_hours_end = Column(Integer, nullable=False, default=9)  # local hour sending resumes
    refresh_after_days = Column(Integer, nullable=False, default=180)
    retention_months = Column(Integer, nullable=False, default=24)
    default_autonomy = Column(String(10), nullable=False, default="copilot")
    sender_domain = Column(String(255), nullable=True)
    # Text shown on the candidate pages next to each consent checkbox.
    consent_text = Column(Text, nullable=True)
    updated_by = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class ContactConsent(Base):
    """Whether a profile may be contacted on a channel for a purpose.
    Only app.talent.consent reads this to decide a send."""

    __tablename__ = "contact_consent"
    __table_args__ = (PrimaryKeyConstraint("profile_id", "channel", "purpose"),)

    profile_id = Column(Integer, ForeignKey("resume_metadata.id", ondelete="CASCADE"), nullable=False)
    channel = Column(String(16), nullable=False)  # email | whatsapp | sms
    purpose = Column(String(16), nullable=False)  # refresh | jobs | interview
    status = Column(String(16), nullable=False)  # granted | withdrawn | denied
    # Where the consent came from: "candidate_page", "application_form",
    # "import:<label>" … plus the lawful basis an admin recorded for imports.
    source = Column(String(120), nullable=True)
    basis = Column(Text, nullable=True)
    recorded_by = Column(String(80), nullable=True)  # user:<id> | candidate | system
    granted_at = Column(DateTime, nullable=True)
    withdrawn_at = Column(DateTime, nullable=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class SuppressionEntry(Base):
    """Global do-not-contact list. Holds only a keyed hash of the normalized
    address (app.talent.identity.suppression_hash), never the address."""

    __tablename__ = "suppression_list"

    value_hash = Column(String(64), primary_key=True)
    kind = Column(String(10), nullable=False)  # email | phone
    reason = Column(String(40), nullable=False)  # unsubscribe | complaint | hard_bounce | withdrawn | erased | admin
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class CvVersion(Base):
    __tablename__ = "cv_versions"
    __table_args__ = (
        UniqueConstraint("profile_id", "version_no", name="uq_cv_versions_profile_version"),
        Index(
            "uq_cv_versions_current", "profile_id", unique=True,
            postgresql_where=text("is_current"), sqlite_where=text("is_current"),
        ),
    )

    id = _uuid_pk()
    profile_id = Column(Integer, ForeignKey("resume_metadata.id", ondelete="CASCADE"), nullable=False, index=True)
    version_no = Column(Integer, nullable=False)
    is_current = Column(Boolean, nullable=False, default=False)
    # Relative path under the upload root ("resumes/…"); NULL when the version
    # came from a confirmation without a new file.
    file_path = Column(String(500), nullable=True)
    file_hash = Column(String(64), nullable=True)
    source = Column(String(30), nullable=False)  # backfill | refresh_upload | refresh_confirm | reparse
    # staged (uploaded, page not submitted) | pending | parsed | failed | skipped
    parse_status = Column(String(16), nullable=False, default="pending")
    parsed = Column(JsonB, nullable=True)
    field_confidence = Column(JsonB, nullable=True)
    parse_confidence = Column(Float, nullable=True)
    diff = Column(JsonB, nullable=True)  # changes against the previous version
    model = Column(String(80), nullable=True)
    prompt_version = Column(String(40), nullable=True)
    embedding_model = Column(String(80), nullable=True)
    embedded_text_hash = Column(String(64), nullable=True)
    embedded_at = Column(DateTime, nullable=True)
    received_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    retention_until = Column(DateTime, nullable=True)


class Campaign(Base):
    """A stream of messages. kind=refresh: one per org per cycle
    (cycle_key "2026-10"); invite / interview / decision: one per job."""

    __tablename__ = "campaigns"
    __table_args__ = (
        UniqueConstraint("organization_id", "kind", "cycle_key", name="uq_campaigns_org_kind_cycle"),
    )

    id = _uuid_pk()
    organization_id = Column(Integer, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True)
    kind = Column(String(16), nullable=False)  # refresh | invite | interview | decision
    cycle_key = Column(String(64), nullable=False)
    name = Column(String(255), nullable=False)
    job_id = Column(Integer, ForeignKey("jobs.id", ondelete="CASCADE"), nullable=True, index=True)
    segment = Column(JsonB, nullable=True)
    status = Column(String(16), nullable=False, default="active")  # active | paused | completed | cancelled
    daily_cap = Column(Integer, nullable=True)  # optional per-campaign cap under the domain cap
    paused_reason = Column(Text, nullable=True)
    paused_at = Column(DateTime, nullable=True)
    created_by = Column(String(80), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class CampaignMember(Base):
    __tablename__ = "campaign_members"
    __table_args__ = (
        PrimaryKeyConstraint("campaign_id", "profile_id"),
        Index("ix_campaign_members_profile_status", "profile_id", "status"),
        Index("ix_campaign_members_campaign_status", "campaign_id", "status"),
    )

    campaign_id = Column(Uuid, ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False)
    profile_id = Column(Integer, ForeignKey("resume_metadata.id", ondelete="CASCADE"), nullable=False)
    # queued | in_progress | responded | no_response | opted_out | bounced | skipped | cancelled
    status = Column(String(16), nullable=False, default="queued")
    status_reason = Column(String(80), nullable=True)
    priority = Column(Integer, nullable=False, default=0)
    enrolled_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    last_event_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)


class Touch(Base):
    """One outbound message. The unique key makes every send idempotent:
    app.talent.outbox inserts the row first and only the caller that moves it
    queued → sending may call a transport."""

    __tablename__ = "touches"
    __table_args__ = (
        UniqueConstraint("profile_id", "campaign_id", "touch_no", name="uq_touches_profile_campaign_no"),
        Index("ix_touches_campaign_status", "campaign_id", "status"),
        Index("ix_touches_domain_sent", "sender_domain", "sent_at"),
    )

    id = _uuid_pk()
    profile_id = Column(Integer, ForeignKey("resume_metadata.id", ondelete="CASCADE"), nullable=False, index=True)
    campaign_id = Column(Uuid, ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False)
    touch_no = Column(Integer, nullable=False)
    channel = Column(String(16), nullable=False)
    purpose = Column(String(16), nullable=False)
    template = Column(String(60), nullable=False)
    # queued | sending | sent | failed | skipped | unknown | bounced | complained
    status = Column(String(16), nullable=False, default="queued")
    skip_reason = Column(String(60), nullable=True)
    sender_domain = Column(String(255), nullable=True)
    provider_message_id = Column(String(255), nullable=True, index=True)
    error = Column(Text, nullable=True)
    attempts = Column(Integer, nullable=False, default=0, server_default="0")
    claimed_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    sent_at = Column(DateTime, nullable=True)
    opened_at = Column(DateTime, nullable=True)
    responded_at = Column(DateTime, nullable=True)
    bounced_at = Column(DateTime, nullable=True)
    complained_at = Column(DateTime, nullable=True)


class SenderDomain(Base):
    """Daily cap ramp per sending domain: [{"from_day": 0, "cap": 500}, …]."""

    __tablename__ = "sender_domains"

    domain = Column(String(255), primary_key=True)
    ramp = Column(JsonB, nullable=False)
    ramp_started_on = Column(Date, nullable=False)
    paused = Column(Boolean, nullable=False, default=False)
    paused_reason = Column(Text, nullable=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class SendCounter(Base):
    """Sends reserved per domain per (sender-local) day. Reserved with one
    conditional UPDATE, so concurrent senders can never exceed the cap."""

    __tablename__ = "send_counters"
    __table_args__ = (PrimaryKeyConstraint("domain", "day"),)

    domain = Column(String(255), nullable=False)
    day = Column(Date, nullable=False)
    total = Column(Integer, nullable=False, default=0)
    bulk = Column(Integer, nullable=False, default=0)


class DeliverabilityEvent(Base):
    __tablename__ = "deliverability_events"

    id = Column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    touch_id = Column(Uuid, ForeignKey("touches.id", ondelete="SET NULL"), nullable=True, index=True)
    campaign_id = Column(Uuid, ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=True, index=True)
    sender_domain = Column(String(255), nullable=True)
    kind = Column(String(20), nullable=False)  # delivered | bounce_hard | bounce_soft | complaint | unsubscribe
    provider = Column(String(30), nullable=True)
    provider_event_id = Column(String(255), nullable=True, unique=True)
    occurred_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class AccessToken(Base):
    """Magic-link tokens for candidate pages. Only the SHA-256 of the token is
    stored; see app.talent.tokens."""

    __tablename__ = "talent_access_tokens"

    id = _uuid_pk()
    token_hash = Column(String(64), nullable=False, unique=True)
    purpose = Column(String(20), nullable=False)  # refresh | invite | interview | my_data | unsubscribe
    profile_id = Column(Integer, ForeignKey("resume_metadata.id", ondelete="CASCADE"), nullable=False, index=True)
    ref_id = Column(String(64), nullable=True)  # touch / match / interview id the link belongs to
    single_use = Column(Boolean, nullable=False, default=False)
    expires_at = Column(DateTime, nullable=False)
    used_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class TalentJobSettings(Base):
    __tablename__ = "talent_job_settings"

    job_id = Column(Integer, ForeignKey("jobs.id", ondelete="CASCADE"), primary_key=True)
    autonomy_mode = Column(String(10), nullable=False, default="copilot")  # autopilot | copilot
    top_k = Column(Integer, nullable=False, default=25)
    interview_mode = Column(String(20), nullable=False, default="async_text")  # async_text | async_voice | live_voice
    question_count = Column(Integer, nullable=False, default=10)
    time_limit_minutes = Column(Integer, nullable=False, default=30)
    slot_minutes = Column(Integer, nullable=False, default=45)
    budget_max_ctc = Column(Float, nullable=True)  # LPA
    max_notice_days = Column(Integer, nullable=True)
    relocation_ok = Column(Boolean, nullable=False, default=True)
    locations = Column(JsonB, nullable=True)  # ["Bengaluru", "Remote"]
    must_haves = Column(JsonB, nullable=True)  # ["5+ years Python", …]
    nice_to_haves = Column(JsonB, nullable=True)
    rubric = Column(JsonB, nullable=True)  # [{"criterion", "description", "weight"}]
    panel_user_ids = Column(JsonB, nullable=True)  # users whose Google calendars are offered
    # Sort order from app.talent.scoring.build_shortlist: [{"match_id", "composite", "rank"}]
    shortlist = Column(JsonB, nullable=True)
    match_status = Column(String(20), nullable=True)  # idle | running | done | failed
    match_error = Column(Text, nullable=True)
    last_matched_at = Column(DateTime, nullable=True)
    embedding_model = Column(String(80), nullable=True)
    embedded_text_hash = Column(String(64), nullable=True)
    embedded_at = Column(DateTime, nullable=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class JobMatch(Base):
    __tablename__ = "job_matches"
    __table_args__ = (
        UniqueConstraint("job_id", "profile_id", name="uq_job_matches_job_profile"),
        Index("ix_job_matches_job_status", "job_id", "status"),
    )

    id = _uuid_pk()
    job_id = Column(Integer, ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False, index=True)
    profile_id = Column(Integer, ForeignKey("resume_metadata.id", ondelete="CASCADE"), nullable=False, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=True, index=True)
    stage1_score = Column(Float, nullable=True)
    vector_score = Column(Float, nullable=True)
    ai_score = Column(Integer, nullable=True)  # server-computed composite (app.talent.rerank)
    model_score = Column(Integer, nullable=True)  # the model's own 0-100
    dimension_scores = Column(JsonB, nullable=True)
    must_haves = Column(JsonB, nullable=True)
    gaps = Column(JsonB, nullable=True)
    strengths = Column(JsonB, nullable=True)
    evidence = Column(JsonB, nullable=True)
    risk_flags = Column(JsonB, nullable=True)
    fields_sent = Column(JsonB, nullable=True)  # exactly which profile fields reached the prompt
    # ranked | needs_refresh | approval_pending | invited | declined | scheduled
    # | interviewed | shortlisted | submitted | closed
    status = Column(String(20), nullable=False, default="ranked")
    status_reason = Column(String(120), nullable=True)
    rank_input_hash = Column(String(64), nullable=True)
    model = Column(String(80), nullable=True)
    prompt_version = Column(String(40), nullable=True)
    # talent_approvals.id of the recruiter decision (no FK: the approval row
    # points back at this match, and SQLite cannot build a cycle).
    invite_approval_id = Column(Uuid, nullable=True)
    invited_at = Column(DateTime, nullable=True)
    responded_at = Column(DateTime, nullable=True)
    response = Column(String(20), nullable=True)  # interested | not_interested
    confirmed = Column(JsonB, nullable=True)  # CTC / notice the candidate confirmed on the invite page
    application_id = Column(Integer, ForeignKey("candidates.id", ondelete="SET NULL"), nullable=True)
    # A recruiter's judgement of the match, for the precision@10 report.
    recruiter_rating = Column(String(16), nullable=True)  # relevant | not_relevant
    rated_by = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    rated_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class TalentApproval(Base):
    """A recorded human decision. The send/submit gates in app.talent.approvals
    refuse to act without an approved row decided by a user."""

    __tablename__ = "talent_approvals"

    id = _uuid_pk()
    kind = Column(String(20), nullable=False)  # invite | submit | reject_message
    organization_id = Column(Integer, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=True, index=True)
    job_id = Column(Integer, ForeignKey("jobs.id", ondelete="CASCADE"), nullable=True, index=True)
    match_id = Column(Uuid, ForeignKey("job_matches.id", ondelete="CASCADE"), nullable=True, index=True)
    status = Column(String(12), nullable=False, default="pending")  # pending | approved | declined
    requested_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    decided_by = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    decided_at = Column(DateTime, nullable=True)
    note = Column(Text, nullable=True)


class TalentInterview(Base):
    __tablename__ = "talent_interviews"

    id = _uuid_pk()
    match_id = Column(Uuid, ForeignKey("job_matches.id", ondelete="CASCADE"), nullable=False, unique=True)
    mode = Column(String(20), nullable=False)  # async_text | async_voice | live_voice
    # offered | scheduled | in_progress | completed | scored | no_show | human_requested | cancelled | expired
    status = Column(String(20), nullable=False, default="offered")
    scheduled_at = Column(DateTime, nullable=True)
    slot_ends_at = Column(DateTime, nullable=True)
    deadline_at = Column(DateTime, nullable=True)
    reschedule_count = Column(Integer, nullable=False, default=0)
    no_show_count = Column(Integer, nullable=False, default=0)
    needs_recruiter = Column(Boolean, nullable=False, default=False)
    calendar_owner_user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    calendar_event_id = Column(String(255), nullable=True)
    meeting_link = Column(String(500), nullable=True)
    ai_disclosed_at = Column(DateTime, nullable=True)
    consent_recorded_at = Column(DateTime, nullable=True)
    human_requested_at = Column(DateTime, nullable=True)
    started_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)
    questions = Column(JsonB, nullable=True)  # [{"id", "text", "source", "must_have"}]
    transcript = Column(JsonB, nullable=True)  # [{"question_id", "kind", "text", "audio_path", "at"}]
    recording_paths = Column(JsonB, nullable=True)
    scores = Column(JsonB, nullable=True)  # [{"criterion", "score", "evidence", "confidence", "override"}]
    overall_score = Column(Float, nullable=True)
    summary = Column(Text, nullable=True)
    integrity_observations = Column(JsonB, nullable=True)
    human_review_status = Column(String(20), nullable=True)  # pending | reviewed
    reviewed_by = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    reviewed_at = Column(DateTime, nullable=True)
    model = Column(String(80), nullable=True)
    prompt_version = Column(String(40), nullable=True)
    legacy_session_id = Column(Integer, ForeignKey("interview_sessions.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class InterviewBooking(Base):
    """A calendar owner's slot held/booked for an interview. Bookings are made
    under a per-owner advisory lock with an overlap check (app.talent.scheduling);
    the partial unique index is a second line of defence."""

    __tablename__ = "talent_interview_bookings"
    __table_args__ = (
        Index(
            "uq_talent_bookings_owner_start", "owner_user_id", "starts_at", unique=True,
            postgresql_where=text("status = 'booked'"), sqlite_where=text("status = 'booked'"),
        ),
    )

    id = _uuid_pk()
    interview_id = Column(Uuid, ForeignKey("talent_interviews.id", ondelete="CASCADE"), nullable=False, index=True)
    owner_user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True)
    starts_at = Column(DateTime, nullable=False)
    ends_at = Column(DateTime, nullable=False)
    status = Column(String(12), nullable=False, default="offered")  # offered | booked | released
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class TalentBatchRun(Base):
    """An Anthropic Message Batch (or embedding backfill) the agent started."""

    __tablename__ = "talent_batch_runs"

    id = _uuid_pk()
    organization_id = Column(Integer, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=True)
    kind = Column(String(20), nullable=False)  # parse | embed
    provider_batch_id = Column(String(255), nullable=True, unique=True)
    status = Column(String(20), nullable=False, default="created")
    total = Column(Integer, nullable=False, default=0)
    succeeded = Column(Integer, nullable=False, default=0)
    failed = Column(Integer, nullable=False, default=0)
    created_by = Column(String(80), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    ended_at = Column(DateTime, nullable=True)


class AuditLog(Base):
    """Append-only record of every outbound message, AI score, human decision
    and deletion. Never holds PII: ids, hashes, counts and field names only."""

    __tablename__ = "audit_log"
    __table_args__ = (
        Index("ix_audit_log_subject", "subject_type", "subject_id"),
        Index("ix_audit_log_org_at", "organization_id", "at"),
    )

    id = Column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)
    organization_id = Column(Integer, nullable=True)
    profile_id = Column(Integer, nullable=True, index=True)  # kept after erasure on purpose: an id is not PII
    actor = Column(String(80), nullable=False)  # agent | user:<id> | candidate | system
    action = Column(String(80), nullable=False, index=True)
    subject_type = Column(String(40), nullable=False)
    subject_id = Column(String(64), nullable=True)
    detail = Column(JsonB, nullable=True)
