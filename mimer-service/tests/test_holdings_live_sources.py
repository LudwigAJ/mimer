"""Live issuer holdings adapters: pure parsers + guarded live ingestion.

All offline — the live adapters' single HTTP call (``_download``) is stubbed so the
call still flows through ``guarded_fetch`` (recent-success cache + source budget +
fetch log) while never touching the network. Mirrors the rates live-adapter tests.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Fund, FundHolding
from app.services import constituent_identity as identity_service
from app.services import market_data_planner as planner
from app.services import source_budget, source_requests
from app.sources import registry
from app.sources.constituents import get_constituent_resolver
from app.sources.holdings import (
    IsharesHoldingsSource,
    JPMorganHoldingsSource,
    get_holdings_source,
    known_holdings_source,
    known_holdings_url,
    parse_ishares_holdings_csv,
    parse_jpmorgan_holdings,
    parse_vanguard_holdings_csv,
)
from app.workers.run import run_job

_ISF = "IE0005042456"  # iShares Core FTSE 100 (known iShares URL)
_JEPG = "IE0003UVYC20"  # JPM Global Equity Premium Income (known JPM URL)
_VUSA = "IE00B3XXRP09"  # Vanguard S&P 500 (export sample)
_ISHARES = "blackrock_ishares_holdings"
_JPM = "jpmorgan_etf_holdings"
_VANGUARD_EXPORT = "vanguard_holdings_export"


# --- realistic provider samples ----------------------------------------------

# iShares CSV: metadata/preamble before the holdings header, quoted thousands,
# percent weights, a placeholder ("-") CUSIP, a bad row (no name) and a trailing
# disclaimer row that must be ignored.
_ISHARES_CSV = (
    "iShares Core FTSE 100 UCITS ETF,,,,,,,,,,,,,,\n"
    'Fund Holdings as of,"30-May-2024",,,,,,,,,,,,,\n'
    'Inception Date,"28-Apr-2000",,,,,,,,,,,,,\n'
    ",,,,,,,,,,,,,,\n"
    "Ticker,Name,Sector,Asset Class,Market Value,Weight (%),Notional Value,Shares,"
    "Price,Location,Exchange,Market Currency,ISIN,SEDOL,CUSIP\n"
    'AZN,ASTRAZENECA PLC,Health Care,Equity,"1,234,567.89","8.10","1,234,567.89",'
    '"12,345","100.50",United Kingdom,London Stock Exchange,GBP,GB0009895292,0989529,-\n'
    'SHEL,SHELL PLC,Energy,Equity,"2,345,678.90","7.60","2,345,678.90",'
    '"45,678","28.50",United Kingdom,London Stock Exchange,GBP,GB00BP6MXD84,BP6MXD8,-\n'
    'HSBA,HSBC HOLDINGS PLC,Financials,Equity,"3,456,789.01","7.00","3,456,789.01",'
    '"99,999","6.80",United Kingdom,London Stock Exchange,GBP,GB0005405286,0540528,-\n'
    ',,,Equity,"0.00","-","0.00","0","0.00",,,GBP,,,\n'  # bad row: no name + no weight
    ",,,,,,,,,,,,,,\n"
    '"The iShares Funds are not sponsored, endorsed, or promoted by FTSE."\n'
)

# JPM CSV-like export: preamble, per-row "As of Date", both % columns (net assets
# preferred), identifiers, and a bad (non-numeric weight) row.
_JPM_CSV = (
    "JPMorgan Global Equity Premium Income Active UCITS ETF\n"
    "As of Date,Ticker,Security Description,Security Type,Shares/Par,Market Value,"
    "% of Net Assets,% of Market Value,Sector,Country,Currency,ISIN,CUSIP\n"
    '05/30/2024,MSFT,MICROSOFT CORP,Common Stock,"12,345","1,000,000.00","1.90","2.00",'
    "Information Technology,United States,USD,US5949181045,594918104\n"
    '05/30/2024,AAPL,APPLE INC,Common Stock,"23,456","950,000.00","1.80","1.85",'
    "Information Technology,United States,USD,US0378331005,037833100\n"
    '05/30/2024,NESN,NESTLE SA,Common Stock,"5,000","800,000.00","1.30","1.35",'
    "Consumer Staples,Switzerland,CHF,CH0038863350,\n"
    '05/30/2024,BAD,BROKEN ROW,Common Stock,"1","1.00","n/a","n/a",,,USD,,\n'  # bad weight
)

# JPM Excel-as-HTML export (content-sniffed): a real <table>, % of Net Assets.
_JPM_HTML = (
    "<html><head><title>Holdings</title></head><body>"
    "<table>"
    "<tr><th>Ticker</th><th>Security Description</th><th>% of Net Assets</th>"
    "<th>Country</th><th>Currency</th><th>ISIN</th></tr>"
    "<tr><td>JPM</td><td>JPMORGAN CHASE &amp; CO</td><td>1.50</td>"
    "<td>United States</td><td>USD</td><td>US46625H1005</td></tr>"
    "<tr><td>SAP</td><td>SAP SE</td><td>1.20</td>"
    "<td>Germany</td><td>EUR</td><td>DE0007164600</td></tr>"
    "</table></body></html>"
)


# --- iShares parser ----------------------------------------------------------


def test_ishares_parser_scans_header_and_parses() -> None:
    records = parse_ishares_holdings_csv(_ISHARES_CSV)
    # Three good rows; the no-name row and the disclaimer/blank rows are isolated.
    assert len(records) == 3
    names = [r.holding_name for r in records]
    assert names == ["ASTRAZENECA PLC", "SHELL PLC", "HSBC HOLDINGS PLC"]
    assert all(r.source == _ISHARES for r in records)
    assert all(r.status == "official" for r in records)


def test_ishares_parser_reads_as_of_from_preamble() -> None:
    records = parse_ishares_holdings_csv(_ISHARES_CSV)
    assert all(r.as_of_date == date(2024, 5, 30) for r in records)


def test_ishares_parser_decimal_and_percent_cleaning() -> None:
    azn = parse_ishares_holdings_csv(_ISHARES_CSV)[0]
    # "Weight (%)" 8.10 -> fraction 0.081; thousands-separated money parsed.
    assert str(azn.weight) == "0.08100000"
    assert azn.market_value == Decimal("1234567.89")
    assert azn.shares == Decimal("12345")


def test_ishares_parser_identifier_and_classification_columns() -> None:
    azn = parse_ishares_holdings_csv(_ISHARES_CSV)[0]
    assert azn.holding_ticker == "AZN"
    assert azn.holding_isin == "GB0009895292"
    assert azn.holding_sedol == "0989529"
    assert azn.holding_cusip is None  # "-" placeholder dropped
    assert azn.sector == "Health Care"
    assert azn.country == "United Kingdom"
    assert azn.currency == "GBP"
    # Asset Class / Exchange / Price have no canonical column -> preserved in raw.
    assert azn.raw_payload and azn.raw_payload["asset_class"] == "Equity"


def test_ishares_parser_tolerates_missing_optional_columns() -> None:
    minimal = "Holdings\nTicker,Name,Weight (%)\nAAA,Alpha Corp,5.00\nBBB,Beta Inc,3.50\n"
    records = parse_ishares_holdings_csv(minimal)
    assert len(records) == 2
    assert records[0].holding_name == "Alpha Corp"
    assert records[0].market_value is None  # absent, not an error
    assert str(records[0].weight) == "0.05000000"


def test_ishares_parser_empty_on_garbage() -> None:
    assert parse_ishares_holdings_csv("not,a,holdings,file\n1,2,3,4\n") == []


# --- JPMorgan parser ---------------------------------------------------------


def test_jpmorgan_csv_parser_maps_columns_and_prefers_net_assets() -> None:
    records = parse_jpmorgan_holdings(_JPM_CSV)
    # Three good rows; the non-numeric-weight row is isolated.
    assert len(records) == 3
    msft = records[0]
    assert msft.holding_name == "MICROSOFT CORP"
    assert msft.holding_ticker == "MSFT"
    assert msft.holding_isin == "US5949181045"
    assert msft.holding_cusip == "594918104"
    assert msft.country == "United States"
    assert msft.currency == "USD"
    assert msft.shares == pytest.approx(12345)
    # "% of Net Assets" (1.90) wins over "% of Market Value" (2.00) -> 0.019.
    assert str(msft.weight) == "0.01900000"
    assert msft.source == _JPM


def test_jpmorgan_parser_reads_per_row_as_of_date() -> None:
    records = parse_jpmorgan_holdings(_JPM_CSV)
    assert all(r.as_of_date == date(2024, 5, 30) for r in records)


def test_jpmorgan_parser_content_sniffs_html_table() -> None:
    records = parse_jpmorgan_holdings(_JPM_HTML)
    assert len(records) == 2
    jpm = records[0]
    assert jpm.holding_name == "JPMORGAN CHASE & CO"  # HTML entity decoded
    assert jpm.holding_ticker == "JPM"
    assert jpm.holding_isin == "US46625H1005"
    assert jpm.currency == "USD"
    assert str(jpm.weight) == "0.01500000"


def test_jpmorgan_parser_isolates_bad_rows() -> None:
    # The "n/a" weight row produces no record; the rest survive.
    names = {r.holding_name for r in parse_jpmorgan_holdings(_JPM_CSV)}
    assert "BROKEN ROW" not in names


# --- Vanguard exported-file parser -------------------------------------------


def test_vanguard_export_parser_maps_columns() -> None:
    sample = (
        "Vanguard S&P 500 UCITS ETF\n"
        "Holdings as of,30/04/2026\n"
        "\n"
        "Holding name,Ticker,SEDOL,ISIN,Sector,Country,Currency,Shares,Market value,% of fund\n"
        'Apple Inc,AAPL,2046251,US0378331005,Technology,US,USD,"1,234,567","250,000,000",7.10\n'
        'Microsoft Corp,MSFT,2588173,US5949181045,Technology,US,USD,"987,654","232,000,000",6.60\n'
    )
    records = parse_vanguard_holdings_csv(sample)
    assert len(records) == 2
    apple = records[0]
    assert apple.holding_name == "Apple Inc"
    assert apple.holding_isin == "US0378331005"
    assert apple.holding_sedol == "2046251"
    assert apple.currency == "USD"
    assert str(apple.weight) == "0.07100000"  # "% of fund" 7.10 -> 0.071
    assert apple.source == _VANGUARD_EXPORT
    assert apple.status == "official_export"
    assert apple.as_of_date == date(2026, 4, 30)


# --- known-URL registry ------------------------------------------------------


def test_known_holdings_url_registry() -> None:
    assert known_holdings_source(_ISF) == _ISHARES
    assert known_holdings_source(_JEPG) == _JPM
    assert known_holdings_source("ZZ0000000000") is None
    # Source must match the ISIN's configured issuer.
    assert known_holdings_url(_ISF, _ISHARES) is not None
    assert known_holdings_url(_ISF, _JPM) is None


# --- live adapter ingestion (guarded; HTTP stubbed) --------------------------


def _patch_ishares(monkeypatch: pytest.MonkeyPatch, *, text: str = _ISHARES_CSV, calls=None):
    async def fake_download(self, url: str) -> str:  # noqa: ANN001
        if calls is not None:
            calls["n"] = calls.get("n", 0) + 1
            calls["url"] = url
        return text

    monkeypatch.setattr(IsharesHoldingsSource, "_download", fake_download)


def _patch_jpmorgan(monkeypatch: pytest.MonkeyPatch, *, text: str = _JPM_CSV, calls=None):
    async def fake_download(self, url: str) -> str:  # noqa: ANN001
        if calls is not None:
            calls["n"] = calls.get("n", 0) + 1
        return text

    monkeypatch.setattr(JPMorganHoldingsSource, "_download", fake_download)


async def _fund(session: AsyncSession, isin: str) -> Fund:
    fund = await session.scalar(select(Fund).where(Fund.isin == isin))
    assert fund is not None
    return fund


async def test_ishares_worker_ingests_via_known_url(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: dict = {}
    _patch_ishares(monkeypatch, calls=calls)
    fund = await _fund(session, _ISF)
    # No --url given: the adapter resolves the verified known URL for this ISIN.
    run = await run_job(session, "issuer_holdings_ingestion", fund_id=fund.id, source_name=_ISHARES)
    assert run.status == "success"
    assert run.source == _ISHARES
    assert run.records_inserted == 3
    assert calls["n"] == 1  # exactly one guarded download
    assert "ISF_holdings" in calls["url"]

    rows = (
        (
            await session.execute(
                select(FundHolding).where(
                    FundHolding.fund_id == fund.id, FundHolding.source == _ISHARES
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 3
    assert all(r.holding_key and r.security_isin for r in rows)


async def test_ishares_worker_uses_guarded_fetch_log(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_ishares(monkeypatch)
    fund = await _fund(session, _ISF)
    await run_job(session, "issuer_holdings_ingestion", fund_id=fund.id, source_name=_ISHARES)
    logs = await source_requests.list_fetch_logs(session, source=_ISHARES, status="success")
    assert logs
    log = logs[0]
    assert log.request_kind == "fetch_ishares_holdings"
    # Safe fetch log: a host/path class, no query/secrets, and only the ISIN key.
    assert log.endpoint_label and "?" not in log.endpoint_label
    assert "APIKEY" not in (log.request_key or "").upper()
    assert log.raw_payload_hash


async def test_ishares_ingest_recent_cache_avoids_repeat_call(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: dict = {}
    _patch_ishares(monkeypatch, calls=calls)
    fund = await _fund(session, _ISF)
    first = await run_job(
        session, "issuer_holdings_ingestion", fund_id=fund.id, source_name=_ISHARES
    )
    second = await run_job(
        session, "issuer_holdings_ingestion", fund_id=fund.id, source_name=_ISHARES
    )
    # Second run is served from the recent-success cache: no extra HTTP call and
    # (the real guarantee) no duplicate rows.
    assert calls["n"] == 1
    assert first.records_inserted == 3
    assert second.records_inserted == 0
    count = await session.scalar(
        select(func.count())
        .select_from(FundHolding)
        .where(FundHolding.fund_id == fund.id, FundHolding.source == _ISHARES)
    )
    assert count == 3


async def test_ishares_ingest_idempotent_and_updates_changed_rows(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.services import holdings_ingestion as holdings_service

    # Bypass the recent-success cache (so each run re-fetches and the idempotent
    # UPSERT is the thing preventing duplicates) and the min-delay spacing (so the
    # back-to-back second fetch is not budget-blocked).
    monkeypatch.setattr(source_requests, "should_skip_recent_success", _no_cache)
    budget = await source_budget.get_budget(session, _ISHARES)
    assert budget is not None
    budget.min_delay_ms = 0
    await session.commit()
    fund = await _fund(session, _ISF)

    _patch_ishares(monkeypatch, text=_ISHARES_CSV)
    src = get_holdings_source(_ISHARES)
    first = await holdings_service.ingest_holdings_for_fund(session, fund, src)
    assert first.inserted == 3

    # Same disclosure date + identities, one corrected weight -> update, not insert.
    changed = _ISHARES_CSV.replace('"8.10"', '"8.50"')
    _patch_ishares(monkeypatch, text=changed)
    second = await holdings_service.ingest_holdings_for_fund(session, fund, src)
    assert second.inserted == 0
    assert second.updated == 1
    assert second.skipped == 2

    row = await session.scalar(
        select(FundHolding).where(
            FundHolding.fund_id == fund.id,
            FundHolding.source == _ISHARES,
            FundHolding.security_isin == "GB0009895292",
        )
    )
    assert row is not None and str(row.weight) == "0.08500000"


async def test_jpmorgan_worker_ingests(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_jpmorgan(monkeypatch)
    fund = await _fund(session, _JEPG)
    run = await run_job(session, "issuer_holdings_ingestion", fund_id=fund.id, source_name=_JPM)
    assert run.status == "success"
    assert run.records_inserted == 3
    rows = (
        (
            await session.execute(
                select(FundHolding).where(
                    FundHolding.fund_id == fund.id, FundHolding.source == _JPM
                )
            )
        )
        .scalars()
        .all()
    )
    assert {r.security_ticker for r in rows} == {"MSFT", "AAPL", "NESN"}


async def test_holdings_budget_block_makes_no_call(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Put the iShares budget in backoff so guarded_fetch blocks BEFORE any fetch.
    await source_budget.apply_backoff(session, _ISHARES, seconds=120)
    await session.commit()

    async def boom(self, url: str):  # noqa: ANN001 - must never run in backoff
        raise AssertionError("a live iShares call was attempted while in backoff")

    monkeypatch.setattr(IsharesHoldingsSource, "_download", boom)
    fund = await _fund(session, _ISF)
    run = await run_job(session, "issuer_holdings_ingestion", fund_id=fund.id, source_name=_ISHARES)
    assert run.records_inserted == 0
    assert run.status == "success"  # clean no-op, not a failure
    rate_limited = await source_requests.list_fetch_logs(
        session, source=_ISHARES, status="rate_limited"
    )
    assert rate_limited


async def test_holdings_missing_source_config_is_clean_noop(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    # VUSA has no configured iShares URL, and no --url is given -> clean no-op.
    async def boom(self, url: str):  # noqa: ANN001 - must never be reached
        raise AssertionError("no URL configured, yet a download was attempted")

    monkeypatch.setattr(IsharesHoldingsSource, "_download", boom)
    fund = await _fund(session, _VUSA)
    run = await run_job(session, "issuer_holdings_ingestion", fund_id=fund.id, source_name=_ISHARES)
    assert run.status == "success"
    assert run.records_inserted == 0
    assert "no_provider_match=1" in (run.message or "")


async def test_ishares_url_override_single_fund(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: dict = {}
    _patch_ishares(monkeypatch, calls=calls)
    fund = await _fund(session, _VUSA)  # no known URL, but an explicit override is given
    override = (
        "https://www.blackrock.com/uk/.../ABC.ajax?dataType=fund&fileName=X_holdings&fileType=csv"
    )
    run = await run_job(
        session, "issuer_holdings_ingestion", fund_id=fund.id, source_name=_ISHARES, url=override
    )
    assert run.records_inserted == 3
    assert calls["url"] == override


async def test_holdings_no_live_calls_in_tests(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Belt-and-braces: even the configured default must never reach the network.
    async def explode(*args, **kwargs):  # pragma: no cover
        raise AssertionError("a live HTTP call was attempted")

    monkeypatch.setattr(httpx.AsyncClient, "get", explode)
    fund = await _fund(session, _VUSA)
    run = await run_job(session, "issuer_holdings_ingestion", fund_id=fund.id)  # fixture default
    assert run.status == "success"
    assert run.records_inserted == 10  # offline fixture, no network


# --- Vanguard export adapter (offline) ---------------------------------------


async def test_vanguard_export_worker_ingests_bundled_sample(
    session: AsyncSession,
) -> None:
    fund = await _fund(session, _VUSA)
    run = await run_job(
        session, "issuer_holdings_ingestion", fund_id=fund.id, source_name=_VANGUARD_EXPORT
    )
    assert run.status == "success"
    assert run.records_inserted == 4
    rows = (
        (
            await session.execute(
                select(FundHolding).where(
                    FundHolding.fund_id == fund.id, FundHolding.source == _VANGUARD_EXPORT
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 4
    assert all(r.status == "fixture" for r in rows)  # bundled sample marker


async def test_vanguard_live_is_planned_and_fails_cleanly(
    session: AsyncSession,
) -> None:
    fund = await _fund(session, _VUSA)
    run = await run_job(
        session, "issuer_holdings_ingestion", fund_id=fund.id, source_name="vanguard_holdings"
    )
    assert run.status == "failed"
    assert "planned" in (run.message or "").lower()


# --- workspace scope ---------------------------------------------------------


async def test_holdings_workspace_scope(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_ishares(monkeypatch)
    # Workspace 1 holds the seeded funds; only ISF has a known iShares URL, so the
    # others are a clean no-op within the same run.
    run = await run_job(
        session,
        "issuer_holdings_ingestion",
        workspace_id=1,
        source_name=_ISHARES,
    )
    assert run.status == "success"
    assert run.records_inserted == 3  # ISF only
    assert "selected_funds=" in (run.message or "")


# --- identity-resolution readiness -------------------------------------------


async def test_live_holdings_ready_for_identity_resolution(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_ishares(monkeypatch)
    fund = await _fund(session, _ISF)
    await run_job(session, "issuer_holdings_ingestion", fund_id=fund.id, source_name=_ISHARES)

    unresolved = await identity_service.unresolved_holdings(session, fund_id=fund.id)
    live = [h for h in unresolved if h.source == _ISHARES]
    assert live and all(h.security_isin for h in live)
    # The canonical identifiers let the resolver build *safe* requests (no name-only).
    resolver = get_constituent_resolver()
    requests, _ids, _unsafe = identity_service.build_requests(live, resolver=resolver)
    assert requests


# --- planner / capabilities / registry ---------------------------------------


def test_planner_holdings_source_candidates_include_live() -> None:
    isf = Fund(isin=_ISF, name="iShares Core FTSE 100")
    candidates = planner._holdings_source_candidates(isf, default="holdings_fixture")
    assert candidates[0] == "holdings_fixture"
    assert _ISHARES in candidates
    # A fund with no configured live source gets only the fixture default.
    other = Fund(isin="ZZ0000000000", name="Unknown ETF")
    assert planner._holdings_source_candidates(other, default="holdings_fixture") == [
        "holdings_fixture"
    ]


def test_registry_holdings_source_statuses() -> None:
    ishares = registry.get_capability(_ISHARES)
    jpm = registry.get_capability(_JPM)
    export = registry.get_capability(_VANGUARD_EXPORT)
    planned = registry.get_capability("vanguard_holdings")
    assert ishares and ishares.adapter_status == "implemented" and ishares.supports_live
    assert jpm and jpm.adapter_status == "implemented" and jpm.supports_live
    assert export and export.adapter_status == "implemented" and not export.supports_live
    assert planned and planned.adapter_status == "planned"
    for cap in (ishares, jpm, export, planned):
        assert "holdings" in cap.data_types


async def test_capabilities_endpoint_lists_live_holdings_sources(client) -> None:
    body = (await client.get("/api/v1/data-sources/capabilities?data_type=holdings")).json()
    names = {c["source_name"] for c in body["data"]}
    assert {_ISHARES, _JPM, _VANGUARD_EXPORT, "vanguard_holdings"} <= names
    assert {"holdings_fixture"} <= names


def test_live_holdings_budgets_are_conservative() -> None:
    specs = source_budget.default_budget_specs()
    for name in (_ISHARES, _JPM):
        assert specs[name]["max_concurrency"] == 1
        assert int(specs[name]["min_delay_ms"]) >= 1000  # type: ignore[arg-type]
        assert int(specs[name]["max_requests_per_minute"]) <= 10  # type: ignore[arg-type]


# --- helpers -----------------------------------------------------------------


async def _no_cache(*args, **kwargs):  # noqa: ANN002, ANN003
    """Stand-in for ``should_skip_recent_success`` that never caches."""
    return None
