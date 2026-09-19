"""
Security regression tests: tenant isolation, private media, candidate session
tokens and personal invite links.

They run against a THROWAWAY Postgres database, never the dev database. The
schema is created from the models and dropped again afterwards:

    set SECURITY_TEST_DATABASE_URL=postgresql+psycopg://user:pass@localhost:5432/anytimehire_security_test
    .venv\\Scripts\\python.exe -m pytest tests_security -q

Kept out of tests/ on purpose: tests/conftest.py imports app.main with whatever
DATABASE_URL is configured, which runs migrations against it.
"""
import json
import os
import secrets
import sys
import tempfile
from types import SimpleNamespace

_URL = os.environ.get("SECURITY_TEST_DATABASE_URL", "")
if not _URL or "test" not in _URL.rsplit("/", 1)[-1].lower():
    raise RuntimeError(
        "SECURITY_TEST_DATABASE_URL must point at a throwaway database whose name contains 'test'"
    )

# Everything below must happen before app.config / app.main are imported.
# Blank third-party credentials so nothing sends real email / WhatsApp or calls
# an AI provider (app.main's load_dotenv(override=False) keeps these values).
os.environ["DATABASE_URL"] = _URL
os.environ["SKIP_STARTUP_MIGRATIONS"] = "1"
for _name in (
    "SMTP_HOST", "SMTP_USER", "SMTP_PASS", "EMAIL_FROM", "WHATSAPP_TOKEN", "WHATSAPP_PHONE_NUMBER_ID",
    "DEEPGRAM_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY", "GOOGLE_TTS_API_KEY",
):
    os.environ[_name] = ""
os.environ["SECRET_KEY"] = "security-tests-" + secrets.token_hex(32)

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BACKEND_DIR)
WORK_DIR = tempfile.mkdtemp(prefix="anytimehire-sec-tests-")
os.chdir(WORK_DIR)  # the app resolves "uploads/" relative to the working directory

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.auth import create_access_token, hash_password  # noqa: E402
from app.database import Base, SessionLocal, engine  # noqa: E402
import app.models  # noqa: E402,F401
from app.models import (  # noqa: E402
    Candidate, InterviewSession, InterviewToken, Job, Organization, Question, ResumeMetadata, User, UserRole,
)
from app.models.interview import SessionStatus  # noqa: E402
from app.models.job import JobStatus  # noqa: E402
from app.refs import encode_ref  # noqa: E402


@pytest.fixture(scope="session")
def client():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    from app.main import app

    with TestClient(app) as test_client:
        yield test_client
    engine.dispose()
    Base.metadata.drop_all(bind=engine)


def _write(rel_path: str, data: bytes) -> None:
    full = os.path.join(WORK_DIR, "uploads", *rel_path.split("/"))
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "wb") as fh:
        fh.write(data)


@pytest.fixture(scope="session")
def world(client):
    """Two tenants with jobs, candidates, sessions, media files and questions,
    plus an org-less recruiter, a suspended tenant and a global super admin."""
    db = SessionLocal()
    try:
        org_a = Organization(name="Org A", domain="orga.test", is_active=True)
        org_b = Organization(name="Org B", domain="orgb.test", is_active=True)
        org_off = Organization(name="Suspended Org", domain="off.test", is_active=False)
        db.add_all([org_a, org_b, org_off])
        db.flush()

        def user(email, role, org):
            u = User(
                name=email.split("@")[0], email=email, password_hash=hash_password("correct-horse-battery"),
                role=role, organization_id=org.id if org else None, is_active=True, is_email_verified=True,
            )
            db.add(u)
            db.flush()
            return u

        admin_a = user("admin@orga.test", UserRole.admin, org_a)
        admin_b = user("admin@orgb.test", UserRole.admin, org_b)
        orphan = user("orphan@nowhere.test", UserRole.recruiter, None)
        suspended = user("recruiter@off.test", UserRole.recruiter, org_off)
        root = user("root@platform.test", UserRole.super_admin, None)

        def job(title, org):
            j = Job(title=title, department="Engineering", location="Remote", job_type="Full-time",
                    status=JobStatus.open, organization_id=org.id)
            db.add(j)
            db.flush()
            return j

        job_a, job_b = job("Job A", org_a), job("Job B", org_b)
        tok_a = InterviewToken(job_id=job_a.id, token=secrets.token_hex(16), is_active=True)
        tok_b = InterviewToken(job_id=job_b.id, token=secrets.token_hex(16), is_active=True)
        db.add_all([tok_a, tok_b])
        db.flush()

        def cand(name, email, j, status, invite=None):
            c = Candidate(name=name, email=email, job_id=j.id, interview_status=status, invite_token=invite)
            db.add(c)
            db.flush()
            return c

        cand_a = cand("Alice", "alice@mailbox.co", j=job_a, status="Scheduled")
        cand_b = cand("Bob", "bob@mailbox.co", j=job_b, status="Scheduled")
        invited = cand("Ivy", "ivy@mailbox.co", j=job_a, status="Scheduled", invite=secrets.token_urlsafe(24))
        # Scheduled before personal invites existed: scheduling stored (and
        # emailed) the job link, so interview_link is set.
        legacy = cand("Leo", "leo@mailbox.co", j=job_a, status="Scheduled")
        legacy.interview_link = f"http://localhost:3000/interview/{tok_a.token}"
        # Moved to "Scheduled" through the pipeline status, never sent a link.
        status_only = cand("Sam", "sam@mailbox.co", j=job_a, status="Scheduled")
        fresh = cand("Nia", "nia@mailbox.co", j=job_a, status="New")
        invited_b = cand("Bea", "bea@mailbox.co", j=job_b, status="Scheduled", invite=secrets.token_urlsafe(24))

        def session(c, tok):
            s = InterviewSession(token_id=tok.id, candidate_id=c.id, job_id=c.job_id, status=SessionStatus.in_progress,
                                 session_metadata=json.dumps({"access_token": secrets.token_urlsafe(32)}))
            db.add(s)
            db.flush()
            return s

        sess_a, sess_b = session(cand_a, tok_a), session(cand_b, tok_b)

        for c, s, tag in ((cand_a, sess_a, "a"), (cand_b, sess_b, "b")):
            _write(f"recordings/{s.id}.webm", f"webm-bytes-{tag}-".encode() * 64)
            _write(f"photos/{c.id}.jpg", f"jpg-bytes-{tag}".encode())
            _write(f"resumes/resume_{tag}.pdf", f"%PDF-{tag}".encode())
            c.recording_link = f"/uploads/recordings/{s.id}.webm"
            c.image = f"/uploads/photos/{c.id}.jpg"
            c.resume_path = os.path.join("uploads", "resumes", f"resume_{tag}.pdf")

        q_a = Question(text="Org A question", category="General", difficulty="easy", organization_id=org_a.id)
        q_b = Question(text="Org B question", category="General", difficulty="easy", organization_id=org_b.id)
        stranger = ResumeMetadata(organization_id=org_b.id, candidate_email="stranger@mailbox.co",
                                  candidate_name="Secret Stranger", parsing_status="completed")
        db.add_all([q_a, q_b, stranger])
        db.commit()

        def headers(u):
            return {"Authorization": f"Bearer {create_access_token(u.id, u.role.value, u.organization_id)}"}

        def session_token(s):
            return json.loads(s.session_metadata)["access_token"]

        return SimpleNamespace(
            org_a=org_a.id, org_b=org_b.id, org_off=org_off.id,
            admin_a=headers(admin_a), admin_b=headers(admin_b), orphan=headers(orphan),
            suspended=headers(suspended), root=headers(root),
            job_a=job_a.id, job_b=job_b.id, job_a_ref=encode_ref("job", job_a.id), job_b_ref=encode_ref("job", job_b.id),
            tok_a=tok_a.token, tok_b=tok_b.token,
            cand_a=cand_a.id, cand_b=cand_b.id,
            cand_a_ref=encode_ref("candidate", cand_a.id), cand_b_ref=encode_ref("candidate", cand_b.id),
            invited_email=invited.email, invite=invited.invite_token, invite_b=invited_b.invite_token,
            legacy_email=legacy.email, fresh_email=fresh.email, status_only_email=status_only.email,
            sess_a=sess_a.id, sess_b=sess_b.id, sess_a_token=session_token(sess_a), sess_b_token=session_token(sess_b),
            q_a=q_a.id, q_b=q_b.id,
        )
    finally:
        db.close()
