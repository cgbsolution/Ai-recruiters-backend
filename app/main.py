import asyncio
import logging
import os
import traceback
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
from fastapi.staticfiles import StaticFiles
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
from app.database import Base, engine
from app.routers import ai, auth, calendar, candidates, client_events, credits, file_manager, interview, jobs, observer, organizations, questions, resume_database, secondary_cam, tts, uploads

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
Generate URL → Candidate Registers → Permission Flow → Device Test
→ Instructions → Cheating Warning → Live AI Interview → Evaluation
→ Candidate Feedback → Optional Meeting Scheduling
```

**Key endpoints:**

| Method | Path | Description |
|---|---|---|
| `POST` | `/jobs/{job_id}/interview-url` | Generate or retrieve secure interview URL for a job |
| `GET` | `/interview/{token}` | Validate token and return job configuration |
| `POST` | `/interview/{token}/register` | Register candidate (email-exists check, create session) |
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
Currently open (no auth). Add Bearer token middleware before production.

### File Uploads
- JD files: `POST /uploads/jd` — PDF / DOC / DOCX ≤ 10 MB
- Resume files: `POST /candidates/upload-resume` — multipart, multiple files, parallel parsing (3 concurrent)
- Bulk resumes: `POST /resume-database/upload` — batch upload to central database
- Static files served at `/uploads/...`

### Models
- Claude `claude-haiku-4-5` for resume parsing (fast, ~1-2s)
- Claude `claude-sonnet-4-6` for interview turns and fit-score deep analysis
- Claude `claude-opus-4-6` for interview evaluations
- Smart scoring engine (no AI) for instant fit-score during upload (~50ms)
"""

app = FastAPI(
    title="CGB AI Recruiters API",
    description=_DESCRIPTION,
    version="3.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
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
                "**Public** candidate-facing AI Avatar Interview system. "
                "No authentication required on these endpoints. "
                "Covers: token validation, candidate registration, AI conversation turns, "
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
            "name": "Uploads",
            "description": "File upload endpoint for Job Description documents (PDF / DOC / DOCX ≤ 10 MB).",
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
@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    logging.error(
        f"UNHANDLED 500 | {request.method} {request.url}\n"
        f"{traceback.format_exc()}"
    )
    return JSONResponse(
        status_code=500,
        content={"detail": f"Internal server error: {type(exc).__name__}: {exc}"},
    )

# ── Session-token enforcement for candidate session endpoints ────────────────
# /interview/sessions/{id}/* used to be completely unauthenticated with small
# sequential integer IDs (IDOR): anyone could read transcripts/evaluations,
# inject answers, or terminate sessions. A per-session token is minted at
# register/verify/login (see interview._get_or_mint_session_access_token) and
# must be echoed back as the X-Session-Token header (or ?st= for sendBeacon).
# Sessions created before this feature have no stored token → grandfathered.
# Registered BEFORE the CORS middlewares (i.e. innermost) so 403s get CORS headers.
import json as _json
import re as _re_sess
from starlette.concurrency import run_in_threadpool

_SESSION_PATH_RE = _re_sess.compile(r"^/interview/sessions/(\d+)/([a-z0-9\-]+)")
# upload-recording: orphan-recovery re-uploads on a later app load with no
#   token in hand; the endpoint has its own empty/smaller-file guard rails.
# coding-submissions: read by the authenticated ADMIN evaluation page, which
#   has no candidate session token.
_SESSION_AUTH_EXEMPT = {"upload-recording", "coding-submissions"}


def _load_session_access_token(sid: int):
    from app.database import SessionLocal as _SL
    from app.models.interview import InterviewSession as _IS
    db = _SL()
    try:
        row = (
            db.query(_IS.session_metadata)
            .filter(_IS.id == sid)
            .first()
        )
    finally:
        db.close()
    if row is None:
        return None, False  # session doesn't exist — let the route 404
    try:
        meta = _json.loads(row[0] or "{}")
    except Exception:
        meta = {}
    return meta.get("access_token"), True


@app.middleware("http")
async def enforce_session_token(request: Request, call_next):
    if request.method != "OPTIONS":
        m = _SESSION_PATH_RE.match(request.url.path)
        if m and m.group(2) not in _SESSION_AUTH_EXEMPT:
            try:
                expected, _exists = await run_in_threadpool(
                    _load_session_access_token, int(m.group(1))
                )
            except Exception as exc:
                logging.error(f"[SessionAuth] token lookup failed: {exc}")
                expected = None
            if expected:
                provided = (
                    request.headers.get("x-session-token")
                    or request.query_params.get("st")
                )
                if provided != expected:
                    return JSONResponse(
                        status_code=403,
                        content={"detail": "Invalid or missing session token"},
                    )
    return await call_next(request)


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
        # Unhandled exception — return 500 WITH CORS headers
        logging.error(f"UNHANDLED {request.method} {request.url}: {exc}", exc_info=True)
        from starlette.responses import JSONResponse
        response = JSONResponse(
            status_code=500,
            content={"detail": "Internal server error"},
        )
    if origin:
        if "*" in _cors_origins:
            response.headers["Access-Control-Allow-Origin"] = "*"
        elif origin in _cors_origins:
            response.headers["Access-Control-Allow-Origin"] = origin
            response.headers["Access-Control-Allow-Credentials"] = "true"
        response.headers["Access-Control-Allow-Methods"] = "*"
        response.headers["Access-Control-Allow-Headers"] = "*"
    return response

# ── Static file serving for uploads ──────────────────────────────────────────
import os

IS_SERVERLESS = bool(os.environ.get("VERCEL") or os.environ.get("AWS_LAMBDA_FUNCTION_NAME"))
UPLOAD_ROOT = "/tmp/uploads" if IS_SERVERLESS else "uploads"

os.makedirs(f"{UPLOAD_ROOT}/jd", exist_ok=True)
os.makedirs(f"{UPLOAD_ROOT}/resumes", exist_ok=True)
os.makedirs(f"{UPLOAD_ROOT}/recordings", exist_ok=True)
# Serve static uploads with CORS headers (screenshots, recordings, resumes)
from starlette.middleware.base import BaseHTTPMiddleware

class CORSStaticMiddleware(BaseHTTPMiddleware):
    """Add CORS headers to /uploads/* responses (StaticFiles doesn't go through
    CORSMiddleware). Only reflects ALLOWED origins — the previous version
    reflected ANY Origin with Allow-Credentials=true, which lets any website a
    user visits read uploaded recordings/resumes cross-origin."""
    async def dispatch(self, request, call_next):
        response = await call_next(request)
        if request.url.path.startswith("/uploads/"):
            origin = request.headers.get("origin", "")
            if origin and origin in _cors_origins:
                response.headers["Access-Control-Allow-Origin"] = origin
                response.headers["Access-Control-Allow-Credentials"] = "true"
            elif "*" in _cors_origins:
                response.headers["Access-Control-Allow-Origin"] = "*"
        return response

app.add_middleware(CORSStaticMiddleware)

# ── Recording download with optional WebM → MP4 conversion ──────────────────
import shutil
import subprocess
import tempfile
from fastapi.responses import FileResponse

def _find_ffmpeg() -> str | None:
    """Find an ffmpeg binary: system PATH first, then the bundled one from imageio-ffmpeg."""
    path = shutil.which("ffmpeg")
    if path:
        return path
    try:
        import imageio_ffmpeg
        path = imageio_ffmpeg.get_ffmpeg_exe()
        if path and os.path.isfile(path):
            return path
    except Exception:
        pass
    return None

_FFMPEG = _find_ffmpeg()
logging.info("ffmpeg binary: %s", _FFMPEG or "NOT FOUND — MP4 conversion disabled")

# Cache converted MP4s so repeated downloads don't re-convert every time
_MP4_CACHE_DIR = os.path.join(UPLOAD_ROOT, "recordings", ".mp4_cache")
os.makedirs(_MP4_CACHE_DIR, exist_ok=True)

@app.get("/recordings/{filename}/download", tags=["Recordings"])
async def download_recording(filename: str, format: str = "original"):
    """
    Download a recording file.

    - **format=original** (default): returns the raw .webm file with a
      Content-Disposition header so the browser saves instead of plays.
    - **format=mp4**: converts the .webm to .mp4 using ffmpeg (system or
      bundled via imageio-ffmpeg). Converted files are cached so subsequent
      downloads are instant.
    """
    safe_name = os.path.basename(filename)          # prevent path traversal
    src = os.path.join(UPLOAD_ROOT, "recordings", safe_name)
    if not os.path.isfile(src):
        raise HTTPException(status_code=404, detail="Recording not found")

    stem = os.path.splitext(safe_name)[0]

    if format == "mp4":
        if not _FFMPEG:
            raise HTTPException(
                status_code=501,
                detail="MP4 conversion unavailable — ffmpeg is not installed on the server.",
            )

        # Check cache first
        cached = os.path.join(_MP4_CACHE_DIR, f"{stem}.mp4")
        if os.path.isfile(cached):
            return FileResponse(
                cached,
                media_type="video/mp4",
                filename=f"{stem}.mp4",
            )

        # Convert to MP4 (H.264 + AAC, fast preset). Runs in a thread so the
        # event loop isn't blocked for minutes while ffmpeg works — other
        # requests keep serving.
        tmp_path = cached + ".tmp"
        try:
            proc = await asyncio.to_thread(
                subprocess.run,
                [
                    _FFMPEG, "-y", "-i", src,
                    "-c:v", "libx264", "-preset", "fast", "-crf", "23",
                    "-c:a", "aac", "-b:a", "128k",
                    "-movflags", "+faststart",
                    tmp_path,
                ],
                capture_output=True, timeout=600,
            )
            if proc.returncode != 0:
                stderr = proc.stderr.decode(errors="replace")[-500:]
                logging.error("ffmpeg conversion failed: %s", stderr)
                raise HTTPException(status_code=500, detail="MP4 conversion failed")
            # Atomic rename into cache
            os.replace(tmp_path, cached)
        except subprocess.TimeoutExpired:
            if os.path.isfile(tmp_path):
                os.remove(tmp_path)
            raise HTTPException(status_code=504, detail="Conversion timed out (video too long)")
        except HTTPException:
            raise
        except Exception as exc:
            if os.path.isfile(tmp_path):
                os.remove(tmp_path)
            logging.error("Unexpected conversion error: %s", exc)
            raise HTTPException(status_code=500, detail="MP4 conversion failed")

        return FileResponse(
            cached,
            media_type="video/mp4",
            filename=f"{stem}.mp4",
        )

    # Default: serve the original .webm as a download
    return FileResponse(
        src,
        media_type="video/webm",
        filename=safe_name,
    )

@app.post("/recordings/fix-all", tags=["Recordings"])
async def fix_all_recordings():
    """
    Remux all WebM recordings that are missing duration/seek metadata.
    This fixes the seekbar in the video player for files recorded by
    Chrome's MediaRecorder (which omits the WebM Cues element).
    Run once after deploying the fix to repair existing recordings.
    """
    if not _FFMPEG:
        raise HTTPException(status_code=501, detail="ffmpeg not available")

    rec_dir = os.path.join(UPLOAD_ROOT, "recordings")
    files = [f for f in os.listdir(rec_dir) if f.endswith(".webm") and not f.startswith(".")]
    fixed = []
    skipped = []
    failed = []

    for fname in files:
        src = os.path.join(rec_dir, fname)
        # Check if duration is already set
        probe = await asyncio.to_thread(
            subprocess.run,
            [_FFMPEG, "-i", src],
            capture_output=True, text=True, timeout=10,
        )
        if "Duration: N/A" not in probe.stderr:
            skipped.append(fname)
            continue

        tmp = src + ".fixed.webm"
        try:
            proc = await asyncio.to_thread(
                subprocess.run,
                [_FFMPEG, "-y", "-i", src, "-c", "copy", tmp],
                capture_output=True, timeout=120,
            )
            if proc.returncode == 0 and os.path.isfile(tmp):
                os.replace(tmp, src)
                fixed.append(fname)
            else:
                if os.path.isfile(tmp):
                    os.remove(tmp)
                failed.append(fname)
        except Exception:
            if os.path.isfile(tmp):
                os.remove(tmp)
            failed.append(fname)

    return {
        "total": len(files),
        "fixed": fixed,
        "skipped": skipped,
        "failed": failed,
    }

app.mount("/uploads", StaticFiles(directory=UPLOAD_ROOT), name="uploads")

# ── Routers ──────────────────────────────────────────────────────────────────
app.include_router(jobs.router, prefix="/jobs", tags=["Jobs"])
app.include_router(jobs.public_router, prefix="/jobs", tags=["Jobs"])
app.include_router(questions.router, prefix="/questions", tags=["Questions"])
app.include_router(candidates.router, prefix="/candidates", tags=["Candidates"])
app.include_router(calendar.router, prefix="/calendar", tags=["Calendar"])
app.include_router(ai.router, prefix="/ai", tags=["AI"])
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


@app.get("/health", tags=["Health"])
def health_check():
    return {"status": "ok", "version": "3.0.0"}
