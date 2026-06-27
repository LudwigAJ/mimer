from __future__ import annotations

from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from httpx import AsyncClient

from app.services import capabilities as capabilities_service
from app.sources import registry
from app.sources.registry import (
    ADAPTER_STATUSES,
    ASSET_CLASSES,
    DATA_TYPES,
    RELIABILITY_TIERS,
    SOURCE_TYPES,
)

_REPO_ROOT = Path(__file__).resolve().parent.parent


# --- source capability registry ---------------------------------------------


def test_registry_entries_use_valid_vocabulary() -> None:
    capabilities = registry.list_capabilities()
    assert capabilities
    names = [c.source_name for c in capabilities]
    assert len(names) == len(set(names)), "source_name values must be unique"
    for cap in capabilities:
        assert cap.source_type in SOURCE_TYPES, cap.source_name
        assert cap.reliability_tier in RELIABILITY_TIERS, cap.source_name
        assert cap.adapter_status in ADAPTER_STATUSES, cap.source_name
        assert cap.asset_classes, cap.source_name
        assert cap.data_types, cap.source_name
        assert set(cap.asset_classes) <= set(ASSET_CLASSES), cap.source_name
        assert set(cap.data_types) <= set(DATA_TYPES), cap.source_name


def test_registry_marks_shipped_adapters_implemented() -> None:
    implemented = {c.source_name for c in registry.implemented_sources()}
    # The adapters that actually exist in this iteration.
    assert {
        "stub",
        "openfigi",
        "stooq",
        "yfinance",
        "issuer_fixture",
        "distribution_fixture",
        "holdings_fixture",
    } <= implemented


def test_get_capability_lookup() -> None:
    assert registry.get_capability("openfigi") is not None
    assert registry.get_capability("does-not-exist") is None


def test_registry_includes_ibkr_and_stooq_market_series_planned() -> None:
    # IBKR Flex import is a planned, high-priority broker source; market data is planned.
    flex = registry.get_capability("ibkr_flex_import")
    assert flex is not None and flex.adapter_status == "planned"
    assert "transactions" in flex.data_types and "high_priority" in flex.tags
    assert registry.get_capability("ibkr_market_data").adapter_status == "planned"
    # Stooq market series is classification-only (planned storage) and never a bond.
    series = registry.get_capability("stooq_market_series")
    assert series is not None and series.adapter_status == "planned"
    assert "market_series" in series.data_types
    assert "sovereign_yield_benchmark_series" in series.data_types


# --- service capability discovery -------------------------------------------


def test_migration_head_constant_matches_alembic() -> None:
    cfg = Config(str(_REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(_REPO_ROOT / "alembic"))
    head = ScriptDirectory.from_config(cfg).get_heads()[0]
    assert capabilities_service.MIGRATION_HEAD == head


def test_worker_status_and_configured_source() -> None:
    assert capabilities_service.worker_status("price_ingestion") == "real"
    assert capabilities_service.worker_status("distribution_ingestion") == "fixture"
    # Holdings + documents are now real plumbing backed by offline fixtures.
    assert capabilities_service.worker_status("issuer_holdings_ingestion") == "fixture"
    assert capabilities_service.worker_status("document_snapshot_ingestion") == "fixture"
    # alert_generation + exposure_recompute are real (database-only, no provider).
    assert capabilities_service.worker_status("alert_generation") == "real"
    assert capabilities_service.worker_status("exposure_recompute") == "real"
    # broker_csv_import is now a real, offline, provider-agnostic worker.
    assert capabilities_service.worker_status("broker_csv_import") == "real"
    assert capabilities_service.configured_source("broker_csv_import") == "generic_csv_v1"
    assert capabilities_service.worker_status("unknown_job") == "planned"

    assert capabilities_service.configured_source("price_ingestion") == "stooq"
    assert (
        capabilities_service.configured_source("distribution_ingestion") == "distribution_fixture"
    )
    assert capabilities_service.configured_source("issuer_holdings_ingestion") == "holdings_fixture"
    assert (
        capabilities_service.configured_source("document_snapshot_ingestion") == "document_fixture"
    )
    # Planned workers have no configured provider adapter.
    assert capabilities_service.configured_source("alert_generation") is None


async def test_capabilities_endpoint(client: AsyncClient) -> None:
    body = (await client.get("/api/v1/capabilities")).json()
    assert body["service"] == "etf-data-service"
    assert body["features"]["price_ingestion"] == "real"
    assert body["features"]["distribution_ingestion"] == "fixture"
    assert body["features"]["issuer_holdings_ingestion"] == "fixture"
    assert body["features"]["identity_resolution"] == "real"
    assert "etf" in body["supported_asset_classes"]
    assert "bond" in body["planned_asset_classes"]
    assert body["configured_sources"]["distribution"] == "distribution_fixture"
    assert body["configured_sources"]["holdings"] == "holdings_fixture"
    holdings_status = {d["name"]: d["status"] for d in body["data_types"]}["holdings"]
    assert holdings_status == "fixture"
    assert body["sources"]  # capability catalogue echoed
    # Never leak secrets: only whether an OpenFIGI key is configured.
    env = body["environment"]
    assert env["openfigi_api_key_configured"] is False
    assert "database_url" not in env
    assert not any("key" in k and k != "openfigi_api_key_configured" for k in env)


async def test_data_sources_endpoint(client: AsyncClient) -> None:
    body = (await client.get("/api/v1/data-sources")).json()
    assert body["meta"]["count"] >= 1
    names = {d["name"] for d in body["data"]}
    assert "stooq" in names
    # Ordered by priority ascending (manual=5 outranks seed=100).
    priorities = [d["priority"] for d in body["data"]]
    assert priorities == sorted(priorities)


async def test_data_source_capabilities_endpoint_filters(client: AsyncClient) -> None:
    all_caps = (await client.get("/api/v1/data-sources/capabilities")).json()
    assert all_caps["meta"]["count"] >= 1

    implemented = (
        await client.get("/api/v1/data-sources/capabilities?adapter_status=implemented")
    ).json()
    assert all(c["adapter_status"] == "implemented" for c in implemented["data"])

    fx = (await client.get("/api/v1/data-sources/capabilities?data_type=fx_rates")).json()
    assert fx["meta"]["count"] >= 1
    assert all("fx_rates" in c["data_types"] for c in fx["data"])


async def test_jobs_expose_implementation_status(client: AsyncClient) -> None:
    jobs = (await client.get("/api/v1/jobs")).json()["data"]
    by_type = {j["job_type"]: j for j in jobs}
    assert by_type["price_ingestion"]["implementation_status"] == "real"
    assert by_type["price_ingestion"]["configured_source"] == "stooq"
    # fx + document ingestion are now real plumbing backed by offline fixtures.
    assert by_type["fx_ingestion"]["implementation_status"] == "fixture"
    assert by_type["fx_ingestion"]["configured_source"] == "fx_fixture"
    assert by_type["document_snapshot_ingestion"]["implementation_status"] == "fixture"
    assert by_type["document_snapshot_ingestion"]["configured_source"] == "document_fixture"
