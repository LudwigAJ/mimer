"""Target-fund (VUSA/ISF/JEPG) live-coverage matrix: honest per-(fund, data type) state.

Guards the per-fund honesty contract: a fixture-fed data type is never counted as live,
NAV is never conflated with the listing price, a blocked provider carries a concrete
blocker, only a genuinely verified config is ``verified_live``, and no cell leaks a
secret/tokenised URL or uses a ticker as identity.
"""

from __future__ import annotations

from httpx import AsyncClient

from app.services import capabilities as capabilities_service
from app.sources import fund_source_coverage as fc
from app.sources import issuer_source_config
from app.sources import source_readiness as readiness


def _cell(fund_symbol: str, data_type: str) -> fc.FundCoverageRow:
    row = fc.coverage_cell(fund_symbol, data_type)
    assert row is not None, (fund_symbol, data_type)
    return row


# --- matrix coverage + vocabulary -------------------------------------------


def test_all_target_funds_and_data_types_present() -> None:
    symbols = {f.symbol for f in fc.TARGET_FUNDS}
    assert symbols == {"VUSA", "ISF", "JEPG"}
    for fund in fc.TARGET_FUNDS:
        data_types = {r.data_type for r in fc.coverage_for_fund(fund)}
        assert data_types == set(fc.FUND_DATA_TYPES)
    # 3 funds x 6 data types.
    assert len(fc.list_coverage_rows()) == 18


def test_every_cell_uses_a_valid_readiness_status() -> None:
    for row in fc.list_coverage_rows():
        assert row.status in readiness.READINESS_STATUSES, (row.fund_symbol, row.data_type)


def test_target_fund_isins_match_the_issuer_config_registry() -> None:
    # The per-fund holdings/distribution config the matrix derives from must exist for the
    # exact ISINs we track (no drift between the two in-code registries).
    assert fc.get_target_fund_by_isin("IE0005042456").symbol == "ISF"  # type: ignore[union-attr]
    isf_holdings = issuer_source_config.get_source_config(
        "IE0005042456", "blackrock_ishares_holdings"
    )
    assert isf_holdings is not None and isf_holdings.source_status == issuer_source_config.VERIFIED


# --- honest per-fund statuses ------------------------------------------------


def test_isf_holdings_is_verified_live_and_scheduler_safe() -> None:
    cell = _cell("ISF", "holdings")
    assert cell.status == readiness.VERIFIED_LIVE
    assert cell.is_live
    assert cell.live_fetch_verified and cell.parse_verified
    assert cell.safe_for_scheduler
    assert cell.last_verified_at is not None
    assert cell.known_blocker is None


def test_jepg_holdings_is_candidate_format_varies() -> None:
    # A 2026-06-27 bounded live verify returned a clean .xlsx (247 rows), but a 2026-06-25
    # fetch returned binary .xls — the format varies across runs, so it stays candidate
    # (not promoted to verified_live) until a stable re-verify.
    cell = _cell("JEPG", "holdings")
    assert cell.status == readiness.CANDIDATE
    assert not cell.is_live
    assert not cell.safe_for_scheduler
    assert cell.known_blocker and "xlsx" in cell.known_blocker.lower()


def test_vusa_holdings_is_planned_with_offline_export_fallback() -> None:
    cell = _cell("VUSA", "holdings")
    assert cell.status == readiness.PLANNED
    assert not cell.is_live
    assert not cell.safe_for_scheduler
    assert cell.offline_export_available  # vanguard_holdings_export exists, but is NOT live
    assert cell.known_blocker


def test_vusa_distributions_is_candidate_blocked_by_tls() -> None:
    cell = _cell("VUSA", "distributions")
    assert cell.status == readiness.CANDIDATE
    assert not cell.is_live
    assert not cell.safe_for_scheduler
    assert cell.known_blocker and (
        "tls" in cell.known_blocker.lower() or "handshake" in cell.known_blocker.lower()
    )


def test_jepg_distributions_is_candidate_no_verified_url() -> None:
    cell = _cell("JEPG", "distributions")
    assert cell.status == readiness.CANDIDATE
    assert not cell.is_live
    assert cell.known_blocker


def test_isf_distributions_is_planned() -> None:
    cell = _cell("ISF", "distributions")
    assert cell.status == readiness.PLANNED
    assert not cell.is_live


def test_listing_price_is_implemented_live_not_verified_and_scheduler_safe() -> None:
    for symbol in ("VUSA", "ISF", "JEPG"):
        cell = _cell(symbol, "listing_price")
        assert cell.status == readiness.IMPLEMENTED_LIVE  # NOT verified (no recorded fetch)
        assert cell.is_live
        assert cell.source_name == "stooq"
        assert not cell.live_fetch_verified
        assert cell.safe_for_scheduler  # Stooq is scheduler-safe


def test_nav_is_planned_and_never_conflated_with_listing_price() -> None:
    for symbol in ("VUSA", "ISF", "JEPG"):
        nav = _cell(symbol, "nav")
        assert nav.status == readiness.PLANNED
        assert not nav.is_live
        assert nav.source_name is None
        assert nav.known_blocker and "nav" in nav.known_blocker.lower()
        # The listing price is a different source and is never relabelled as NAV.
        price = _cell(symbol, "listing_price")
        assert price.source_name != nav.source_name


def test_facts_and_documents_are_fixture_not_live() -> None:
    for symbol in ("VUSA", "ISF", "JEPG"):
        for data_type in ("facts", "documents"):
            cell = _cell(symbol, data_type)
            assert cell.status == readiness.FIXTURE
            assert not cell.is_live
            assert cell.known_blocker  # explains there is no live source


# --- safety invariants -------------------------------------------------------


def test_only_verified_cells_claim_live_fetch_and_nothing_claims_stored() -> None:
    for row in fc.list_coverage_rows():
        if row.live_fetch_verified:
            assert row.status == readiness.VERIFIED_LIVE, (row.fund_symbol, row.data_type)
        # The verify path is fetch+parse only — no cell claims a recorded live store.
        assert row.stored_verified is False


def test_no_candidate_or_planned_cell_is_scheduler_safe() -> None:
    for row in fc.list_coverage_rows():
        if row.status in (readiness.CANDIDATE, readiness.PLANNED, readiness.FIXTURE):
            assert not row.safe_for_scheduler, (row.fund_symbol, row.data_type)


def test_blocked_cells_carry_blocker_text() -> None:
    for row in fc.list_coverage_rows():
        if row.is_blocked:
            assert row.known_blocker and row.known_blocker.strip()


def test_no_cell_leaks_a_secret_or_tokenised_url() -> None:
    # Public reference identifiers only — never a real download URL, host or secret in any
    # text field (guidance text like "do not guess the ajax URL pattern" is fine).
    forbidden = ("http://", "https://", "://", "token=", "apikey", "secret")
    for row in fc.list_coverage_rows():
        blob = " ".join(
            str(x) for x in (row.known_blocker, row.next_action, row.notes, row.source_name) if x
        ).lower()
        for needle in forbidden:
            assert needle not in blob, (row.fund_symbol, row.data_type, needle)


def test_source_name_is_never_a_bare_ticker() -> None:
    # Identity/provenance is the source adapter name, never the fund ticker.
    tickers = {f.symbol.lower() for f in fc.TARGET_FUNDS}
    for row in fc.list_coverage_rows():
        if row.source_name:
            assert row.source_name.lower() not in tickers


# --- summary rollup ----------------------------------------------------------


def test_summary_counts_are_honest() -> None:
    s = fc.summary()
    assert s.target_funds_total == 3
    assert s.target_funds_with_live_price == 3  # Stooq for all three
    assert s.target_funds_with_live_holdings == 1  # ISF only
    assert s.target_funds_with_live_distributions == 0
    assert s.target_funds_with_live_facts == 0
    assert s.target_funds_with_live_documents == 0
    assert s.fund_sources_verified_live == 1
    assert s.fund_sources_fixture_only == 6  # facts + documents across 3 funds
    assert s.fund_source_blockers >= 1


def test_per_fund_summary_matches_capabilities_example() -> None:
    by_symbol = {f.fund_symbol: f for f in fc.summary().funds}
    # VUSA: price live, holdings blocked, distributions blocked, facts/docs fixture.
    assert by_symbol["VUSA"].live_price and not by_symbol["VUSA"].live_holdings
    # ISF: price live, holdings live.
    assert by_symbol["ISF"].live_price and by_symbol["ISF"].live_holdings
    # JEPG: price live, holdings blocked.
    assert by_symbol["JEPG"].live_price and not by_symbol["JEPG"].live_holdings
    for fund in by_symbol.values():
        assert not fund.live_facts and not fund.live_documents


# --- service + endpoint ------------------------------------------------------


def test_capabilities_service_builds_fund_coverage() -> None:
    matrix = capabilities_service.build_fund_coverage_matrix()
    assert matrix.meta.count == 18
    isf = capabilities_service.build_fund_coverage_matrix("ISF")
    assert isf.meta.count == 6
    assert {r.fund_symbol for r in isf.data} == {"ISF"}


async def test_fund_coverage_endpoint(client: AsyncClient) -> None:
    body = (await client.get("/api/v1/data-sources/fund-coverage")).json()
    assert body["meta"]["count"] == 18
    assert body["summary"]["target_funds_with_live_holdings"] == 1
    statuses = {(r["fund_symbol"], r["data_type"]): r["status"] for r in body["data"]}
    assert statuses[("ISF", "holdings")] == "verified_live"
    assert statuses[("JEPG", "holdings")] == "candidate"
    assert statuses[("VUSA", "nav")] == "planned"


async def test_fund_coverage_endpoint_filters(client: AsyncClient) -> None:
    vusa = (await client.get("/api/v1/data-sources/fund-coverage?fund_symbol=VUSA")).json()
    assert {r["fund_symbol"] for r in vusa["data"]} == {"VUSA"}
    holdings = (await client.get("/api/v1/data-sources/fund-coverage?data_type=holdings")).json()
    assert {r["data_type"] for r in holdings["data"]} == {"holdings"}
    assert len(holdings["data"]) == 3
    verified = (await client.get("/api/v1/data-sources/fund-coverage?status=verified_live")).json()
    assert all(r["status"] == "verified_live" for r in verified["data"])


async def test_capabilities_endpoint_includes_fund_coverage(client: AsyncClient) -> None:
    body = (await client.get("/api/v1/capabilities")).json()
    fundcov = body["fund_coverage"]
    assert fundcov["target_funds_total"] == 3
    assert fundcov["target_funds_with_live_holdings"] == 1
    assert len(fundcov["funds"]) == 3
