from typing import List
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    DATABASE_URL: str = "postgresql+psycopg://postgres:Deepak%40123@localhost:5432/ai_recruiters"
    ANTHROPIC_API_KEY: str = ""
    CORS_ORIGINS: List[str] = ["http://localhost:3000", "http://127.0.0.1:3000"]
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
