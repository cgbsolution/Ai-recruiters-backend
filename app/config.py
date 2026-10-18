import json
from typing import List
from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # Always set in .env / the environment; no credential lives in the code.
    DATABASE_URL: str = "postgresql+psycopg://postgres@localhost:5432/ai_recruiters"
    # ── AI Provider ─────────────────────────────────────────────────────────
    # Set AI_PROVIDER to: "anthropic", "gemini", or "openai"
    # Then set the corresponding API key below
    AI_PROVIDER: str = "anthropic"
    ANTHROPIC_API_KEY: str = ""
    GEMINI_API_KEY: str = ""
    OPENAI_API_KEY: str = ""
    DEEPGRAM_API_KEY: str = ""
    # When Deepgram refuses to mint a short-lived grant token for this key,
    # /interview/deepgram-token hands the admitted candidate's browser the
    # PERMANENT key instead (anyone who opens DevTools can copy it). Give the
    # backend a key with the Member role — which can mint grants — and then set
    # this to false so the master key can never leave the server.
    DEEPGRAM_ALLOW_MASTER_KEY_FALLBACK: bool = True
    GOOGLE_TTS_API_KEY: str = ""
    CORS_ORIGINS: str = "*"

    @field_validator("CORS_ORIGINS", mode="before")
    @classmethod
    def _parse_cors(cls, v):
        if isinstance(v, list):
            return ",".join(v)
        return v or ""

    # These origins are always allowed regardless of the CORS_ORIGINS env var
    _ALWAYS_ALLOWED = [
        "https://app.anytimehire.ai",
        "https://myepfo.com",
        "https://www.myepfo.com",
    ]

    @property
    def cors_origins_list(self) -> List[str]:
        raw = self.CORS_ORIGINS.strip()
        if not raw:
            origins = ["http://localhost:3000"]
        elif raw.startswith("["):
            try:
                origins = json.loads(raw)
            except json.JSONDecodeError:
                origins = [o.strip() for o in raw.split(",") if o.strip()]
        else:
            origins = [o.strip() for o in raw.split(",") if o.strip()]

        # Always ensure production origins are included
        for url in self._ALWAYS_ALLOWED:
            if url not in origins:
                origins.append(url)
        return origins
    UPLOAD_DIR: str = "uploads/jd"
    RESUME_UPLOAD_DIR: str = "uploads/resumes"
    MAX_UPLOAD_SIZE_MB: int = 10
    # Antivirus for uploaded resumes (app.services.file_safety): a clamd server,
    # e.g. "clamav" in Dokploy (docker-compose.clamav.dokploy.yml). When set,
    # every resume is scanned and refused if the scanner can't be reached.
    CLAMAV_HOST: str = ""
    CLAMAV_PORT: int = 3310
    CLAMAV_TIMEOUT_SECONDS: int = 30
    APP_ENV: str = "development"
    SECRET_KEY: str = "change-me-in-production"
    # Idle timeout: a login token lives this long and the frontend renews it
    # (POST /auth/refresh) while the user is active, so only this much time
    # WITHOUT activity signs someone out.
    SESSION_TIMEOUT_MINUTES: int = 60 * 24  # 24 hours — override via .env
    # Hard ceiling on one sign-in, however active: renewals stop this many
    # days after the password was entered. 0 disables the ceiling.
    SESSION_MAX_LIFETIME_DAYS: int = 30
    # Resume Database bulk parsing. AI concurrency is bounded by the LLM
    # provider's rate limits; extract workers are separate processes (CPU
    # heavy PDF reading kept away from the live-interview event loop).
    RESUME_AI_CONCURRENCY: int = 4
    RESUME_EXTRACT_WORKERS: int = 2
    RESUME_EXTRACT_TIMEOUT_SECONDS: int = 90
    # Background processes that compress stored PDFs/DOCX (lowest priority).
    RESUME_COMPRESS_WORKERS: int = 1
    RESUME_AI_TIMEOUT_SECONDS: int = 120
    # HMAC keys for signed media URLs (app.media) and opaque page-URL refs
    # (app.refs). Optional: when empty they are derived from SECRET_KEY with
    # domain separation. Rotating a key invalidates outstanding links/refs.
    MEDIA_SIGNING_KEY: str = ""
    URL_REF_KEY: str = ""

    @property
    def secret_key_is_weak(self) -> bool:
        """True for the shipped placeholder or a trivially short key — JWTs
        signed with it can be forged by anyone who has read this repo."""
        key = (self.SECRET_KEY or "").strip()
        return len(key) < 32 or key.lower().startswith("change-me") or "your-secret" in key.lower()

    # ── AskMira (HRMS) link ──────────────────────────────────────────────────
    # The secret this product shares with AskMira (AskMira's INTERVIEW_TOOL_SECRET).
    # It signs AskMira's provisioning calls and sign-in links (app.routers.askmira).
    # Empty = the link is off and those endpoints answer 404.
    ASKMIRA_SHARED_SECRET: str = ""

    # ── Email / SMTP ─────────────────────────────────────────────────────────
    SMTP_HOST: str = ""
    SMTP_PORT: int = 587
    SMTP_USER: str = ""
    SMTP_PASS: str = ""
    EMAIL_FROM: str = ""

    # ── WhatsApp Business Cloud API ──────────────────────────────────────────
    # Get these from Meta for Developers → WhatsApp → API Setup
    WHATSAPP_TOKEN: str = ""
    WHATSAPP_PHONE_NUMBER_ID: str = ""

    # ── Google OAuth2 / Calendar (for Google Meet integration) ───────────────
    GOOGLE_CLIENT_ID: str = ""
    GOOGLE_CLIENT_SECRET: str = ""
    GOOGLE_REDIRECT_URI: str = "http://localhost:8000/auth/google/callback"

    # ── Live Observer (P2P WebRTC) ──────────────────────────────────────────
    # Public frontend base used to build observer share links sent in emails.
    OBSERVER_LINK_BASE_URL: str = "http://localhost:3000"

    # Public frontend base for links in transactional email (password reset).
    # Defaults to OBSERVER_LINK_BASE_URL so existing deployments, which already
    # set that one, need no new environment variable.
    FRONTEND_BASE_URL: str = ""

    @property
    def frontend_base_url(self) -> str:
        return (self.FRONTEND_BASE_URL or self.OBSERVER_LINK_BASE_URL).rstrip("/")
    # STUN: Google's free public servers, comma-separated.
    OBSERVER_STUN_URLS: str = "stun:stun.l.google.com:19302,stun:stun1.l.google.com:19302"
    # TURN (optional): Metered.ca free tier credentials. Leave empty to run STUN-only.
    OBSERVER_TURN_URL: str = ""
    OBSERVER_TURN_USERNAME: str = ""
    OBSERVER_TURN_CREDENTIAL: str = ""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )


settings = Settings()
