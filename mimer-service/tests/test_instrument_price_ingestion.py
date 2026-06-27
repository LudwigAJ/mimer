"""Unified instrument EOD price ingestion — selection, worker, planner, API.

Covers the slice that lets a single worker/service price *any* resolved
``instrument_listing`` — ETF/fund constituents *and* directly-held imported broker
holdings — through the one ``instrument_prices`` path. All offline: the fixture
provider never touches the network. No test may make a live call.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    InstrumentListing,
    InstrumentPrice,
    PortfolioTransaction,
    Workspace,
)
from app.schemas.broker_import import BrokerImportRequest
from app.services import broker_imports as broker_service
from app.services import imported_instrument_resolution as iir
from app.services import instrument_prices as price_service
from app.services import market_data_planner
from app.workers.run import run_job

_FIXTURE = "instrument_price_fixture"
TSLA_ISIN = "US88160R1014"

# TSLA (by ISIN) + AAPL (by ticker+name) resolve via the fixture; AMB is ambiguous,
# ZZZZ is not found, the name-only row is unsafe — so exactly two resolved imported
# listings (TSLA, AAPL) become priceable.
DIRECT_CSV = (
    "date,type,symbol,isin,name,quantity,price,net_amount,currency\n"
    f"2026-06-15,buy,TSLA,{TSLA_ISIN},Tesla Inc,5,210,-1050,USD\n"
    "2026-06-16,buy,AAPL,,Apple Inc,3,180,-540,USD\n"
    "2026-06-17,buy,AMB,,Ambiguous HoldCo,2,10,-20,USD\n"
    "2026-06-18,buy,ZZZZ,,Totally Unknown Ltd,1,5,-5,USD\n"
    "2026-06-19,buy,,,Some Name Only PLC,1,5,-5,GBP\n"
)


async def _workspace_id(session: AsyncSession) -> int:
    return (await session.scalar(select(Workspace.id).order_by(Workspace.id))) or 1


async def _commit(session: AsyncSession, wid: int, csv_text: str = DIRECT_CSV) -> None:
    await broker_service.commit_import(
        session, wid, request=BrokerImportRequest(csv_text=csv_text, source_filename="s.csv")
    )


async def _commit_and_resolve(session: AsyncSession, wid: int, csv_text: str = DIRECT_CSV) -> None:
    await _commit(session, wid, csv_text)
    await iir.resolve_imported_instruments(
        session, workspace_id=wid, source="constituent_identity_fixture"
    )
    await session.commit()


async def _tsla_listing_id(session: AsyncSession) -> int:
    txn = await session.scalar(
        select(PortfolioTransaction).where(PortfolioTransaction.symbol == "TSLA")
    )
    assert txn is not None and txn.instrument_listing_id is not None
    return txn.instrument_listing_id


async def _resolve_constituents(session: AsyncSession, *, workspace_id: int) -> None:
    await run_job(
        session,
        "constituent_identity_resolution",
        workspace_id=workspace_id,
        source_name="constituent_identity_fixture",
    )


# --- selection ---------------------------------------------------------------


async def test_selects_resolved_imported_listings(session: AsyncSession) -> None:
    wid = await _workspace_id(session)
    await _commit_and_resolve(session, wid)
    selection = await price_service.select_priceable_listings(session, workspace_id=wid)
    tickers = sorted(ln.ticker for ln in selection.listings)
    # Exactly the two resolved imported direct holdings (constituents unresolved).
    assert tickers == ["AAPL", "TSLA"]


async def test_skips_unresolved_and_ambiguous_imported(session: AsyncSession) -> None:
    wid = await _workspace_id(session)
    await _commit_and_resolve(session, wid)
    # AMB (ambiguous), ZZZZ (not found) and the name-only row never link a listing.
    ids = await price_service._imported_resolved_listing_ids(
        session, workspace_id=wid, broker_import_id=None
    )
    assert len(ids) == 2  # only TSLA + AAPL


async def test_dedupes_repeated_imported_transactions(session: AsyncSession) -> None:
    wid = await _workspace_id(session)
    # Two TSLA buys on different dates => two transactions, one resolved listing.
    csv = (
        "date,type,symbol,isin,name,quantity,price,net_amount,currency\n"
        f"2026-06-15,buy,TSLA,{TSLA_ISIN},Tesla Inc,5,210,-1050,USD\n"
        f"2026-06-20,buy,TSLA,{TSLA_ISIN},Tesla Inc,2,215,-430,USD\n"
    )
    await _commit_and_resolve(session, wid, csv)
    selection = await price_service.select_priceable_listings(session, workspace_id=wid)
    assert len(selection.listings) == 1  # TSLA priced once


async def test_constituent_listings_still_selected(session: AsyncSession) -> None:
    wid = await _workspace_id(session)
    await _resolve_constituents(session, workspace_id=wid)
    selection = await price_service.select_priceable_listings(session, workspace_id=wid)
    # The held ETFs' resolved constituents are selectable (no imports committed).
    assert selection.listings


async def test_explicit_listing_and_limit(session: AsyncSession) -> None:
    wid = await _workspace_id(session)
    await _commit_and_resolve(session, wid)
    tsla = await _tsla_listing_id(session)
    only = await price_service.select_priceable_listings(session, instrument_listing_id=tsla)
    assert [ln.id for ln in only.listings] == [tsla]

    limited = await price_service.select_priceable_listings(session, workspace_id=wid, limit=1)
    assert len(limited.listings) == 1


async def test_fresh_skipped_unless_forced(session: AsyncSession) -> None:
    wid = await _workspace_id(session)
    await _commit_and_resolve(session, wid)
    await price_service.ingest_instrument_eod_prices(session, workspace_id=wid, source=_FIXTURE)
    await session.commit()

    # skip_fresh drops the now-fresh listings; force/no-skip keeps them.
    skipped = await price_service.select_priceable_listings(
        session, workspace_id=wid, skip_fresh=True
    )
    assert skipped.listings == []
    assert skipped.skipped_fresh == 2
    kept = await price_service.select_priceable_listings(
        session, workspace_id=wid, skip_fresh=False
    )
    assert len(kept.listings) == 2


# --- service / worker --------------------------------------------------------


async def test_worker_prices_imported_direct_holdings(session: AsyncSession) -> None:
    wid = await _workspace_id(session)
    await _commit_and_resolve(session, wid)
    run = await run_job(
        session, "instrument_eod_price_ingestion", workspace_id=wid, source_name=_FIXTURE
    )
    assert run.status == "success"
    assert run.records_inserted >= 2
    assert "selected=2" in run.message
    assert f"source={_FIXTURE}" in run.message

    tsla = await _tsla_listing_id(session)
    bars = await session.scalar(
        select(func.count())
        .select_from(InstrumentPrice)
        .where(InstrumentPrice.instrument_listing_id == tsla)
    )
    assert bars >= 1
    listing = await session.get(InstrumentListing, tsla)
    assert listing.last_price_at is not None  # reads as fresh now


async def test_worker_idempotent_and_force(session: AsyncSession) -> None:
    wid = await _workspace_id(session)
    await _commit_and_resolve(session, wid)
    await run_job(session, "instrument_eod_price_ingestion", workspace_id=wid, source_name=_FIXTURE)
    count_1 = await session.scalar(select(func.count()).select_from(InstrumentPrice))

    # Default rerun skips fresh listings (nothing selected, no new rows).
    rerun = await run_job(
        session, "instrument_eod_price_ingestion", workspace_id=wid, source_name=_FIXTURE
    )
    assert "selected=0" in rerun.message
    assert rerun.records_inserted == 0

    # Force re-prices the fresh listings; the idempotent upsert writes no duplicates.
    forced = await run_job(
        session,
        "instrument_eod_price_ingestion",
        workspace_id=wid,
        source_name=_FIXTURE,
        force=True,
    )
    assert "selected=2" in forced.message
    assert forced.records_inserted == 0 and forced.records_updated == 0
    count_2 = await session.scalar(select(func.count()).select_from(InstrumentPrice))
    assert count_1 == count_2  # no duplicate rows


async def test_worker_broker_import_scope(session: AsyncSession) -> None:
    wid = await _workspace_id(session)
    result = await broker_service.commit_import(
        session, wid, request=BrokerImportRequest(csv_text=DIRECT_CSV, source_filename="s.csv")
    )
    await iir.resolve_imported_instruments(
        session, broker_import_id=result.import_id, source="constituent_identity_fixture"
    )
    await session.commit()
    run = await run_job(
        session,
        "instrument_eod_price_ingestion",
        broker_import_id=result.import_id,
        source_name=_FIXTURE,
    )
    assert run.status == "success"
    assert "selected=2" in run.message


async def test_worker_no_priceable_is_clean_noop(session: AsyncSession) -> None:
    wid = await _workspace_id(session)
    # No constituents resolved, no imports committed => nothing priceable.
    run = await run_job(
        session, "instrument_eod_price_ingestion", workspace_id=wid, source_name=_FIXTURE
    )
    assert run.status == "success"
    assert run.records_inserted == 0
    assert "No priceable instrument listings" in run.message


async def test_unpriceable_listing_counted(session: AsyncSession) -> None:
    from app.db.models import Instrument

    instrument = Instrument(
        identity_key="manual:no-ticker",
        instrument_type="equity",
        name="No Ticker Co",
        status="active",
        source="manual",
    )
    session.add(instrument)
    await session.flush()
    listing = InstrumentListing(
        instrument_id=instrument.id,
        listing_key="figi:BBG_NOTICKER",
        ticker=None,  # nothing to fetch
        figi="BBG000NOTICK",
        currency="USD",
        source="manual",
        status="active",
    )
    session.add(listing)
    await session.commit()
    selection = await price_service.select_priceable_listings(
        session, instrument_listing_id=listing.id
    )
    assert selection.listings == []
    assert selection.skipped_unpriceable == 1


async def test_fixture_path_makes_no_network_call(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    import httpx

    def explode(*args, **kwargs):  # pragma: no cover - only fires on a real call
        raise AssertionError("instrument price ingestion attempted a network call")

    monkeypatch.setattr(httpx.AsyncClient, "get", explode)
    monkeypatch.setattr(httpx.AsyncClient, "post", explode)
    wid = await _workspace_id(session)
    await _commit_and_resolve(session, wid)
    result = await price_service.ingest_instrument_eod_prices(
        session, workspace_id=wid, source=_FIXTURE
    )
    assert result.inserted >= 2


# --- planner / diagnostics / capabilities ------------------------------------


async def test_planner_stops_emitting_after_imported_prices(session: AsyncSession) -> None:
    wid = await _workspace_id(session)
    await _commit_and_resolve(session, wid)
    before = await market_data_planner.build_plan(session, wid, include_constituents=True)
    assert any(i.item_type == "fetch_imported_instrument_price" for i in before.items)
    assert before.summary.imported_ready_for_prices >= 1

    await run_job(session, "instrument_eod_price_ingestion", workspace_id=wid, source_name=_FIXTURE)
    after = await market_data_planner.build_plan(session, wid, include_constituents=True)
    assert not any(i.item_type == "fetch_imported_instrument_price" for i in after.items)
    assert after.summary.imported_ready_for_prices == 0


async def test_diagnostics_surface_instrument_price_health(
    client: AsyncClient, session: AsyncSession
) -> None:
    # ``client`` and ``session`` share the test DB; drive setup via the worker.
    wid = await _workspace_id(session)
    await _commit_and_resolve(session, wid)

    before = (await client.get(f"/api/v1/workspaces/{wid}/diagnostics")).json()
    assert before["missing_imported_instrument_prices"] >= 1
    assert "instrument_price_ingestion_failures" in before

    await run_job(session, "instrument_eod_price_ingestion", workspace_id=wid, source_name=_FIXTURE)
    await session.commit()

    after = (await client.get(f"/api/v1/workspaces/{wid}/diagnostics")).json()
    assert after["missing_imported_instrument_prices"] == 0
    assert after["instrument_price_ingestion_failures"] == 0


async def test_capabilities_mark_instrument_price_ingestion(client: AsyncClient) -> None:
    body = (await client.get("/api/v1/capabilities")).json()
    assert body["features"]["instrument_eod_price_ingestion"] == "fixture"
    assert body["features"]["imported_instrument_prices"] == "fixture"
    # The price data type stays real (live-capable Stooq/yfinance path remains).
    statuses = {d["name"]: d["status"] for d in body["data_types"]}
    assert statuses["prices"] == "real"


# --- API / time-series -------------------------------------------------------


async def test_imported_listing_prices_and_timeseries(
    client: AsyncClient, session: AsyncSession
) -> None:
    body = {"broker_name": "generic_csv_v1", "source_filename": "s.csv", "csv_text": DIRECT_CSV}
    await client.post("/api/v1/workspaces/1/broker-imports/commit", json=body)
    await client.post(
        "/api/v1/workspaces/1/transactions/resolve",
        json={"source": "constituent_identity_fixture"},
    )
    tsla = await _tsla_listing_id(session)
    await run_job(session, "instrument_eod_price_ingestion", workspace_id=1, source_name=_FIXTURE)
    await session.commit()

    prices = (await client.get(f"/api/v1/instrument-listings/{tsla}/prices")).json()
    assert prices["meta"]["count"] >= 1
    assert prices["data"][0]["close"]

    series = (
        await client.get(f"/api/v1/instrument-listings/{tsla}/time-series?kind=price&range=1y")
    ).json()
    assert series["subject"]["type"] == "instrument_listing"
    assert series["points"] and series["points"][0]["value"]
