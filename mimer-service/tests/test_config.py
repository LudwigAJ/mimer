from __future__ import annotations

from app.core.config import Settings
from app.sources import get_distribution_source, get_issuer_facts_source, get_price_source


def test_ingestion_source_defaults() -> None:
    settings = Settings()
    assert settings.price_source_default == "stooq"
    assert settings.distribution_source_default == "distribution_fixture"
    assert settings.issuer_facts_source_default == "issuer_fixture"
    # Ingestion defaults must stay offline fixtures so a fresh deploy/tests never
    # make a surprise live call.
    assert settings.rates_source_default == "rates_fixture"
    assert settings.fx_source_default == "fx_fixture"
    assert settings.holdings_source_default == "holdings_fixture"


def test_database_url_default_and_override(monkeypatch) -> None:
    # Default targets a host-local Postgres (the documented host-run default).
    assert Settings(_env_file=None).database_url.startswith("postgresql+asyncpg://")

    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@db:5432/mimer")
    assert Settings(_env_file=None).database_url == "postgresql+asyncpg://u:p@db:5432/mimer"


def test_environment_label(monkeypatch) -> None:
    assert Settings(_env_file=None).environment == "development"
    monkeypatch.setenv("ENVIRONMENT", "production")
    assert Settings(_env_file=None).environment == "production"


def test_api_token_unset_disables_auth() -> None:
    settings = Settings(_env_file=None)
    assert settings.api_token == ""
    assert settings.api_auth_enabled is False


def test_api_token_set_enables_auth(monkeypatch) -> None:
    # Read from the MIMER_API_TOKEN env var (not API_TOKEN).
    monkeypatch.setenv("MIMER_API_TOKEN", "a-long-random-value")
    settings = Settings(_env_file=None)
    assert settings.api_token == "a-long-random-value"
    assert settings.api_auth_enabled is True


def test_api_token_constructible_by_field_name() -> None:
    # populate_by_name lets tests/overrides build a token-bearing Settings cleanly.
    settings = Settings(api_token="x", _env_file=None)
    assert settings.api_auth_enabled is True


def test_cors_origins_parse_from_comma_string(monkeypatch) -> None:
    monkeypatch.setenv("CORS_ALLOW_ORIGINS", "https://app.example.com, https://admin.example.com ,")
    settings = Settings(_env_file=None)
    assert settings.cors_allow_origins == [
        "https://app.example.com",
        "https://admin.example.com",
    ]


def test_source_registries_resolve_defaults() -> None:
    # Fixture providers resolve with no API key / network.
    assert get_distribution_source().name == "distribution_fixture"
    assert get_issuer_facts_source().name == "issuer_fixture"
    assert get_price_source().name == "stooq"


def test_unknown_source_raises() -> None:
    import pytest

    with pytest.raises(ValueError):
        get_distribution_source("no-such-source")
