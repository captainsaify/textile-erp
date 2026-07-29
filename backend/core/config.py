"""Application settings, loaded from environment variables.

See docs/01_Architecture.md §9 (Configuration & environments) and
docs/16_Deployment.md §5 (Secrets) for the rationale: 12-factor config,
no secrets in code, one Settings object as the single source of truth.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    environment: str = "local"

    database_url: str
    test_database_url: str | None = None

    redis_url: str = "redis://localhost:6379/0"

    jwt_signing_key: str
    jwt_access_token_expire_minutes: int = 15
    jwt_refresh_token_expire_days: int = 7

    whatsapp_app_secret: str = ""
    whatsapp_access_token: str = ""
    whatsapp_verify_token: str = ""
    whatsapp_phone_number_id: str = ""
    whatsapp_api_version: str = "v23.0"
    whatsapp_rate_limit_per_minute: int = Field(default=30, gt=0)

    # "webjs" = whatsapp-web.js bridge (supports groups); "meta" = official
    # Cloud API webhooks. Both transports feed the same dispatcher.
    whatsapp_transport: str = "webjs"
    bridge_url: str = "http://localhost:3001"
    bridge_shared_secret: str = ""

    #: The partners' group chat, reached through the whatsapp-web.js
    #: relay because Meta's Cloud API cannot post to a group at all.
    #: Empty means group sharing is simply off -- the ERP is unaffected.
    group_chat_id: str = ""
    group_broadcast_enabled: bool = False

    anthropic_api_key: str = ""

    backup_encryption_key: str = ""

    attachments_dir: str = "./data/attachments"
    max_attachment_size_mb: int = Field(default=15, gt=0)

    # "paddle" (docs/07_OCR.md §6 primary) or "tesseract"; the other is
    # always the fallback, and an unavailable engine degrades silently.
    ocr_primary_engine: str = "paddle"

    # Read sheets with Claude vision before falling back to local OCR.
    # Local OCR's accuracy is bounded by grid detection on a photo; a
    # vision model reads the table as a table. Needs anthropic_api_key.
    ocr_use_vision: bool = True
    #: Transcribing a table is a reading task, not a reasoning one, and
    #: it was defaulting to the most expensive model available. Haiku is
    #: roughly a fifth the price per sheet. Override with VISION_MODEL if
    #: a hard-to-read sheet needs more; `ocr compare` puts the two side
    #: by side on a real photo before you decide.
    vision_model: str = "claude-haiku-4-5"

    @property
    def is_local(self) -> bool:
        return self.environment == "local"


@lru_cache
def get_settings() -> Settings:
    """Cached settings singleton -- env is read once per process."""
    return Settings()
