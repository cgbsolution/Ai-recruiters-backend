from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.config import settings
from app.database import Base, engine
from app.routers import ai, candidates, interview, jobs, questions, uploads

import app.models  # noqa: F401 — ensure all models are registered with Base

# Create tables (dev only — use Alembic for production)
Base.metadata.create_all(bind=engine)

_DESCRIPTION = """
## CGB Solutions — AI Recruiters API

Full-stack recruitment platform with AI-powered features including a live AI Avatar Interview system.

---

### Admin Platform

| Area | Description |
|---|---|
| **Jobs** | Create and manage job openings with skills, responsibilities, interview settings, and question banks |
| **Candidates** | Add manually or upload resumes (PDF/DOCX) with auto-parsing and fit-score computation |
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
- Resume files: `POST /candidates/upload-resume` — multipart, multiple files
- Static files served at `/uploads/...`

### Models
Claude `claude-sonnet-4-6` for interview turns · Claude `claude-opus-4-6` for evaluations
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
                "interview settings, question banks, pre-interview fields. "
                "Also includes: generate interview URL, list interview sessions, add meeting slots."
            ),
        },
        {
            "name": "Candidates",
            "description": (
                "Candidate management: create manually, upload & auto-parse resumes (PDF/DOCX), "
                "compute LLM fit score, schedule interview (email + WhatsApp), "
                "promote through hiring stages, view evaluation report."
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
            "name": "Health",
            "description": "Health check endpoint — returns API status and version.",
        },
    ],
)

# ── CORS ─────────────────────────────────────────────────────────────────────
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

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
app.include_router(questions.router, prefix="/questions", tags=["Questions"])
app.include_router(candidates.router, prefix="/candidates", tags=["Candidates"])
app.include_router(ai.router, prefix="/ai", tags=["AI"])
app.include_router(uploads.router, prefix="/uploads", tags=["Uploads"])
app.include_router(interview.router, tags=["Interview"])


@app.get("/health", tags=["Health"])
def health_check():
    return {"status": "ok", "version": "3.0.0"}
