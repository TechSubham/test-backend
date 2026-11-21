"""Centralised configuration.

Every setting comes from an environment variable (or a `.env` file in the repo root or
`backend/`). Secrets are held as `SecretStr` and are never included in `repr`, logs or
API responses.
"""

from __future__ import annotations

from functools import cached_property
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

MIN_KEYS_FOR_READINESS = 2

_SQLITE_PREFIXES = ("sqlite+aiosqlite://", "sqlite://")


def _split_csv(raw: str) -> list[str]:
    return [item.strip() for item in raw.split(",") if item.strip()]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=("../.env", ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- LLM / key pool -------------------------------------------------------------
    llm_api_keys: SecretStr = Field(default=SecretStr(""))
    llm_model: str = "gemini/gemini-3.8-flash"
    llm_key_models: str = ""
    llm_api_base: str | None = None
    llm_timeout_seconds: float = Field(default=30.0, gt=0)
    llm_temperature: float = Field(default=0.2, ge=0, le=2)
    llm_max_output_tokens: int = Field(default=1024, gt=0)
    default_system_prompt: str = "You are a helpful assistant."
    max_history_messages: int = Field(default=40, ge=0)

    # --- Storage --------------------------------------------------------------------
    database_url: str = "sqlite:///./data/app.db"

    # --- Cache ----------------------------------------------------------------------
    cache_type: Literal["local"] = "local"
    cache_ttl_seconds: int = Field(default=86_400, gt=0)

    # --- HTTP -----------------------------------------------------------------------
    allowed_origins: str = "http://localhost:3000"
    enable_failure_simulation: bool = False

    # --- Ops ------------------------------------------------------------------------
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    app_env: Literal["development", "test", "production"] = "development"

    @field_validator("llm_api_base", mode="before")
    @classmethod
    def _blank_to_none(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("log_level", mode="before")
    @classmethod
    def _upper_log_level(cls, value: object) -> object:
        return value.upper() if isinstance(value, str) else value

    @field_validator("database_url")
    @classmethod
    def _validate_database_url(cls, value: str) -> str:
        value = value.strip()
        if not value.startswith(_SQLITE_PREFIXES):
            raise ValueError("DATABASE_URL must be a SQLite URL, e.g. sqlite:////data/app.db")
        rest = value.split("://", 1)[1]
        if not rest.startswith("/"):
            raise ValueError("DATABASE_URL must look like sqlite:///<path> (three or four slashes)")
        if rest == "/":
            raise ValueError("DATABASE_URL is missing a database path")
        return value

    # --- Derived values -------------------------------------------------------------
    @cached_property
    def api_keys(self) -> list[SecretStr]:
        return [SecretStr(k) for k in _split_csv(self.llm_api_keys.get_secret_value())]

    @cached_property
    def key_models(self) -> list[str]:
        return _split_csv(self.llm_key_models)

    @cached_property
    def cors_origins(self) -> list[str]:
        return _split_csv(self.allowed_origins)

    @cached_property
    def database_path(self) -> Path | None:
        """Filesystem path of the SQLite DB, or None for an in-memory database."""
        path = self.database_url.split("://", 1)[1][1:]  # drop the separating '/'
        path = path.split("?", 1)[0]
        if path in ("", ":memory:"):
            return None
        return Path(path)

    @cached_property
    def async_database_url(self) -> str:
        """SQLAlchemy async URL (`sqlite+aiosqlite://...`) for the plain DATABASE_URL."""
        if self.database_url.startswith("sqlite+aiosqlite://"):
            return self.database_url
        return "sqlite+aiosqlite://" + self.database_url[len("sqlite://") :]

    def secret_values(self) -> list[str]:
        """Raw secrets, used only by the log-redaction filter."""
        return [k.get_secret_value() for k in self.api_keys]


def get_settings() -> Settings:
    return Settings()
