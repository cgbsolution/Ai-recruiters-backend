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

_provider: Optional[str] = None
_client = None


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


async def _anthropic_ask(prompt: str, model: str, max_tokens: int, retries: int) -> str:
    import anthropic
    client = _get_anthropic_client()
    for attempt in range(retries):
        try:
            message = await client.messages.create(
                model=model,
                max_tokens=max_tokens,
                messages=[{"role": "user", "content": prompt}],
            )
            return message.content[0].text
        except anthropic.APIStatusError as exc:
            if exc.status_code == 529 and attempt < retries - 1:
                await asyncio.sleep(2 ** attempt)
                continue
            raise


async def _anthropic_ask_with_system(
    system: str, messages: list, model: str, max_tokens: int,
    timeout_seconds: int, retries: int,
) -> str:
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
            return response.content[0].text
        except anthropic.APIStatusError as exc:
            if exc.status_code == 529 and attempt < retries - 1:
                await asyncio.sleep(2 ** attempt)
                continue
            raise


# ── Gemini Client ───────────────────────────────────────────────────────────

def _get_gemini_client():
    global _client
    if _client is None:
        from google import genai
        _client = genai.Client(api_key=settings.GEMINI_API_KEY)
    return _client


async def _gemini_ask(prompt: str, model: str, max_tokens: int, retries: int) -> str:
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
            return response.text
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
) -> str:
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
            return response.text
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


async def _openai_ask(prompt: str, model: str, max_tokens: int, retries: int) -> str:
    client = _get_openai_client()
    for attempt in range(retries):
        try:
            response = await client.chat.completions.create(
                model=model,
                max_tokens=max_tokens,
                messages=[{"role": "user", "content": prompt}],
            )
            return response.choices[0].message.content
        except Exception as exc:
            if attempt < retries - 1:
                await asyncio.sleep(2 ** attempt)
                continue
            raise


async def _openai_ask_with_system(
    system: str, messages: list, model: str, max_tokens: int,
    timeout_seconds: int, retries: int,
) -> str:
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
            return response.choices[0].message.content
        except Exception as exc:
            if attempt < retries - 1:
                await asyncio.sleep(2 ** attempt)
                continue
            raise


# ── Public API (provider-agnostic) ──────────────────────────────────────────

async def ask(
    prompt: str,
    tier: str = "fast",
    max_tokens: int = 1024,
    retries: int = 3,
) -> str:
    """
    Send a simple prompt to the LLM. tier = "fast" or "advanced".
    """
    provider = get_provider()
    model = get_model(tier)

    if provider == "anthropic":
        return await _anthropic_ask(prompt, model, max_tokens, retries)
    elif provider == "gemini":
        return await _gemini_ask(prompt, model, max_tokens, retries)
    elif provider == "openai":
        return await _openai_ask(prompt, model, max_tokens, retries)
    else:
        raise ValueError(f"Unknown provider: {provider}")


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
    """
    provider = get_provider()
    model = get_model(tier)

    if provider == "anthropic":
        return await _anthropic_ask_with_system(system, messages, model, max_tokens, timeout_seconds, retries)
    elif provider == "gemini":
        return await _gemini_ask_with_system(system, messages, model, max_tokens, timeout_seconds, retries)
    elif provider == "openai":
        return await _openai_ask_with_system(system, messages, model, max_tokens, timeout_seconds, retries)
    else:
        raise ValueError(f"Unknown provider: {provider}")
