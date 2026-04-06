import logging
import traceback

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
from app.routers import ai, auth, candidates, credits, file_manager, interview, jobs, organizations, questions, resume_database, secondary_cam, tts, uploads

import app.models  # noqa: F401 — ensure all models are registered with Base

# Run Alembic migrations on startup (adds missing columns/tables safely)
try:
    from alembic.config import Config
    from alembic import command
    import os

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
        # evaluations table — newer columns added after initial schema
        "ALTER TABLE evaluations ADD COLUMN IF NOT EXISTS per_question_evaluations TEXT",
        "ALTER TABLE evaluations ADD COLUMN IF NOT EXISTS skills_ratings TEXT",
        "ALTER TABLE evaluations ADD COLUMN IF NOT EXISTS meets_threshold BOOLEAN DEFAULT FALSE",
        "ALTER TABLE evaluations ADD COLUMN IF NOT EXISTS pronunciation_score FLOAT",
        "ALTER TABLE evaluations ADD COLUMN IF NOT EXISTS enthusiasm_score FLOAT",
        "ALTER TABLE evaluations ADD COLUMN IF NOT EXISTS grammar_score FLOAT",
        "ALTER TABLE evaluations ADD COLUMN IF NOT EXISTS conciseness_score FLOAT",
        "ALTER TABLE evaluations ADD COLUMN IF NOT EXISTS clarity_score FLOAT",
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
        # email logs
        "CREATE TABLE IF NOT EXISTS email_logs (id SERIAL PRIMARY KEY, candidate_id INTEGER REFERENCES candidates(id) ON DELETE CASCADE, subject VARCHAR(500) NOT NULL, triggered_by VARCHAR(100) DEFAULT 'SYSTEM', trigger_event VARCHAR(100), sent_at TIMESTAMP DEFAULT NOW(), status VARCHAR(20) DEFAULT 'sent')",
]
try:
    with engine.connect() as conn:
        for sql in _raw_migrations:
            try:
                conn.execute(sa.text(sql))
            except Exception:
                pass  # column/type already exists — safe to skip
        conn.commit()
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
app.mount("/uploads", StaticFiles(directory=UPLOAD_ROOT), name="uploads")

# ── Routers ──────────────────────────────────────────────────────────────────
app.include_router(jobs.router, prefix="/jobs", tags=["Jobs"])
app.include_router(jobs.public_router, prefix="/jobs", tags=["Jobs"])
app.include_router(questions.router, prefix="/questions", tags=["Questions"])
app.include_router(candidates.router, prefix="/candidates", tags=["Candidates"])
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


@app.get("/health", tags=["Health"])
def health_check():
    return {"status": "ok", "version": "3.0.0"}
