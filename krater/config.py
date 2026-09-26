"""Application settings, loaded from environment variables prefixed ``KRATER_``."""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

#: Sentinel used as the default `secret_key`. Production must override it.
DEFAULT_SECRET_KEY = "insecure-dev-secret-change-me"


class Settings(BaseSettings):
    """Krater's configuration. All fields read from `KRATER_<FIELD_NAME>` env vars (or a `.env` file)."""

    model_config = SettingsConfigDict(env_prefix="KRATER_", env_file=".env", extra="ignore")

    env: Literal["development", "test", "production"] = "development"
    database_url: str = "postgresql+psycopg://root:root@localhost:5432/krater_dev"
    secret_key: str = DEFAULT_SECRET_KEY
    base_url: str = "http://localhost:8000"

    # Weave (Patchwork Labs identity provider) integration. See docs/weave-integration.md.
    weave_mode: Literal["stub", "live"] = "stub"
    weave_issuer: str = ""
    weave_client_id: str = ""
    weave_client_secret: str = ""
    weave_api_base_url: str = ""
    weave_service_key: str = ""
    weave_stub_users_file: str = ""

    @model_validator(mode="after")
    def _validate_production_safety(self) -> Settings:
        if self.env == "production":
            if self.weave_mode == "stub":
                raise ValueError("KRATER_WEAVE_MODE cannot be 'stub' when KRATER_ENV=production")
            if self.secret_key == DEFAULT_SECRET_KEY:
                raise ValueError("KRATER_SECRET_KEY must be set to a non-default value when KRATER_ENV=production")
        return self


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide `Settings` instance, built once and cached."""
    return Settings()
