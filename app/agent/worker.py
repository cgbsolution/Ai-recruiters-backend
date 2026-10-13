"""
The Talent Agent worker: a separate ASGI app that serves the Inngest functions
at /api/inngest, so agent work (CV parsing, matching, scoring, sending) never
competes with live interviews for the API process's event loop.

    uvicorn app.agent.worker:app --host 0.0.0.0 --port 8001

In the Docker image, start.sh runs it next to the API on 127.0.0.1:8001 and
the API forwards /api/inngest to it (app.agent.proxy), so Inngest is pointed
at https://<API host>/api/inngest. Deployed as its own service instead
(TALENT_EMBEDDED_WORKER=false on the API), point Inngest at
https://<worker host>/api/inngest.
Local development: `npx inngest-cli@latest dev -u http://localhost:8001/api/inngest`
with INNGEST_DEV=1.

It does not import app.main, so it runs no migrations, no recording sweeper
and no bulk-upload queue — those stay with the single API process.
"""

import logging

import inngest.fast_api
from fastapi import FastAPI, Request

import app.models  # noqa: F401 — register every table
from app.agent.client import inngest_client
from app.agent.common import run_db
from app.agent.registry import FUNCTIONS
from app.talent import heartbeat
from app.talent.config import talent_config

logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("app").setLevel(logging.INFO)

app = FastAPI(title="AnytimeHire Talent Agent worker", docs_url=None, redoc_url=None, openapi_url=None)
inngest.fast_api.serve(app, inngest_client, FUNCTIONS)


@app.middleware("http")
async def check_in_when_synced(request: Request, call_next):
    """A successful sync (PUT /api/inngest) proves Inngest can reach this worker:
    record a check-in at once, so the app's "worker never checked in" warning
    clears right after syncing instead of at the next 10-minute check-in."""
    response = await call_next(request)
    if request.method == "PUT" and request.url.path == "/api/inngest" and response.status_code == 200:
        try:
            await run_db(heartbeat.beat)
        except Exception as exc:  # the sync itself succeeded; the cron check-in follows
            logging.getLogger("app.agent").warning("Check-in after sync failed: %s", type(exc).__name__)
    return response


@app.get("/health")
def health():
    return {"status": "ok", "agent_enabled": talent_config.TALENT_AGENT_ENABLED, "functions": len(FUNCTIONS)}
