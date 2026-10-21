import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.auth import create_access_token, hash_password
from app.database import Base, get_db
from app.main import app
from app.models import Organization, User, UserRole

TEST_DATABASE_URL = "sqlite:///./test.db"

engine = create_engine(TEST_DATABASE_URL, connect_args={"check_same_thread": False})
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


@pytest.fixture(scope="session", autouse=True)
def setup_db():
    Base.metadata.create_all(bind=engine)
    yield
    Base.metadata.drop_all(bind=engine)


@pytest.fixture
def db():
    session = TestingSessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture(scope="session")
def auth_token(setup_db):
    """A signed-in recruiter of an active organization. Every API route needs
    one; these tests were written before sign-in was enforced."""
    session = TestingSessionLocal()
    try:
        # The job fixtures use the code editor, which a super admin turns on per org.
        org = Organization(
            name="Test Org", domain="tests.example", is_active=True, allow_code_editor=True
        )
        session.add(org)
        session.flush()
        user = User(
            name="Test Recruiter",
            email="recruiter@tests.example",
            password_hash=hash_password("Passw0rd!!"),
            role=UserRole.recruiter,
            organization_id=org.id,
            is_active=True,
            is_email_verified=True,
        )
        session.add(user)
        session.commit()
        return create_access_token(
            user.id, user.role.value, org_id=org.id, password_hash=user.password_hash
        )
    finally:
        session.close()


@pytest.fixture
def client(db, auth_token):
    def override_get_db():
        try:
            yield db
        finally:
            pass

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app, headers={"Authorization": f"Bearer {auth_token}"}) as c:
        yield c
    app.dependency_overrides.clear()
