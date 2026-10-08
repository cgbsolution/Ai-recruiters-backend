"""
The Talent Agent worker: a separate ASGI app that serves the Inngest functions
at /api/inngest, so agent work (CV parsing, matching, scoring, sending) never
competes with live interviews for the API process's event loop.

    uvicorn app.agent.worker:app --host 0.0.0.0 --port 8001

Point Inngest (Cloud or the self-hosted server) at https://<worker host>/api/inngest.
Local development: `npx inngest-cli@latest dev -u http://localhost:8001/api/inngest`
with INNGEST_DEV=1.

It does not import app.main, so it runs no migrations, no recording sweeper
and no bulk-upload queue — those stay with the single API process.
"""

import logging

import inngest.fast_api
from fastapi import FastAPI

import app.models  # noqa: F401 — register every table
from app.agent.client import inngest_client
from app.agent.registry import FUNCTIONS
from app.talent.config import talent_config

logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("app").setLevel(logging.INFO)

app = FastAPI(title="AnytimeHire Talent Agent worker", docs_url=None, redoc_url=None, openapi_url=None)
inngest.fast_api.serve(app, inngest_client, FUNCTIONS)


@app.get("/health")
def health():
    return {"status": "ok", "agent_enabled": talent_config.TALENT_AGENT_ENABLED, "functions": len(FUNCTIONS)}
