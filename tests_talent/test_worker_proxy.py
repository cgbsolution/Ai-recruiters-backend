"""The API forwards /api/inngest to the worker running next to it
(app.agent.proxy), and the worker checks in as soon as Inngest syncs it."""
import httpx
import pytest
from fastapi.testclient import TestClient
from inngest._internal.comm_lib.handler import CommHandler
from inngest._internal.comm_lib.models import CommResponse

from app.agent import proxy
from app.models.talent import AgentHeartbeat
from app.talent import heartbeat
from app.talent.config import talent_config


def _fake_worker(monkeypatch, handle):
    monkeypatch.setattr(proxy, "_client", httpx.AsyncClient(base_url="http://127.0.0.1:8001",
                                                            transport=httpx.MockTransport(handle)))


def test_api_forwards_inngest_requests_to_the_worker(client, monkeypatch):
    seen = {}

    def handle(request: httpx.Request) -> httpx.Response:
        seen.update(method=request.method, url=str(request.url), body=request.content, headers=request.headers)
        return httpx.Response(206, json={"ok": True}, headers={
            "x-inngest-sdk": "inngest-py:v0.5.19", "x-inngest-req-version": "1", "server": "uvicorn"})

    _fake_worker(monkeypatch, handle)
    r = client.post("/api/inngest?fnId=match-job&stepId=step", content=b'{"event":{}}', headers={
        "content-type": "application/json", "x-inngest-signature": "t=1&s=abc", "host": "api.example.com",
        "x-forwarded-proto": "https"})
    assert r.status_code == 206 and r.json() == {"ok": True}
    # Inngest reads its own response headers; the worker's server header isn't repeated.
    assert r.headers["x-inngest-sdk"] == "inngest-py:v0.5.19" and r.headers["x-inngest-req-version"] == "1"
    assert r.headers.get("server") != "uvicorn"
    assert seen["method"] == "POST"
    assert seen["url"] == "http://127.0.0.1:8001/api/inngest?fnId=match-job&stepId=step"
    assert seen["body"] == b'{"event":{}}'
    assert seen["headers"]["x-inngest-signature"] == "t=1&s=abc"  # the worker verifies it
    # The worker registers the API's public URL: same Host, and the scheme the
    # API itself saw — never a client-supplied X-Forwarded-Proto.
    assert seen["headers"]["host"] == "api.example.com"
    assert seen["headers"]["x-forwarded-proto"] == "http"


@pytest.mark.parametrize("method", ["GET", "PUT"])
def test_sync_and_introspection_are_forwarded_too(client, monkeypatch, method):
    _fake_worker(monkeypatch, lambda request: httpx.Response(200, json={"method": request.method}))
    assert client.request(method, "/api/inngest").json() == {"method": method}


def test_worker_not_running_is_a_clear_502(client, monkeypatch):
    def refuse(request):
        raise httpx.ConnectError("refused")

    _fake_worker(monkeypatch, refuse)
    r = client.put("/api/inngest")
    assert r.status_code == 502 and "isn't running" in r.json()["detail"]


def test_worker_checks_in_when_a_sync_succeeds(db, monkeypatch):
    from app.agent.worker import app as worker_app

    db.query(AgentHeartbeat).delete()
    db.commit()
    with TestClient(worker_app) as worker:
        async def failed_sync(self, req, *args, **kwargs):
            return CommResponse(body={"message": "bad signing key"}, status_code=500)

        monkeypatch.setattr(CommHandler, "put", failed_sync)
        assert worker.put("/api/inngest").status_code == 500
        db.expire_all()
        assert heartbeat.worker_status(db)["last_seen_at"] is None

        async def synced(self, req, *args, **kwargs):
            return CommResponse(body={"ok": True})

        monkeypatch.setattr(CommHandler, "put", synced)
        assert worker.put("/api/inngest").status_code == 200
        db.expire_all()
        assert heartbeat.worker_status(db)["ok"] is True


def test_warning_names_the_url_to_sync(db, monkeypatch):
    db.query(AgentHeartbeat).delete()
    db.commit()
    monkeypatch.setattr(talent_config, "TALENT_PUBLIC_API_URL", "https://api.example.com/")
    assert "https://api.example.com/api/inngest" in heartbeat.worker_problem(db)
    monkeypatch.setattr(talent_config, "TALENT_EMBEDDED_WORKER", False)
    assert "https://<your worker address>/api/inngest" in heartbeat.worker_problem(db)


def test_settings_warns_when_the_signing_key_is_missing(client, world, db, monkeypatch):
    db.query(AgentHeartbeat).delete()
    db.commit()
    org = world.org()
    h = world.headers(world.user(org))
    monkeypatch.setattr(talent_config, "INNGEST_DEV", "")
    monkeypatch.setattr(talent_config, "INNGEST_EVENT_KEY", "evt-key")
    monkeypatch.setattr(talent_config, "INNGEST_SIGNING_KEY", "")
    warnings = client.get("/talent/settings", headers=h).json()["warnings"]
    assert any(w.startswith("INNGEST_SIGNING_KEY is not set") for w in warnings)
    monkeypatch.setattr(talent_config, "INNGEST_SIGNING_KEY", "signkey-prod-123")
    warnings = client.get("/talent/settings", headers=h).json()["warnings"]
    assert not any("INNGEST_SIGNING_KEY" in w for w in warnings)
    assert any("never checked in" in w for w in warnings)


def test_settings_names_inngest_dev_left_on_a_server(client, world, db):
    """A local .env copied to production (INNGEST_DEV=1) shows as the real cause,
    not only as "the worker never checked in"."""
    db.query(AgentHeartbeat).delete()
    db.commit()
    h = world.headers(world.user(world.org()))
    on_server = client.get("/talent/settings", headers={**h, "host": "api.example.com"}).json()["warnings"]
    assert any(w.startswith("INNGEST_DEV is set on this server") for w in on_server)
    assert not any("never checked in" in w for w in on_server)
    for local in ("testserver", "localhost:8000", "127.0.0.1:8000", "192.168.1.45:8000"):
        warnings = client.get("/talent/settings", headers={**h, "host": local}).json()["warnings"]
        assert not any("INNGEST_DEV" in w for w in warnings), local


def test_run_matching_names_inngest_dev_left_on_a_server(client, world, db, monkeypatch):
    from app.agent import client as agent_client
    from tests_talent.test_match_progress import _job

    org = world.org()
    world.enable(org, "match")
    job = _job(world, org)
    h = world.headers(world.user(org))
    monkeypatch.setattr(agent_client, "_sink", None)  # really try to send: dev mode, no dev server
    monkeypatch.setattr(agent_client.inngest_client, "send_sync", lambda *a, **k: (_ for _ in ()).throw(OSError("refused")))
    r = client.post(f"/talent/jobs/{job.id}/match", headers={**h, "host": "api.example.com"})
    assert r.status_code == 503 and r.json()["detail"].startswith("INNGEST_DEV is set on this server")
    r = client.post(f"/talent/jobs/{job.id}/match", headers=h)  # locally: the general message
    assert r.status_code == 503 and "Inngest is unreachable" in r.json()["detail"]
