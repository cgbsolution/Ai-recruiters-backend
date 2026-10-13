import logging
import os
import traceback
import uuid
from pathlib import Path

# Load .env into os.environ BEFORE any module-level reads (e.g.
# code_execution_service reads PISTON_API_URL / JUDGE0_RAPIDAPI_KEY at import
# time). pydantic-settings populates only the `settings` object, not os.environ.
try:
    from dotenv import load_dotenv
    load_dotenv(dotenv_path=Path(__file__).resolve().parent.parent / ".env", override=False)
except ImportError:
    pass

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

import sqlalchemy as sa
from app.config import settings

logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
# Keep app-level loggers at INFO so our own messages still appear
logging.getLogger("app").setLevel(logging.INFO)
# pdfminer logs a WARNING per glyph lookup for PDFs with incomplete font
# metadata ("Could not get FontBBox ..."). Text still extracts; during a bulk
# resume upload those lines buried every real message in the log.
logging.getLogger("pdfminer").setLevel(logging.ERROR)

# Login JWTs (and signed media URLs / URL refs, unless MEDIA_SIGNING_KEY /
# URL_REF_KEY are set) are keyed off SECRET_KEY. Warn loudly, but never refuse
# to boot a live service over it.
if settings.secret_key_is_weak:
    logging.getLogger("app.security").critical(
        "SECRET_KEY is a placeholder or shorter than 32 characters — anyone who knows "
        "it can forge login tokens. Rotate it to a long random value (users will have "
        "to sign in again; set URL_REF_KEY first if bookmarked page links must keep working)."
    )

from app.database import Base, engine
from app import media
from app.routers import ai, auth, calendar, candidates, client_events, credits, file_manager, interview, jobs, observer, organizations, questions, resume_database, secondary_cam, talent_admin, talent_jobs, talent_pipeline, talent_public, tts, uploads

import app.models  # noqa: F401 — ensure all models are registered with Base
import app.services.observer_events  # noqa: F401 — wire SQLAlchemy listeners for the live observer broker

# Run Alembic migrations on startup (adds missing columns/tables safely).
# Set SKIP_STARTUP_MIGRATIONS=1 once migrations run as a deploy step (the
# Dockerfile already runs startup.py + `alembic upgrade head`) — import-time
# migrations add seconds to EVERY cold start, which candidates experience as
# "loading interview…" timeouts on serverless / restart-heavy platforms.
_SKIP_STARTUP_MIGRATIONS = os.environ.get("SKIP_STARTUP_MIGRATIONS", "").lower() in ("1", "true", "yes")

if _SKIP_STARTUP_MIGRATIONS:
    logging.info("SKIP_STARTUP_MIGRATIONS set — skipping import-time migrations")
else:
    try:
        from alembic.config import Config
        from alembic import command

        _base_dir = os.path.dirname(os.path.dirname(__file__))
        alembic_cfg = Config(os.path.join(_base_dir, "alembic.ini"))
        alembic_cfg.set_main_option("script_location", os.path.join(_base_dir, "alembic"))

        # Check if alembic_version table exists — if not, the DB was created
        # with create_all() and we need to stamp it before upgrading
        from sqlalchemy import inspect as sa_inspect
        _inspector = sa_inspect(engine)
        if "alembic_version" not in _inspector.get_table_names():
            logging.info("No alembic_version table — stamping DB at base revision")
            # Stamp at initial so Alembic knows tables exist, then upgrade the rest
            command.stamp(alembic_cfg, "f9ffeeb88d8e")  # initial_schema

        command.upgrade(alembic_cfg, "head")
        logging.info("Alembic migrations applied successfully")
    except Exception as e:
        logging.warning(f"Alembic migration failed: {e}")
        Base.metadata.create_all(bind=engine)
        logging.info("Fallback: Base.metadata.create_all() completed")

# Always run raw SQL migrations — ensures columns exist even if Alembic succeeded
# (Alembic may not have the latest column additions)
import sqlalchemy as sa
_raw_migrations = [
        "ALTER TABLE candidates ADD COLUMN IF NOT EXISTS resume_metadata_id INTEGER REFERENCES resume_metadata(id)",
        "ALTER TABLE candidates ADD COLUMN IF NOT EXISTS parsing_status VARCHAR(20)",
        "ALTER TABLE candidates ADD COLUMN IF NOT EXISTS mail_contact_history BOOLEAN DEFAULT FALSE",
        "ALTER TABLE candidates ADD COLUMN IF NOT EXISTS pre_interview_responses TEXT",
        "ALTER TABLE candidates ADD COLUMN IF NOT EXISTS candidate_stage VARCHAR(50) DEFAULT 'Candidates'",
        "ALTER TABLE candidates ADD COLUMN IF NOT EXISTS interview_status VARCHAR(60) DEFAULT 'New'",
        "ALTER TABLE candidates ADD COLUMN IF NOT EXISTS interview_date TIMESTAMP",
        "ALTER TABLE candidates ADD COLUMN IF NOT EXISTS interview_timezone VARCHAR(100)",
        "ALTER TABLE candidates ADD COLUMN IF NOT EXISTS interview_link VARCHAR(500)",
        "ALTER TABLE candidates ADD COLUMN IF NOT EXISTS allow_candidate_reschedule BOOLEAN DEFAULT FALSE",
        "ALTER TABLE candidates ADD COLUMN IF NOT EXISTS joining_flexibility VARCHAR(30) DEFAULT 'anytime'",
        "ALTER TABLE candidates ADD COLUMN IF NOT EXISTS schedule_email_template TEXT",
        "ALTER TABLE candidates ADD COLUMN IF NOT EXISTS score FLOAT",
        "ALTER TABLE candidates ADD COLUMN IF NOT EXISTS trust_score FLOAT",
        "ALTER TABLE candidates ADD COLUMN IF NOT EXISTS grammar_score FLOAT",
        "ALTER TABLE candidates ADD COLUMN IF NOT EXISTS confidence_score FLOAT",
        "ALTER TABLE candidates ADD COLUMN IF NOT EXISTS clarity_score FLOAT",
        "ALTER TABLE candidates ADD COLUMN IF NOT EXISTS pronunciation_score FLOAT",
        "ALTER TABLE candidates ADD COLUMN IF NOT EXISTS enthusiasm_score FLOAT",
        "ALTER TABLE candidates ADD COLUMN IF NOT EXISTS conciseness_score FLOAT",
        "ALTER TABLE candidates ADD COLUMN IF NOT EXISTS ai_cheating_score FLOAT",
        "ALTER TABLE candidates ADD COLUMN IF NOT EXISTS recording_link VARCHAR(500)",
        "ALTER TABLE candidates ADD COLUMN IF NOT EXISTS video_duration VARCHAR(50)",
        "ALTER TABLE candidates ADD COLUMN IF NOT EXISTS resume_fit_score VARCHAR(20)",
        "ALTER TABLE candidates ADD COLUMN IF NOT EXISTS fit_score_reasoning TEXT",
        "ALTER TABLE candidates ADD COLUMN IF NOT EXISTS fit_score_details TEXT",
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'candidatestatus') THEN CREATE TYPE candidatestatus AS ENUM ('Applied','Interview','Offered','Selected','Hired','Rejected'); END IF; END $$;",
        "DO $$ BEGIN ALTER TYPE candidatestatus ADD VALUE IF NOT EXISTS 'Selected'; EXCEPTION WHEN duplicate_object THEN NULL; END $$;",
        "DO $$ BEGIN ALTER TYPE candidatestatus ADD VALUE IF NOT EXISTS 'Hired'; EXCEPTION WHEN duplicate_object THEN NULL; END $$;",
        "DO $$ BEGIN ALTER TYPE jobstatus ADD VALUE IF NOT EXISTS 'Finished'; EXCEPTION WHEN duplicate_object THEN NULL; END $$;",
        "DO $$ BEGIN ALTER TYPE jobstatus ADD VALUE IF NOT EXISTS 'finished'; EXCEPTION WHEN duplicate_object THEN NULL; END $$;",
        # evaluations table — newer columns added after initial schema
        "ALTER TABLE evaluations ADD COLUMN IF NOT EXISTS per_question_evaluations TEXT",
        "ALTER TABLE evaluations ADD COLUMN IF NOT EXISTS skills_ratings TEXT",
        "ALTER TABLE evaluations ADD COLUMN IF NOT EXISTS meets_threshold BOOLEAN DEFAULT FALSE",
        "ALTER TABLE evaluations ADD COLUMN IF NOT EXISTS pronunciation_score FLOAT",
        "ALTER TABLE evaluations ADD COLUMN IF NOT EXISTS enthusiasm_score FLOAT",
        "ALTER TABLE evaluations ADD COLUMN IF NOT EXISTS grammar_score FLOAT",
        "ALTER TABLE evaluations ADD COLUMN IF NOT EXISTS conciseness_score FLOAT",
        "ALTER TABLE evaluations ADD COLUMN IF NOT EXISTS clarity_score FLOAT",
        # Convert the legacy native-enum `recommendation` column to plain VARCHAR.
        # The ORM (RecommendationType, impl=String) writes free-text values like
        # "No Hire", but Postgres refuses to implicitly cast varchar -> the old
        # enum type — which was failing EVERY evaluation insert with
        # "column recommendation is of type recommendation but expression is of
        # type character varying". Only convert while it is still the enum, so
        # this is idempotent (skipped once the column is already varchar).
        """
        DO $$
        BEGIN
          IF EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_name = 'evaluations'
              AND column_name = 'recommendation'
              AND data_type = 'USER-DEFINED'
          ) THEN
            ALTER TABLE evaluations
              ALTER COLUMN recommendation TYPE VARCHAR(32) USING recommendation::text;
          END IF;
        END $$;
        """,
        # organizations — feature flags
        "ALTER TABLE organizations ADD COLUMN IF NOT EXISTS allow_resume_database BOOLEAN DEFAULT FALSE",
        "ALTER TABLE organizations ADD COLUMN IF NOT EXISTS allow_code_editor BOOLEAN DEFAULT FALSE",
        "ALTER TABLE organizations ADD COLUMN IF NOT EXISTS allow_whatsapp BOOLEAN DEFAULT FALSE",
        # interview_screenshots table
        "CREATE TABLE IF NOT EXISTS interview_screenshots (id SERIAL PRIMARY KEY, session_id INTEGER REFERENCES interview_sessions(id) ON DELETE CASCADE, file_path VARCHAR(500), captured_at TIMESTAMP, face_count INTEGER)",
        "ALTER TABLE interview_screenshots ADD COLUMN IF NOT EXISTS face_count INTEGER",
        # users table
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS phone VARCHAR(50)",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS designation VARCHAR(150)",
        # users — Google OAuth
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS google_access_token TEXT",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS google_refresh_token TEXT",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS google_email VARCHAR(255)",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS google_connected_at TIMESTAMP",
        "ALTER TABLE organizations ADD COLUMN IF NOT EXISTS allow_fit_score BOOLEAN DEFAULT FALSE",
        # secondary camera verification
        "CREATE TABLE IF NOT EXISTS secondary_cam_sessions (id SERIAL PRIMARY KEY, session_id INTEGER REFERENCES interview_sessions(id) ON DELETE CASCADE UNIQUE, pairing_code VARCHAR(32) UNIQUE NOT NULL, status VARCHAR(20) DEFAULT 'pending', device_info TEXT, last_heartbeat TIMESTAMP, positioning_snapshot VARCHAR(500), created_at TIMESTAMP DEFAULT NOW(), verified_at TIMESTAMP)",
        "CREATE TABLE IF NOT EXISTS secondary_cam_snapshots (id SERIAL PRIMARY KEY, secondary_session_id INTEGER REFERENCES secondary_cam_sessions(id) ON DELETE CASCADE, file_path VARCHAR(500) NOT NULL, captured_at TIMESTAMP DEFAULT NOW())",
        "DO $$ BEGIN ALTER TYPE mixofquestions ADD VALUE IF NOT EXISTS 'ai_only'; EXCEPTION WHEN duplicate_object THEN NULL; END $$;",
        # New proctoring violation: candidate stopped screen sharing mid-interview.
        "DO $$ BEGIN ALTER TYPE violationtype ADD VALUE IF NOT EXISTS 'screen_share_stopped'; EXCEPTION WHEN duplicate_object THEN NULL; END $$;",
        "ALTER TABLE candidates ADD COLUMN IF NOT EXISTS added_by VARCHAR(255)",
        "ALTER TABLE interview_sessions ADD COLUMN IF NOT EXISTS interview_language VARCHAR(20) DEFAULT 'en'",
        # email logs
        "CREATE TABLE IF NOT EXISTS email_logs (id SERIAL PRIMARY KEY, candidate_id INTEGER REFERENCES candidates(id) ON DELETE CASCADE, subject VARCHAR(500) NOT NULL, triggered_by VARCHAR(100) DEFAULT 'SYSTEM', trigger_event VARCHAR(100), sent_at TIMESTAMP DEFAULT NOW(), status VARCHAR(20) DEFAULT 'sent')",
        # live observer tokens (P2P WebRTC + SSE transcript)
        "CREATE TABLE IF NOT EXISTS observer_tokens (id SERIAL PRIMARY KEY, token VARCHAR(64) NOT NULL, candidate_id INTEGER REFERENCES candidates(id) ON DELETE CASCADE NOT NULL, job_id INTEGER REFERENCES jobs(id) ON DELETE CASCADE NOT NULL, session_id INTEGER REFERENCES interview_sessions(id) ON DELETE SET NULL, room_name VARCHAR(120) NOT NULL, created_by_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL, created_by_email VARCHAR(255), expires_at TIMESTAMP, revoked_at TIMESTAMP, view_count INTEGER NOT NULL DEFAULT 0, created_at TIMESTAMP NOT NULL DEFAULT NOW())",
        "CREATE UNIQUE INDEX IF NOT EXISTS ix_observer_tokens_token ON observer_tokens(token)",
        "CREATE INDEX IF NOT EXISTS ix_observer_tokens_candidate_id ON observer_tokens(candidate_id)",
        "CREATE INDEX IF NOT EXISTS ix_observer_tokens_session_id ON observer_tokens(session_id)",
        "CREATE INDEX IF NOT EXISTS ix_observer_tokens_room_name ON observer_tokens(room_name)",
        # client-side telemetry (recording-upload failures, crashes, memory pressure)
        "CREATE TABLE IF NOT EXISTS client_events (id SERIAL PRIMARY KEY, session_id INTEGER, event_type VARCHAR(60) NOT NULL, message TEXT, context TEXT, url VARCHAR(1000), user_agent VARCHAR(500), device_memory_gb FLOAT, hardware_concurrency INTEGER, client_at VARCHAR(40), created_at TIMESTAMP DEFAULT NOW())",
        "CREATE INDEX IF NOT EXISTS ix_client_events_session_id ON client_events(session_id)",
        "CREATE INDEX IF NOT EXISTS ix_client_events_event_type ON client_events(event_type)",
        "CREATE INDEX IF NOT EXISTS ix_client_events_created_at ON client_events(created_at)",
        # personal interview invite links (app.invites)
        "ALTER TABLE candidates ADD COLUMN IF NOT EXISTS invite_token VARCHAR(64)",
        "CREATE UNIQUE INDEX IF NOT EXISTS ix_candidates_invite_token ON candidates (invite_token)",
        # Nira's feedback, stored apart from the next question (interview_text)
        "ALTER TABLE interview_messages ADD COLUMN IF NOT EXISTS feedback TEXT",
        # Bulk resume upload: dedup by file content + restart-safe queue
        "ALTER TABLE resume_metadata ADD COLUMN IF NOT EXISTS file_hash VARCHAR(64)",
        "ALTER TABLE resume_metadata ADD COLUMN IF NOT EXISTS batch_id VARCHAR(32)",
        "ALTER TABLE resume_metadata ADD COLUMN IF NOT EXISTS parse_mode VARCHAR(10)",
        "CREATE INDEX IF NOT EXISTS ix_resume_metadata_file_hash ON resume_metadata (file_hash)",
        "CREATE INDEX IF NOT EXISTS ix_resume_metadata_batch_id ON resume_metadata (batch_id)",
        # Resume Database search page: derived facts + recruiter workflow
        "ALTER TABLE resume_metadata ADD COLUMN IF NOT EXISTS pipeline_status VARCHAR(20) NOT NULL DEFAULT 'new'",
        "ALTER TABLE resume_metadata ADD COLUMN IF NOT EXISTS source VARCHAR(20)",
        "ALTER TABLE resume_metadata ADD COLUMN IF NOT EXISTS current_company VARCHAR(255)",
        "ALTER TABLE resume_metadata ADD COLUMN IF NOT EXISTS city VARCHAR(100)",
        "ALTER TABLE resume_metadata ADD COLUMN IF NOT EXISTS highest_education VARCHAR(20)",
        "ALTER TABLE resume_metadata ADD COLUMN IF NOT EXISTS parse_quality INTEGER",
        "ALTER TABLE resume_metadata ADD COLUMN IF NOT EXISTS skills_index TEXT",
        "ALTER TABLE resume_metadata ADD COLUMN IF NOT EXISTS certs_index TEXT",
        "ALTER TABLE resume_metadata ADD COLUMN IF NOT EXISTS search_text TEXT",
        "ALTER TABLE resume_metadata ADD COLUMN IF NOT EXISTS current_ctc FLOAT",
        "ALTER TABLE resume_metadata ADD COLUMN IF NOT EXISTS expected_ctc FLOAT",
        "ALTER TABLE resume_metadata ADD COLUMN IF NOT EXISTS notice_period_days INTEGER",
        "ALTER TABLE resume_metadata ADD COLUMN IF NOT EXISTS open_to_relocate BOOLEAN",
        "ALTER TABLE resume_metadata ADD COLUMN IF NOT EXISTS derived_version INTEGER",
        "ALTER TABLE resume_metadata ADD COLUMN IF NOT EXISTS recruiter_fields TEXT",
        "ALTER TABLE resume_metadata ADD COLUMN IF NOT EXISTS parser_version INTEGER",
        "CREATE INDEX IF NOT EXISTS ix_resume_metadata_pipeline_status ON resume_metadata (pipeline_status)",
        "CREATE INDEX IF NOT EXISTS ix_resume_metadata_city ON resume_metadata (city)",
]
if not _SKIP_STARTUP_MIGRATIONS:
    try:
        # Run in AUTOCOMMIT (each statement is its own transaction, so one failure
        # can't abort the rest) and hold a Postgres advisory lock (same key as
        # startup.py) so concurrent workers / rolling deploys SERIALIZE these DDLs
        # instead of deadlocking on ALTER TABLE locks. Retry transient deadlocks.
        import time as _t
        _MIG_LOCK_KEY = 727274
        with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
            _locked = False
            try:
                conn.execute(sa.text("SELECT pg_advisory_lock(:k)").bindparams(k=_MIG_LOCK_KEY))
                _locked = True
            except Exception:
                pass
            try:
                for sql in _raw_migrations:
                    for _attempt in range(3):
                        try:
                            conn.execute(sa.text(sql))
                            break
                        except Exception as _e:
                            if "deadlock" in str(_e).lower() and _attempt < 2:
                                _t.sleep(0.5 * (_attempt + 1))
                                continue
                            break  # already exists / non-fatal — safe to skip
            finally:
                if _locked:
                    try:
                        conn.execute(sa.text("SELECT pg_advisory_unlock(:k)").bindparams(k=_MIG_LOCK_KEY))
                    except Exception:
                        pass
        logging.info("Raw SQL migrations applied successfully")
    except Exception as e2:
        logging.warning(f"Raw SQL migrations failed: {e2}")

_DESCRIPTION = """
## CGB Solutions — AI Recruiters API

Full-stack recruitment platform with AI-powered features including a live AI Avatar Interview system.

---

### Admin Platform

| Area | Description |
|---|---|
| **Jobs** | Create and manage job openings with skills, responsibilities, interview settings, and question banks |
| **Candidates** | Add manually or upload resumes (PDF/DOCX) with parallel auto-parsing (Haiku) and instant fit-score computation |
| **AI Tools** | Generate JDs, interview questions, evaluate answers, parse resumes, compute fit scores |
| **Scheduling** | Schedule interviews with email (SMTP) + WhatsApp (Meta Business API) notifications |
| **Pipeline** | Stage progression: Candidates → AI Round → Panel Round → Offer-Made / Rejected |
| **Evaluation** | Full candidate report with post-interview scores at `GET /candidates/{id}/evaluation` |

---

### AI Avatar Interview System

A fully automated, candidate-facing interview flow triggered by a unique per-job URL.

**Flow:**
```
Schedule Interview (personal invite link) → Candidate Verifies Access → Permission Flow → Device Test
→ Instructions → Cheating Warning → Live AI Interview → Evaluation
→ Candidate Feedback → Optional Meeting Scheduling
```

**Key endpoints:**

| Method | Path | Description |
|---|---|---|
| `POST` | `/jobs/{job_id}/interview-url` | Generate or retrieve secure interview URL for a job |
| `GET` | `/interview/{token}` | Validate token and return job configuration |
| `POST` | `/interview/{token}/verify-access` | Admit a candidate from their personal invite link; issues the session token |
| `POST` | `/interview/sessions/{id}/start` | Start interview — returns AI opening question |
| `POST` | `/interview/sessions/{id}/message` | Send candidate answer — returns next AI question |
| `POST` | `/interview/sessions/{id}/violation` | Log cheating event (tab switch, face detection, etc.) |
| `POST` | `/interview/sessions/{id}/complete` | End interview and trigger AI evaluation (Claude Opus) |
| `GET` | `/interview/sessions/{id}/evaluation` | Retrieve structured evaluation with scores |
| `POST` | `/interview/sessions/{id}/feedback` | Submit candidate experience feedback |
| `GET` | `/interview/sessions/{id}/slots` | List available meeting slots for the job |
| `POST` | `/interview/sessions/{id}/schedule-meeting` | Book a post-interview meeting slot |
| `GET` | `/jobs/{job_id}/interview-sessions` | Admin: list all sessions for a job |
| `POST` | `/jobs/{job_id}/meeting-slots` | Admin: add available meeting slots |

**Cheating Detection:**
Violations are logged per session. After **5 violations** the session is auto-terminated.
Violation types: `tab_switch` · `fullscreen_exit` · `face_not_detected` · `multiple_faces`

**AI Evaluation Scores** (0–100 each):
`technical` · `communication` · `problem_solving` · `confidence` → `overall_score`

Recommendations: `Strong Hire` (≥80) · `Hire` (≥65) · `Maybe` (≥50) · `No Hire` (<50)

---

### Authentication
- **Admin API**: `Authorization: Bearer <JWT>` from `POST /auth/login`. Jobs, candidates and
  organizations are scoped to the caller's organization; anything outside it returns 404.
- **Candidate interview**: a candidate is admitted through their personal invite link
  (`POST /interview/{token}/verify-access`), which issues a per-session token. Candidate calls on
  `/interview/sessions/{id}/*` and `/secondary-cam/{id}/*` must send it as the `X-Session-Token`
  header (`?st=` is accepted only by the sendBeacon routes and the observer publish WebSockets).
- **Live observer**: `/observe/{token}` links are private, revocable links minted by recruiters.

### File Uploads
- Resume files: `POST /candidates/upload-resume` — multipart, multiple files, parallel parsing (3 concurrent)
- Bulk resumes: `POST /resume-database/upload` — batch upload to central database
- Uploaded media (recordings, photos, screenshots, resumes) is private: there is no public `/uploads`
  path. Authorized API responses carry short-lived signed `/media/...` URLs.

### Models
- Claude `claude-haiku-4-5` for resume parsing (fast, ~1-2s)
- Claude `claude-sonnet-4-6` for interview turns and fit-score deep analysis
- Claude `claude-opus-4-6` for interview evaluations
- Smart scoring engine (no AI) for instant fit-score during upload (~50ms)
"""

# The interactive docs map every endpoint (super-admin ones included) for
# anyone who finds them; production (APP_ENV=production) doesn't serve them.
_API_DOCS = (settings.APP_ENV or "").strip().lower() != "production"

app = FastAPI(
    title="CGB AI Recruiters API",
    description=_DESCRIPTION,
    version="3.0.0",
    docs_url="/docs" if _API_DOCS else None,
    redoc_url="/redoc" if _API_DOCS else None,
    openapi_url="/openapi.json" if _API_DOCS else None,
    contact={"name": "CGB Solutions", "email": "sindubrj@cgbindia.com"},
    openapi_tags=[
        {
            "name": "Jobs",
            "description": (
                "CRUD for job openings and all nested data: skills, responsibilities, "
                "interview settings, question banks, pre-interview fields.\n\n"
                "**Delete** permanently removes the job and all associated data "
                "(skills, responsibilities, questions, settings, tokens, sessions, "
                "meeting slots, metadata, pre-interview fields, scoring training data). "
                "Candidates are preserved with `job_id` set to `null`.\n\n"
                "Also includes: generate interview URL, list interview sessions, add meeting slots."
            ),
        },
        {
            "name": "Candidates",
            "description": (
                "Candidate management: create manually, upload & auto-parse resumes (PDF/DOCX).\n\n"
                "**Resume Upload** processes up to 3 resumes in parallel using Claude Haiku (~1-2s each). "
                "Fit scores are computed instantly via the 12-dimension smart scoring engine. "
                "Poll `GET /candidates/{id}/parsing-status` for progressive updates.\n\n"
                "**Fit Score Recompute** (`POST /candidates/{id}/compute-fit-score`) uses full AI "
                "semantic analysis for higher accuracy (~15-20s).\n\n"
                "Also: schedule interview (email + WhatsApp), promote through hiring stages, "
                "view evaluation report."
            ),
        },
        {
            "name": "Interview",
            "description": (
                "Candidate-facing AI Avatar Interview system (no admin login). "
                "Candidates are admitted through their personal invite link; candidate calls on "
                "`/interview/sessions/{id}/*` require the `X-Session-Token` header issued at admission. "
                "Covers: token validation, candidate access, AI conversation turns, "
                "violation logging, session completion, AI evaluation generation, "
                "candidate feedback, and meeting slot booking."
            ),
        },
        {
            "name": "AI",
            "description": (
                "Admin LLM-powered tools: generate structured JD, generate interview questions, "
                "evaluate candidate answers (scored 0–100), suggest questions by category, "
                "parse resume to structured JSON, compute resume-to-JD fit score."
            ),
        },
        {
            "name": "Questions",
            "description": "Global question bank — full CRUD for reusable interview questions.",
        },
        {
            "name": "Media",
            "description": (
                "Private uploads (recordings, photos, screenshots, resumes) served only through "
                "short-lived signed `/media/...` URLs handed out by authorized API responses."
            ),
        },
        {
            "name": "Resume Database",
            "description": (
                "Central resume store — bulk upload, parse, and search resumes independently of jobs. "
                "Supports batch upload with choice of fast (regex) or AI (Claude) parsing. "
                "Use `POST /jobs/{job_id}/match-candidates` to match database resumes against a job."
            ),
        },
        {
            "name": "AI Credits",
            "description": (
                "AI credit usage analytics — monthly, weekly, and daily token consumption "
                "broken down by feature (Interview, JD Generation, Resume Parsing, Fit Score, etc.). "
                "Powers the admin dashboard usage chart."
            ),
        },
        {
            "name": "Health",
            "description": "Health check endpoint — returns API status and version.",
        },
    ],
)

# ── Global exception handler — logs full traceback for every 500 ─────────────
# The body stays generic: exception text can carry SQL, parameters and file
# paths. error_id ties a user's report to the log entry. Only the path is
# logged — query strings carry OAuth state/code and session tokens.
@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    error_id = uuid.uuid4().hex
    logging.error(
        f"UNHANDLED 500 [{error_id}] | {request.method} {request.url.path}\n"
        + "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    )
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error", "error_id": error_id},
    )

# Candidate session-token checks are a per-route FastAPI dependency
# (app.session_auth.require_session_token) on every /interview/sessions/{id}/*
# and /secondary-cam/{id}/* route — no longer a path-regex middleware here.


# ── CORS ─────────────────────────────────────────────────────────────────────
_cors_origins = settings.cors_origins_list

app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# FastAPI's CORSMiddleware does NOT add CORS headers to 500 error responses,
# causing the browser to report a CORS error instead of the real error.
# This middleware ensures CORS headers are present on ALL responses.
@app.middleware("http")
async def add_cors_on_error(request: Request, call_next):
    origin = request.headers.get("origin", "")
    try:
        response = await call_next(request)
    except Exception as exc:
        # Unhandled exception — return 500 WITH CORS headers. Generic body and a
        # path-only log line, same as unhandled_exception_handler above.
        error_id = uuid.uuid4().hex
        logging.error(
            f"UNHANDLED 500 [{error_id}] | {request.method} {request.url.path}: {exc!r}",
            exc_info=exc,
        )
        response = JSONResponse(
            status_code=500,
            content={"detail": "Internal server error", "error_id": error_id},
        )
    if origin:
        if "*" in _cors_origins:
            response.headers["Access-Control-Allow-Origin"] = "*"
        elif origin in _cors_origins:
            response.headers["Access-Control-Allow-Origin"] = origin
            response.headers["Access-Control-Allow-Credentials"] = "true"
        response.headers["Access-Control-Allow-Methods"] = "*"
        response.headers["Access-Control-Allow-Headers"] = "*"
    # Baseline security headers on every API response. /media is exempt from
    # X-Frame-Options: the admin UI previews resumes from it in an <iframe>.
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    if not request.url.path.startswith("/media/"):
        response.headers.setdefault("X-Frame-Options", "DENY")
    return response

# ── Upload folders ───────────────────────────────────────────────────────────
# Nothing under the upload root is publicly routable. The old
# app.mount("/uploads", StaticFiles(...)) served every tenant's recordings,
# photos and resumes to anyone; authorized API responses now carry short-lived
# signed /media/... URLs, served by app.media.router — a normal route, so the
# CORS middlewares above cover its responses (including 403/404).
for _upload_sub in ("jd", "resumes", "recordings", "photos", "screenshots", "talent_audio"):
    os.makedirs(os.path.join(media.UPLOAD_ROOT, _upload_sub), exist_ok=True)

# MP4 conversion now lives in app.media. interview.py still does
# `from app.main import _FFMPEG` (upload remux / recording assembly), so keep it.
_FFMPEG = media.find_ffmpeg()
logging.info("ffmpeg binary: %s", _FFMPEG or "NOT FOUND — MP4 conversion disabled")

# ── Routers ──────────────────────────────────────────────────────────────────
app.include_router(jobs.router, prefix="/jobs", tags=["Jobs"])
app.include_router(jobs.public_router, prefix="/jobs", tags=["Jobs"])
app.include_router(questions.router, prefix="/questions", tags=["Questions"])
app.include_router(candidates.router, prefix="/candidates", tags=["Candidates"])
app.include_router(calendar.router, prefix="/calendar", tags=["Calendar"])
app.include_router(ai.router, prefix="/ai", tags=["AI"])
# POST /uploads/jd (authenticated; the job form uploads the JD file here). It was
# shadowed by the old public /uploads static mount; files land in UPLOAD_ROOT/jd,
# which app.services.jd_metadata_service.resolve_jd_file reads from.
app.include_router(uploads.router, prefix="/uploads", tags=["Uploads"])
app.include_router(auth.router, prefix="/auth", tags=["Authentication"])
app.include_router(interview.router, tags=["Interview"])
app.include_router(resume_database.router, prefix="/resume-database", tags=["Resume Database"])
app.include_router(credits.router, prefix="/credits", tags=["AI Credits"])
app.include_router(organizations.router, prefix="/organizations", tags=["Organizations"])
app.include_router(file_manager.router, prefix="/file-manager", tags=["File Manager"])
app.include_router(secondary_cam.router, tags=["Secondary Camera"])
app.include_router(tts.router, tags=["Text-to-Speech"])
app.include_router(observer.router, tags=["Live Observer"])
app.include_router(client_events.router, tags=["Client Telemetry"])
app.include_router(media.router)
# Talent Agent (app.talent): admin API, the candidate pages' API (magic links, no login)
# and the Inngest endpoint /api/inngest. By default the Inngest functions run in
# their own process (app.agent.worker, started next to the API by start.sh) and
# the API forwards /api/inngest to it; TALENT_SERVE_IN_API serves them in-process.
app.include_router(talent_admin.router, prefix="/talent", tags=["Talent Agent"])
app.include_router(talent_jobs.router, prefix="/talent", tags=["Talent Agent"])
app.include_router(talent_pipeline.router, prefix="/talent", tags=["Talent Agent"])
app.include_router(talent_public.router, prefix="/talent/public", tags=["Talent Agent (candidates)"])
from app.talent.config import talent_config as _talent_config  # noqa: E402

if _talent_config.TALENT_SERVE_IN_API:
    # A missing INNGEST_SIGNING_KEY outside dev mode must not stop the API
    # from booting (live interviews depend on it): log it and serve without the agent.
    try:
        import inngest.fast_api  # noqa: E402

        from app.agent.client import inngest_client  # noqa: E402
        from app.agent.registry import FUNCTIONS  # noqa: E402

        inngest.fast_api.serve(app, inngest_client, FUNCTIONS)
    except Exception as _exc:  # noqa: BLE001
        logging.getLogger("app.agent").error(
            "Talent Agent functions NOT served (%s). Set INNGEST_SIGNING_KEY, or INNGEST_DEV=1 locally.",
            type(_exc).__name__,
        )
elif _talent_config.TALENT_EMBEDDED_WORKER:
    from app.agent import proxy as _agent_proxy  # noqa: E402

    app.include_router(_agent_proxy.router)


@app.on_event("startup")
async def _attach_observer_broker_loop():
    """Capture the running event loop so the observer broker can safely
    dispatch events queued by sync producers (SQLAlchemy listeners running
    in worker threads for sync endpoints)."""
    import asyncio
    from app.services.observer_broker import broker
    broker.attach_loop(asyncio.get_running_loop())


@app.on_event("startup")
async def _start_recording_sweeper():
    """Periodically assemble any interview recordings whose live-uploaded parts
    were never stitched into a final video — e.g. the candidate's tab/laptop
    closed the instant the interview ended, so the client never called
    /recording-finalize and /complete (which schedules the safety-net) was
    never reached either. Without this, those recordings sit on disk as raw
    parts forever and the video looks 'lost' with no error in the logs.

    The sweeper guarantees a candidate-named 'SAVED OK (sweeper)' line appears
    in the backend terminal for every finished interview's video, and only ever
    touches recordings that have been idle long enough to be safe."""
    import asyncio
    from app.routers.interview import sweep_orphan_recording_parts

    _rec_log = logging.getLogger("app.recording")

    async def _loop():
        # Wait a bit after boot so startup/migrations settle, then sweep every 5 min.
        await asyncio.sleep(90)
        while True:
            try:
                recovered = await asyncio.to_thread(sweep_orphan_recording_parts)
                if recovered:
                    _rec_log.info(
                        f"[Recording] sweeper recovered {recovered} orphaned recording(s) "
                        f"whose interview ended without the client finalizing."
                    )
            except Exception as exc:  # never let the loop die
                _rec_log.warning(f"[Recording] sweeper pass failed: {exc}")
            await asyncio.sleep(300)

    asyncio.create_task(_loop())


@app.on_event("startup")
async def _resume_unfinished_bulk_parses():
    """Bulk resume parsing is queued in memory. Rows a restart (deploy, crash)
    left pending/processing are queued again, so a large upload resumes where
    it stopped instead of sitting at "pending" forever."""
    from app.routers.resume_database import requeue_unfinished_bulk_rows

    try:
        await requeue_unfinished_bulk_rows()
    except Exception as exc:  # never block startup over this
        logging.getLogger("app.resume_bulk").warning("Could not re-queue unfinished resume parses: %s", exc)


@app.on_event("startup")
async def _backfill_resume_search_fields():
    """Fill the Resume Database search columns (city, CTC, notice period, parse
    quality, ...) for rows stored before they existed. One-off per
    PROFILE_VERSION; runs in the background so startup is not delayed."""
    import asyncio
    from app.services.resume_profile import backfill_profiles

    log = logging.getLogger("app.resume_search")

    async def _run():
        try:
            updated = await asyncio.to_thread(backfill_profiles)
            if updated:
                log.info("Filled search fields for %d existing resume(s)", updated)
        except Exception as exc:  # the page still works; filters just lack old rows
            log.warning("Resume search backfill failed: %s", exc)

    app.state.resume_backfill_task = asyncio.create_task(_run())


@app.on_event("shutdown")
def _stop_extraction_workers():
    from app.services.extraction_pool import shutdown_extraction_pool

    shutdown_extraction_pool()


@app.get("/health", tags=["Health"])
def health_check():
    return {"status": "ok", "version": "3.0.0"}
