"""
Every Talent Agent call to Claude goes through `structured()`:

- JSON-schema structured output (output_config.format) on models that support
  it; on older models a single strict tool the model is told to call. Either
  way the answer is schema-shaped JSON, never free text to scrape.
- Low variance: current models take no temperature, so `effort` (low by
  default for extraction) and the schema do that job. Effort is sent only to
  models that accept it.
- The system prompt is cached (it is identical across a run of thousands).
- Server-side refusal fallback on the models that support it. A refusal that
  survives it yields `refused=True` and no data — callers treat that as
  "needs a human", never as a score.
- Every call is attributed to its organization in ai_credit_usage.

Untrusted text (CVs, transcripts) goes in the user turn inside explicit
<untrusted_*> tags, and every system prompt says to treat it as data.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, List, Optional

from app.config import settings
from app.talent.config import talent_config

logger = logging.getLogger("app.talent.claude")

# $ per 1M tokens (input, output) for the cost column in ai_credit_usage.
_COST = {
    "claude-opus-5-5": (4.0, 20.0), "claude-opus-5": (5.0, 25.0), "claude-opus-4-8": (5.0, 25.0),
    "claude-opus-4-7": (5.0, 25.0), "claude-opus-4-6": (5.0, 25.0), "claude-sonnet-5-5": (2.0, 10.0),
    "claude-sonnet-5": (2.0, 10.0), "claude-sonnet-4-6": (3.0, 15.0), "claude-haiku-4-5": (1.0, 5.0),
    "claude-fable-5-1": (10.0, 50.0), "claude-fable-5": (10.0, 50.0),
}
_STRUCTURED_PREFIXES = ("claude-fable-5", "claude-mythos-5", "claude-opus-5", "claude-opus-4-8", "claude-sonnet-5",
                        "claude-haiku-4-5", "claude-opus-4-5", "claude-opus-4-1")
_NO_EFFORT_PREFIXES = ("claude-haiku", "claude-sonnet-4-5", "claude-3")
_FALLBACK_PREFIXES = ("claude-opus-5-5", "claude-opus-5", "claude-fable-5-1", "claude-sonnet-5-5")


class ClaudeUnavailable(RuntimeError):
    """No API key configured — callers fall back to their non-AI path."""


@dataclass
class ClaudeResult:
    data: Optional[dict]
    model: str
    prompt_version: str
    refused: bool = False
    stop_reason: Optional[str] = None
    input_tokens: int = 0
    output_tokens: int = 0
    request_id: Optional[str] = None
    served_by: Optional[str] = None
    extra: dict = field(default_factory=dict)


def available() -> bool:
    return bool(settings.ANTHROPIC_API_KEY)


def supports_structured_output(model: str) -> bool:
    return model.startswith(_STRUCTURED_PREFIXES)


def supports_effort(model: str) -> bool:
    return not model.startswith(_NO_EFFORT_PREFIXES)


def supports_fallback(model: str) -> bool:
    return any(model == p or model.startswith(p + "-") for p in _FALLBACK_PREFIXES)


_client = None


def client():
    global _client
    if not available():
        raise ClaudeUnavailable("ANTHROPIC_API_KEY is not set")
    if _client is None:
        import anthropic

        _client = anthropic.AsyncAnthropic(
            api_key=settings.ANTHROPIC_API_KEY, max_retries=3, timeout=float(talent_config.TALENT_LLM_TIMEOUT_SECONDS),
        )
    return _client


def request_params(*, system: str, user: Any, schema: dict, model: str, effort: str, max_tokens: int,
                   for_batch: bool = False) -> dict:
    """Messages API parameters for one structured call (shared with the Batch API path)."""
    params: dict = {
        "model": model,
        "max_tokens": max_tokens,
        "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
        "messages": [{"role": "user", "content": user}],
    }
    output_config: dict = {}
    if supports_effort(model) and effort:
        output_config["effort"] = effort
    if supports_structured_output(model):
        output_config["format"] = {"type": "json_schema", "schema": schema}
    else:
        params["tools"] = [{"name": "submit_result", "description": "Submit the result.", "strict": True,
                            "input_schema": schema}]
        params["tool_choice"] = {"type": "tool", "name": "submit_result"}
    if output_config:
        params["output_config"] = output_config
    if not for_batch and talent_config.TALENT_LLM_REFUSAL_FALLBACK and supports_fallback(model):
        params["betas"] = ["server-side-fallback-2026-07-01"]
        params["fallbacks"] = "default"
    return params


def parse_message(message, model: str, prompt_version: str) -> ClaudeResult:
    usage = getattr(message, "usage", None)
    result = ClaudeResult(
        data=None, model=model, prompt_version=prompt_version, stop_reason=getattr(message, "stop_reason", None),
        input_tokens=getattr(usage, "input_tokens", 0) or 0, output_tokens=getattr(usage, "output_tokens", 0) or 0,
        request_id=getattr(message, "_request_id", None), served_by=getattr(message, "model", None),
    )
    if result.stop_reason == "refusal":
        result.refused = True
        return result
    if result.stop_reason == "max_tokens":
        raise ValueError(f"{model} hit max_tokens; output truncated")
    for block in message.content:
        if block.type == "text" and supports_structured_output(model):
            result.data = json.loads(block.text)
            return result
        if block.type == "tool_use" and block.name == "submit_result":
            result.data = block.input if isinstance(block.input, dict) else json.loads(block.input)
            return result
    raise ValueError("no structured output in the response")


def _log_usage(feature: str, result: ClaudeResult, organization_id: Optional[int], job_id: Optional[int]) -> None:
    from app.database import SessionLocal
    from app.models.ai_credit_usage import AiCreditUsage

    rate_in, rate_out = _COST.get(result.model, (0.0, 0.0))
    db = SessionLocal()
    try:
        db.add(AiCreditUsage(
            feature=feature[:50], provider="anthropic", model=result.model[:60], tier="talent",
            input_tokens=result.input_tokens, output_tokens=result.output_tokens,
            total_tokens=result.input_tokens + result.output_tokens,
            estimated_cost=(result.input_tokens * rate_in + result.output_tokens * rate_out) / 1_000_000,
            organization_id=organization_id, job_id=job_id,
        ))
        db.commit()
    except Exception as exc:  # usage accounting never fails the call
        logger.warning("Could not record AI usage: %s", type(exc).__name__)
    finally:
        db.close()


async def structured(
    *, feature: str, system: str, user: Any, schema: dict, model: str, effort: str, prompt_version: str,
    max_tokens: int = 8000, organization_id: Optional[int] = None, job_id: Optional[int] = None,
) -> ClaudeResult:
    import asyncio

    params = request_params(system=system, user=user, schema=schema, model=model, effort=effort,
                            max_tokens=max_tokens)
    message = await client().beta.messages.create(**params)
    result = parse_message(message, model, prompt_version)
    await asyncio.to_thread(_log_usage, feature, result, organization_id, job_id)
    if result.refused:
        logger.warning("Claude declined a %s request (request %s)", feature, result.request_id)
    return result


def untrusted(tag: str, payload: Any) -> str:
    """Wrap untrusted content for the user turn. JSON-encode structured data so
    nothing inside can close the tag."""
    body = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False, indent=1)
    body = body.replace(f"</{tag}>", f"<\\/{tag}>")
    return f"<{tag}>\n{body}\n</{tag}>"


def joined(parts: List[str]) -> str:
    return "\n\n".join(p for p in parts if p)
