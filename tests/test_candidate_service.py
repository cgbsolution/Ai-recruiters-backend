import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models.candidate import Candidate, EmailLog
from app.models.job import Job  # noqa: F401 - registers job tables for metadata
from app.models.organization import Organization  # noqa: F401 - registers org table
from app.models.resume_metadata import ResumeMetadata  # noqa: F401 - registers FK table
from app.models.user import User  # noqa: F401 - resolves org relationship target
from app.schemas.candidate import CandidateUpdate
from app.services import candidate_service


@pytest.fixture
def db_session():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(bind=engine)
        engine.dispose()


def _add_candidate(db, name: str, email: str, job_id: int) -> Candidate:
    candidate = Candidate(name=name, email=email, job_id=job_id)
    db.add(candidate)
    db.commit()
    db.refresh(candidate)
    return candidate


def test_update_candidate_normalizes_email_and_text(db_session):
    candidate = _add_candidate(db_session, "Old Name", "old@example.com", 1)

    updated = candidate_service.update_candidate(
        db_session,
        candidate.id,
        CandidateUpdate(name="  New Name  ", email="  NEW@Example.COM  ", phone="  555  "),
    )

    assert updated is not None
    assert updated.name == "New Name"
    assert updated.email == "new@example.com"
    assert updated.phone == "555"


def test_update_candidate_rejects_duplicate_email_in_same_job(db_session):
    candidate = _add_candidate(db_session, "First", "first@example.com", 1)
    _add_candidate(db_session, "Existing", "taken@example.com", 1)

    with pytest.raises(candidate_service.DuplicateCandidateEmailError, match="already exists"):
        candidate_service.update_candidate(
            db_session,
            candidate.id,
            CandidateUpdate(email="  TAKEN@Example.COM  "),
        )

    db_session.refresh(candidate)
    assert candidate.email == "first@example.com"


def test_update_candidate_allows_same_email_in_different_job(db_session):
    candidate = _add_candidate(db_session, "First", "first@example.com", 1)
    _add_candidate(db_session, "Other Job", "taken@example.com", 2)

    updated = candidate_service.update_candidate(
        db_session,
        candidate.id,
        CandidateUpdate(email="taken@example.com"),
    )

    assert updated is not None
    assert updated.email == "taken@example.com"


def test_delete_candidate_removes_email_logs(db_session):
    """A candidate who had ever been emailed used to be undeletable: the
    email_logs FK carried no ON DELETE rule, so the DELETE raised
    ForeignKeyViolation on Postgres."""
    candidate = _add_candidate(db_session, "Emailed", "emailed@example.com", 1)
    db_session.add(EmailLog(candidate_id=candidate.id, subject="Interview scheduled"))
    db_session.commit()
    candidate_id = candidate.id

    assert candidate_service.delete_candidate(db_session, candidate_id) is True
    assert db_session.query(Candidate).filter_by(id=candidate_id).count() == 0
    assert db_session.query(EmailLog).filter_by(candidate_id=candidate_id).count() == 0


def test_email_log_fk_declares_cascade():
    """The database-level half of the fix: without ondelete on the model,
    create_all() rebuilds the constraint that made candidates undeletable."""
    fk = next(iter(EmailLog.__table__.c.candidate_id.foreign_keys))
    assert fk.ondelete == "CASCADE"
