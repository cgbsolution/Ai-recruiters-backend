"""
The Talent Agent's AI, whichever provider serves it (TALENT_LLM_PROVIDER):

    ollama     self-hosted, the default — no Anthropic credits (app.talent.ollama_chat)
    anthropic  Claude (app.talent.claude)

Call sites use only this module: structured() for a schema-shaped answer,
available() before trying, and is_transient() / account_problem_from() to
tell "retry later" from "no call can succeed" (no credit, model not pulled).
"""

from __future__ import annotations

from typing import Any, Optional

from app.talent import claude, ollama_chat
from app.talent.config import talent_config

Result = claude.ClaudeResult
Unavailable = claude.ClaudeUnavailable


def provider() -> str:
    return "anthropic" if talent_config.TALENT_LLM_PROVIDER.strip().lower() == "anthropic" else "ollama"


def is_ollama() -> bool:
    return provider() == "ollama"


def available() -> bool:
    """Whether to try at all (Claude needs a key; Ollama only an address)."""
    return bool(talent_config.OLLAMA_BASE_URL) if is_ollama() else claude.available()


def model_for(claude_model: str) -> str:
    """The model that will actually answer (shown on matches, part of cache keys)."""
    return ollama_chat.model_name() if is_ollama() else claude_model


def label() -> str:
    """Short provider/model label for the UI and audit."""
    return f"Ollama · {ollama_chat.model_name()}" if is_ollama() else "Claude"


async def structured(**kwargs: Any) -> Result:
    if is_ollama():
        return await ollama_chat.structured(**kwargs)
    return await claude.structured(**kwargs)


def is_transient(exc: BaseException) -> bool:
    """Worth retrying the step (Inngest retries it)."""
    if isinstance(exc, ollama_chat.OllamaTransient):
        return True
    try:
        import anthropic
    except ImportError:  # pragma: no cover
        return False
    if isinstance(exc, (anthropic.RateLimitError, anthropic.APIConnectionError, anthropic.APITimeoutError)):
        return True
    return isinstance(exc, anthropic.APIStatusError) and exc.status_code >= 500


def account_problem_from(exc: BaseException) -> Optional[str]:
    """When an error means no call can succeed, say why; else None."""
    if isinstance(exc, ollama_chat.OllamaModelMissing):
        return (f"Ollama has no model {exc.args[0]!r}: run `ollama pull {exc.args[0]}` on the Ollama server, "
                "then run matching again.")
    return claude.account_problem_from(exc)


def account_problem(max_age: float = 600) -> Optional[str]:
    """For the settings page: can the configured provider serve the agent right now?"""
    if is_ollama():
        return ollama_chat.problem(max_age=min(max_age, 60))
    return claude.account_problem(max_age=max_age)


def followups_enabled() -> bool:
    """Interview follow-ups are generated while the candidate waits."""
    return available() and (not is_ollama() or talent_config.TALENT_OLLAMA_FOLLOWUPS)


def rerank_pool() -> int:
    return int(talent_config.TALENT_OLLAMA_RERANK_POOL if is_ollama() else talent_config.TALENT_RERANK_POOL)


def rerank_chunk() -> int:
    return max(1, int(talent_config.TALENT_OLLAMA_RERANK_CHUNK)) if is_ollama() else 10


def rerank_concurrency() -> int:
    return max(1, int(talent_config.TALENT_OLLAMA_PARALLEL if is_ollama() else talent_config.TALENT_RERANK_CONCURRENCY))
