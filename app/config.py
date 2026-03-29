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
    CORS_ORIGINS: str = "http://localhost:3000,http://127.0.0.1:3000,https://ai-recruiters-frontend.vercel.app,https://myepfo.com,https://www.myepfo.com,https://app.anytimehire.ai"

    @field_validator("CORS_ORIGINS", mode="before")
    @classmethod
    def _parse_cors(cls, v):
        if isinstance(v, list):
            return ",".join(v)
        return v or ""

    @property
    def cors_origins_list(self) -> List[str]:
        raw = self.CORS_ORIGINS.strip()
        if not raw:
            return ["http://localhost:3000"]
        # Support JSON array format: '["url1","url2"]'
        if raw.startswith("["):
            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                pass
        # Comma-separated format: "url1,url2"
        return [o.strip() for o in raw.split(",") if o.strip()]
    UPLOAD_DIR: str = "uploads/jd"
    RESUME_UPLOAD_DIR: str = "uploads/resumes"
    MAX_UPLOAD_SIZE_MB: int = 10
    APP_ENV: str = "development"
    SECRET_KEY: str = "change-me-in-production"

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

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )


settings = Settings()
