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
