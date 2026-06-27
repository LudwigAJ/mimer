"""Constituent EOD price ingestion — model, fixture, ingestion, planner, API.

All offline: the fixture provider never touches the network, and the live
Stooq/yfinance path is exercised with a mocked HTTP adapter + the real source
budget / fetch-log / request-cache plumbing. No test may make a live call.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    Fund,
    Instrument,
    InstrumentListing,
    InstrumentPrice,
    ScheduledJob,
    Workspace,
)
from app.services import instrument_prices as price_service
from app.services import market_data_planner, source_budget, source_requests
from app.sources.instrument_prices import (
    NO_DATA,
    OK,
    FixtureInstrumentPriceSource,
    InstrumentPriceFetchResult,
    InstrumentPriceRecord,
    InstrumentPriceRequest,
    get_instrument_price_source,
)
from app.workers.run import run_job

_VUSA = "IE00B3XXRP09"
_FIXTURE = "instrument_price_fixture"


async def _fund_id(session: AsyncSession, isin: str = _VUSA) -> int:
    return await session.scalar(select(Fund.id).where(Fund.isin == isin))


async def _workspace_id(session: AsyncSession) -> int:
    return (await session.execute(select(Workspace.id))).scalars().first()


async def _resolve_identities(session: AsyncSession, *, fund_id=None, workspace_id=None) -> None:
    await run_job(
        session,
        "constituent_identity_resolution",
        fund_id=fund_id,
        workspace_id=workspace_id,
        source_name="constituent_identity_fixture",
    )


async def _a_resolved_listing(session: AsyncSession) -> InstrumentListing:
    listing = await session.scalar(select(InstrumentListing).order_by(InstrumentListing.id))
    assert listing is not None
    return listing


# --- fixture provider --------------------------------------------------------


async def test_fixture_provider_is_deterministic(session: AsyncSession) -> None:
    src = FixtureInstrumentPriceSource()
    req = [
        InstrumentPriceRequest(instrument_listing_id=1, ticker="AAPL", mic="XNAS", currency="USD")
    ]
    a = await src.fetch_eod_prices(session, req)
    b = await src.fetch_eod_prices(session, req)
    assert a.outcomes[1] == OK
    assert len(a.records) >= 30  # a usable chart window
    assert [(r.price_date, r.close) for r in a.records] == [
        (r.price_date, r.close) for r in b.records
    ]
    # OHLC is coherent: low <= close <= high.
    for r in a.records:
        assert r.low <= r.close <= r.high
        assert r.currency == "USD"


async def test_fixture_provider_handles_multiple_and_missing(session: AsyncSession) -> None:
    src = FixtureInstrumentPriceSource()
    reqs = [
        InstrumentPriceRequest(instrument_listing_id=1, ticker="AAPL", currency="USD"),
        InstrumentPriceRequest(instrument_listing_id=2, ticker="SHEL", currency="GBP"),
        InstrumentPriceRequest(instrument_listing_id=3, ticker="ZZZZ", currency="USD"),
    ]
    out = await src.fetch_eod_prices(session, reqs)
    assert out.outcomes[1] == OK and out.outcomes[2] == OK
    assert out.outcomes[3] == NO_DATA  # unknown ticker => no data, not an error
    listing_ids = {r.instrument_listing_id for r in out.records}
    assert listing_ids == {1, 2}


async def test_fixture_provider_respects_date_range(session: AsyncSession) -> None:
    src = FixtureInstrumentPriceSource()
    end = date.today()
    start = end - timedelta(days=9)
    out = await src.fetch_eod_prices(
        session,
        [InstrumentPriceRequest(instrument_listing_id=1, ticker="AAPL", currency="USD")],
        start_date=start,
        end_date=end,
    )
    dates = [r.price_date for r in out.records]
    assert min(dates) >= start and max(dates) == end
    assert len(dates) == 10


async def test_fixture_provider_makes_no_network_call(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    import httpx

    def explode(*args, **kwargs):  # pragma: no cover - only fires on a real call
        raise AssertionError("instrument price fixture attempted a network call")

    monkeypatch.setattr(httpx.AsyncClient, "get", explode)
    monkeypatch.setattr(httpx.AsyncClient, "post", explode)
    src = FixtureInstrumentPriceSource()
    out = await src.fetch_eod_prices(
        session, [InstrumentPriceRequest(instrument_listing_id=1, ticker="AAPL")]
    )
    assert out.records


# --- worker: fixture ingestion ----------------------------------------------


async def test_worker_single_listing_ingestion(session: AsyncSession) -> None:
    await _resolve_identities(session, workspace_id=await _workspace_id(session))
    listing = await _a_resolved_listing(session)
    run = await run_job(
        session,
        "constituent_eod_price_ingestion",
        instrument_listing_id=listing.id,
        source_name=_FIXTURE,
    )
    assert run.status == "success"
    assert run.records_inserted >= 30
    assert "listings=1" in run.message
    rows = await session.scalar(
        select(func.count())
        .select_from(InstrumentPrice)
        .where(InstrumentPrice.instrument_listing_id == listing.id)
    )
    assert rows == run.records_inserted
    # last_price_at is bumped so the listing reads as fresh.
    await session.refresh(listing)
    assert listing.last_price_at is not None


async def test_worker_fund_scoped_ingestion(session: AsyncSession) -> None:
    fund_id = await _fund_id(session)
    await _resolve_identities(session, fund_id=fund_id)
    run = await run_job(
        session, "constituent_eod_price_ingestion", fund_id=fund_id, source_name=_FIXTURE
    )
    assert run.status == "success"
    assert run.records_inserted >= 1


async def test_worker_all_scope_dedupes_shared_constituents(session: AsyncSession) -> None:
    await _resolve_identities(session)  # every fund
    run = await run_job(session, "constituent_eod_price_ingestion", source_name=_FIXTURE)
    assert run.status == "success"
    # Apple is held by VUSA *and* JPM => one instrument => one priced listing.
    instruments = await session.scalar(select(func.count()).select_from(Instrument))
    listings_priced = await session.scalar(
        select(func.count(func.distinct(InstrumentPrice.instrument_listing_id)))
    )
    assert listings_priced == instruments  # one primary listing per instrument


async def test_worker_limit_respected(session: AsyncSession) -> None:
    await _resolve_identities(session, workspace_id=await _workspace_id(session))
    await run_job(session, "constituent_eod_price_ingestion", source_name=_FIXTURE, limit=2)
    priced = await session.scalar(
        select(func.count(func.distinct(InstrumentPrice.instrument_listing_id)))
    )
    assert priced <= 2


async def test_worker_idempotent_rerun(session: AsyncSession) -> None:
    fund_id = await _fund_id(session)
    await _resolve_identities(session, fund_id=fund_id)
    await run_job(session, "constituent_eod_price_ingestion", fund_id=fund_id, source_name=_FIXTURE)
    count_1 = await session.scalar(select(func.count()).select_from(InstrumentPrice))
    run2 = await run_job(
        session, "constituent_eod_price_ingestion", fund_id=fund_id, source_name=_FIXTURE
    )
    count_2 = await session.scalar(select(func.count()).select_from(InstrumentPrice))
    assert run2.records_inserted == 0  # no new bars
    assert run2.records_updated == 0  # identical values => no update
    assert count_1 == count_2  # no duplicate rows


async def test_worker_changed_bar_updates_only_changed_row(session: AsyncSession) -> None:
    listing = await _seed_instrument_listing(session, ticker="AAPL", mic="XNAS", currency="USD")
    await run_job(
        session,
        "constituent_eod_price_ingestion",
        instrument_listing_id=listing.id,
        source_name=_FIXTURE,
    )
    # Corrupt a single stored bar; a rerun must fix exactly that one row.
    row = await session.scalar(
        select(InstrumentPrice)
        .where(InstrumentPrice.instrument_listing_id == listing.id)
        .order_by(InstrumentPrice.price_date.desc())
    )
    row.close = Decimal("0.01")
    await session.commit()
    run = await run_job(
        session,
        "constituent_eod_price_ingestion",
        instrument_listing_id=listing.id,
        source_name=_FIXTURE,
    )
    assert run.records_inserted == 0
    assert run.records_updated == 1  # only the corrupted bar


async def test_worker_no_data_counted(session: AsyncSession) -> None:
    listing = await _seed_instrument_listing(session, ticker="ZZZZ", mic="XNAS", currency="USD")
    run = await run_job(
        session,
        "constituent_eod_price_ingestion",
        instrument_listing_id=listing.id,
        source_name=_FIXTURE,
    )
    assert run.records_inserted == 0
    assert "no_data=1" in run.message
    assert run.status == "success"


async def test_worker_no_resolved_listings_is_clean_noop(session: AsyncSession) -> None:
    # No identity resolution run, so nothing is priced.
    run = await run_job(session, "constituent_eod_price_ingestion", source_name=_FIXTURE)
    assert run.status == "success"
    assert run.records_inserted == 0
    assert "No resolved constituent listings" in run.message


async def test_ingestion_isolates_a_bad_bar(session: AsyncSession) -> None:
    listing = await _seed_instrument_listing(session, ticker="AAPL", mic="XNAS", currency="USD")

    class _BadOneSource:
        name = "instrument_price_fixture"  # endswith _fixture => offline, ttl 0

        async def fetch_eod_prices(self, session, requests, **_):
            r = requests[0]
            return InstrumentPriceFetchResult(
                records=[
                    InstrumentPriceRecord(
                        instrument_listing_id=r.instrument_listing_id,
                        price_date=date(2026, 6, 1),
                        close=Decimal("10"),
                        source="instrument_price_fixture",
                    ),
                    InstrumentPriceRecord(
                        instrument_listing_id=r.instrument_listing_id,
                        price_date=date(2026, 6, 2),
                        close=None,  # type: ignore[arg-type]  # forces a per-bar failure
                        source="instrument_price_fixture",
                    ),
                ],
                outcomes={r.instrument_listing_id: OK},
            )

    counts = await price_service.ingest_prices(session, _BadOneSource(), [listing])
    assert counts.inserted == 1  # the good bar landed
    assert counts.failed == 1  # the bad bar was isolated


# --- live (stooq) path: budget + cache, mocked HTTP --------------------------


async def test_live_budget_blocked_makes_no_call(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.sources.stooq import StooqSource

    listing = await _seed_instrument_listing(session, ticker="AAPL", mic="XNAS", currency="USD")
    await source_budget.apply_backoff(session, "stooq", seconds=120)
    await session.commit()

    async def boom(*args, **kwargs):  # pragma: no cover - must never run in backoff
        raise AssertionError("a live Stooq call was attempted while in backoff")

    monkeypatch.setattr(StooqSource, "fetch", boom)
    run = await run_job(
        session,
        "constituent_eod_price_ingestion",
        instrument_listing_id=listing.id,
        source_name="stooq",
    )
    assert "skipped_budget=1" in run.message
    assert run.records_inserted == 0
    rate_limited = await source_requests.list_fetch_logs(
        session, source="stooq", status="rate_limited"
    )
    assert rate_limited


async def test_live_cache_skips_repeat(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.sources.base import PricePoint
    from app.sources.stooq import StooqSource

    listing = await _seed_instrument_listing(session, ticker="AAPL", mic="XNAS", currency="USD")
    calls = {"n": 0}

    async def counting_fetch(self, *, ticker, exchange=None, currency=None):
        calls["n"] += 1
        return [PricePoint(date(2026, 6, 3), Decimal("190.00"), currency)]

    monkeypatch.setattr(StooqSource, "fetch", counting_fetch)
    run1 = await run_job(
        session,
        "constituent_eod_price_ingestion",
        instrument_listing_id=listing.id,
        source_name="stooq",
    )
    assert run1.records_inserted == 1
    run2 = await run_job(
        session,
        "constituent_eod_price_ingestion",
        instrument_listing_id=listing.id,
        source_name="stooq",
    )
    assert calls["n"] == 1  # second run served from the recent-success cache
    assert "skipped_cached=1" in run2.message
    # The fetch log carries no secrets.
    logs = await source_requests.list_fetch_logs(session, source="stooq", status="success")
    assert logs and "APIKEY" not in logs[0].request_key.upper()
    assert logs[0].request_kind == "fetch_eod_prices"


def test_live_budgets_are_conservative() -> None:
    specs = source_budget.default_budget_specs()
    for name in ("stooq", "yfinance"):
        assert specs[name]["max_requests_per_minute"] <= 30
    # The offline fixture is permissive (no live network to protect).
    assert specs[_FIXTURE]["max_requests_per_minute"] is None


def test_live_source_requires_explicit_selection() -> None:
    # Default is the offline fixture; live providers only when explicitly named.
    assert get_instrument_price_source().name == _FIXTURE
    assert get_instrument_price_source("stooq").name == "stooq"
    assert get_instrument_price_source("yfinance").name == "yfinance"


# --- planner integration -----------------------------------------------------


async def test_planner_missing_then_fresh_after_ingestion(session: AsyncSession) -> None:
    wid = await _workspace_id(session)
    await _resolve_identities(session, workspace_id=wid)

    before = await market_data_planner.build_plan(session, wid, include_constituents=True)
    assert before.summary.constituent_prices_missing >= 1
    assert before.summary.constituent_prices_fresh == 0
    assert before.summary.constituents_ready_for_eod_prices == (
        before.summary.constituent_prices_missing + before.summary.constituent_prices_stale
    )
    assert any(i.item_type == "fetch_constituent_price" for i in before.items)

    await run_job(
        session, "constituent_eod_price_ingestion", workspace_id=wid, source_name=_FIXTURE
    )
    after = await market_data_planner.build_plan(session, wid, include_constituents=True)
    assert after.summary.constituent_prices_fresh >= 1
    assert after.summary.constituent_prices_missing == 0
    assert after.summary.constituents_ready_for_eod_prices == 0
    assert not any(i.item_type == "fetch_constituent_price" for i in after.items)


async def test_planner_no_price_items_for_unresolved(session: AsyncSession) -> None:
    wid = await _workspace_id(session)
    # No resolution run: every constituent is unresolved => no price work planned.
    plan = await market_data_planner.build_plan(session, wid, include_constituents=True)
    assert plan.summary.constituents_ready_for_eod_prices == 0
    assert not any(i.item_type == "fetch_constituent_price" for i in plan.items)
    assert plan.summary.unresolved_constituents >= 1


# --- API endpoints -----------------------------------------------------------


async def test_constituents_include_prices(client: AsyncClient, session: AsyncSession) -> None:
    fund_id = await _fund_id(session)
    await _resolve_identities(session, fund_id=fund_id)
    await run_job(session, "constituent_eod_price_ingestion", fund_id=fund_id, source_name=_FIXTURE)

    body = (
        await client.get(
            f"/api/v1/funds/{fund_id}/constituents?status=resolved&include_prices=true"
        )
    ).json()
    assert body["constituents"]
    priced = [c for c in body["constituents"] if c["latest_price"]]
    assert priced
    lp = priced[0]["latest_price"]
    assert lp["close"] and lp["price_date"] and lp["currency"]
    assert lp["source"] == _FIXTURE
    assert lp["freshness"] == "fresh"
    assert lp["instrument_listing_id"]


async def test_instrument_listing_prices_and_timeseries(
    client: AsyncClient, session: AsyncSession
) -> None:
    fund_id = await _fund_id(session)
    await _resolve_identities(session, fund_id=fund_id)
    await run_job(session, "constituent_eod_price_ingestion", fund_id=fund_id, source_name=_FIXTURE)
    listing = await _a_resolved_listing(session)

    prices = (await client.get(f"/api/v1/instrument-listings/{listing.id}/prices")).json()
    assert prices["meta"]["count"] >= 1
    assert prices["data"][0]["close"]

    series = (
        await client.get(
            f"/api/v1/instrument-listings/{listing.id}/time-series?kind=price&range=1y"
        )
    ).json()
    assert series["subject"]["type"] == "instrument_listing"
    assert series["kind"] == "price"
    assert series["points"]
    assert series["points"][0]["value"]

    # Instrument-level convenience endpoints (primary listing).
    instrument_id = listing.instrument_id
    inst_prices = (await client.get(f"/api/v1/instruments/{instrument_id}/prices")).json()
    assert inst_prices["meta"]["count"] >= 1
    inst_series = (
        await client.get(f"/api/v1/instruments/{instrument_id}/time-series?kind=price")
    ).json()
    assert inst_series["subject"]["type"] == "instrument"
    assert inst_series["points"]


async def test_instrument_listing_prices_404(client: AsyncClient) -> None:
    resp = await client.get("/api/v1/instrument-listings/999999/prices")
    assert resp.status_code == 404


async def test_market_data_plan_endpoint_reflects_prices(
    client: AsyncClient, session: AsyncSession
) -> None:
    wid = await _workspace_id(session)
    await _resolve_identities(session, workspace_id=wid)
    await run_job(
        session, "constituent_eod_price_ingestion", workspace_id=wid, source_name=_FIXTURE
    )
    plan = (
        await client.get(f"/api/v1/workspaces/{wid}/market-data-plan?include_constituents=true")
    ).json()
    assert plan["summary"]["constituent_prices_fresh"] >= 1
    assert plan["summary"]["constituent_prices_missing"] == 0


async def test_diagnostics_expose_constituent_price_coverage(
    client: AsyncClient, session: AsyncSession
) -> None:
    wid = await _workspace_id(session)
    await _resolve_identities(session, workspace_id=wid)
    await run_job(
        session, "constituent_eod_price_ingestion", workspace_id=wid, source_name=_FIXTURE
    )
    diag = (await client.get(f"/api/v1/workspaces/{wid}/diagnostics")).json()
    assert diag["missing_constituent_prices"] == 0
    assert "stale_constituent_prices" in diag
    assert "constituent_price_ingestion_failures" in diag
    assert "budget_blocked_constituent_price_fetches" in diag
    assert diag["constituent_price_coverage"] == "1.0000"


async def test_source_budget_and_fetch_logs_visible(
    client: AsyncClient, session: AsyncSession
) -> None:
    budgets = (await client.get("/api/v1/source-budgets")).json()["data"]
    names = {b["source_name"] for b in budgets}
    assert {_FIXTURE, "stooq", "yfinance"} <= names


# --- job trigger / scheduler compatibility ----------------------------------


async def test_job_trigger_runs_constituent_price_ingestion(
    client: AsyncClient, session: AsyncSession
) -> None:
    await _resolve_identities(session)
    session.add(
        ScheduledJob(
            name="daily_constituent_eod_price_ingestion",
            job_type="constituent_eod_price_ingestion",
            schedule_kind="daily",
            is_active=True,
        )
    )
    await session.commit()
    job = next(
        j
        for j in (await client.get("/api/v1/jobs")).json()["data"]
        if j["job_type"] == "constituent_eod_price_ingestion"
    )
    assert job["implementation_status"] == "fixture"
    assert job["configured_source"] == _FIXTURE

    run = await client.post(f"/api/v1/jobs/{job['id']}/run")
    assert run.status_code == 201
    body = run.json()
    assert body["status"] == "success"
    assert body["records_inserted"] >= 1


# --- helpers -----------------------------------------------------------------


async def _seed_instrument_listing(
    session: AsyncSession, *, ticker: str, mic: str, currency: str
) -> InstrumentListing:
    """A standalone resolved instrument + listing (no holdings needed)."""
    instrument = Instrument(
        identity_key=f"ticker:{ticker}",
        instrument_type="equity",
        name=f"{ticker} Test",
        status="active",
        source="manual",
    )
    session.add(instrument)
    await session.flush()
    listing = InstrumentListing(
        instrument_id=instrument.id,
        listing_key=f"ticker:{ticker}|{mic}",
        ticker=ticker,
        mic=mic,
        currency=currency,
        source="manual",
        status="active",
    )
    session.add(listing)
    await session.commit()
    return listing
