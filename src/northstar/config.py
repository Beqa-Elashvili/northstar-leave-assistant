"""Application settings loaded from environment variables (and an optional `.env` file).

Secrets are typed as `SecretStr` so they are never shown in reprs, logs or error messages.
"""

from __future__ import annotations

import re
from datetime import date
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = PROJECT_ROOT / "data"
DOCUMENTS_DIR = PROJECT_ROOT / "documents"
MIGRATIONS_DIR = PROJECT_ROOT / "migrations"

_EMPLOYEE_ID_RE = re.compile(r"^E\d{4}$")
_SCHEMA_RE = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # Business date. Required so that rule evaluation never silently depends on the machine clock.
    app_today: date = Field(alias="APP_TODAY")
    app_timezone: str = Field(default="Asia/Tbilisi", alias="APP_TIMEZONE")

    database_url: SecretStr | None = Field(default=None, alias="DATABASE_URL")
    db_schema: str = Field(default="public", alias="DB_SCHEMA")

    supabase_url: str | None = Field(default=None, alias="SUPABASE_URL")
    supabase_service_role_key: SecretStr | None = Field(default=None, alias="SUPABASE_SERVICE_ROLE_KEY")

    gemini_api_key: SecretStr | None = Field(default=None, alias="GEMINI_API_KEY")
    gemini_model: str = Field(default="gemini-2.5-flash", alias="GEMINI_MODEL")
    embedding_model: str = Field(default="gemini-embedding-001", alias="EMBEDDING_MODEL")
    embedding_dim: int = Field(default=768, alias="EMBEDDING_DIM")

    demo_employee_id: str | None = Field(default=None, alias="DEMO_EMPLOYEE_ID")

    test_database_url: SecretStr | None = Field(default=None, alias="TEST_DATABASE_URL")

    @field_validator("app_timezone")
    @classmethod
    def _valid_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"unknown time zone: {value!r}") from exc
        return value

    @field_validator("db_schema")
    @classmethod
    def _valid_schema(cls, value: str) -> str:
        if not _SCHEMA_RE.fullmatch(value):
            raise ValueError("DB_SCHEMA must be a lowercase SQL identifier")
        return value

    @field_validator("demo_employee_id")
    @classmethod
    def _valid_employee_id(cls, value: str | None) -> str | None:
        if value in (None, ""):
            return None
        value = value.strip().upper()
        if not _EMPLOYEE_ID_RE.fullmatch(value):
            raise ValueError("DEMO_EMPLOYEE_ID must look like E1001")
        return value

    @field_validator("embedding_dim")
    @classmethod
    def _valid_dim(cls, value: int) -> int:
        if not 1 <= value <= 2000:
            raise ValueError("EMBEDDING_DIM must be between 1 and 2000 (pgvector HNSW limit)")
        return value

    @field_validator(
        "database_url", "supabase_service_role_key", "gemini_api_key", "test_database_url", mode="before"
    )
    @classmethod
    def _empty_secret_is_none(cls, value):
        return None if value in ("", None) else value

    @field_validator("supabase_url", mode="before")
    @classmethod
    def _empty_url_is_none(cls, value):
        return None if value in ("", None) else value

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.app_timezone)


class ConfigurationError(RuntimeError):
    """Raised when a required setting is missing; the message never contains secret values."""


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]


def require_database_url(settings: Settings | None = None) -> str:
    settings = settings or get_settings()
    if settings.database_url is None:
        raise ConfigurationError("DATABASE_URL is not set. Copy .env.example to .env and fill it in.")
    return settings.database_url.get_secret_value()
