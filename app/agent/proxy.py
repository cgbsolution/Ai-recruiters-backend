"""
/api/inngest on the API, forwarded to the Talent Agent worker that start.sh
runs next to it (TALENT_WORKER_URL, 127.0.0.1:8001).

Inngest then needs only one URL — the API's own — while agent work still runs
in its own process, so matching 40K profiles never competes with live
interviews for the API's event loop. The worker checks each request's Inngest
signature itself; this route only passes requests and responses through.
"""

from __future__ import annotations

import logging
from typing import Optional

import httpx
from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse

from app.talent.config import talent_config

logger = logging.getLogger("app.agent")
router = APIRouter(include_in_schema=False)

SERVE_PATH = "/api/inngest"
# Hop-by-hop headers, and ones the API's own server sets on the way out.
_DROP_REQUEST = {"connection", "keep-alive", "proxy-authorization", "te", "trailer", "transfer-encoding",
                 "upgrade", "content-length", "x-forwarded-proto"}
_DROP_RESPONSE = {"connection", "keep-alive", "proxy-authenticate", "te", "trailer", "transfer-encoding",
                  "upgrade", "content-length", "content-encoding", "server", "date"}

_client: Optional[httpx.AsyncClient] = None


def _http() -> httpx.AsyncClient:
    global _client
    if _client is None:
        # A step can wait minutes on Claude (scoring a chunk, an interview turn).
        _client = httpx.AsyncClient(base_url=talent_config.TALENT_WORKER_URL,
                                    timeout=httpx.Timeout(900.0, connect=5.0))
    return _client


@router.api_route(SERVE_PATH, methods=["GET", "POST", "PUT"])
async def forward_to_worker(request: Request) -> Response:
    headers = {k: v for k, v in request.headers.items() if k.lower() not in _DROP_REQUEST}
    # The Host header passes through and the scheme goes as X-Forwarded-Proto
    # (the worker trusts it from 127.0.0.1), so the URL the worker registers
    # with Inngest is this API's public https://…/api/inngest.
    headers["x-forwarded-proto"] = request.url.scheme
    try:
        upstream = await _http().request(request.method, SERVE_PATH, params=request.query_params,
                                         content=await request.body(), headers=headers)
    except httpx.TransportError as exc:
        logger.error("Talent Agent worker unreachable at %s: %s", talent_config.TALENT_WORKER_URL, type(exc).__name__)
        return JSONResponse(status_code=502, content={
            "detail": "The Talent Agent worker isn't running next to the API (start.sh starts it)."})
    return Response(content=upstream.content, status_code=upstream.status_code,
                    headers={k: v for k, v in upstream.headers.items() if k.lower() not in _DROP_RESPONSE})
