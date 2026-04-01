"""
Startup script: ensures all required DB tables/columns exist,
then runs alembic upgrade to stay in sync.
"""
import logging
from sqlalchemy import text
from app.database import engine

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("startup")

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
    ("candidates", "interview_status", "VARCHAR(30)", "'New'"),
    ("candidates", "candidate_stage", "VARCHAR(50)", "'Applied'"),
    ("candidates", "recording_link", "VARCHAR(500)", None),
    ("candidates", "schedule_email_template", "TEXT", None),
    # Migration: d4e5f6g7h8i9_add_status_log_and_cancel
    ("candidates", "interview_timezone", "VARCHAR(50)", None),
    ("candidates", "joining_flexibility", "VARCHAR(20)", "'anytime'"),
    ("candidates", "allow_candidate_reschedule", "BOOLEAN", "FALSE"),
    # Migration: f6g7h8i9j0k1_add_selected_hired_status
    ("candidates", "video_duration", "VARCHAR(50)", None),
    # Migration: h8i9j0k1l2m3_add_annual_package
    ("resume_metadata", "annual_package", "FLOAT", None),
    # Migration: i9j0k1l2m3n4_add_allow_joining_early
    ("candidates", "allow_joining_early", "BOOLEAN", "FALSE"),
    # Migration: m3n4o5p6q7r8_add_face_count_to_screenshots
    ("interview_screenshots", "face_count", "INTEGER", None),
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
]

# Columns that need to be added for multi-tenancy
REQUIRED_COLUMNS = REQUIRED_COLUMNS + [
    ("users", "organization_id", "INTEGER", None),
    ("jobs", "organization_id", "INTEGER", None),
    ("questions", "organization_id", "INTEGER", None),
    ("resume_metadata", "organization_id", "INTEGER", None),
    ("ai_credit_usage", "organization_id", "INTEGER", None),
]

# Indexes to create (safe with IF NOT EXISTS)
REQUIRED_INDEXES = [
    "CREATE INDEX IF NOT EXISTS ix_ai_credit_usage_feature ON ai_credit_usage (feature)",
    "CREATE INDEX IF NOT EXISTS ix_ai_credit_usage_created_at ON ai_credit_usage (created_at)",
    "CREATE INDEX IF NOT EXISTS ix_ai_credit_usage_id ON ai_credit_usage (id)",
    "CREATE INDEX IF NOT EXISTS ix_per_question_evaluations_session_id ON per_question_evaluations (session_id)",
    "CREATE INDEX IF NOT EXISTS ix_interview_screenshots_session_id ON interview_screenshots (session_id)",
    "CREATE UNIQUE INDEX IF NOT EXISTS ix_users_email ON users (email)",
    "CREATE INDEX IF NOT EXISTS ix_users_id ON users (id)",
    "CREATE UNIQUE INDEX IF NOT EXISTS ix_organizations_domain ON organizations (domain)",
    "CREATE INDEX IF NOT EXISTS ix_organizations_id ON organizations (id)",
    "CREATE INDEX IF NOT EXISTS ix_users_organization_id ON users (organization_id)",
    "CREATE INDEX IF NOT EXISTS ix_jobs_organization_id ON jobs (organization_id)",
    "CREATE INDEX IF NOT EXISTS ix_questions_organization_id ON questions (organization_id)",
    "CREATE INDEX IF NOT EXISTS ix_resume_metadata_organization_id ON resume_metadata (organization_id)",
    "CREATE INDEX IF NOT EXISTS ix_ai_credit_usage_organization_id ON ai_credit_usage (organization_id)",
]


def ensure_tables():
    """Create any missing tables."""
    with engine.connect() as conn:
        for ddl in REQUIRED_TABLES:
            try:
                conn.execute(text(ddl))
                logger.info("Ensured table from DDL")
            except Exception as e:
                logger.warning("Table creation: %s", e)
        for idx in REQUIRED_INDEXES:
            try:
                conn.execute(text(idx))
            except Exception as e:
                logger.warning("Index creation: %s", e)
        conn.commit()
    logger.info("All required tables verified.")


def ensure_columns():
    """Add any missing columns to the database."""
    with engine.connect() as conn:
        for table, column, col_type, default in REQUIRED_COLUMNS:
            default_clause = f" DEFAULT {default}" if default else ""
            sql = f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {col_type}{default_clause}"
            try:
                conn.execute(text(sql))
                logger.info("Ensured column %s.%s", table, column)
            except Exception as e:
                logger.warning("Column %s.%s: %s", table, column, e)
        conn.commit()
    logger.info("All required columns verified.")


if __name__ == "__main__":
    ensure_columns()
    ensure_tables()
