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


# ── Connecting from AskMira, per organization ───────────────────────────────

CALLBACK = "https://askmira.test/settings/connectors/callback"


def _pkce():
    import base64
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    return verifier, challenge


def _approve(client, headers, tenant, challenge, redirect_uri=CALLBACK):
    return client.post("/integrations/askmira/connect/approve", headers=headers, json={
        "tenant_id": tenant, "tenant_name": "CGB Solutions", "redirect_uri": redirect_uri,
        "state": "opaque-state-from-askmira", "challenge": challenge,
    })


def _connect(client, headers, tenant):
    """Allow, then swap the code as AskMira's server would. Returns the secret."""
    from urllib.parse import parse_qs, urlparse
    verifier, challenge = _pkce()
    allowed = _approve(client, headers, tenant, challenge)
    assert allowed.status_code == 200, allowed.text
    query = parse_qs(urlparse(allowed.json()["redirect_to"]).query)
    assert query["state"] == ["opaque-state-from-askmira"] and query["product"] == ["interview"]
    swapped = client.post("/integrations/askmira/connect/token",
                          json={"code": query["code"][0], "verifier": verifier, "tenant_id": tenant})
    assert swapped.status_code == 200, swapped.text
    return swapped.json()


def _fresh_org():
    """An organization of its own, and its admin's headers: tests here connect and disconnect."""
    from app.auth import create_access_token
    with SessionLocal() as db:
        org = Organization(name=f"Conn {uuid.uuid4().hex[:6]}", domain=f"conn-{uuid.uuid4().hex[:8]}.test",
                           is_active=True)
        db.add(org)
        db.flush()
        admin = User(name="Org Admin", email=f"admin-{uuid.uuid4().hex[:8]}@{org.domain}",
                     password_hash=hash_password("x" * 12), role=UserRole.admin,
                     organization_id=org.id, is_active=True)
        db.add(admin)
        db.commit()
        token = create_access_token(admin.id, admin.role.value, admin.organization_id)
        return org.id, {"Authorization": f"Bearer {token}"}


def _sync(client, tenant, secret, users):
    return _provision(client, "/users/sync", {"tenant_id": tenant, "complete": True, "users": users},
                      secret=secret)


def test_an_org_admin_connects_and_the_secret_reaches_only_that_org(client, world):
    tenant, other = str(uuid.uuid4()), str(uuid.uuid4())
    org_id, admin = _fresh_org()
    me = client.get("/integrations/askmira/my-connection", headers=admin).json()
    assert me["connected"] is False

    made = _connect(client, admin, tenant)
    assert made["external_ref"] == str(org_id) and len(made["secret"]) >= 32
    secret = made["secret"]
    with SessionLocal() as db:
        from app.models.askmira import AskmiraOrgConnection
        row = db.get(AskmiraOrgConnection, org_id)
        assert row.askmira_name == "CGB Solutions" and secret not in row.secret_nonce  # never stored
        assert db.get(Organization, org_id).askmira_tenant_id == tenant

    people = [{"id": str(uuid.uuid4()), "email": f"hire-{uuid.uuid4().hex[:6]}@conn-test.com", "name": "HR",
               "role": "admin"}]
    assert _sync(client, tenant, secret, people).status_code == 200
    # The server-wide secret no longer speaks for a workspace that connected itself…
    assert _sync(client, tenant, SECRET, people).status_code == 401
    # …and this workspace's secret can't speak for any other.
    assert _sync(client, other, secret, people).status_code == 401

    status = client.get("/integrations/askmira/my-connection", headers=admin).json()
    assert status["connected"] is True and status["askmira_origin"] == "https://askmira.test"

    # Disconnected here: the secret stops working at once.
    gone = client.delete("/integrations/askmira/my-connection", headers=admin)
    assert gone.status_code == 200 and gone.json()["connected"] is False
    assert _sync(client, tenant, secret, people).status_code == 401


def test_a_code_is_one_use_and_useless_without_the_verifier(client, world):
    from urllib.parse import parse_qs, urlparse
    tenant = str(uuid.uuid4())
    org_id, admin = _fresh_org()
    verifier, challenge = _pkce()
    code = parse_qs(urlparse(_approve(client, admin, tenant, challenge).json()["redirect_to"]).query)["code"][0]
    swap = lambda v, t=tenant: client.post(  # noqa: E731
        "/integrations/askmira/connect/token", json={"code": code, "verifier": v, "tenant_id": t})
    assert swap(secrets.token_urlsafe(48)).status_code == 400  # wrong verifier burns the code
    assert swap(verifier).status_code == 400
    with SessionLocal() as db:
        assert db.get(Organization, org_id).askmira_tenant_id is None


def test_only_an_org_admin_may_allow_and_only_to_an_askmira_callback(client, world):
    tenant = str(uuid.uuid4())
    _, challenge = _pkce()
    assert _approve(client, world.orphan, tenant, challenge).status_code == 403  # recruiter, no org
    assert _approve(client, world.root, tenant, challenge).status_code == 403  # no organization to give
    for bad in ("http://askmira.test/settings/connectors/callback",  # plain http off localhost
                "https://evil.test/steal",  # not AskMira's callback
                "https://askmira.test/settings/connectors/callback?next=//evil.test"):
        assert _approve(client, world.admin_b, tenant, challenge, redirect_uri=bad).status_code == 422, bad


def test_an_org_connects_to_one_workspace_and_askmira_can_revoke(client, world):
    tenant = str(uuid.uuid4())
    _, admin = _fresh_org()
    secret = _connect(client, admin, tenant)["secret"]
    _, challenge = _pkce()
    assert _approve(client, admin, str(uuid.uuid4()), challenge).status_code == 409
    # Nor may a second organization take a workspace that is already connected.
    _, someone_else = _fresh_org()
    assert _approve(client, someone_else, tenant, challenge).status_code == 409

    body = json.dumps({"tenant_id": tenant}).encode()
    revoke = _token("revoke", tenant, secret=secret, bh=hashlib.sha256(body).hexdigest())
    done = client.post("/integrations/askmira/connect/revoke", content=body,
                       headers={"Authorization": f"Bearer {revoke}", "Content-Type": "application/json"})
    assert done.status_code == 200
    assert client.get("/integrations/askmira/my-connection", headers=admin).json()["connected"] is False


def test_a_super_admin_of_the_connected_org_can_read_it_but_never_sign_in_from_askmira(client, world):
    tenant = str(uuid.uuid4())
    org_id, admin = _fresh_org()
    secret = _connect(client, admin, tenant)["secret"]
    with SessionLocal() as db:
        owner = User(name="Owner", email=f"owner-{uuid.uuid4().hex[:6]}@conn-test.com",
                     password_hash=hash_password("x" * 12), role=UserRole.super_admin,
                     organization_id=org_id, is_active=True)
        db.add(owner)
        db.commit()
        owner_email = owner.email

    def ask(email):
        payload = {"tenant_id": tenant, "actor": {"id": str(uuid.uuid4()), "email": email, "role": "admin"},
                   "kind": "jobs", "params": {}}
        body = json.dumps(payload).encode()
        tok = _token("query", tenant, secret=secret, bh=hashlib.sha256(body).hexdigest())
        return client.post("/integrations/askmira/query", content=body,
                           headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json"})

    assert ask(owner_email).status_code == 200  # reads this organization's jobs
    launch = _token("launch", str(uuid.uuid4()), secret=secret, tid=tenant, ext=str(org_id),
                    email=owner_email, name="Owner", role="admin")
    refused = client.post("/auth/askmira/exchange", json={"token": launch})
    assert refused.status_code == 403 and "Sign in to AnytimeHire directly" in refused.json()["detail"]
