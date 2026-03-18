"""
Startup script: ensures all required DB columns exist,
then runs alembic stamp + upgrade to stay in sync.
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
    ("candidates", "resume_fit_reason", "TEXT", None),
    ("candidates", "resume_metadata", "TEXT", None),
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
    ("candidates", "video_duration", "FLOAT", None),
    # Migration: h8i9j0k1l2m3_add_annual_package
    ("resume_metadata", "annual_package", "FLOAT", None),
    # Migration: i9j0k1l2m3n4_add_allow_joining_early
    ("candidates", "allow_joining_early", "BOOLEAN", "FALSE"),
]


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
