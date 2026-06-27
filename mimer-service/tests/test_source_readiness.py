"""Production data-source readiness matrix: honest statuses + scheduler-safety.

These guard the operational-honesty contract: a fixture is never dressed up as a
production/live source, a blocked candidate is never marked scheduler-safe, and the
matrix never leaks a secret or uses a ticker as identity.
"""

from __future__ import annotations

from httpx import AsyncClient

from app.services import capabilities as capabilities_service
from app.sources import issuer_source_config, registry
from app.sources import source_readiness as readiness


def _row(source_name: str) -> readiness.SourceReadinessRow:
    row = readiness.get_row(source_name)
    assert row is not None, source_name
    return row


# --- matrix coverage + vocabulary -------------------------------------------


def test_matrix_covers_the_important_data_types() -> None:
    data_types = {r.data_type for r in readiness.list_rows()}
    # Every data type the live-readiness slice must represent.
    assert {
        "reference_rates",
        "fx_rates",
        "prices",
        "holdings",
        "distributions",
        "identity",
        "transactions",
        "market_series",
    } <= data_types


def test_every_row_uses_a_valid_status() -> None:
    for row in readiness.list_rows():
        assert row.status in readiness.READINESS_STATUSES, row.source_name
        # A scheduler-safe source must name a worker and a real cadence.
        if row.safe_for_scheduler:
            assert row.worker_name, row.source_name
            assert row.recommended_cadence, row.source_name


def test_source_names_are_unique() -> None:
    names = [r.source_name for r in readiness.list_rows()]
    assert len(names) == len(set(names))


# --- honest statuses ---------------------------------------------------------


def test_fixtures_are_never_live_or_verified() -> None:
    for row in readiness.list_rows():
        if row.source_name.endswith("_fixture"):
            assert row.status == readiness.FIXTURE, row.source_name
            assert row.safe_for_scheduler is False, row.source_name


def test_ishares_holdings_is_verified_live_matching_the_config_registry() -> None:
    # The matrix must agree with the single source of truth for the ISF verification.
    cfg = issuer_source_config.get_source_config("IE0005042456", "blackrock_ishares_holdings")
    assert cfg is not None and cfg.source_status == issuer_source_config.VERIFIED
    row = _row("blackrock_ishares_holdings")
    assert row.status == readiness.VERIFIED_LIVE
    assert row.safe_for_scheduler is True
    assert row.last_verified_at == cfg.verified_at  # no drift from the config registry


def test_jpmorgan_holdings_is_candidate_and_not_scheduler_safe() -> None:
    row = _row("jpmorgan_etf_holdings")
    assert row.status == readiness.CANDIDATE
    assert row.safe_for_scheduler is False
    assert row.known_blockers and "binary" in row.known_blockers.lower()


def test_vanguard_distributions_candidate_blocked_by_tls() -> None:
    row = _row("vanguard_distributions")
    assert row.status == readiness.CANDIDATE
    assert row.safe_for_scheduler is False
    assert row.known_blockers and "tls" in row.known_blockers.lower()


def test_ishares_distributions_planned_not_scheduler_safe() -> None:
    row = _row("blackrock_ishares_distributions")
    assert row.status == readiness.PLANNED
    assert row.safe_for_scheduler is False


def test_boe_rates_planned() -> None:
    row = _row("boe_rates")
    assert row.status == readiness.PLANNED
    assert row.safe_for_scheduler is False


def test_fx_is_still_fixture_only() -> None:
    # The only fx source that may schedule is the fixture (dev) — and it is not safe.
    fx_rows = readiness.rows_for_data_type("fx_rates")
    assert {r.source_name for r in fx_rows} == {"fx_fixture", "ecb"}
    assert _row("fx_fixture").status == readiness.FIXTURE
    assert _row("ecb").status == readiness.PLANNED  # no live FX adapter implemented yet
    assert not any(r.safe_for_scheduler for r in fx_rows)


# --- scheduler-safety contract ----------------------------------------------


def test_scheduler_safe_only_for_verified_or_implemented_live() -> None:
    for row in readiness.scheduler_safe_sources():
        assert row.status in (readiness.VERIFIED_LIVE, readiness.IMPLEMENTED_LIVE), row.source_name
        assert row.status not in (
            readiness.FIXTURE,
            readiness.CANDIDATE,
            readiness.PLANNED,
            readiness.UNSUPPORTED,
        )


def test_candidate_and_planned_are_never_scheduler_safe() -> None:
    for row in readiness.list_rows():
        if row.status in (readiness.CANDIDATE, readiness.PLANNED):
            assert row.safe_for_scheduler is False, row.source_name


def test_export_only_parsers_are_implemented_live_but_never_scheduler_safe() -> None:
    # Offline manual-export parsers are real (implemented_live) but file-driven: there is no
    # remote endpoint to schedule, so they must never be marked scheduler-safe.
    for name in ("vanguard_holdings_export", "vanguard_distributions_export"):
        row = _row(name)
        assert row.status == readiness.IMPLEMENTED_LIVE, name
        assert row.safe_for_scheduler is False, name


def test_verify_fund_sources_is_explicit_only_not_a_readiness_source() -> None:
    # The bounded verifier is a diagnostic command, never a seeded/scheduled production source.
    assert readiness.get_row("verify_fund_sources") is None


def test_us_treasury_and_ecb_rates_are_scheduler_safe_official_sources() -> None:
    for name in ("us_treasury_rates", "ecb_rates"):
        row = _row(name)
        assert row.status == readiness.IMPLEMENTED_LIVE
        assert row.safe_for_scheduler is True
        assert row.worker_name == "rates_ingestion"


# --- IBKR --------------------------------------------------------------------


def test_ibkr_flex_is_planned_high_priority() -> None:
    row = _row("ibkr_flex_import")
    assert row.status == readiness.PLANNED
    assert row.safe_for_scheduler is False
    assert row.requires_secret is True  # Flex token
    assert row.known_blockers and "high-priority" in row.known_blockers.lower()
    # Registry capability is tagged high_priority + planned.
    cap = registry.get_capability("ibkr_flex_import")
    assert cap is not None and cap.adapter_status == "planned"
    assert "high_priority" in cap.tags


def test_ibkr_market_data_is_planned_optional_needs_gateway() -> None:
    row = _row("ibkr_market_data")
    assert row.status == readiness.PLANNED
    assert row.requires_secret is True
    assert row.requires_running_gateway is True
    assert row.safe_for_scheduler is False


# --- safety: no secrets, no ticker-as-identity ------------------------------


def test_matrix_carries_no_secret_values_or_tokenised_urls() -> None:
    # The matrix may *describe* that a token is required (it must), but must never embed a
    # secret value, a tokenised URL query param, or a bearer header. Example targets are
    # public ticker:ISIN identifiers only.
    banned = ("token=", "api_key=", "apikey=", "password=", "secret=", "bearer ", "key=")
    for row in readiness.list_rows():
        blob = " ".join(
            str(x).lower()
            for x in (row.notes, row.known_blockers, row.next_action, *row.example_targets)
            if x
        )
        for needle in banned:
            assert needle not in blob, f"{row.source_name}: {needle}"
        for target in row.example_targets:
            # ticker:ISIN shape — identity is ISIN, ticker is only a label.
            assert ":" in target and len(target.split(":")[1]) == 12, target


def test_default_for_worker_matches_configured_defaults() -> None:
    # Derived from live settings, never hard-coded: the fixtures are the configured
    # defaults; the live/candidate/planned sources are not.
    assert readiness.default_for_worker(_row("rates_fixture")) is True
    assert readiness.default_for_worker(_row("holdings_fixture")) is True
    assert readiness.default_for_worker(_row("us_treasury_rates")) is False
    assert readiness.default_for_worker(_row("blackrock_ishares_holdings")) is False
    # stooq IS the configured fund-listing price default.
    assert readiness.default_for_worker(_row("stooq")) is True


def test_missing_required_live_sources_is_honest() -> None:
    missing = readiness.missing_required_live_sources()
    # holdings has a verified-live source (iShares ISF); rates/fx/prices do not yet.
    assert "holdings" not in missing
    assert {"reference_rates", "fx_rates", "prices"} <= set(missing)


# --- endpoint ----------------------------------------------------------------


async def test_readiness_endpoint(client: AsyncClient) -> None:
    body = (await client.get("/api/v1/data-sources/readiness")).json()
    assert body["meta"]["count"] == len(readiness.list_rows())
    assert body["summary"]["scheduler_safe_count"] >= 1
    by_name = {r["source_name"]: r for r in body["data"]}
    assert by_name["blackrock_ishares_holdings"]["status"] == "verified_live"
    assert by_name["ibkr_flex_import"]["status"] == "planned"


async def test_readiness_endpoint_filters(client: AsyncClient) -> None:
    safe = (await client.get("/api/v1/data-sources/readiness?scheduler_safe=true")).json()
    assert all(r["safe_for_scheduler"] for r in safe["data"])
    candidates = (await client.get("/api/v1/data-sources/readiness?status=candidate")).json()
    assert all(r["status"] == "candidate" for r in candidates["data"])
    assert not any(r["safe_for_scheduler"] for r in candidates["data"])
    holdings = (await client.get("/api/v1/data-sources/readiness?data_type=holdings")).json()
    assert all(r["data_type"] == "holdings" for r in holdings["data"])


async def test_capabilities_exposes_readiness_summary(client: AsyncClient) -> None:
    body = (await client.get("/api/v1/capabilities")).json()
    summary = body["source_readiness"]
    assert summary["verified_live_count"] >= 1
    assert summary["fixture_count"] >= 1
    assert summary["planned_count"] >= 1
    assert "reference_rates" in summary["required_live_data_types"]
    # The summary must match the in-code matrix exactly.
    assert summary["total_sources"] == len(readiness.list_rows())


def test_service_summary_matches_matrix() -> None:
    matrix = capabilities_service.build_source_readiness_matrix()
    assert len(matrix.data) == matrix.meta.count == len(readiness.list_rows())
    assert matrix.summary.scheduler_safe_count == len(readiness.scheduler_safe_sources())
