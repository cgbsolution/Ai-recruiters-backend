"""
Talent Agent tests. They run against a THROWAWAY Postgres database (never the
dev database); the schema is built from the models and every table is
truncated between tests.

    set TALENT_TEST_DATABASE_URL=postgresql+psycopg://postgres:pw@localhost:55440/talent_test
    .venv\\Scripts\\python.exe -m pytest tests_talent -q

Run it once against a server WITH pgvector and once against one without:
matching must work in both storage modes.
"""
import os
import secrets
import sys
import tempfile
from datetime import datetime

_URL = os.environ.get("TALENT_TEST_DATABASE_URL", "")
if not _URL or "test" not in _URL.rsplit("/", 1)[-1].lower():
    raise RuntimeError("TALENT_TEST_DATABASE_URL must point at a throwaway database whose name contains 'test'")

# Must happen before app.* is imported. Blank third-party credentials so nothing
# real is ever called (app.main's load_dotenv(override=False) keeps these).
os.environ["DATABASE_URL"] = _URL
os.environ["SKIP_STARTUP_MIGRATIONS"] = "1"
for _name in (
    "SMTP_HOST", "SMTP_USER", "SMTP_PASS", "WHATSAPP_TOKEN", "WHATSAPP_PHONE_NUMBER_ID",
    "DEEPGRAM_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY", "GOOGLE_TTS_API_KEY",
    "VOYAGE_API_KEY", "INNGEST_EVENT_KEY", "INNGEST_SIGNING_KEY",
):
    os.environ[_name] = ""
os.environ["EMAIL_FROM"] = "talent@agent.test"
os.environ["SECRET_KEY"] = "talent-tests-" + secrets.token_hex(32)
os.environ["TALENT_AGENT_ENABLED"] = "true"
os.environ["TALENT_EMAIL_TRANSPORT"] = "memory"
os.environ["EMBEDDING_PROVIDER"] = "hash"
os.environ["INNGEST_DEV"] = "1"
os.environ["TALENT_DELIVERABILITY_WEBHOOK_SECRET"] = "whsec-test"

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BACKEND_DIR)
WORK_DIR = tempfile.mkdtemp(prefix="anytimehire-talent-tests-")
os.chdir(WORK_DIR)  # uploads/ resolves relative to the working directory

import pytest  # noqa: E402
import sqlalchemy as sa  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.auth import create_access_token, hash_password  # noqa: E402
from app.database import Base, SessionLocal, engine  # noqa: E402
import app.models  # noqa: E402,F401
from app.models import Job, Organization, ResumeMetadata, User, UserRole  # noqa: E402
from app.models.job import JobSkill, JobStatus  # noqa: E402


def _reset_schema():
    with engine.begin() as conn:
        conn.execute(sa.text("DROP SCHEMA public CASCADE"))
        conn.execute(sa.text("CREATE SCHEMA public"))
    Base.metadata.create_all(bind=engine)
    from app.talent import vectors
    from app.talent.schema import ensure_db_objects

    vectors.reset_cache()
    with engine.begin() as conn:
        ensure_db_objects(conn)


@pytest.fixture(scope="session")
def client():
    _reset_schema()
    from app.main import app

    with TestClient(app) as test_client:
        yield test_client
    engine.dispose()


@pytest.fixture(autouse=True)
def _clean_tables(client):
    yield
    names = ", ".join(f'"{t.name}"' for t in Base.metadata.sorted_tables)
    with engine.begin() as conn:
        conn.execute(sa.text(f"TRUNCATE {names} RESTART IDENTITY CASCADE"))
    try:
        from app.talent.transports import memory_outbox
    except ImportError:  # before Phase 1 there is no outbox
        return
    memory_outbox.clear()


@pytest.fixture
def db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


class World:
    """Factories for the objects tests need."""

    def __init__(self, db):
        self.db = db

    def org(self, name="Acme", domain=None, resume_db=True):
        org = Organization(name=name, domain=domain or f"{name.lower()}-{secrets.token_hex(3)}.test",
                           is_active=True, allow_resume_database=resume_db)
        self.db.add(org)
        self.db.commit()
        return org

    def user(self, org, role=UserRole.admin, email=None):
        u = User(name="U", email=email or f"{role.value}-{secrets.token_hex(4)}@{org.domain if org else 'root.test'}",
                 password_hash=hash_password("correct-horse-battery"), role=role,
                 organization_id=org.id if org else None, is_active=True, is_email_verified=True)
        self.db.add(u)
        self.db.commit()
        return u

    def headers(self, user):
        role = user.role.value if hasattr(user.role, "value") else str(user.role)
        token = create_access_token(user.id, role, user.organization_id, user.password_hash)
        return {"Authorization": f"Bearer {token}"}

    def profile(self, org, email=None, phone=None, created_at=None, **fields):
        meta = ResumeMetadata(
            organization_id=org.id,
            candidate_email=email or f"cand-{secrets.token_hex(4)}@mail.test",
            candidate_name=fields.pop("candidate_name", "Asha Rao"),
            phone=phone,
            parsing_status=fields.pop("parsing_status", "completed"),
            **fields,
        )
        self.db.add(meta)
        self.db.commit()
        if created_at is not None:
            self.db.execute(
                sa.update(ResumeMetadata).where(ResumeMetadata.id == meta.id).values(created_at=created_at)
            )
            self.db.commit()
            self.db.refresh(meta)
        return meta

    def enable(self, org, *loops, quiet_hours=None, **settings):
        """Switch loops on for `org`. Quiet hours are off unless given as (start, end):
        the suite must not depend on the hour it runs at."""
        from app.models.talent import TalentSettings

        row = self.db.query(TalentSettings).filter_by(organization_id=org.id).first()
        if row is None:
            row = TalentSettings(organization_id=org.id, flags={}, timezone="Asia/Kolkata", refresh_after_days=180,
                                 retention_months=24, default_autonomy="copilot")
            self.db.add(row)
        row.flags = {**(row.flags or {}), **{loop: True for loop in loops}}
        row.quiet_hours_start, row.quiet_hours_end = quiet_hours or (0, 0)
        for k, v in settings.items():
            setattr(row, k, v)
        self.db.commit()
        return row

    def consent(self, profile, *purposes, channel="email", status="granted"):
        from app.models.talent import ContactConsent

        for purpose in purposes or ("refresh", "jobs", "interview"):
            self.db.merge(ContactConsent(profile_id=profile.id, channel=channel, purpose=purpose, status=status,
                                         source="test", granted_at=datetime.utcnow()))
        self.db.commit()

    def job(self, org, title="Backend Engineer", skills=("Python", "PostgreSQL"), **fields):
        job = Job(title=title, department="Engineering", location=fields.pop("location", "Bengaluru"),
                  job_type="Full-time", status=JobStatus.open, organization_id=org.id,
                  experience_min=fields.pop("experience_min", 2), experience_max=fields.pop("experience_max", 8),
                  **fields)
        self.db.add(job)
        self.db.flush()
        for s in skills:
            self.db.add(JobSkill(job_id=job.id, name=s, skill=s))
        self.db.commit()
        return job


@pytest.fixture
def world(db):
    return World(db)


@pytest.fixture(autouse=True)
def _no_real_events():
    """No test ever talks to an Inngest server; `events` below captures instead."""
    from app.agent import client as agent_client

    agent_client.set_sink(lambda name, data: None)
    yield
    agent_client.set_sink(None)


@pytest.fixture
def events():
    """Capture Inngest events emitted through app.agent.client.emit."""
    from app.agent import client as agent_client

    captured = []
    agent_client.set_sink(lambda name, data: captured.append((name, data)))
    yield captured
    agent_client.set_sink(None)


@pytest.fixture
def outbox():
    from app.talent.transports import memory_outbox

    return memory_outbox


class CapturingInngest:
    """A mocked Inngest client that records what step.send_event sends."""

    def __new__(cls):
        from inngest.experimental import mocked

        class _Client(mocked.Inngest):
            sent = []

            async def send(self, events, *, skip_middleware=False):
                events = events if isinstance(events, list) else [events]
                self.sent.extend(events)
                return ["0" * 26 for _ in events]

        client = _Client(app_id="talent-test", is_production=False)
        client.sent = []
        return client


def utcnow():
    return datetime.utcnow()
