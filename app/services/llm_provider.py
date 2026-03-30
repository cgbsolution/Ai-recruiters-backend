"""
LLM Provider Abstraction Layer.

Switch between Anthropic, Google Gemini, and OpenAI by changing env vars:
  AI_PROVIDER=anthropic|gemini|openai
  ANTHROPIC_API_KEY=...
  GEMINI_API_KEY=...
  OPENAI_API_KEY=...

All AI service calls go through ask() and ask_with_system() —
no other file needs to change when switching providers.
"""

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Optional

from app.config import settings

logger = logging.getLogger(__name__)

# ── Model mapping per provider ──────────────────────────────────────────────
# "fast" = cheaper model for generation, parsing, conversation
# "advanced" = powerful model for evaluation, scoring
MODEL_MAP = {
    "anthropic": {
        "fast": "claude-sonnet-4-6",
        "advanced": "claude-opus-4-6",
    },
    "gemini": {
        "fast": "gemini-2.5-flash",
        "advanced": "gemini-2.5-pro",
    },
    "openai": {
        "fast": "gpt-4o-mini",
        "advanced": "gpt-4o",
    },
}

# Approximate cost per 1M tokens (USD) — used for estimated_cost
COST_PER_1M = {
    "claude-sonnet-4-6":  {"input": 3.0,  "output": 15.0},
    "claude-opus-4-6":    {"input": 15.0, "output": 75.0},
    "gemini-2.5-flash":   {"input": 0.15, "output": 0.60},
    "gemini-2.5-pro":     {"input": 1.25, "output": 10.0},
    "gpt-4o-mini":        {"input": 0.15, "output": 0.60},
    "gpt-4o":             {"input": 2.50, "output": 10.0},
}

_provider: Optional[str] = None
_client = None


@dataclass
class LLMResult:
    """Container for LLM response text + token usage metadata."""
    text: str
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    model: str = ""
    provider: str = ""
    tier: str = ""
    estimated_cost: float = 0.0


def _estimate_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    rates = COST_PER_1M.get(model, {"input": 0, "output": 0})
    return (input_tokens * rates["input"] + output_tokens * rates["output"]) / 1_000_000


def get_provider() -> str:
    """Get the configured AI provider name."""
    global _provider
    if _provider is None:
        _provider = getattr(settings, "AI_PROVIDER", "anthropic").strip().lower()
        if _provider not in MODEL_MAP:
            logger.warning("Unknown AI_PROVIDER '%s', falling back to anthropic", _provider)
            _provider = "anthropic"
        logger.info("AI Provider: %s", _provider)
    return _provider


def get_model(tier: str = "fast") -> str:
    """Get model name for the current provider. tier = 'fast' or 'advanced'."""
    return MODEL_MAP[get_provider()][tier]


# Expose as constants for backward compatibility
SONNET = property(lambda self: get_model("fast"))
OPUS = property(lambda self: get_model("advanced"))


# ── Anthropic Client ────────────────────────────────────────────────────────

def _get_anthropic_client():
    import anthropic
    global _client
    if _client is None:
        _client = anthropic.AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY)
    return _client


async def _anthropic_ask(prompt: str, model: str, max_tokens: int, retries: int) -> LLMResult:
    import anthropic
    client = _get_anthropic_client()
    for attempt in range(retries):
        try:
            message = await client.messages.create(
                model=model,
                max_tokens=max_tokens,
                messages=[{"role": "user", "content": prompt}],
            )
            inp = getattr(message.usage, "input_tokens", 0) or 0
            out = getattr(message.usage, "output_tokens", 0) or 0
            return LLMResult(
                text=message.content[0].text,
                input_tokens=inp,
                output_tokens=out,
                total_tokens=inp + out,
                model=model,
                provider="anthropic",
                estimated_cost=_estimate_cost(model, inp, out),
            )
        except anthropic.APIStatusError as exc:
            if exc.status_code == 529 and attempt < retries - 1:
                await asyncio.sleep(2 ** attempt)
                continue
            raise


async def _anthropic_ask_with_system(
    system: str, messages: list, model: str, max_tokens: int,
    timeout_seconds: int, retries: int,
) -> LLMResult:
    import anthropic
    client = _get_anthropic_client()
    for attempt in range(retries):
        try:
            response = await client.messages.create(
                model=model,
                max_tokens=max_tokens,
                system=system,
                messages=messages,
                timeout=timeout_seconds,
            )
            inp = getattr(response.usage, "input_tokens", 0) or 0
            out = getattr(response.usage, "output_tokens", 0) or 0
            return LLMResult(
                text=response.content[0].text,
                input_tokens=inp,
                output_tokens=out,
                total_tokens=inp + out,
                model=model,
                provider="anthropic",
                estimated_cost=_estimate_cost(model, inp, out),
            )
        except anthropic.APIStatusError as exc:
            if exc.status_code == 529 and attempt < retries - 1:
                await asyncio.sleep(2 ** attempt)
                continue
            raise
        except anthropic.APITimeoutError:
            if attempt < retries - 1:
                wait = 5 * (attempt + 1)  # 5s, 10s, ...
                logger.warning(f"[LLM] Anthropic timeout (attempt {attempt + 1}/{retries}), retrying in {wait}s...")
                await asyncio.sleep(wait)
                continue
            raise


# ── Gemini Client ───────────────────────────────────────────────────────────

def _get_gemini_client():
    global _client
    if _client is None:
        from google import genai
        _client = genai.Client(api_key=settings.GEMINI_API_KEY)
    return _client


async def _gemini_ask(prompt: str, model: str, max_tokens: int, retries: int) -> LLMResult:
    from google.genai import types
    client = _get_gemini_client()
    for attempt in range(retries):
        try:
            response = await asyncio.to_thread(
                client.models.generate_content,
                model=model,
                contents=prompt,
                config=types.GenerateContentConfig(
                    max_output_tokens=max_tokens,
                    temperature=0.2,
                    response_mime_type="application/json",
                ),
            )
            usage = getattr(response, "usage_metadata", None)
            inp = getattr(usage, "prompt_token_count", 0) or 0 if usage else 0
            out = getattr(usage, "candidates_token_count", 0) or 0 if usage else 0
            return LLMResult(
                text=response.text,
                input_tokens=inp,
                output_tokens=out,
                total_tokens=inp + out,
                model=model,
                provider="gemini",
                estimated_cost=_estimate_cost(model, inp, out),
            )
        except Exception as exc:
            logger.warning("Gemini API attempt %d/%d failed: %s", attempt + 1, retries, exc)
            if attempt < retries - 1:
                wait = (2 ** attempt) * 2  # 2s, 4s, 8s
                await asyncio.sleep(wait)
                continue
            raise


async def _gemini_ask_with_system(
    system: str, messages: list, model: str, max_tokens: int,
    timeout_seconds: int, retries: int,
) -> LLMResult:
    from google.genai import types
    client = _get_gemini_client()

    # Convert Anthropic-style messages to Gemini format
    contents = []
    for msg in messages:
        role = "user" if msg["role"] == "user" else "model"
        contents.append({"role": role, "parts": [{"text": msg["content"]}]})

    for attempt in range(retries):
        try:
            response = await asyncio.to_thread(
                client.models.generate_content,
                model=model,
                contents=contents,
                config=types.GenerateContentConfig(
                    system_instruction=system,
                    max_output_tokens=max_tokens,
                    temperature=0.3,
                ),
            )
            usage = getattr(response, "usage_metadata", None)
            inp = getattr(usage, "prompt_token_count", 0) or 0 if usage else 0
            out = getattr(usage, "candidates_token_count", 0) or 0 if usage else 0
            return LLMResult(
                text=response.text,
                input_tokens=inp,
                output_tokens=out,
                total_tokens=inp + out,
                model=model,
                provider="gemini",
                estimated_cost=_estimate_cost(model, inp, out),
            )
        except Exception as exc:
            logger.warning("Gemini API (system) attempt %d/%d failed: %s", attempt + 1, retries, exc)
            if attempt < retries - 1:
                wait = (2 ** attempt) * 2
                await asyncio.sleep(wait)
                continue
            raise


# ── OpenAI Client ───────────────────────────────────────────────────────────

def _get_openai_client():
    global _client
    if _client is None:
        from openai import AsyncOpenAI
        _client = AsyncOpenAI(api_key=settings.OPENAI_API_KEY)
    return _client


async def _openai_ask(prompt: str, model: str, max_tokens: int, retries: int) -> LLMResult:
    client = _get_openai_client()
    for attempt in range(retries):
        try:
            response = await client.chat.completions.create(
                model=model,
                max_tokens=max_tokens,
                messages=[{"role": "user", "content": prompt}],
            )
            usage = response.usage
            inp = getattr(usage, "prompt_tokens", 0) or 0 if usage else 0
            out = getattr(usage, "completion_tokens", 0) or 0 if usage else 0
            return LLMResult(
                text=response.choices[0].message.content,
                input_tokens=inp,
                output_tokens=out,
                total_tokens=inp + out,
                model=model,
                provider="openai",
                estimated_cost=_estimate_cost(model, inp, out),
            )
        except Exception as exc:
            if attempt < retries - 1:
                await asyncio.sleep(2 ** attempt)
                continue
            raise


async def _openai_ask_with_system(
    system: str, messages: list, model: str, max_tokens: int,
    timeout_seconds: int, retries: int,
) -> LLMResult:
    client = _get_openai_client()

    # Convert to OpenAI format
    oai_messages = [{"role": "system", "content": system}]
    for msg in messages:
        role = "user" if msg["role"] == "user" else "assistant"
        oai_messages.append({"role": role, "content": msg["content"]})

    for attempt in range(retries):
        try:
            response = await client.chat.completions.create(
                model=model,
                max_tokens=max_tokens,
                messages=oai_messages,
                timeout=timeout_seconds,
            )
            usage = response.usage
            inp = getattr(usage, "prompt_tokens", 0) or 0 if usage else 0
            out = getattr(usage, "completion_tokens", 0) or 0 if usage else 0
            return LLMResult(
                text=response.choices[0].message.content,
                input_tokens=inp,
                output_tokens=out,
                total_tokens=inp + out,
                model=model,
                provider="openai",
                estimated_cost=_estimate_cost(model, inp, out),
            )
        except Exception as exc:
            if attempt < retries - 1:
                await asyncio.sleep(2 ** attempt)
                continue
            raise


# ── Internal dispatchers (return full LLMResult) ─────────────────────────────

async def _ask_full(
    prompt: str,
    tier: str = "fast",
    max_tokens: int = 1024,
    retries: int = 3,
) -> LLMResult:
    provider = get_provider()
    model = get_model(tier)
    dispatch = {
        "anthropic": _anthropic_ask,
        "gemini": _gemini_ask,
        "openai": _openai_ask,
    }
    fn = dispatch.get(provider)
    if fn is None:
        raise ValueError(f"Unknown provider: {provider}")
    result = await fn(prompt, model, max_tokens, retries)
    result.tier = tier
    return result


async def _ask_with_system_full(
    system: str,
    messages: list,
    tier: str = "fast",
    max_tokens: int = 512,
    timeout_seconds: int = 25,
    retries: int = 3,
) -> LLMResult:
    provider = get_provider()
    model = get_model(tier)
    dispatch = {
        "anthropic": _anthropic_ask_with_system,
        "gemini": _gemini_ask_with_system,
        "openai": _openai_ask_with_system,
    }
    fn = dispatch.get(provider)
    if fn is None:
        raise ValueError(f"Unknown provider: {provider}")
    result = await fn(system, messages, model, max_tokens, timeout_seconds, retries)
    result.tier = tier
    return result


# ── Public API (provider-agnostic) ──────────────────────────────────────────

async def ask(
    prompt: str,
    tier: str = "fast",
    max_tokens: int = 1024,
    retries: int = 3,
) -> str:
    """
    Send a simple prompt to the LLM. tier = "fast" or "advanced".
    Returns only the text (backward-compatible).
    """
    result = await _ask_full(prompt, tier, max_tokens, retries)
    return result.text


async def ask_with_usage(
    prompt: str,
    tier: str = "fast",
    max_tokens: int = 1024,
    retries: int = 3,
) -> LLMResult:
    """
    Send a simple prompt to the LLM. Returns full LLMResult with token usage.
    """
    return await _ask_full(prompt, tier, max_tokens, retries)


async def ask_with_system(
    system: str,
    messages: list,
    tier: str = "fast",
    max_tokens: int = 512,
    timeout_seconds: int = 25,
    retries: int = 3,
) -> str:
    """
    Send a system prompt + message history to the LLM. tier = "fast" or "advanced".
    Returns only the text (backward-compatible).
    """
    result = await _ask_with_system_full(system, messages, tier, max_tokens, timeout_seconds, retries)
    return result.text


async def ask_with_system_usage(
    system: str,
    messages: list,
    tier: str = "fast",
    max_tokens: int = 512,
    timeout_seconds: int = 25,
    retries: int = 3,
) -> LLMResult:
    """
    Send a system prompt + message history to the LLM. Returns full LLMResult with token usage.
    """
    return await _ask_with_system_full(system, messages, tier, max_tokens, timeout_seconds, retries)
