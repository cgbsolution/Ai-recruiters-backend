"""
Talent Agent calls on the self-hosted Ollama (TALENT_LLM_PROVIDER=ollama).

Same contract as app.talent.claude.structured(): the system prompt, the
untrusted user turn and the JSON schema go in; schema-shaped JSON comes back.
Ollama constrains generation to the schema (`format`), temperature is 0 so
the same input scores the same way, and every call is recorded in
ai_credit_usage at zero cost. Nothing leaves your servers.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Optional

import httpx

from app.talent.claude import ClaudeResult
from app.talent.config import talent_config

logger = logging.getLogger("app.talent.ollama")


class OllamaTransient(RuntimeError):
    """Ollama unreachable or too slow this time — the caller retries later."""


class OllamaModelMissing(RuntimeError):
    """The configured chat model isn't pulled on the Ollama server."""


def base_url() -> str:
    return talent_config.OLLAMA_BASE_URL.rstrip("/")


def model_name() -> str:
    return talent_config.TALENT_OLLAMA_MODEL


def _text(user: Any) -> str:
    """The user turn as plain text (Ollama takes a string, not content blocks)."""
    if isinstance(user, str):
        return user
    if isinstance(user, list):
        return "\n\n".join(b.get("text", "") if isinstance(b, dict) else str(b) for b in user)
    return json.dumps(user, ensure_ascii=False)


_http: Optional[httpx.AsyncClient] = None


def _client() -> httpx.AsyncClient:
    global _http
    if _http is None:
        _http = httpx.AsyncClient(timeout=httpx.Timeout(float(talent_config.TALENT_OLLAMA_TIMEOUT_SECONDS), connect=10.0))
    return _http


def _log_usage(feature: str, result: ClaudeResult, organization_id: Optional[int], job_id: Optional[int]) -> None:
    from app.database import SessionLocal
    from app.models.ai_credit_usage import AiCreditUsage

    db = SessionLocal()
    try:
        db.add(AiCreditUsage(
            feature=feature[:50], provider="ollama", model=result.model[:60], tier="talent",
            input_tokens=result.input_tokens, output_tokens=result.output_tokens,
            total_tokens=result.input_tokens + result.output_tokens, estimated_cost=0.0,
            organization_id=organization_id, job_id=job_id,
        ))
        db.commit()
    except Exception as exc:  # usage accounting never fails the call
        logger.warning("Could not record AI usage: %s", type(exc).__name__)
    finally:
        db.close()


async def structured(
    *, feature: str, system: str, user: Any, schema: dict, prompt_version: str, max_tokens: int = 8000,
    organization_id: Optional[int] = None, job_id: Optional[int] = None, **_ignored: Any,
) -> ClaudeResult:
    """One schema-constrained chat call. `model`/`effort` from the Claude call
    sites are ignored: TALENT_OLLAMA_MODEL serves every task."""
    import asyncio

    model = model_name()
    body = {
        "model": model, "stream": False, "format": schema, "keep_alive": "30m",
        "options": {"temperature": 0, "num_ctx": int(talent_config.TALENT_OLLAMA_NUM_CTX), "num_predict": max_tokens},
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": _text(user)}],
    }
    started = time.monotonic()
    try:
        resp = await _client().post(f"{base_url()}/api/chat", json=body)
    except (httpx.TransportError, httpx.TimeoutException) as exc:
        raise OllamaTransient(f"Ollama unreachable or timed out at {base_url()} ({type(exc).__name__})") from exc
    if resp.status_code == 404:
        raise OllamaModelMissing(model)
    if resp.status_code >= 500:
        raise OllamaTransient(f"Ollama returned {resp.status_code}")
    resp.raise_for_status()
    payload = resp.json()
    content = (payload.get("message") or {}).get("content") or ""
    result = ClaudeResult(
        data=None, model=model, prompt_version=prompt_version, stop_reason=payload.get("done_reason"),
        input_tokens=int(payload.get("prompt_eval_count") or 0), output_tokens=int(payload.get("eval_count") or 0),
        served_by=f"ollama:{model}",
    )
    if result.stop_reason == "length":
        raise ValueError(f"{model} hit num_predict={max_tokens}; output truncated")
    try:
        data = json.loads(content)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{model} returned text that isn't JSON") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{model} returned JSON that isn't an object")
    result.data = data
    logger.info("%s on %s: %d in / %d out tokens, %.0fs", feature, model, result.input_tokens, result.output_tokens,
                time.monotonic() - started)
    await asyncio.to_thread(_log_usage, feature, result, organization_id, job_id)
    return result


_checked: dict = {}  # (base, model) -> (monotonic time, problem)


def problem(max_age: float = 60) -> Optional[str]:
    """For the settings page: can Ollama serve the chat model right now?"""
    base, model = base_url(), model_name()
    seen = _checked.get((base, model))
    if max_age and seen and time.monotonic() - seen[0] < max_age:
        return seen[1]
    try:
        names = [m.get("name", "") for m in httpx.get(f"{base}/api/tags", timeout=3).json().get("models", [])]
    except Exception:
        found = f"Ollama is not reachable at {base}: match scoring, CV parsing and agent interviews can't run."
    else:
        wanted = model if ":" in model else f"{model}:latest"
        found = (None if wanted in names or model in names else
                 f"Ollama has no model {model!r}: run `ollama pull {model}` on the Ollama server "
                 "(match scoring, CV parsing and agent interviews need it).")
    _checked[(base, model)] = (time.monotonic(), found)
    return found
