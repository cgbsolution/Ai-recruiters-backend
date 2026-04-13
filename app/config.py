import json
from typing import List
from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    DATABASE_URL: str = "postgresql+psycopg://postgres:Deepak%40123@localhost:5432/ai_recruiters"
    # ── AI Provider ─────────────────────────────────────────────────────────
    # Set AI_PROVIDER to: "anthropic", "gemini", or "openai"
    # Then set the corresponding API key below
    AI_PROVIDER: str = "anthropic"
    ANTHROPIC_API_KEY: str = ""
    GEMINI_API_KEY: str = ""
    OPENAI_API_KEY: str = ""
    DEEPGRAM_API_KEY: str = ""
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
    APP_ENV: str = "development"
    SECRET_KEY: str = "change-me-in-production"
    SESSION_TIMEOUT_MINUTES: int = 60 * 24  # 24 hours — override via .env

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

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )


settings = Settings()
