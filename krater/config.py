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

    # SkyPilot integration. See docs/skypilot-integration.md and docs/dev/skypilot-spike.md.
    # `fake` uses an in-memory SkyPilot for dev and tests; `live` talks to a real API server over REST.
    skypilot_mode: Literal["fake", "live"] = "fake"
    skypilot_api_url: str = ""
    skypilot_service_token: str = ""  # admin service-account bearer token Krater uses for REST calls
    # Shared secret carried in the admin-policy URL's query string (RestfulAdminPolicy can't send headers). Not a
    # strong secret: SkyPilot clients fetch the URL too, so the endpoint must be safe for any member to call.
    skypilot_policy_token: str = ""
    skypilot_reconcile_interval_minutes: int = 5
    skypilot_budget_warn_percent: int = 80
    skypilot_autodown_idle_minutes: int = 30
    skypilot_max_hourly_cost_cents: int = 500  # per-instance cap forced onto every launch

    # S3-compatible object storage (gallery screenshots). docker-compose runs a temporary SeaweedFS as `storage`.
    # `fake` uses an in-memory store for dev and tests; `live` talks to a real S3-compatible bucket. See
    # docs/dev/storage.md.
    s3_mode: Literal["fake", "live"] = "fake"
    s3_endpoint_url: str = ""
    s3_public_endpoint_url: str = ""
    s3_region: str = "us-east-1"
    s3_bucket: str = "krater-screenshots"
    s3_access_key_id: str = ""
    s3_secret_access_key: str = ""

    @model_validator(mode="after")
    def _validate_production_safety(self) -> Settings:
        if self.env == "production":
            if self.weave_mode == "stub":
                raise ValueError("KRATER_WEAVE_MODE cannot be 'stub' when KRATER_ENV=production")
            if self.skypilot_mode == "fake":
                raise ValueError("KRATER_SKYPILOT_MODE cannot be 'fake' when KRATER_ENV=production")
            if self.s3_mode == "fake":
                raise ValueError("KRATER_S3_MODE cannot be 'fake' when KRATER_ENV=production")
            if self.skypilot_mode == "live" and len(self.skypilot_policy_token) < 32:
                raise ValueError("KRATER_SKYPILOT_POLICY_TOKEN must be at least 32 characters in production")
            if self.secret_key == DEFAULT_SECRET_KEY:
                raise ValueError("KRATER_SECRET_KEY must be set to a non-default value when KRATER_ENV=production")
        return self


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide `Settings` instance, built once and cached."""
    return Settings()
