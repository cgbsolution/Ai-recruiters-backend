"""
Startup script: a BACKSTOP that ensures required DB tables/columns exist.

Alembic owns the schema and runs first (see the Dockerfile CMD); this script
only patches over columns that older deployments created outside Alembic. Keep
every type and default here identical to the migration and the SQLAlchemy
model that define the column — when this ran *before* Alembic it silently won
the race, and the narrower types it created (interview_timezone VARCHAR(50),
joining_flexibility VARCHAR(20)) became production's real schema.
"""
import logging
import time
from sqlalchemy import text
from app.database import engine

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("startup")

# A fixed advisory-lock key so concurrent processes/containers (rolling deploys,
# multiple workers) SERIALIZE their schema migrations instead of deadlocking on
# ALTER TABLE locks. Session-level lock auto-releases if a process dies.
_MIGRATION_LOCK_KEY = 727274


def _autocommit_conn():
    """Connection where each statement is its own transaction — so one failed
    DDL can't abort (and cascade-fail) the rest of the batch."""
    return engine.connect().execution_options(isolation_level="AUTOCOMMIT")


def _exec_safe(conn, sql: str, label: str, retries: int = 4) -> bool:
    """Execute one DDL statement, retrying transient deadlocks. Never raises."""
    for attempt in range(1, retries + 1):
        try:
            conn.execute(text(sql))
            return True
        except Exception as e:
            if "deadlock" in str(e).lower() and attempt < retries:
                logger.warning("%s: deadlock — retry %d/%d", label, attempt, retries)
                time.sleep(0.5 * attempt)
                continue
            logger.warning("%s: %s", label, e)
            return False
    return False


def _advisory_lock(conn) -> bool:
    try:
        conn.execute(text("SELECT pg_advisory_lock(:k)").bindparams(k=_MIGRATION_LOCK_KEY))
        return True
    except Exception as e:
        logger.warning("advisory lock unavailable (continuing without it): %s", e)
        return False


def _advisory_unlock(conn) -> None:
    try:
        conn.execute(text("SELECT pg_advisory_unlock(:k)").bindparams(k=_MIGRATION_LOCK_KEY))
    except Exception:
        pass

# All columns that migrations should have added.
# Format: (table, column, sql_type, default)
REQUIRED_COLUMNS = [
    # Migration: a1b2c3d4e5f6_add_candidate_extended_fields
    ("candidates", "resume_fit_score", "VARCHAR(20)", None),
    ("candidates", "fit_score_reasoning", "TEXT", None),
    ("candidates", "fit_score_details", "TEXT", None),
    ("candidates", "resume_metadata_id", "INTEGER", None),
    ("candidates", "interview_date", "TIMESTAMP", None),
    ("candidates", "interview_link", "VARCHAR(500)", None),
    ("candidates", "interview_status", "VARCHAR(60)", "'New'"),
    ("candidates", "candidate_stage", "VARCHAR(50)", "'Candidates'"),
    ("candidates", "recording_link", "VARCHAR(500)", None),
    ("candidates", "schedule_email_template", "TEXT", None),
    # Migration: d4e5f6g7h8i9_add_status_log_and_cancel
    ("candidates", "interview_timezone", "VARCHAR(100)", None),
    ("candidates", "joining_flexibility", "VARCHAR(30)", "'anytime'"),
    ("candidates", "allow_candidate_reschedule", "BOOLEAN", "FALSE"),
    # Migration: f6g7h8i9j0k1_add_selected_hired_status
    ("candidates", "video_duration", "VARCHAR(50)", None),
    # Migration: h8i9j0k1l2m3_add_annual_package
    ("resume_metadata", "annual_package", "FLOAT", None),
    # Migration: i9j0k1l2m3n4_add_allow_joining_early
    ("candidates", "allow_joining_early", "BOOLEAN", "FALSE"),
    # Migration: m3n4o5p6q7r8_add_face_count_to_screenshots
    ("interview_screenshots", "face_count", "INTEGER", None),
    # Migration: a4b5c6d7e8f9_add_coding_submissions
    ("evaluations", "coding_score", "FLOAT", None),
]

# Tables that need to be created if they don't exist.
# These use CREATE TABLE IF NOT EXISTS so they're safe to run repeatedly.
REQUIRED_TABLES = [
    # Migration: j0k1l2m3n4o5_add_per_question_evaluations
    """
    CREATE TABLE IF NOT EXISTS per_question_evaluations (
        id SERIAL PRIMARY KEY,
        session_id INTEGER NOT NULL REFERENCES interview_sessions(id) ON DELETE CASCADE,
        question_index INTEGER NOT NULL DEFAULT 0,
        question_text TEXT NOT NULL,
        answer_text TEXT,
        score FLOAT DEFAULT 0,
        feedback TEXT,
        created_at TIMESTAMP DEFAULT NOW()
    )
    """,
    # Migration: k1l2m3n4o5p6_add_ai_credit_usage
    """
    CREATE TABLE IF NOT EXISTS ai_credit_usage (
        id SERIAL PRIMARY KEY,
        feature VARCHAR(50) NOT NULL,
        provider VARCHAR(20) NOT NULL,
        model VARCHAR(60) NOT NULL,
        tier VARCHAR(20) NOT NULL,
        input_tokens INTEGER NOT NULL DEFAULT 0,
        output_tokens INTEGER NOT NULL DEFAULT 0,
        total_tokens INTEGER NOT NULL DEFAULT 0,
        estimated_cost FLOAT NOT NULL DEFAULT 0.0,
        job_id INTEGER,
        candidate_id INTEGER,
        session_id INTEGER,
        created_at TIMESTAMP NOT NULL DEFAULT NOW()
    )
    """,
    # Migration: l2m3n4o5p6q7_add_interview_screenshots
    """
    CREATE TABLE IF NOT EXISTS interview_screenshots (
        id SERIAL PRIMARY KEY,
        session_id INTEGER NOT NULL REFERENCES interview_sessions(id) ON DELETE CASCADE,
        file_path VARCHAR(500) NOT NULL,
        captured_at TIMESTAMP NOT NULL DEFAULT NOW()
    )
    """,
    # Migration: n4o5p6q7r8s9_add_users_table
    """
    DO $$
    BEGIN
        IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'userrole') THEN
            CREATE TYPE userrole AS ENUM ('super_admin', 'admin', 'recruiter');
        END IF;
    END
    $$;
    """,
    """
    CREATE TABLE IF NOT EXISTS users (
        id SERIAL PRIMARY KEY,
        name VARCHAR(150) NOT NULL,
        email VARCHAR(255) NOT NULL UNIQUE,
        password_hash TEXT NOT NULL,
        role userrole NOT NULL DEFAULT 'recruiter',
        is_active BOOLEAN NOT NULL DEFAULT TRUE,
        is_email_verified BOOLEAN NOT NULL DEFAULT FALSE,
        email_verify_token VARCHAR(128),
        email_verify_expires TIMESTAMP,
        created_at TIMESTAMP NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMP
    )
    """,
    # Migration: o5p6q7r8s9t0_add_organizations_multi_tenant
    """
    CREATE TABLE IF NOT EXISTS organizations (
        id SERIAL PRIMARY KEY,
        name VARCHAR(255) NOT NULL,
        domain VARCHAR(255) NOT NULL UNIQUE,
        is_active BOOLEAN NOT NULL DEFAULT TRUE,
        created_at TIMESTAMP NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMP
    )
    """,
    # Migration: a4b5c6d7e8f9_add_coding_submissions
    """
    CREATE TABLE IF NOT EXISTS coding_submissions (
        id SERIAL PRIMARY KEY,
        session_id INTEGER NOT NULL REFERENCES interview_sessions(id) ON DELETE CASCADE,
        question_index INTEGER,
        title VARCHAR(255),
        description TEXT,
        difficulty VARCHAR(20),
        language VARCHAR(40) NOT NULL,
        code TEXT NOT NULL,
        test_cases_total INTEGER NOT NULL DEFAULT 0,
        test_cases_passed INTEGER NOT NULL DEFAULT 0,
        test_results TEXT,
        execution_time_ms INTEGER,
        submitted_at TIMESTAMP NOT NULL DEFAULT NOW()
    )
    """,
]

# Columns that need to be added for multi-tenancy
REQUIRED_COLUMNS = REQUIRED_COLUMNS + [
    ("users", "organization_id", "INTEGER", None),
    ("jobs", "organization_id", "INTEGER", None),
    ("questions", "organization_id", "INTEGER", None),
    ("resume_metadata", "organization_id", "INTEGER", None),
    ("ai_credit_usage", "organization_id", "INTEGER", None),
    # Migration: k5l6m7n8o9p0_add_candidate_invite_token
    ("candidates", "invite_token", "VARCHAR(64)", None),
    # Migration: l6m7n8o9p0q1_add_interview_message_feedback
    ("interview_messages", "feedback", "TEXT", None),
]

# Indexes to create (safe with IF NOT EXISTS)
REQUIRED_INDEXES = [
    "CREATE INDEX IF NOT EXISTS ix_ai_credit_usage_feature ON ai_credit_usage (feature)",
    "CREATE INDEX IF NOT EXISTS ix_ai_credit_usage_created_at ON ai_credit_usage (created_at)",
    "CREATE INDEX IF NOT EXISTS ix_ai_credit_usage_id ON ai_credit_usage (id)",
    "CREATE INDEX IF NOT EXISTS ix_per_question_evaluations_session_id ON per_question_evaluations (session_id)",
    "CREATE INDEX IF NOT EXISTS ix_interview_screenshots_session_id ON interview_screenshots (session_id)",
    "CREATE INDEX IF NOT EXISTS ix_coding_submissions_session_id ON coding_submissions (session_id)",
    "CREATE UNIQUE INDEX IF NOT EXISTS ix_users_email ON users (email)",
    "CREATE INDEX IF NOT EXISTS ix_users_id ON users (id)",
    "CREATE UNIQUE INDEX IF NOT EXISTS ix_organizations_domain ON organizations (domain)",
    "CREATE INDEX IF NOT EXISTS ix_organizations_id ON organizations (id)",
    "CREATE INDEX IF NOT EXISTS ix_users_organization_id ON users (organization_id)",
    "CREATE INDEX IF NOT EXISTS ix_jobs_organization_id ON jobs (organization_id)",
    "CREATE INDEX IF NOT EXISTS ix_questions_organization_id ON questions (organization_id)",
    "CREATE INDEX IF NOT EXISTS ix_resume_metadata_organization_id ON resume_metadata (organization_id)",
    "CREATE INDEX IF NOT EXISTS ix_ai_credit_usage_organization_id ON ai_credit_usage (organization_id)",
    "CREATE UNIQUE INDEX IF NOT EXISTS ix_candidates_invite_token ON candidates (invite_token)",
]


def ensure_tables():
    """Create any missing tables/indexes — autocommit per statement + advisory
    lock + deadlock retry, so concurrent startups can't deadlock or cascade-fail."""
    with _autocommit_conn() as conn:
        locked = _advisory_lock(conn)
        try:
            for ddl in REQUIRED_TABLES:
                if _exec_safe(conn, ddl, "Table DDL"):
                    logger.info("Ensured table from DDL")
            for idx in REQUIRED_INDEXES:
                _exec_safe(conn, idx, "Index")
        finally:
            if locked:
                _advisory_unlock(conn)
    logger.info("All required tables verified.")


def ensure_columns():
    """Add any missing columns — autocommit per statement + advisory lock +
    deadlock retry. Previously these ran in ONE transaction, so a deadlock on
    one ALTER aborted the whole batch ('current transaction is aborted')."""
    with _autocommit_conn() as conn:
        locked = _advisory_lock(conn)
        try:
            for table, column, col_type, default in REQUIRED_COLUMNS:
                default_clause = f" DEFAULT {default}" if default else ""
                sql = f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {col_type}{default_clause}"
                if _exec_safe(conn, sql, f"Column {table}.{column}"):
                    logger.info("Ensured column %s.%s", table, column)
        finally:
            if locked:
                _advisory_unlock(conn)
    logger.info("All required columns verified.")


if __name__ == "__main__":
    ensure_columns()
    ensure_tables()
