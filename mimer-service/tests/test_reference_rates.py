"""Official / reference-rate ingestion foundation.

Covers the slice that collects + persists official/reference rate *observations*
(ECB/BoE policy rates, €STR/SONIA/SOFR/Fed Funds, US Treasury par yields) into
``reference_rates`` and exposes/monitors them. Everything is offline: the fixture
provider never touches the network, and no test may make a live call.

Compute boundary (AGENTS.md): the backend only collects/normalises/persists/
serves observations — it never builds curves, bootstraps, interpolates, computes
forward rates or prices bonds. Tests assert that boundary stays intact.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import Base
from app.db.models import ReferenceRate
from app.services import market_data_planner, source_budget, source_requests
from app.services import rates_ingestion as rates_service
from app.sources.rates import (
    SUPPORTED_RATE_CURRENCIES,
    ECBRatesSource,
    FixtureRatesSource,
    ReferenceRateRecord,
    TreasuryRatesSource,
    get_rates_source,
    parse_ecb_sdmx_csv,
    parse_treasury_par_yield_xml,
)
from app.workers.run import run_job

_FIXTURE = "rates_fixture"
_TREASURY = "us_treasury_rates"

# A small, structurally faithful slice of the official Treasury daily par-yield XML
# feed: two business days with the OData namespaces the real feed uses. Day 1 is
# complete; day 2 deliberately omits BC_6MONTH (a missing tenor cell) and carries a
# non-numeric BC_20YEAR (a bad cell to isolate). BC_1_5MONTH (6-week, non-integer
# months) and the duplicate BC_30YEARDISPLAY must be skipped by the parser.
_SAMPLE_TREASURY_XML = """<?xml version="1.0" encoding="utf-8" standalone="yes" ?>
<feed xml:base="https://home.treasury.gov/x"
      xmlns:d="http://schemas.microsoft.com/ado/2007/08/dataservices"
      xmlns:m="http://schemas.microsoft.com/ado/2007/08/dataservices/metadata"
      xmlns="http://www.w3.org/2005/Atom">
  <entry><content type="application/xml"><m:properties>
    <d:Id m:type="Edm.Int32">1</d:Id>
    <d:NEW_DATE m:type="Edm.DateTime">2026-01-02T00:00:00</d:NEW_DATE>
    <d:BC_1MONTH m:type="Edm.Double">5.40</d:BC_1MONTH>
    <d:BC_1_5MONTH m:type="Edm.Double">5.39</d:BC_1_5MONTH>
    <d:BC_3MONTH m:type="Edm.Double">5.38</d:BC_3MONTH>
    <d:BC_6MONTH m:type="Edm.Double">5.30</d:BC_6MONTH>
    <d:BC_1YEAR m:type="Edm.Double">5.05</d:BC_1YEAR>
    <d:BC_2YEAR m:type="Edm.Double">4.70</d:BC_2YEAR>
    <d:BC_10YEAR m:type="Edm.Double">4.30</d:BC_10YEAR>
    <d:BC_30YEAR m:type="Edm.Double">4.45</d:BC_30YEAR>
    <d:BC_30YEARDISPLAY m:type="Edm.Double">4.45</d:BC_30YEARDISPLAY>
  </m:properties></content></entry>
  <entry><content type="application/xml"><m:properties>
    <d:Id m:type="Edm.Int32">2</d:Id>
    <d:NEW_DATE m:type="Edm.DateTime">2026-01-03T00:00:00</d:NEW_DATE>
    <d:BC_1MONTH m:type="Edm.Double">5.41</d:BC_1MONTH>
    <d:BC_3MONTH m:type="Edm.Double">5.39</d:BC_3MONTH>
    <d:BC_6MONTH m:type="Edm.Double"></d:BC_6MONTH>
    <d:BC_10YEAR m:type="Edm.Double">4.31</d:BC_10YEAR>
    <d:BC_20YEAR m:type="Edm.Double">N/A</d:BC_20YEAR>
    <d:BC_30YEAR m:type="Edm.Double">4.46</d:BC_30YEAR>
  </m:properties></content></entry>
</feed>"""

# Day 1 -> 7 valid tenors (1M,3M,6M,1Y,2Y,10Y,30Y); day 2 -> 4 (1M,3M,10Y,30Y;
# 6M empty + 20Y non-numeric are dropped) = 11 observations.
_SAMPLE_TREASURY_ROWS = 11


def _patch_treasury_http(
    monkeypatch: pytest.MonkeyPatch,
    *,
    text: str = _SAMPLE_TREASURY_XML,
    calls: dict | None = None,
) -> None:
    """Stub the adapter's per-year HTTP fetch so it parses fixture XML — no real call.

    Patches only ``TreasuryRatesSource._fetch_year_xml`` (the single place httpx is
    used), so the call still flows through ``guarded_fetch`` (cache/budget/fetch log)
    while never touching the network or the in-process ASGI test client.
    """

    async def fake_fetch_year(self, year: int) -> str:  # noqa: ANN001
        if calls is not None:
            calls["n"] = calls.get("n", 0) + 1
        return text

    monkeypatch.setattr(TreasuryRatesSource, "_fetch_year_xml", fake_fetch_year)


# --- fixture source ----------------------------------------------------------


async def test_fixture_returns_all_currency_families() -> None:
    records = await FixtureRatesSource().fetch_rates()
    names = {r.rate_name for r in records}
    # EUR policy + facility rates and €STR.
    assert {
        "ECB_MAIN_REFINANCING_RATE",
        "ECB_DEPOSIT_FACILITY_RATE",
        "ECB_MARGINAL_LENDING_RATE",
        "ESTR",
    } <= names
    # GBP Bank Rate + SONIA.
    assert {"BOE_BANK_RATE", "SONIA"} <= names
    # USD Treasury par yields + money-market benchmarks.
    assert {"US_TREASURY_PAR_YIELD", "SOFR", "FED_FUNDS_EFFECTIVE"} <= names


async def test_fixture_treasury_tenor_parsing() -> None:
    records = await FixtureRatesSource().fetch_rates(rate_family="treasury_par_yield")
    tenors = {(r.tenor, r.tenor_months) for r in records}
    assert ("1M", 1) in tenors
    assert ("10Y", 120) in tenors
    assert ("30Y", 360) in tenors
    # Every par-yield row carries a tenor + numeric months; all USD/treasury.
    assert all(r.tenor and r.tenor_months for r in records)
    assert {r.currency for r in records} == {"USD"}
    assert {r.rate_family for r in records} == {"treasury_par_yield"}


async def test_fixture_latest_day_equals_base_value() -> None:
    records = await FixtureRatesSource().fetch_rates(currency="EUR", rate_family="overnight_rate")
    estr = [r for r in records if r.rate_name == "ESTR"]
    latest = max(estr, key=lambda r: r.rate_date)
    # The most-recent day is exactly the clean base value (no wave) by design.
    assert latest.rate_value == Decimal("3.66")
    assert latest.unit == "percent"
    assert latest.status == "fixture"


async def test_fixture_currency_and_date_filtering() -> None:
    eur = await FixtureRatesSource().fetch_rates(currency="EUR")
    assert {r.currency for r in eur} == {"EUR"}

    window = await FixtureRatesSource().fetch_rates(
        currency="GBP", start_date=date(2026, 1, 1), end_date=date(2026, 1, 5)
    )
    dates = {r.rate_date for r in window}
    assert dates == {date(2026, 1, d) for d in range(1, 6)}


async def test_fixture_unknown_filter_is_empty_not_error() -> None:
    assert await FixtureRatesSource().fetch_rates(currency="ZZZ") == []
    assert await FixtureRatesSource().fetch_rates(rate_family="made_up") == []


# --- ingestion service -------------------------------------------------------


async def test_ingest_inserts_rows(session: AsyncSession) -> None:
    result = await rates_service.ingest_reference_rates(session, source=_FIXTURE)
    assert result.selected > 0
    assert result.inserted == result.selected
    assert result.updated == 0 and result.failed == 0
    assert result.is_fixture is True
    count = await session.scalar(select(func.count()).select_from(ReferenceRate))
    assert count == result.inserted


async def test_ingest_is_idempotent(session: AsyncSession) -> None:
    first = await rates_service.ingest_reference_rates(session, source=_FIXTURE)
    await session.commit()
    second = await rates_service.ingest_reference_rates(session, source=_FIXTURE)
    # Re-running the same offline fixture changes nothing: all skipped, no dupes.
    assert second.inserted == 0
    assert second.updated == 0
    assert second.skipped == second.selected == first.inserted
    count = await session.scalar(select(func.count()).select_from(ReferenceRate))
    assert count == first.inserted


async def test_ingest_updates_changed_value(session: AsyncSession) -> None:
    await rates_service.ingest_reference_rates(session, source=_FIXTURE, currency="GBP")
    await session.commit()
    row = await session.scalar(
        select(ReferenceRate).where(ReferenceRate.rate_name == "BOE_BANK_RATE").limit(1)
    )
    assert row is not None
    row.rate_value = Decimal("99")
    await session.commit()
    # Re-ingest restores the published value (idempotent upsert updates in place).
    result = await rates_service.ingest_reference_rates(session, source=_FIXTURE, currency="GBP")
    assert result.updated >= 1


async def test_ingest_currency_filter(session: AsyncSession) -> None:
    await rates_service.ingest_reference_rates(session, source=_FIXTURE, currency="EUR")
    currencies = set((await session.execute(select(ReferenceRate.currency).distinct())).scalars())
    assert currencies == {"EUR"}


async def test_ingest_country_region_filter(session: AsyncSession) -> None:
    await rates_service.ingest_reference_rates(
        session, source=_FIXTURE, country_or_region="united_states"
    )
    regions = set(
        (await session.execute(select(ReferenceRate.country_or_region).distinct())).scalars()
    )
    assert regions == {"united_states"}


async def test_ingest_date_range_and_limit(session: AsyncSession) -> None:
    ranged = await rates_service.ingest_reference_rates(
        session,
        source=_FIXTURE,
        currency="GBP",
        start_date=date(2026, 1, 1),
        end_date=date(2026, 1, 3),
    )
    # GBP = BOE_BANK_RATE + SONIA over 3 days.
    assert ranged.start_date == date(2026, 1, 1)
    assert ranged.selected == 6

    limited = await rates_service.ingest_reference_rates(session, source=_FIXTURE, limit=4)
    assert limited.selected == 4


async def test_ingest_isolates_a_failed_observation(session: AsyncSession) -> None:
    class _PartlyBadSource:
        name = "rates_fixture_test"

        async def fetch_rates(self, **_kwargs):
            good = ReferenceRateRecord(
                rate_date=date(2026, 6, 24),
                currency="USD",
                country_or_region="united_states",
                rate_family="overnight_rate",
                rate_name="SOFR",
                rate_value=Decimal("5.31"),
                source=self.name,
            )
            bad = ReferenceRateRecord(
                rate_date=date(2026, 6, 24),
                currency="USD",
                country_or_region="united_states",
                rate_family="overnight_rate",
                rate_name="BROKEN",
                rate_value=None,  # type: ignore[arg-type]
                source=self.name,
            )
            return [good, bad]

    import app.sources.rates as rates_source_module

    rates_source_module._SOURCES["rates_fixture_test"] = _PartlyBadSource()  # type: ignore[assignment]
    try:
        result = await rates_service.ingest_reference_rates(session, source="rates_fixture_test")
    finally:
        rates_source_module._SOURCES.pop("rates_fixture_test", None)
    assert result.inserted == 1  # the good SOFR row landed
    assert result.failed == 1  # the bad row was isolated, not fatal


# --- worker ------------------------------------------------------------------


async def test_worker_records_job_run(session: AsyncSession) -> None:
    run = await run_job(session, "rates_ingestion", source_name=_FIXTURE)
    assert run.status == "success"
    assert run.records_inserted and run.records_inserted > 0
    assert f"source={_FIXTURE}" in run.message
    assert run.job_type == "rates_ingestion"


async def test_worker_planned_source_fails_cleanly(session: AsyncSession) -> None:
    run = await run_job(session, "rates_ingestion", source_name="boe_rates")
    # A planned live adapter never makes a surprise call — it fails cleanly.
    assert run.status == "failed"
    assert "planned" in run.message.lower()
    # Nothing was written.
    count = await session.scalar(select(func.count()).select_from(ReferenceRate))
    assert count == 0


# --- API ---------------------------------------------------------------------


async def _ingest_all(session: AsyncSession) -> None:
    await rates_service.ingest_reference_rates(session, source=_FIXTURE)
    await session.commit()


async def test_api_list_and_filters(client: AsyncClient, session: AsyncSession) -> None:
    await _ingest_all(session)
    body = (await client.get("/api/v1/rates?currency=EUR")).json()
    assert body["meta"]["count"] > 0
    assert all(r["currency"] == "EUR" for r in body["data"])

    treasury = (
        await client.get(
            "/api/v1/rates?country_or_region=united_states&rate_family=treasury_par_yield"
        )
    ).json()
    assert treasury["meta"]["count"] > 0
    assert all(r["rate_family"] == "treasury_par_yield" for r in treasury["data"])
    assert all(r["tenor"] for r in treasury["data"])


async def test_api_latest_one_per_series(client: AsyncClient, session: AsyncSession) -> None:
    await _ingest_all(session)
    body = (await client.get("/api/v1/rates/latest?currency=EUR")).json()
    names = sorted(r["rate_name"] for r in body["data"])
    # EUR has four distinct series (3 ECB rates + €STR), newest row each.
    assert names == [
        "ECB_DEPOSIT_FACILITY_RATE",
        "ECB_MAIN_REFINANCING_RATE",
        "ECB_MARGINAL_LENDING_RATE",
        "ESTR",
    ]


async def test_api_time_series(client: AsyncClient, session: AsyncSession) -> None:
    await _ingest_all(session)
    body = (await client.get("/api/v1/rates/time-series?rate_name=ESTR&currency=EUR")).json()
    assert body["rate_name"] == "ESTR"
    assert body["currency"] == "EUR"
    assert body["points"] and body["points"][0]["rate_value"]
    # Oldest-first ordering.
    dates = [p["rate_date"] for p in body["points"]]
    assert dates == sorted(dates)


async def test_api_sources_catalogue(client: AsyncClient) -> None:
    body = (await client.get("/api/v1/rates/sources")).json()
    by_source = {s["source"]: s for s in body["data"]}
    assert by_source["rates_fixture"]["adapter_status"] == "implemented"
    # The live US Treasury + ECB adapters are implemented; BoE remains planned.
    assert by_source["us_treasury_rates"]["adapter_status"] == "implemented"
    assert by_source["ecb_rates"]["adapter_status"] == "implemented"
    assert by_source["boe_rates"]["adapter_status"] == "planned"


async def test_api_sources_metadata_flags(client: AsyncClient) -> None:
    body = (await client.get("/api/v1/rates/sources")).json()
    by_source = {s["source"]: s for s in body["data"]}
    # The offline fixture is the configured default; live sources are explicit-only.
    fixture = by_source["rates_fixture"]
    assert fixture["is_fixture"] is True
    assert fixture["requires_live_fetch"] is False
    assert fixture["is_default"] is True
    treasury = by_source["us_treasury_rates"]
    assert treasury["is_fixture"] is False
    assert treasury["requires_live_fetch"] is True
    assert treasury["is_default"] is False
    assert "USD" in treasury["currencies"]
    assert "treasury_par_yield" in treasury["rate_families"]


async def test_api_limit_validation_and_unknown_filter(
    client: AsyncClient, session: AsyncSession
) -> None:
    await _ingest_all(session)
    # limit=0 is rejected (ge=1).
    assert (await client.get("/api/v1/rates?limit=0")).status_code == 422
    # An unknown currency yields an empty (but valid) list, not an error.
    empty = (await client.get("/api/v1/rates?currency=ZZZ")).json()
    assert empty["meta"]["count"] == 0


# --- planner -----------------------------------------------------------------


async def test_planner_emits_then_stops(client: AsyncClient, session: AsyncSession) -> None:
    wid = 1
    before = await market_data_planner.build_plan(session, wid)
    assert any(i.item_type == "fetch_reference_rates" for i in before.items)
    assert before.summary.reference_rate_currencies_missing >= 1

    await _ingest_all(session)
    after = await market_data_planner.build_plan(session, wid)
    assert not any(i.item_type == "fetch_reference_rates" for i in after.items)
    assert after.summary.reference_rate_currencies_missing == 0


async def test_planner_flags_stale_rates(session: AsyncSession) -> None:
    # Ingest GBP with an old window so the latest observation reads as stale.
    await rates_service.ingest_reference_rates(
        session,
        source=_FIXTURE,
        currency="GBP",
        start_date=date(2026, 1, 1),
        end_date=date(2026, 1, 10),
    )
    await session.commit()
    plan = await market_data_planner.build_plan(session, 1)
    gbp = [
        i
        for i in plan.items
        if i.item_type == "fetch_reference_rates" and i.identifier_value == "GBP"
    ]
    assert gbp and "stale" in gbp[0].reason


# --- diagnostics -------------------------------------------------------------


async def test_diagnostics_reference_rate_fields(
    client: AsyncClient, session: AsyncSession
) -> None:
    before = (await client.get("/api/v1/diagnostics")).json()
    assert before["reference_rates"] == 0
    # All three supported currencies are missing official rates before ingestion.
    assert before["missing_reference_rates"] == len(SUPPORTED_RATE_CURRENCIES)
    assert before["latest_reference_rate_date"] is None
    assert before["rates_ingestion_failures"] == 0

    await _ingest_all(session)
    after = (await client.get("/api/v1/diagnostics")).json()
    assert after["reference_rates"] > 0
    assert after["missing_reference_rates"] == 0
    assert after["stale_reference_rates"] == 0
    assert after["latest_reference_rate_date"] is not None


# --- capabilities ------------------------------------------------------------


async def test_capabilities_mark_reference_rates(client: AsyncClient) -> None:
    body = (await client.get("/api/v1/capabilities")).json()
    assert body["features"]["rates_ingestion"] == "fixture"
    statuses = {d["name"]: d["status"] for d in body["data_types"]}
    assert statuses["reference_rates"] == "fixture"
    # Curves stay planned — the backend never builds them.
    assert statuses["yield_curves"] == "planned"
    assert body["configured_sources"]["reference_rates"] == "rates_fixture"


async def test_data_source_capabilities_include_rates(client: AsyncClient) -> None:
    caps = (await client.get("/api/v1/data-sources/capabilities?data_type=reference_rates")).json()
    by_source = {c["source_name"]: c for c in caps["data"]}
    assert by_source["rates_fixture"]["adapter_status"] == "implemented"
    # The live US Treasury + ECB adapters are implemented; BoE stays planned.
    assert by_source["us_treasury_rates"]["adapter_status"] == "implemented"
    assert by_source["ecb_rates"]["adapter_status"] == "implemented"
    assert by_source["boe_rates"]["adapter_status"] == "planned"
    assert all("reference_rates" in c["data_types"] for c in caps["data"])


# --- safety / compute boundary ----------------------------------------------


def test_no_curve_or_discount_tables_exist() -> None:
    names = set(Base.metadata.tables)
    assert "reference_rates" in names
    # The backend stores observations only — never curves / discount factors / a
    # pricing table.
    forbidden = [
        n for n in names if any(k in n for k in ("curve", "discount", "bootstrap", "forward_rate"))
    ]
    assert forbidden == []


def test_rates_module_builds_no_curves() -> None:
    """Guard: the rates code path exposes no curve/bootstrap/interpolation helper."""
    import app.services.rates as read_mod
    import app.services.rates_ingestion as ingest_mod
    import app.sources.rates as source_mod

    banned = ("bootstrap", "interpolat", "discount_factor", "forward_rate", "build_curve", "fit_")
    for module in (read_mod, ingest_mod, source_mod):
        for attr in dir(module):
            assert not any(b in attr.lower() for b in banned), f"{module.__name__}.{attr}"


async def test_resolver_rejects_unknown_source() -> None:
    with pytest.raises(ValueError, match="Unknown rates source"):
        get_rates_source("not_a_real_source")


async def test_fixture_path_makes_no_network_call(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    import httpx

    def explode(*args, **kwargs):  # pragma: no cover - only fires on a real call
        raise AssertionError("reference-rate ingestion attempted a network call")

    monkeypatch.setattr(httpx.AsyncClient, "get", explode)
    monkeypatch.setattr(httpx.AsyncClient, "post", explode)
    result = await rates_service.ingest_reference_rates(session, source=_FIXTURE)
    assert result.inserted > 0


# --- US Treasury live adapter: parser (pure, offline) ------------------------


def test_treasury_parser_maps_tenors_dates_and_decimals() -> None:
    rows = parse_treasury_par_yield_xml(_SAMPLE_TREASURY_XML)
    assert len(rows) == _SAMPLE_TREASURY_ROWS
    # Every row is the one normalised USD par-yield series, official provenance.
    assert {r.currency for r in rows} == {"USD"}
    assert {r.country_or_region for r in rows} == {"united_states"}
    assert {r.rate_family for r in rows} == {"treasury_par_yield"}
    assert {r.rate_name for r in rows} == {"US_TREASURY_PAR_YIELD"}
    assert {r.unit for r in rows} == {"percent"}
    assert {r.status for r in rows} == {"official"}
    assert {r.source for r in rows} == {"us_treasury_rates"}
    # Tenor label/months mapping.
    tenors = {(r.tenor, r.tenor_months) for r in rows}
    assert {("1M", 1), ("3M", 3), ("10Y", 120), ("30Y", 360)} <= tenors
    # Date parsing (ISO datetime -> date).
    assert {r.rate_date for r in rows} == {date(2026, 1, 2), date(2026, 1, 3)}
    # Decimal parsing (numeric value preserved).
    day1_1m = next(r for r in rows if r.rate_date == date(2026, 1, 2) and r.tenor == "1M")
    assert day1_1m.rate_value == Decimal("5.40")


def test_treasury_parser_skips_missing_and_isolates_bad_cells() -> None:
    rows = parse_treasury_par_yield_xml(_SAMPLE_TREASURY_XML)
    day2 = {r.tenor for r in rows if r.rate_date == date(2026, 1, 3)}
    # BC_6MONTH was empty (missing cell) and BC_20YEAR was "N/A" (bad numeric):
    # both are dropped, the rest of the row survives.
    assert "6M" not in day2
    assert "20Y" not in day2
    assert day2 == {"1M", "3M", "10Y", "30Y"}
    # The 6-week BC_1_5MONTH and the duplicate BC_30YEARDISPLAY are never mapped.
    assert all(r.tenor not in {"1.5M", "6W"} for r in rows)


def test_treasury_parser_tolerates_empty_or_junk_feed() -> None:
    assert parse_treasury_par_yield_xml("<feed></feed>") == []


# --- US Treasury live adapter: ingestion (mocked HTTP, no live calls) --------


async def test_treasury_ingest_inserts(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_treasury_http(monkeypatch)
    result = await rates_service.ingest_reference_rates(
        session,
        source=_TREASURY,
        start_date=date(2026, 1, 1),
        end_date=date(2026, 1, 31),
    )
    assert result.is_fixture is False
    assert result.inserted == _SAMPLE_TREASURY_ROWS
    assert result.failed == 0
    count = await session.scalar(select(func.count()).select_from(ReferenceRate))
    assert count == _SAMPLE_TREASURY_ROWS


async def test_treasury_ingest_uses_guarded_fetch_log(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_treasury_http(monkeypatch)
    await rates_service.ingest_reference_rates(
        session, source=_TREASURY, start_date=date(2026, 1, 1), end_date=date(2026, 1, 31)
    )
    logs = await source_requests.list_fetch_logs(session, source=_TREASURY, status="success")
    assert logs
    log = logs[0]
    assert log.request_kind == "fetch_treasury_par_yields"
    # The fetch log carries a host/path class + safe params only — never secrets.
    assert log.endpoint_label and "?" not in log.endpoint_label
    assert "APIKEY" not in (log.request_key or "").upper()
    assert log.raw_payload_hash  # provenance hash, not the payload


async def test_treasury_ingest_idempotent_and_cached(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: dict = {}
    _patch_treasury_http(monkeypatch, calls=calls)
    first = await rates_service.ingest_reference_rates(
        session, source=_TREASURY, start_date=date(2026, 1, 1), end_date=date(2026, 1, 31)
    )
    second = await rates_service.ingest_reference_rates(
        session, source=_TREASURY, start_date=date(2026, 1, 1), end_date=date(2026, 1, 31)
    )
    # The second run is served from the recent-success cache: no extra HTTP call,
    # nothing re-selected, and (the real guarantee) no duplicate rows.
    assert calls["n"] == 1
    assert second.selected == 0
    count = await session.scalar(select(func.count()).select_from(ReferenceRate))
    assert count == first.inserted == _SAMPLE_TREASURY_ROWS


async def test_treasury_ingest_currency_filter(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: dict = {}
    _patch_treasury_http(monkeypatch, calls=calls)
    # A non-USD currency is a clean no-op for this provider — no fetch attempted.
    eur = await rates_service.ingest_reference_rates(session, source=_TREASURY, currency="EUR")
    assert eur.selected == 0
    assert calls.get("n", 0) == 0
    usd = await rates_service.ingest_reference_rates(
        session,
        source=_TREASURY,
        currency="USD",
        start_date=date(2026, 1, 1),
        end_date=date(2026, 1, 31),
    )
    assert usd.inserted == _SAMPLE_TREASURY_ROWS


async def test_treasury_ingest_date_range_filter(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_treasury_http(monkeypatch)
    # Narrow to the second day only — the first day's observations are filtered out.
    result = await rates_service.ingest_reference_rates(
        session, source=_TREASURY, start_date=date(2026, 1, 3), end_date=date(2026, 1, 3)
    )
    assert result.inserted == 4
    dates = set((await session.execute(select(ReferenceRate.rate_date).distinct())).scalars())
    assert dates == {date(2026, 1, 3)}


async def test_treasury_ingest_limit(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_treasury_http(monkeypatch)
    result = await rates_service.ingest_reference_rates(
        session,
        source=_TREASURY,
        start_date=date(2026, 1, 1),
        end_date=date(2026, 1, 31),
        limit=5,
    )
    assert result.selected == 5
    assert result.inserted == 5


async def test_treasury_budget_block_makes_no_call(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    import httpx

    # The default budget for us_treasury_rates is seeded; put it in backoff so
    # guarded_fetch blocks BEFORE any fetch is attempted.
    await source_budget.apply_backoff(session, _TREASURY, seconds=120)
    await session.commit()

    async def boom(*args, **kwargs):  # pragma: no cover - must never run in backoff
        raise AssertionError("a live Treasury call was attempted while in backoff")

    monkeypatch.setattr(httpx.AsyncClient, "get", boom)
    result = await rates_service.ingest_reference_rates(
        session, source=_TREASURY, start_date=date(2026, 1, 1), end_date=date(2026, 1, 31)
    )
    assert result.inserted == 0
    rate_limited = await source_requests.list_fetch_logs(
        session, source=_TREASURY, status="rate_limited"
    )
    assert rate_limited


# --- US Treasury live adapter: worker + API ----------------------------------


async def test_treasury_worker_records_job_run(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_treasury_http(monkeypatch)
    run = await run_job(
        session,
        "rates_ingestion",
        source_name=_TREASURY,
        start_date=date(2026, 1, 1),
        end_date=date(2026, 1, 31),
    )
    assert run.status == "success"
    assert run.source == _TREASURY
    assert run.records_inserted == _SAMPLE_TREASURY_ROWS


async def test_treasury_latest_after_ingestion(
    client: AsyncClient, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_treasury_http(monkeypatch)
    await rates_service.ingest_reference_rates(
        session, source=_TREASURY, start_date=date(2026, 1, 1), end_date=date(2026, 1, 31)
    )
    await session.commit()
    body = (await client.get("/api/v1/rates/latest?currency=USD&source=us_treasury_rates")).json()
    assert body["meta"]["count"] > 0
    assert {r["rate_name"] for r in body["data"]} == {"US_TREASURY_PAR_YIELD"}
    assert all(r["source"] == "us_treasury_rates" for r in body["data"])
    # latest is per-series (one row per tenor): seven distinct tenors published.
    by_tenor = {r["tenor"]: r for r in body["data"]}
    assert set(by_tenor) == {"1M", "3M", "6M", "1Y", "2Y", "10Y", "30Y"}
    # Tenors published on both days take the most recent (2026-01-03) observation;
    # tenors only on day one keep their (older) day-one row — no gap-filling.
    assert by_tenor["10Y"]["rate_date"] == "2026-01-03"
    assert by_tenor["6M"]["rate_date"] == "2026-01-02"


def test_treasury_requires_explicit_selection() -> None:
    # The default stays the offline fixture; the live adapter is explicit-only.
    assert get_rates_source().name == _FIXTURE
    assert get_rates_source(_TREASURY).name == _TREASURY
    assert isinstance(get_rates_source(_TREASURY), TreasuryRatesSource)


def test_treasury_source_metadata() -> None:
    source = TreasuryRatesSource()
    assert source.is_fixture is False
    assert source.requires_live_fetch is True
    assert source.supported_currencies == ("USD",)
    assert source.supported_rate_families == ("treasury_par_yield",)


async def test_treasury_direct_call_requires_session() -> None:
    # The live adapter refuses to run without the budget/fetch-log session, so a
    # bare fetch_rates() can never make a surprise unguarded call.
    with pytest.raises(RuntimeError, match="requires a database session"):
        await TreasuryRatesSource().fetch_rates(currency="USD")


def test_treasury_budget_is_conservative() -> None:
    specs = source_budget.default_budget_specs()
    assert specs[_TREASURY]["max_requests_per_minute"] <= 10
    assert specs[_TREASURY]["min_delay_ms"] >= 1000


async def test_treasury_planned_siblings_still_fail_cleanly(session: AsyncSession) -> None:
    # BoE remains planned: a surprise live call is impossible, the run fails clean.
    run = await run_job(session, "rates_ingestion", source_name="boe_rates")
    assert run.status == "failed"
    assert "planned" in run.message.lower()
    count = await session.scalar(select(func.count()).select_from(ReferenceRate))
    assert count == 0


# --- ECB live adapter: static sample payloads (official-shaped SDMX csvdata) --
#
# Two faithful slices of the ECB Data Portal SDMX ``format=csvdata`` feed. The FM
# (key interest rates) and EST (€STR) dataflows use a DIFFERENT column order — note
# TIME_PERIOD/OBS_VALUE sit at different positions — so a correct parser must read
# columns by NAME, not position. Policy rates are change-date observations (one row
# per change); €STR is daily. Edge cases included: an empty OBS_VALUE cell (missing),
# a non-numeric cell (bad), and an unknown series KEY (must be ignored).
_SAMPLE_ECB_FM_CSV = (
    "KEY,FREQ,REF_AREA,CURRENCY,PROVIDER_FM,INSTRUMENT_FM,PROVIDER_FM_ID,"
    "DATA_TYPE_FM,TIME_PERIOD,OBS_VALUE,OBS_STATUS,TITLE\n"
    "FM.B.U2.EUR.4F.KR.MRR_FR.LEV,B,U2,EUR,4F,KR,MRR_FR,LEV,2025-06-11,2.15,A,Main refi\n"
    "FM.B.U2.EUR.4F.KR.DFR.LEV,B,U2,EUR,4F,KR,DFR,LEV,2025-06-11,2,A,Deposit facility\n"
    "FM.B.U2.EUR.4F.KR.MLFR.LEV,B,U2,EUR,4F,KR,MLFR,LEV,2025-06-11,2.4,A,Marginal lending\n"
    "FM.B.U2.EUR.4F.KR.DFR.LEV,B,U2,EUR,4F,KR,DFR,LEV,2026-03-12,2.5,A,Deposit facility\n"
    "FM.B.U2.EUR.4F.KR.DFR.LEV,B,U2,EUR,4F,KR,DFR,LEV,2026-04-17,,A,Deposit facility\n"
    "FM.B.U2.EUR.4F.KR.MLFR.LEV,B,U2,EUR,4F,KR,MLFR,LEV,2026-04-17,N/A,A,Marginal lending\n"
    "FM.B.U2.EUR.4F.KR.UNKNOWN.LEV,B,U2,EUR,4F,KR,UNKNOWN,LEV,2026-04-17,9.99,A,Unknown\n"
)
# FM valid rows: MRR_FR@2025-06-11, DFR@2025-06-11, MLFR@2025-06-11, DFR@2026-03-12.
_SAMPLE_ECB_FM_ROWS = 4

_SAMPLE_ECB_EST_CSV = (
    "KEY,FREQ,BENCHMARK_ITEM,DATA_TYPE_EST,TIME_PERIOD,OBS_VALUE,OBS_STATUS,TITLE\n"
    "EST.B.EU000A2X2A25.WT,B,EU000A2X2A25,WT,2026-06-22,1.9,A,Euro short-term rate\n"
    "EST.B.EU000A2X2A25.WT,B,EU000A2X2A25,WT,2026-06-23,1.92,A,Euro short-term rate\n"
    "EST.B.EU000A2X2A25.WT,B,EU000A2X2A25,WT,2026-06-24,,A,Euro short-term rate\n"
)
# EST valid rows: 2026-06-22, 2026-06-23 (2026-06-24 has an empty value -> skipped).
_SAMPLE_ECB_EST_ROWS = 2

_ECB = "ecb_rates"


def _patch_ecb_http(
    monkeypatch: pytest.MonkeyPatch,
    *,
    fm: str = _SAMPLE_ECB_FM_CSV,
    est: str = _SAMPLE_ECB_EST_CSV,
    calls: dict | None = None,
) -> None:
    """Stub the ECB adapter's per-dataflow HTTP fetch so it parses fixture CSV.

    Patches only ``ECBRatesSource._fetch_flow_csv`` (the single place httpx is used),
    so the call still flows through ``guarded_fetch`` (cache/budget/fetch log) while
    never touching the network. The fake returns the FM or EST sample by dataflow.
    """

    async def fake_fetch_flow(self, flow, series_key, start, end):  # noqa: ANN001
        if calls is not None:
            calls["n"] = calls.get("n", 0) + 1
            calls.setdefault("flows", []).append(flow)
        return fm if flow == "FM" else est

    monkeypatch.setattr(ECBRatesSource, "_fetch_flow_csv", fake_fetch_flow)


async def _disable_ecb_min_delay(session: AsyncSession) -> None:
    """Drop the seeded ecb_rates min-delay so a both-dataflow run needs no real sleep.

    The conservative min_delay (asserted elsewhere) would otherwise space the second
    dataflow's guarded_fetch by 1s; tests that fetch BOTH flows zero it for speed.
    """
    budget = await source_budget.get_budget(session, _ECB)
    assert budget is not None
    budget.min_delay_ms = 0
    await session.commit()


# --- ECB live adapter: parser (pure, offline) --------------------------------


def test_ecb_parser_maps_policy_rates() -> None:
    rows = parse_ecb_sdmx_csv(_SAMPLE_ECB_FM_CSV)
    assert len(rows) == _SAMPLE_ECB_FM_ROWS
    # Every row is an official EUR euro-area observation from ecb_rates.
    assert {r.currency for r in rows} == {"EUR"}
    assert {r.country_or_region for r in rows} == {"euro_area"}
    assert {r.unit for r in rows} == {"percent"}
    assert {r.status for r in rows} == {"official"}
    assert {r.source for r in rows} == {"ecb_rates"}
    # Policy/facility rates carry no tenor.
    assert all(r.tenor is None for r in rows)
    # rate_name -> rate_family mapping matches the fixture vocabulary.
    families = {(r.rate_name, r.rate_family) for r in rows}
    assert ("ECB_MAIN_REFINANCING_RATE", "policy_rate") in families
    assert ("ECB_DEPOSIT_FACILITY_RATE", "deposit_facility") in families
    assert ("ECB_MARGINAL_LENDING_RATE", "lending_facility") in families
    # Decimal parsing (integer "2" -> Decimal 2) + change-date observations preserved.
    dfr = sorted(
        (r for r in rows if r.rate_name == "ECB_DEPOSIT_FACILITY_RATE"),
        key=lambda r: r.rate_date,
    )
    assert [r.rate_date for r in dfr] == [date(2025, 6, 11), date(2026, 3, 12)]
    assert dfr[0].rate_value == Decimal("2")
    assert dfr[1].rate_value == Decimal("2.5")
    # Provenance URL points at the official ECB Data Portal series page.
    assert all("data.ecb.europa.eu" in (r.source_url or "") for r in rows)


def test_ecb_parser_maps_estr() -> None:
    rows = parse_ecb_sdmx_csv(_SAMPLE_ECB_EST_CSV)
    assert len(rows) == _SAMPLE_ECB_EST_ROWS
    assert {r.rate_name for r in rows} == {"ESTR"}
    assert {r.rate_family for r in rows} == {"overnight_rate"}
    assert {r.currency for r in rows} == {"EUR"}
    assert all(r.tenor is None for r in rows)
    # Daily series; Decimal parsing preserves the published value.
    first = next(r for r in rows if r.rate_date == date(2026, 6, 22))
    assert first.rate_value == Decimal("1.9")
    assert {r.rate_date for r in rows} == {date(2026, 6, 22), date(2026, 6, 23)}


def test_ecb_parser_skips_missing_and_isolates_bad() -> None:
    rows = parse_ecb_sdmx_csv(_SAMPLE_ECB_FM_CSV)
    # 2026-04-17 carried an empty DFR (missing) + an "N/A" MLFR (bad numeric) + an
    # UNKNOWN series: all are dropped, so that date contributes nothing.
    assert date(2026, 4, 17) not in {r.rate_date for r in rows}
    # The unknown series KEY is never mapped to a rate_name.
    assert all("UNKNOWN" not in r.rate_name for r in rows)


def test_ecb_parser_tolerates_empty_or_headers_only() -> None:
    assert parse_ecb_sdmx_csv("") == []
    assert parse_ecb_sdmx_csv("KEY,TIME_PERIOD,OBS_VALUE\n") == []


# --- ECB live adapter: ingestion (mocked HTTP, no live calls) ----------------


async def test_ecb_ingest_estr_inserts(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_ecb_http(monkeypatch)
    # Target the overnight family -> only the EST dataflow (a single request).
    result = await rates_service.ingest_reference_rates(
        session, source=_ECB, rate_family="overnight_rate"
    )
    assert result.is_fixture is False
    assert result.inserted == _SAMPLE_ECB_EST_ROWS
    assert result.failed == 0
    names = set((await session.execute(select(ReferenceRate.rate_name).distinct())).scalars())
    assert names == {"ESTR"}


async def test_ecb_ingest_collects_policy_and_estr(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_ecb_http(monkeypatch)
    await _disable_ecb_min_delay(session)
    # No family filter -> both dataflows (FM key rates + EST €STR) in one run.
    result = await rates_service.ingest_reference_rates(session, source=_ECB)
    assert result.inserted == _SAMPLE_ECB_FM_ROWS + _SAMPLE_ECB_EST_ROWS
    assert result.failed == 0
    names = set((await session.execute(select(ReferenceRate.rate_name).distinct())).scalars())
    assert names == {
        "ECB_MAIN_REFINANCING_RATE",
        "ECB_DEPOSIT_FACILITY_RATE",
        "ECB_MARGINAL_LENDING_RATE",
        "ESTR",
    }


async def test_ecb_ingest_uses_guarded_fetch_log(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_ecb_http(monkeypatch)
    await rates_service.ingest_reference_rates(session, source=_ECB, rate_family="overnight_rate")
    logs = await source_requests.list_fetch_logs(session, source=_ECB, status="success")
    assert logs
    log = logs[0]
    assert log.request_kind == "fetch_ecb_rates"
    # The fetch log carries a host/path class + safe params only — never secrets.
    assert log.endpoint_label and "?" not in log.endpoint_label
    assert "data-api.ecb.europa.eu" in log.endpoint_label
    assert log.raw_payload_hash  # provenance hash, not the payload


async def test_ecb_ingest_idempotent_and_cached(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: dict = {}
    _patch_ecb_http(monkeypatch, calls=calls)
    first = await rates_service.ingest_reference_rates(
        session, source=_ECB, rate_family="overnight_rate"
    )
    second = await rates_service.ingest_reference_rates(
        session, source=_ECB, rate_family="overnight_rate"
    )
    # The second run is served from the recent-success cache: no extra HTTP call,
    # nothing re-selected, and (the real guarantee) no duplicate rows.
    assert calls["n"] == 1
    assert second.selected == 0
    count = await session.scalar(select(func.count()).select_from(ReferenceRate))
    assert count == first.inserted == _SAMPLE_ECB_EST_ROWS


async def test_ecb_ingest_currency_filter(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: dict = {}
    _patch_ecb_http(monkeypatch, calls=calls)
    # A non-EUR currency is a clean no-op for this provider — no fetch attempted.
    usd = await rates_service.ingest_reference_rates(session, source=_ECB, currency="USD")
    assert usd.selected == 0
    assert calls.get("n", 0) == 0
    eur = await rates_service.ingest_reference_rates(
        session, source=_ECB, currency="EUR", rate_family="overnight_rate"
    )
    assert eur.inserted == _SAMPLE_ECB_EST_ROWS


async def test_ecb_ingest_date_range_filter(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_ecb_http(monkeypatch)
    # Narrow to 2026-06-23 only — the 2026-06-22 €STR observation is filtered out.
    result = await rates_service.ingest_reference_rates(
        session,
        source=_ECB,
        rate_family="overnight_rate",
        start_date=date(2026, 6, 23),
        end_date=date(2026, 6, 23),
    )
    assert result.inserted == 1
    dates = set((await session.execute(select(ReferenceRate.rate_date).distinct())).scalars())
    assert dates == {date(2026, 6, 23)}


async def test_ecb_budget_block_makes_no_call(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    import httpx

    # The default budget for ecb_rates is seeded; put it in backoff so guarded_fetch
    # blocks BEFORE any fetch is attempted.
    await source_budget.apply_backoff(session, _ECB, seconds=120)
    await session.commit()

    async def boom(*args, **kwargs):  # pragma: no cover - must never run in backoff
        raise AssertionError("a live ECB call was attempted while in backoff")

    monkeypatch.setattr(httpx.AsyncClient, "get", boom)
    result = await rates_service.ingest_reference_rates(
        session, source=_ECB, rate_family="overnight_rate"
    )
    assert result.inserted == 0
    rate_limited = await source_requests.list_fetch_logs(
        session, source=_ECB, status="rate_limited"
    )
    assert rate_limited


# --- ECB live adapter: worker + API ------------------------------------------


async def test_ecb_worker_records_job_run(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_ecb_http(monkeypatch)
    run = await run_job(session, "rates_ingestion", source_name=_ECB, rate_family="overnight_rate")
    assert run.status == "success"
    assert run.source == _ECB
    assert run.records_inserted == _SAMPLE_ECB_EST_ROWS


async def test_ecb_latest_after_ingestion(
    client: AsyncClient, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_ecb_http(monkeypatch)
    await _disable_ecb_min_delay(session)
    await rates_service.ingest_reference_rates(session, source=_ECB)
    await session.commit()
    body = (await client.get("/api/v1/rates/latest?currency=EUR&source=ecb_rates")).json()
    by_name = {r["rate_name"]: r for r in body["data"]}
    assert set(by_name) == {
        "ECB_MAIN_REFINANCING_RATE",
        "ECB_DEPOSIT_FACILITY_RATE",
        "ECB_MARGINAL_LENDING_RATE",
        "ESTR",
    }
    assert all(r["source"] == "ecb_rates" for r in by_name.values())
    # latest-per-series takes the most recent deposit-facility change (no forward-fill).
    assert by_name["ECB_DEPOSIT_FACILITY_RATE"]["rate_date"] == "2026-03-12"


async def test_ecb_time_series_estr(
    client: AsyncClient, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_ecb_http(monkeypatch)
    await rates_service.ingest_reference_rates(session, source=_ECB, rate_family="overnight_rate")
    await session.commit()
    body = (
        await client.get("/api/v1/rates/time-series?rate_name=ESTR&currency=EUR&source=ecb_rates")
    ).json()
    assert body["rate_name"] == "ESTR"
    assert body["currency"] == "EUR"
    assert body["points"] and body["points"][0]["rate_value"]
    dates = [p["rate_date"] for p in body["points"]]
    assert dates == sorted(dates)  # oldest-first


def test_ecb_requires_explicit_selection() -> None:
    # The default stays the offline fixture; the live ECB adapter is explicit-only.
    assert get_rates_source().name == _FIXTURE
    assert isinstance(get_rates_source(_ECB), ECBRatesSource)


def test_ecb_source_metadata() -> None:
    source = ECBRatesSource()
    assert source.is_fixture is False
    assert source.requires_live_fetch is True
    assert source.supported_currencies == ("EUR",)
    assert "overnight_rate" in source.supported_rate_families
    assert "policy_rate" in source.supported_rate_families


async def test_ecb_direct_call_requires_session() -> None:
    # The live adapter refuses to run without the budget/fetch-log session, so a bare
    # fetch_rates() can never make a surprise unguarded call.
    with pytest.raises(RuntimeError, match="requires a database session"):
        await ECBRatesSource().fetch_rates(currency="EUR")


def test_ecb_budget_is_conservative() -> None:
    specs = source_budget.default_budget_specs()
    assert specs[_ECB]["max_requests_per_minute"] <= 10
    assert specs[_ECB]["min_delay_ms"] >= 1000
