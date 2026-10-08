"""
Talent Agent settings — every knob is an environment variable (or .env).

Nothing here turns the agent on by itself: TALENT_AGENT_ENABLED is the global
kill switch and each loop is also switched per organization in
talent_settings.flags (see app.talent.settings).
"""

from datetime import timedelta
from typing import List, Tuple

from pydantic_settings import BaseSettings, SettingsConfigDict


class TalentConfig(BaseSettings):
    # ── Master switch ────────────────────────────────────────────────────────
    # False: no Inngest function sends, matches or scores anything, whatever
    # the per-organization flags say. The admin UI and candidate pages still
    # load (candidate links already sent keep working: opt-out must).
    TALENT_AGENT_ENABLED: bool = False

    # ── Inngest ──────────────────────────────────────────────────────────────
    INNGEST_APP_ID: str = "anytimehire-talent-agent"
    INNGEST_EVENT_KEY: str = ""
    INNGEST_SIGNING_KEY: str = ""
    # "1"/"true" (or a dev-server URL) for the local dev server; empty in production.
    INNGEST_DEV: str = ""
    # Also mount /api/inngest on the main API process. Off by default: run
    # `uvicorn app.agent.worker:app` as its own process so agent work never
    # competes with live interviews for the API's event loop.
    TALENT_SERVE_IN_API: bool = False

    # ── Claude ───────────────────────────────────────────────────────────────
    # Model per task. Change these, not code. Effort applies to models that
    # accept it (output_config.effort).
    TALENT_PARSE_MODEL: str = "claude-opus-5-5"
    TALENT_RERANK_MODEL: str = "claude-opus-5-5"
    TALENT_SCORER_MODEL: str = "claude-opus-5-5"
    TALENT_INTERVIEW_MODEL: str = "claude-opus-5-5"
    TALENT_PARSE_EFFORT: str = "low"
    TALENT_RERANK_EFFORT: str = "medium"
    TALENT_SCORER_EFFORT: str = "high"
    TALENT_INTERVIEW_EFFORT: str = "low"
    TALENT_LLM_TIMEOUT_SECONDS: int = 180
    # Server-side refusal fallback on realtime calls (not available on Batches).
    TALENT_LLM_REFUSAL_FALLBACK: bool = True
    TALENT_RERANK_CONCURRENCY: int = 4
    TALENT_RERANK_POOL: int = 200  # Stage 2 keeps this many for the Claude rerank

    # ── Embeddings (Anthropic has no embeddings endpoint) ────────────────────
    # "" = off (Stage 2 falls back to keyword similarity) | voyage | gemini | openai
    # | hash (deterministic local vectors — tests/dev only, no semantic meaning)
    EMBEDDING_PROVIDER: str = ""
    EMBEDDING_MODEL: str = ""  # default per provider, see app.talent.embeddings
    EMBEDDING_DIM: int = 1024
    VOYAGE_API_KEY: str = ""

    # ── Sending ──────────────────────────────────────────────────────────────
    # email: smtp (existing SMTP_* settings) | memory (tests) | disabled
    TALENT_EMAIL_TRANSPORT: str = "smtp"
    # Daily cap ramp per sender domain: caps, then how many days each step lasts.
    TALENT_SEND_RAMP: str = "500,2000,5000"
    TALENT_SEND_RAMP_STEP_DAYS: int = 7
    # Share of a day's cap bulk refresh mail may use; the rest is kept for
    # invites, interview reminders and candidate replies.
    TALENT_BULK_SHARE: float = 0.7
    TALENT_BOUNCE_PAUSE_RATE: float = 0.03
    TALENT_COMPLAINT_PAUSE_RATE: float = 0.001
    # Rates are judged once a campaign has this many sends (an early bounce or
    # complaint still counts — a lower sample only delays the verdict).
    TALENT_HEALTH_MIN_SAMPLE: int = 100
    TALENT_SOFT_BOUNCES_UNREACHABLE: int = 3
    # Shared secret for POST /talent/webhooks/deliverability (HMAC-SHA256 hex
    # of the raw body in X-Talent-Signature).
    TALENT_DELIVERABILITY_WEBHOOK_SECRET: str = ""
    # Key for suppression-list hashes; falls back to SECRET_KEY. Rotating it
    # orphans the existing list — don't.
    TALENT_SUPPRESSION_KEY: str = ""

    # ── Cadence ──────────────────────────────────────────────────────────────
    TALENT_REFRESH_TOUCH_GAPS_DAYS: str = "3,4"  # touch1 → (3d) → touch2 → (4d) → touch3
    TALENT_INVITE_REMINDER_DAYS: int = 3
    TALENT_INVITE_CLOSE_DAYS: int = 3
    TALENT_ENROLL_FRACTION: float = 0.4  # share of the bulk budget new enrolments may take per day
    TALENT_PUBLIC_BASE_URL: str = ""  # candidate links; defaults to FRONTEND_BASE_URL

    # ── Links ────────────────────────────────────────────────────────────────
    TALENT_REFRESH_LINK_DAYS: int = 14
    TALENT_INVITE_LINK_DAYS: int = 10
    TALENT_INTERVIEW_LINK_DAYS: int = 14
    TALENT_MY_DATA_LINK_DAYS: int = 30

    # ── Interviews ───────────────────────────────────────────────────────────
    TALENT_AUDIO_MAX_MB: int = 15

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", case_sensitive=False, extra="ignore")

    @property
    def send_ramp(self) -> List[Tuple[int, int]]:
        """[(from_day, cap), …] — e.g. [(0, 500), (7, 2000), (14, 5000)]."""
        caps = [int(c) for c in self.TALENT_SEND_RAMP.split(",") if c.strip()]
        return [(i * self.TALENT_SEND_RAMP_STEP_DAYS, cap) for i, cap in enumerate(caps)] or [(0, 500)]

    @property
    def refresh_touch_gaps(self) -> List[timedelta]:
        return [timedelta(days=float(d)) for d in self.TALENT_REFRESH_TOUCH_GAPS_DAYS.split(",") if d.strip()]

    @property
    def inngest_is_dev(self) -> bool:
        return bool(self.INNGEST_DEV.strip()) and self.INNGEST_DEV.strip().lower() not in ("0", "false", "no")


talent_config = TalentConfig()
