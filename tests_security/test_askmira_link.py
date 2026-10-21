"""
The AskMira link (app.routers.askmira): provisioning, people sync and sign-in.

Tokens are minted here exactly as AskMira mints them (HS256, aud
"askmira:interview", iss "askmira", 60-second life, `bh` = SHA-256 of the body
on provisioning calls).
"""
import hashlib
import json
import secrets
import time
import uuid

import pytest
from jose import jwt

from app.auth import hash_password
from app.config import settings
from app.database import SessionLocal
from app.models import Organization, User, UserRole

SECRET = "askmira-test-secret-" + secrets.token_hex(16)


@pytest.fixture(autouse=True)
def linked(monkeypatch):
    monkeypatch.setattr(settings, "ASKMIRA_SHARED_SECRET", SECRET)


def _token(use, sub, *, secret=SECRET, aud="askmira:interview", **extra):
    now = int(time.time())
    claims = {
        "iss": "askmira", "aud": aud, "sub": sub, "use": use,
        "iat": now, "exp": now + 60, "jti": secrets.token_urlsafe(18), **extra,
    }
    return jwt.encode(claims, secret, algorithm="HS256")


def _provision(client, path, payload, *, tamper=False, **token_kw):
    body = json.dumps(payload).encode()
    token = _token("provision", payload["tenant_id"], bh=hashlib.sha256(body).hexdigest(), **token_kw)
    if tamper:
        body = body.replace(b'"name"', b'"name" ', 1)
    return client.post(
        f"/integrations/askmira{path}", content=body,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )


def _company(tenant_id, **extra):
    return {"tenant_id": tenant_id, "name": "Linked Co", "slug": "linked-co", "domain": None, **extra}


def _launch(client, tenant_id, org_id, email, role="admin", name="Asha Rao", sub=None):
    token = _token(
        "launch", sub or str(uuid.uuid4()), tid=tenant_id, ext=str(org_id),
        email=email, name=name, role=role,
    )
    return client.post("/auth/askmira/exchange", json={"token": token}), token


def test_link_is_off_without_a_secret(client, monkeypatch):
    monkeypatch.setattr(settings, "ASKMIRA_SHARED_SECRET", "")
    tenant = str(uuid.uuid4())
    assert _provision(client, "/organizations", _company(tenant)).status_code == 404
    assert client.post("/auth/askmira/exchange", json={"token": "x"}).status_code == 404


def test_forged_or_tampered_assertions_are_refused(client):
    tenant = str(uuid.uuid4())
    assert _provision(client, "/organizations", _company(tenant), secret="wrong-" * 8).status_code == 401
    assert _provision(client, "/organizations", _company(tenant), aud="askmira:expense").status_code == 401
    assert _provision(client, "/organizations", _company(tenant), tamper=True).status_code == 401
    # A token for one workspace can't create another.
    payload = _company(tenant)
    body = json.dumps(payload).encode()
    token = _token("provision", str(uuid.uuid4()), bh=hashlib.sha256(body).hexdigest())
    r = client.post("/integrations/askmira/organizations", content=body,
                    headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    assert r.status_code == 401
    # A launch token is not a provisioning token.
    r = client.post("/integrations/askmira/organizations", content=body,
                    headers={"Authorization": f"Bearer {_token('launch', tenant)}", "Content-Type": "application/json"})
    assert r.status_code == 401


def test_organization_is_created_once_and_adopted_by_domain(client):
    tenant = str(uuid.uuid4())
    first = _provision(client, "/organizations", _company(tenant)).json()
    again = _provision(client, "/organizations", _company(tenant)).json()
    assert first["external_ref"] == again["external_ref"]
    assert first["domain"].endswith(".askmira.invalid")  # no real domain: nobody can self-sign-up into it

    db = SessionLocal()
    existing = Organization(name="Already Here", domain="already-here-test.com", is_active=True)
    db.add(existing)
    db.commit()
    existing_id = existing.id
    db.close()

    other = str(uuid.uuid4())
    adopted = _provision(client, "/organizations", _company(other, domain="Already-Here-Test.com")).json()
    assert adopted["adopted"] is True and adopted["external_ref"] == str(existing_id)

    # Already linked to `other`: a third workspace may not claim it.
    third = _provision(client, "/organizations", _company(str(uuid.uuid4()), domain="already-here-test.com"))
    assert third.status_code == 409


def test_roster_sync_adds_updates_and_switches_off_only_managed_users(client):
    tenant = str(uuid.uuid4())
    org_id = int(_provision(client, "/organizations", _company(tenant, slug="roster-co")).json()["external_ref"])

    db = SessionLocal()
    native = User(name="Made Here", email="native@roster-co-test.com", password_hash=hash_password("Passw0rd!!"),
                  role=UserRole.recruiter, organization_id=org_id, is_active=True)
    elsewhere_org = Organization(name="Elsewhere", domain=f"elsewhere-{uuid.uuid4().hex[:6]}.com", is_active=True)
    db.add_all([native, elsewhere_org])
    db.flush()
    db.add(User(name="Elsewhere", email="taken@roster-co-test.com", password_hash="x",
                role=UserRole.admin, organization_id=elsewhere_org.id, is_active=True))
    db.commit()
    db.close()

    people = [
        {"id": str(uuid.uuid4()), "email": "hr@roster-co-test.com", "name": "HR", "role": "admin"},
        {"id": str(uuid.uuid4()), "email": "mgr@roster-co-test.com", "name": "Manager", "role": "recruiter"},
        {"id": str(uuid.uuid4()), "email": "taken@roster-co-test.com", "name": "Taken", "role": "admin"},
        {"id": str(uuid.uuid4()), "email": "odd@roster-co-test.com", "name": "Odd", "role": "employee"},
    ]
    r = _provision(client, "/users/sync", {"tenant_id": tenant, "complete": True, "users": people})
    assert r.status_code == 200, r.text
    result = r.json()
    assert result["created"] == 2
    assert {s["email"] for s in result["skipped"]} == {"taken@roster-co-test.com", "odd@roster-co-test.com"}

    # The manager leaves AskMira: switched off. The account made here is untouched.
    r = _provision(client, "/users/sync", {"tenant_id": tenant, "complete": True, "users": people[:1]})
    assert r.json()["deactivated"] == 1
    db = SessionLocal()
    by_email = {u.email: u for u in db.query(User).filter(User.organization_id == org_id)}
    assert by_email["mgr@roster-co-test.com"].is_active is False
    assert by_email["native@roster-co-test.com"].is_active is True
    assert by_email["hr@roster-co-test.com"].role == UserRole.admin
    db.close()

    # Back in the roster: switched on again.
    r = _provision(client, "/users/sync", {"tenant_id": tenant, "complete": True, "users": people[:2]})
    assert r.json()["updated"] >= 1


def test_sync_for_an_unlinked_workspace_is_404(client):
    tenant = str(uuid.uuid4())
    r = _provision(client, "/users/sync", {"tenant_id": tenant, "complete": True, "users": []})
    assert r.status_code == 404


def test_launch_signs_in_once_and_creates_a_joiner(client):
    tenant = str(uuid.uuid4())
    org_id = int(_provision(client, "/organizations", _company(tenant, slug="launch-co")).json()["external_ref"])

    r, token = _launch(client, tenant, org_id, "joiner@launch-co-test.com", role="recruiter")
    assert r.status_code == 200, r.text
    session = r.json()
    assert session["user"]["role"] == "recruiter" and session["user"]["organization_id"] == org_id
    me = client.get("/auth/me", headers={"Authorization": f"Bearer {session['access_token']}"})
    assert me.status_code == 200 and me.json()["email"] == "joiner@launch-co-test.com"

    # The same link a second time is refused.
    again = client.post("/auth/askmira/exchange", json={"token": token})
    assert again.status_code == 401

    # A role change in AskMira applies at the next launch.
    r, _ = _launch(client, tenant, org_id, "joiner@launch-co-test.com", role="admin")
    assert r.json()["user"]["role"] == "admin"


def test_launch_cannot_take_over_another_organizations_account(client):
    tenant = str(uuid.uuid4())
    org_id = int(_provision(client, "/organizations", _company(tenant, slug="takeover-co")).json()["external_ref"])
    db = SessionLocal()
    victim_org = Organization(name="Victim", domain=f"victim-{uuid.uuid4().hex[:6]}.com", is_active=True)
    db.add(victim_org)
    db.flush()
    db.add(User(name="Victim Admin", email="boss@victim-test.com", password_hash="x",
                role=UserRole.admin, organization_id=victim_org.id, is_active=True))
    db.commit()
    db.close()

    r, _ = _launch(client, tenant, org_id, "boss@victim-test.com")
    assert r.status_code == 409

    # Nor can a launch name a workspace that isn't linked, or the wrong organization.
    r, _ = _launch(client, str(uuid.uuid4()), org_id, "x@takeover-test.com")
    assert r.status_code == 403
    r, _ = _launch(client, tenant, org_id + 999999, "x@takeover-test.com")
    assert r.status_code == 403


def test_switched_off_user_or_suspended_org_cannot_launch(client):
    tenant = str(uuid.uuid4())
    org_id = int(_provision(client, "/organizations", _company(tenant, slug="off-co")).json()["external_ref"])
    person = {"id": str(uuid.uuid4()), "email": "gone@off-co-test.com", "name": "Gone", "role": "admin"}
    _provision(client, "/users/sync", {"tenant_id": tenant, "complete": True, "users": [person]})
    _provision(client, "/users/sync", {"tenant_id": tenant, "complete": True, "users": []})
    r, _ = _launch(client, tenant, org_id, "gone@off-co-test.com")
    assert r.status_code == 403

    db = SessionLocal()
    db.query(Organization).filter(Organization.id == org_id).update({"is_active": False})
    db.commit()
    db.close()
    r, _ = _launch(client, tenant, org_id, "new@off-co-test.com")
    assert r.status_code == 403


# ── Read-only queries (Ask Mira) ─────────────────────────────────────────────

def _query(client, tenant_id, actor, kind, params=None, *, use="query", sub=None):
    payload = {"tenant_id": tenant_id, "actor": actor, "kind": kind, "params": params or {}}
    body = json.dumps(payload).encode()
    token = _token(use, sub or tenant_id, bh=hashlib.sha256(body).hexdigest())
    return client.post(
        "/integrations/askmira/query", content=body,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )


def _actor(email, role="admin"):
    return {"id": str(uuid.uuid4()), "email": email, "role": role}


def _hiring_world(client, slug):
    """A linked organization with an admin, a recruiter, two jobs and three candidates
    (one an auto-created placeholder), and a second linked organization with its own."""
    from datetime import datetime, timedelta

    from app.models import Candidate, InterviewSession, InterviewToken, Job
    from app.models.candidate import CandidateStatus
    from app.models.interview import SessionStatus
    from app.models.job import JobStatus

    tenant, other = str(uuid.uuid4()), str(uuid.uuid4())
    org_id = int(_provision(client, "/organizations", _company(tenant, slug=slug)).json()["external_ref"])
    other_org = int(
        _provision(client, "/organizations", _company(other, slug=f"{slug}-other")).json()["external_ref"]
    )
    people = [
        {"id": str(uuid.uuid4()), "email": f"hr@{slug}-test.com", "name": "HR", "role": "admin"},
        {"id": str(uuid.uuid4()), "email": f"mgr@{slug}-test.com", "name": "Mgr", "role": "recruiter"},
    ]
    r = _provision(client, "/users/sync", {"tenant_id": tenant, "complete": True, "users": people})
    assert r.status_code == 200, r.text
    _provision(client, "/users/sync", {"tenant_id": other, "complete": True, "users": [
        {"id": str(uuid.uuid4()), "email": f"hr@{slug}-other-test.com", "name": "Other HR", "role": "admin"},
    ]})

    now = datetime.utcnow()
    db = SessionLocal()
    backend = Job(title="Backend Engineer", department="Engineering", location="Pune", job_type="Full-time",
                  openings=2, status=JobStatus.open, organization_id=org_id)
    design = Job(title="Product Designer", department="Design", location="Remote", job_type="Full-time",
                 openings=1, status=JobStatus.draft, organization_id=org_id)
    secret_job = Job(title="Secret Role", department="Elsewhere", location="Delhi", job_type="Full-time",
                     openings=5, status=JobStatus.open, organization_id=other_org)
    db.add_all([backend, design, secret_job])
    db.flush()
    priya = Candidate(name="Priya Nair", email=f"priya@{slug}-cand.com", phone="+91 90000 00001",
                      job_id=backend.id, status=CandidateStatus.interview, candidate_stage="AI Round",
                      interview_status="Scheduled", interview_date=now + timedelta(days=2))
    ravi = Candidate(name="Ravi Kumar", email=f"ravi@{slug}-cand.com", job_id=backend.id,
                     status=CandidateStatus.applied, candidate_stage="AI Round",
                     interview_status="Terminated – Cheating Detected",
                     interview_date=now - timedelta(days=1),
                     score=92.0, ai_cheating_score=10.0, resume_fit_score="high")
    placeholder = Candidate(name="Pending upload", email=f"pending_{uuid.uuid4().hex[:6]}@resume.local",
                            job_id=backend.id)
    stranger = Candidate(name="Other Org Person", email=f"x@{slug}-cand.com", job_id=secret_job.id,
                         interview_status="Scheduled", interview_date=now + timedelta(days=1))
    db.add_all([priya, ravi, placeholder, stranger])
    db.flush()
    token = InterviewToken(job_id=backend.id, token=secrets.token_hex(16))
    db.add(token)
    db.flush()
    db.add(InterviewSession(token_id=token.id, candidate_id=ravi.id, job_id=backend.id,
                            status=SessionStatus.completed, completed_at=now - timedelta(days=1)))
    db.commit()
    db.close()
    return tenant, other, people


def test_query_answers_for_the_linked_organization_only(client):
    from datetime import datetime, timedelta, timezone

    tenant, other, people = _hiring_world(client, "hire-q")
    hr = _actor(people[0]["email"])

    r = _query(client, tenant, hr, "overview")
    assert r.status_code == 200, r.text
    overview = r.json()
    assert overview["kind"] == "overview"
    assert overview["jobs"] == {"Open": 1, "Draft": 1}
    assert overview["open_positions"] == 2  # the other organization's five are not here
    assert overview["candidates"] == {"Interview": 1, "Applied": 1}  # the placeholder is no applicant
    assert overview["upcoming_interviews"] == 1
    assert overview["completed_interviews_30d"] == 1

    jobs = _query(client, tenant, hr, "jobs").json()
    assert jobs["total"] == 2
    by_title = {job["title"]: job for job in jobs["jobs"]}
    assert set(by_title) == {"Backend Engineer", "Product Designer"}
    assert by_title["Backend Engineer"]["applicants"] == 2
    assert by_title["Backend Engineer"]["interviews_held"] == 1
    assert _query(client, tenant, hr, "jobs", {"status": "draft"}).json()["total"] == 1
    assert _query(client, tenant, hr, "jobs", {"query": "engin"}).json()["total"] == 1

    r = _query(client, tenant, hr, "candidates")
    candidates = r.json()
    assert {c["name"] for c in candidates["candidates"]} == {"Priya Nair", "Ravi Kumar"}
    ravi = next(c for c in candidates["candidates"] if c["name"] == "Ravi Kumar")
    assert ravi["interview"] == "Ended early"
    # No contact details, scores, fit score or proctoring finding ever leave.
    text = r.text.lower()
    for word in ("cheat", "score", "priya@", "+91", "resume", "high"):
        assert word not in text, word
    assert _query(client, tenant, hr, "candidates", {"query": "pri"}).json()["total"] == 1
    applied = _query(client, tenant, hr, "candidates", {"status": "applied"}).json()
    assert [c["name"] for c in applied["candidates"]] == ["Ravi Kumar"]
    # A LIKE wildcard in the search is taken literally.
    assert _query(client, tenant, hr, "candidates", {"query": "%"}).json()["total"] == 0

    now = datetime.now(timezone.utc)
    window = {"start": (now - timedelta(days=3)).isoformat(), "end": (now + timedelta(days=3)).isoformat()}
    interviews = _query(client, tenant, hr, "interviews", window).json()
    assert [i["candidate"] for i in interviews["interviews"]] == ["Ravi Kumar", "Priya Nair"]
    assert [i["status"] for i in interviews["interviews"]] == ["Ended early", "Scheduled"]

    # A recruiter sees what they see in the tool: the whole organization.
    mgr = _actor(people[1]["email"], role="recruiter")
    assert _query(client, tenant, mgr, "jobs").json()["total"] == 2

    # The other workspace sees only its own.
    other_hr = _actor("hr@hire-q-other-test.com")
    assert [j["title"] for j in _query(client, other, other_hr, "jobs").json()["jobs"]] == ["Secret Role"]


def test_query_refuses_strangers_other_workspaces_and_other_tokens(client):
    tenant, other, people = _hiring_world(client, "hire-r")
    hr = _actor(people[0]["email"])

    # A provisioning or launch token is not a query token.
    assert _query(client, tenant, hr, "jobs", use="provision").status_code == 401
    assert _query(client, tenant, hr, "jobs", use="launch").status_code == 401
    # A token for one workspace can't ask about another.
    assert _query(client, other, hr, "jobs", sub=tenant).status_code == 401
    # Someone from the other organization can't read this one.
    assert _query(client, tenant, _actor("hr@hire-r-other-test.com"), "jobs").status_code == 403
    # Nor can someone unknown here, or someone AskMira gives no hiring role.
    assert _query(client, tenant, _actor("nobody@hire-r-test.com"), "jobs").status_code == 403
    assert _query(client, tenant, _actor(people[0]["email"], role="employee"), "jobs").status_code == 403
    # A workspace that isn't linked is not found.
    assert _query(client, str(uuid.uuid4()), hr, "jobs").status_code == 404
    assert _query(client, tenant, hr, "salaries").status_code == 422
    too_long = {"start": "2026-01-01T00:00:00+00:00", "end": "2026-12-31T00:00:00+00:00"}
    assert _query(client, tenant, hr, "interviews", too_long).status_code == 422

    # Switched off by AskMira: no more answers.
    _provision(client, "/users/sync", {"tenant_id": tenant, "complete": True, "users": people[1:]})
    assert _query(client, tenant, hr, "jobs").status_code == 403


# ── Connection key (no .env edit) ────────────────────────────────────────────

def _decode_key(key):
    import base64
    body = key.removeprefix("amk1.")
    return json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))


def _handshake(client, secret):
    body = json.dumps({"product": "interview", "from": "askmira"}).encode()
    token = _token("handshake", "askmira", secret=secret, bh=hashlib.sha256(body).hexdigest())
    return client.post(
        "/integrations/askmira/handshake", content=body,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )


def test_connection_key_connects_askmira_without_env(client, world, monkeypatch):
    monkeypatch.setattr(settings, "ASKMIRA_SHARED_SECRET", "")
    addresses = {"api_url": "https://api.anytimehire.test", "app_url": "https://app.anytimehire.test"}
    # Only the platform super admin may generate one.
    assert client.post("/integrations/askmira/connection-key", json=addresses,
                       headers=world.admin_a).status_code == 403
    assert client.get("/integrations/askmira/connection", headers=world.root).json()["connected"] is False

    made = client.post("/integrations/askmira/connection-key", json=addresses, headers=world.root)
    assert made.status_code == 200, made.text
    key = _decode_key(made.json()["key"])
    assert key["p"] == "interview" and key["api"] == addresses["api_url"] and len(key["s"]) >= 32
    with SessionLocal() as db:
        from app.models.askmira import AskmiraConnection
        row = db.get(AskmiraConnection, 1)
        assert row.pending_nonce and key["s"] not in (row.pending_nonce or "")  # the secret is never stored

    # Pending only: provisioning is not accepted with it yet.
    tenant = str(uuid.uuid4())
    assert _provision(client, "/organizations", _company(tenant), secret=key["s"]).status_code == 404

    assert _handshake(client, "a-secret-this-server-never-issued-0123456789").status_code == 401
    done = _handshake(client, key["s"])
    assert done.status_code == 200 and done.json()["product"] == "interview"
    status = client.get("/integrations/askmira/connection", headers=world.root).json()
    assert status["connected"] is True and status["source"] == "key" and status["pending_until"] is None

    created = _provision(client, "/organizations", _company(tenant), secret=key["s"])
    assert created.status_code == 200, created.text
    # Checking again later with the active secret works, and changes nothing.
    assert _handshake(client, key["s"]).status_code == 200

    gone = client.delete("/integrations/askmira/connection", headers=world.root)
    assert gone.status_code == 200 and gone.json()["connected"] is False
    assert _provision(client, "/organizations", _company(tenant), secret=key["s"]).status_code == 404


def test_a_new_key_replaces_the_connection_only_once_its_handshake_lands(client, world):
    addresses = {"api_url": "https://api.anytimehire.test", "app_url": "https://app.anytimehire.test"}
    first = _decode_key(client.post("/integrations/askmira/connection-key", json=addresses,
                                    headers=world.root).json()["key"])
    assert _handshake(client, first["s"]).status_code == 200
    second = _decode_key(client.post("/integrations/askmira/connection-key", json=addresses,
                                     headers=world.root).json()["key"])
    tenant = str(uuid.uuid4())
    # Until AskMira uses the new key, the old one still works.
    assert _provision(client, "/organizations", _company(tenant), secret=first["s"]).status_code == 200
    assert _handshake(client, second["s"]).status_code == 200
    assert _provision(client, "/organizations", _company(tenant), secret=first["s"]).status_code == 401
    assert _provision(client, "/organizations", _company(tenant), secret=second["s"]).status_code == 200
    client.delete("/integrations/askmira/connection", headers=world.root)


def test_connection_key_refuses_plain_http_off_localhost(client, world):
    bad = {"api_url": "http://api.anytimehire.test", "app_url": "https://app.anytimehire.test"}
    assert client.post("/integrations/askmira/connection-key", json=bad, headers=world.root).status_code == 422
