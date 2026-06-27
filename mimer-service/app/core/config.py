"""Application configuration loaded from environment variables."""

from __future__ import annotations

from functools import lru_cache
from typing import Annotated

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration.

    Values are read from environment variables (and an optional `.env` file).
    Defaults are tuned for running the API directly on the host against a
    locally exposed Postgres; Docker Compose overrides the database host.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        # Allow populating ``api_token`` by its field name too (it carries a
        # ``MIMER_API_TOKEN`` env alias) so tests/overrides can construct it cleanly.
        populate_by_name=True,
    )

    # Free-form deployment label (e.g. development | production). Surfaced in
    # diagnostics/logs; does not change behaviour on its own.
    environment: str = Field(default="development")

    database_url: str = Field(
        default="postgresql+asyncpg://etf:etf_password@localhost:5432/etf_data",
    )
    api_host: str = Field(default="0.0.0.0")
    api_port: int = Field(default=8080)
    log_level: str = Field(default="info")

    # --- API auth ---
    # Optional shared Bearer token guarding the /api/v1 surface. Blank/unset =>
    # auth disabled (local dev, every route open); any non-empty value => each
    # /api/v1 request must send ``Authorization: Bearer <token>`` (the /health
    # liveness/readiness probes stay open). Read from the ``MIMER_API_TOKEN`` env
    # var; never logged and never returned in any response (not even diagnostics
    # or capabilities — only whether auth is enabled, via ``api_auth_enabled``).
    api_token: str = Field(default="", validation_alias="MIMER_API_TOKEN")
    # NoDecode: pydantic-settings would otherwise try to JSON-decode an env value
    # for a list field (so `CORS_ALLOW_ORIGINS=a,b` from .env / Compose would crash
    # the app on startup with an obscure SettingsError). NoDecode hands the raw
    # string to ``_split_origins`` below, which splits it on commas.
    cors_allow_origins: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["http://localhost:3000", "http://localhost:5173"],
    )

    # Portfolio reporting base currency. Personal service => single base currency.
    base_currency: str = Field(default="GBP")

    # --- Instrument resolver ---
    # Default to the offline "stub" provider so the system works without network
    # access or an API key. Set to "openfigi" in real environments.
    resolver_default_provider: str = Field(default="stub")
    openfigi_api_key: str | None = Field(default=None)

    # --- Ingestion source adapters ---
    # Each ingestion worker resolves its provider via a registry keyed by these
    # names. Fixture providers need no API key and never hit the network, so the
    # service works offline and tests stay deterministic.
    price_source_default: str = Field(default="stooq")
    distribution_source_default: str = Field(default="distribution_fixture")
    issuer_facts_source_default: str = Field(default="issuer_fixture")
    holdings_source_default: str = Field(default="holdings_fixture")
    fx_source_default: str = Field(default="fx_fixture")
    document_source_default: str = Field(default="document_fixture")
    # Constituent identity resolution defaults to the offline fixture resolver so
    # the worker/scheduler never makes a surprise live OpenFIGI call. Pass
    # ``--source openfigi`` explicitly to use the live, budget-guarded path.
    constituent_identity_source_default: str = Field(default="constituent_identity_fixture")
    # Constituent EOD price ingestion defaults to the offline fixture provider so
    # the worker/scheduler never makes a surprise live Stooq/yfinance call. Pass
    # ``--source stooq`` / ``--source yfinance`` explicitly for the live,
    # budget-guarded path (use a small --limit; never loop over every holding).
    constituent_price_source_default: str = Field(default="instrument_price_fixture")
    # Broker CSV import parser/format. Offline + deterministic; the worker/API
    # never makes a live call (instruments resolve against existing identity).
    broker_import_source_default: str = Field(default="generic_csv_v1")
    # Official / reference-rate ingestion defaults to the offline fixture provider
    # so the worker/scheduler never makes a surprise live ECB/BoE/Treasury call.
    # Live adapters (ecb_rates/boe_rates/us_treasury_rates) are explicit + planned.
    rates_source_default: str = Field(default="rates_fixture")

    # --- Scheduler / job leasing ---
    # Poll interval (loop mode) and lease length for the scheduler worker. The
    # lease (lock_expires_at) is what prevents two scheduler processes from
    # running the same due job; a crashed lease is reclaimable after it expires.
    scheduler_poll_seconds: int = Field(default=30)
    scheduler_lease_seconds: int = Field(default=300)

    # --- Source request cache ---
    # A successful external fetch with the same request key inside this TTL is
    # skipped (cache hit), so identical requests are not repeated in a tight loop.
    request_cache_ttl_seconds: int = Field(default=21600)  # 6h

    @property
    def api_auth_enabled(self) -> bool:
        """True when a non-empty API token is configured (Bearer auth enforced)."""
        return bool(self.api_token)

    @field_validator("cors_allow_origins", mode="before")
    @classmethod
    def _split_origins(cls, value: object) -> object:
        """Allow CORS origins to be provided as a comma-separated string."""
        if isinstance(value, str):
            return [origin.strip() for origin in value.split(",") if origin.strip()]
        return value


@lru_cache
def get_settings() -> Settings:
    """Return a cached Settings instance."""
    return Settings()
