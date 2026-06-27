"""Portfolio valuation/readiness snapshots — construction, idempotency, worker,
API, planner, diagnostics, capabilities, compute-boundary safety.

All offline: the valuation service consumes already-ingested prices/FX only. It
never fetches a price/FX source, never resolves identity, and never computes PnL.
No test requires the network.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    FundListing,
    Instrument,
    InstrumentListing,
    InstrumentPrice,
    PortfolioTransaction,
    PortfolioValuationRow,
    PortfolioValuationSnapshot,
    Price,
    Workspace,
)
from app.services import capabilities as capabilities_service
from app.services import diagnostics as diagnostics_service
from app.services import market_data_planner
from app.services import portfolio_valuation as pv
from app.workers.run import run_job

_TODAY = date.today()


async def _workspace_id(session: AsyncSession) -> int:
    return (await session.scalar(select(Workspace.id).order_by(Workspace.id))) or 1


async def _fund_listing(session: AsyncSession, ticker: str) -> FundListing:
    fl = await session.scalar(select(FundListing).where(FundListing.ticker == ticker))
    assert fl is not None
    return fl


_KEYSEQ = {"n": 0}


def _txn(
    wid: int,
    *,
    transaction_type: str = "buy",
    quantity: Decimal | None = None,
    currency: str = "GBP",
    status: str = "committed",
    fund_id: int | None = None,
    fund_listing_id: int | None = None,
    instrument_id: int | None = None,
    instrument_listing_id: int | None = None,
    symbol: str | None = None,
    isin: str | None = None,
    name: str | None = None,
    net_amount: Decimal | None = None,
    cash_currency: str | None = None,
) -> PortfolioTransaction:
    _KEYSEQ["n"] += 1
    return PortfolioTransaction(
        workspace_id=wid,
        transaction_key=f"vk{_KEYSEQ['n']}",
        transaction_type=transaction_type,
        trade_date=date(2026, 6, 1),
        quantity=quantity,
        currency=currency,
        status=status,
        source="broker_csv",
        fund_id=fund_id,
        fund_listing_id=fund_listing_id,
        instrument_id=instrument_id,
        instrument_listing_id=instrument_listing_id,
        symbol=symbol,
        isin=isin,
        name=name,
        net_amount=net_amount,
        cash_currency=cash_currency,
    )


async def _instrument_listing(
    session: AsyncSession,
    *,
    ticker: str,
    currency: str,
    price: Decimal | None = None,
    price_date: date | None = None,
) -> InstrumentListing:
    instr = Instrument(
        identity_key=f"id-{ticker}", instrument_type="equity", name=ticker, source="manual"
    )
    session.add(instr)
    await session.flush()
    listing = InstrumentListing(
        instrument_id=instr.id,
        listing_key=f"{ticker}|XX",
        ticker=ticker,
        currency=currency,
        source="manual",
    )
    session.add(listing)
    await session.flush()
    if price is not None:
        session.add(
            InstrumentPrice(
                instrument_listing_id=listing.id,
                price_date=price_date or _TODAY,
                close=price,
                currency=currency,
                source="manual",
            )
        )
        await session.flush()
    return listing


def _row(computed: pv.ComputedValuation, key: str) -> pv._ValRow:
    for r in computed.rows:
        if r.position_key == key:
            return r
    raise AssertionError(f"row {key} not found in {[r.position_key for r in computed.rows]}")


# --- snapshot construction ---------------------------------------------------


@pytest.mark.asyncio
async def test_resolved_fund_listing_with_price_and_fx_is_valued(session: AsyncSession) -> None:
    wid = await _workspace_id(session)
    vusa = await _fund_listing(session, "VUSA")  # seed GBP price 75.00
    session.add(_txn(wid, quantity=Decimal("10"), fund_id=vusa.fund_id, fund_listing_id=vusa.id))
    await session.commit()

    computed = await pv.compute_valuation(session, wid)
    row = _row(computed, f"fund_listing:{vusa.id}")
    assert row.position_type == "fund_listing"
    assert row.valuation_status == "valued"
    assert row.readiness_status == "ready"
    assert row.local_currency == "GBP"
    assert row.market_value_base == Decimal("750.00")
    assert row.blocking_reasons == []


@pytest.mark.asyncio
async def test_gbx_listing_is_normalised_to_gbp(session: AsyncSession) -> None:
    wid = await _workspace_id(session)
    isf = await _fund_listing(session, "ISF")  # seed GBX price 850.00 -> 8.50 GBP
    session.add(_txn(wid, quantity=Decimal("50"), fund_id=isf.fund_id, fund_listing_id=isf.id))
    await session.commit()
    computed = await pv.compute_valuation(session, wid)
    row = _row(computed, f"fund_listing:{isf.id}")
    assert row.local_currency == "GBP"
    assert row.latest_price == Decimal("8.50000000")
    assert row.market_value_base == Decimal("425.00")


@pytest.mark.asyncio
async def test_resolved_instrument_missing_price(session: AsyncSession) -> None:
    wid = await _workspace_id(session)
    listing = await _instrument_listing(session, ticker="FOO", currency="USD")  # no price
    session.add(
        _txn(
            wid,
            quantity=Decimal("5"),
            currency="USD",
            instrument_id=listing.instrument_id,
            instrument_listing_id=listing.id,
        )
    )
    await session.commit()
    computed = await pv.compute_valuation(session, wid)
    row = _row(computed, f"instrument:{listing.instrument_id}")
    assert row.position_type == "instrument_listing"
    assert row.valuation_status == "missing_price"
    assert row.readiness_status == "blocked"
    assert row.latest_price_status == "missing"
    assert row.market_value_base is None
    assert row.blocking_reasons == ["missing_price"]


@pytest.mark.asyncio
async def test_resolved_instrument_with_price_and_fx_valued(session: AsyncSession) -> None:
    wid = await _workspace_id(session)
    listing = await _instrument_listing(session, ticker="BAR", currency="USD", price=Decimal("100"))
    session.add(
        _txn(
            wid,
            quantity=Decimal("5"),
            currency="USD",
            instrument_id=listing.instrument_id,
            instrument_listing_id=listing.id,
        )
    )
    await session.commit()
    computed = await pv.compute_valuation(session, wid)
    row = _row(computed, f"instrument:{listing.instrument_id}")
    assert row.valuation_status == "valued"
    assert row.local_currency == "USD"
    assert row.fx_status in ("fresh", "stale", "same_currency")
    # 500 USD / 1.27 (GBP->USD) -> ~393.70 GBP
    assert row.market_value_base == Decimal("393.70")


@pytest.mark.asyncio
async def test_resolved_instrument_missing_fx(session: AsyncSession) -> None:
    wid = await _workspace_id(session)
    # JPY has no FX path to GBP in the seed -> priced but unconvertible.
    listing = await _instrument_listing(
        session, ticker="JPN", currency="JPY", price=Decimal("1000")
    )
    session.add(
        _txn(
            wid,
            quantity=Decimal("3"),
            currency="JPY",
            instrument_id=listing.instrument_id,
            instrument_listing_id=listing.id,
        )
    )
    await session.commit()
    computed = await pv.compute_valuation(session, wid)
    row = _row(computed, f"instrument:{listing.instrument_id}")
    assert row.valuation_status == "missing_fx"
    assert row.readiness_status == "blocked"
    assert row.latest_price == Decimal("1000.00000000")  # price present
    assert row.market_value_local == Decimal("3000.00")
    assert row.market_value_base is None
    assert row.fx_status == "missing"
    assert row.blocking_reasons == ["missing_fx"]


@pytest.mark.asyncio
async def test_stale_price(session: AsyncSession) -> None:
    wid = await _workspace_id(session)
    listing = await _instrument_listing(
        session,
        ticker="OLD",
        currency="USD",
        price=Decimal("10"),
        price_date=_TODAY - timedelta(days=30),
    )
    session.add(
        _txn(
            wid,
            quantity=Decimal("2"),
            currency="USD",
            instrument_id=listing.instrument_id,
            instrument_listing_id=listing.id,
        )
    )
    await session.commit()
    computed = await pv.compute_valuation(session, wid)
    row = _row(computed, f"instrument:{listing.instrument_id}")
    assert row.valuation_status == "stale_price"
    assert row.readiness_status == "stale"
    assert row.market_value_base is not None  # still valued, just flagged stale
    assert computed.stale_price == 1


@pytest.mark.asyncio
async def test_cash_rows(session: AsyncSession) -> None:
    wid = await _workspace_id(session)
    session.add(
        _txn(wid, transaction_type="cash_deposit", net_amount=Decimal("500"), currency="GBP")
    )
    session.add(
        _txn(wid, transaction_type="cash_deposit", net_amount=Decimal("250"), currency="USD")
    )
    session.add(
        _txn(wid, transaction_type="cash_deposit", net_amount=Decimal("1000"), currency="JPY")
    )
    await session.commit()
    computed = await pv.compute_valuation(session, wid)

    gbp = _row(computed, "cash:GBP")
    assert gbp.position_type == "cash"
    assert gbp.valuation_status == "cash_only"
    assert gbp.readiness_status == "cash"
    assert gbp.market_value_base == Decimal("500.00")

    usd = _row(computed, "cash:USD")
    assert usd.valuation_status == "cash_only"
    assert usd.market_value_base == Decimal("196.85")  # 250 / 1.27

    jpy = _row(computed, "cash:JPY")
    assert jpy.valuation_status == "missing_fx"
    assert jpy.readiness_status == "blocked"
    assert jpy.market_value_base is None
    assert computed.cash_rows == 3


@pytest.mark.asyncio
async def test_unresolved_and_ambiguous(session: AsyncSession) -> None:
    wid = await _workspace_id(session)
    session.add(
        _txn(
            wid, quantity=Decimal("1"), currency="USD", symbol="ZZZ", status="unresolved_instrument"
        )
    )
    session.add(
        _txn(
            wid, quantity=Decimal("1"), currency="USD", symbol="AMB", status="ambiguous_instrument"
        )
    )
    await session.commit()
    computed = await pv.compute_valuation(session, wid)
    statuses = {r.symbol: r.valuation_status for r in computed.rows if r.symbol}
    assert statuses["ZZZ"] == "unresolved_instrument"
    assert statuses["AMB"] == "ambiguous_instrument"
    assert computed.unresolved == 1
    assert computed.ambiguous == 1
    types = {r.symbol: r.position_type for r in computed.rows if r.symbol}
    assert types["ZZZ"] == "unresolved"
    assert types["AMB"] == "ambiguous"


@pytest.mark.asyncio
async def test_zero_quantity(session: AsyncSession) -> None:
    wid = await _workspace_id(session)
    vusa = await _fund_listing(session, "VUSA")
    session.add(_txn(wid, quantity=Decimal("10"), fund_id=vusa.fund_id, fund_listing_id=vusa.id))
    session.add(
        _txn(
            wid,
            transaction_type="sell",
            quantity=Decimal("10"),
            fund_id=vusa.fund_id,
            fund_listing_id=vusa.id,
        )
    )
    await session.commit()
    computed = await pv.compute_valuation(session, wid)
    row = _row(computed, f"fund_listing:{vusa.id}")
    assert row.quantity == Decimal("0")
    assert row.valuation_status == "zero_quantity"
    assert row.market_value_base == Decimal("0")


# --- idempotency -------------------------------------------------------------


@pytest.mark.asyncio
async def test_idempotent_rerun_no_duplicate(session: AsyncSession) -> None:
    wid = await _workspace_id(session)
    vusa = await _fund_listing(session, "VUSA")
    session.add(_txn(wid, quantity=Decimal("10"), fund_id=vusa.fund_id, fund_listing_id=vusa.id))
    await session.commit()

    r1 = await pv.recompute_portfolio_valuation_snapshot(session, wid)
    await session.commit()
    assert r1.snapshot_created and not r1.snapshot_skipped

    r2 = await pv.recompute_portfolio_valuation_snapshot(session, wid)
    await session.commit()
    assert r2.snapshot_skipped and not r2.snapshot_created
    assert r2.snapshot_id == r1.snapshot_id

    n = await session.scalar(select(func.count()).select_from(PortfolioValuationSnapshot))
    assert n == 1


@pytest.mark.asyncio
async def test_new_price_creates_new_snapshot(session: AsyncSession) -> None:
    wid = await _workspace_id(session)
    vusa = await _fund_listing(session, "VUSA")
    session.add(_txn(wid, quantity=Decimal("10"), fund_id=vusa.fund_id, fund_listing_id=vusa.id))
    await session.commit()
    r1 = await pv.recompute_portfolio_valuation_snapshot(session, wid)
    await session.commit()

    # A newer fund-listing price changes the valuation inputs -> new snapshot.
    session.add(
        Price(
            fund_listing_id=vusa.id,
            price_date=_TODAY,
            price=Decimal("99.00"),
            currency="GBP",
            source="manual",
        )
    )
    await session.commit()
    r2 = await pv.recompute_portfolio_valuation_snapshot(session, wid)
    await session.commit()
    assert r2.snapshot_created
    assert r2.snapshot_id != r1.snapshot_id
    n = await session.scalar(select(func.count()).select_from(PortfolioValuationSnapshot))
    assert n == 2


@pytest.mark.asyncio
async def test_force_refreshes_unchanged_snapshot(session: AsyncSession) -> None:
    wid = await _workspace_id(session)
    vusa = await _fund_listing(session, "VUSA")
    session.add(_txn(wid, quantity=Decimal("10"), fund_id=vusa.fund_id, fund_listing_id=vusa.id))
    await session.commit()
    await pv.recompute_portfolio_valuation_snapshot(session, wid)
    await session.commit()
    forced = await pv.recompute_portfolio_valuation_snapshot(session, wid, force=True)
    await session.commit()
    assert forced.snapshot_updated and not forced.snapshot_created
    # Still exactly one snapshot; rows refreshed in place.
    n = await session.scalar(select(func.count()).select_from(PortfolioValuationSnapshot))
    assert n == 1
    nrows = await session.scalar(select(func.count()).select_from(PortfolioValuationRow))
    assert nrows >= 1


# --- worker ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_worker_records_job_run(session: AsyncSession) -> None:
    wid = await _workspace_id(session)
    vusa = await _fund_listing(session, "VUSA")
    session.add(_txn(wid, quantity=Decimal("10"), fund_id=vusa.fund_id, fund_listing_id=vusa.id))
    await session.commit()

    run = await run_job(session, "portfolio_valuation_recompute", workspace_id=wid)
    assert run.status == "success"
    assert run.records_inserted == 1  # one snapshot created
    assert "created=1" in (run.message or "")


@pytest.mark.asyncio
async def test_worker_all_workspaces_when_unscoped(session: AsyncSession) -> None:
    wid = await _workspace_id(session)
    vusa = await _fund_listing(session, "VUSA")
    session.add(_txn(wid, quantity=Decimal("10"), fund_id=vusa.fund_id, fund_listing_id=vusa.id))
    await session.commit()
    run = await run_job(session, "portfolio_valuation_recompute")
    assert run.status == "success"
    assert "workspaces=" in (run.message or "")


@pytest.mark.asyncio
async def test_worker_respects_base_currency_and_as_of(session: AsyncSession) -> None:
    wid = await _workspace_id(session)
    vusa = await _fund_listing(session, "VUSA")
    session.add(_txn(wid, quantity=Decimal("10"), fund_id=vusa.fund_id, fund_listing_id=vusa.id))
    await session.commit()
    await run_job(
        session,
        "portfolio_valuation_recompute",
        workspace_id=wid,
        base_currency="USD",
        as_of_date=_TODAY,
    )
    snap = await pv.get_latest_snapshot(session, wid)
    assert snap is not None
    assert snap.base_currency == "USD"
    assert snap.as_of_date == _TODAY


# --- API ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_api_latest_and_coverage_and_recompute(
    client: AsyncClient, session: AsyncSession
) -> None:
    wid = await _workspace_id(session)
    vusa = await _fund_listing(session, "VUSA")
    session.add(_txn(wid, quantity=Decimal("10"), fund_id=vusa.fund_id, fund_listing_id=vusa.id))
    session.add(
        _txn(
            wid, quantity=Decimal("1"), currency="USD", symbol="ZZZ", status="unresolved_instrument"
        )
    )
    await session.commit()

    # Recompute via the API (writes a snapshot).
    resp = await client.post(f"/api/v1/workspaces/{wid}/portfolio/valuation/recompute")
    assert resp.status_code == 200
    body = resp.json()
    assert body["snapshot_created"] is True
    assert body["positions_valued"] >= 1
    assert body["unresolved"] == 1

    latest = await client.get(f"/api/v1/workspaces/{wid}/portfolio/valuation/latest")
    assert latest.status_code == 200
    lbody = latest.json()
    assert lbody["cached"] is True
    assert lbody["summary"]["positions_valued"] >= 1
    # source/provenance present on rows.
    row = next(r for r in lbody["rows"] if r["position_type"] == "fund_listing")
    assert row["latest_price_source"] is not None
    assert row["source"] == "portfolio_valuation"

    # Filter by valuation_status.
    filtered = await client.get(
        f"/api/v1/workspaces/{wid}/portfolio/valuation/latest",
        params={"valuation_status": "unresolved_instrument"},
    )
    fbody = filtered.json()
    assert fbody["rows"]
    assert all(r["valuation_status"] == "unresolved_instrument" for r in fbody["rows"])
    assert fbody["rows"][0]["blocking_reasons"] == ["unresolved_instrument"]

    coverage = await client.get(f"/api/v1/workspaces/{wid}/portfolio/valuation/coverage")
    assert coverage.status_code == 200
    cbody = coverage.json()
    assert "rows" not in cbody
    assert cbody["summary"]["positions_selected"] >= 2


@pytest.mark.asyncio
async def test_api_get_valuation_on_the_fly_when_no_snapshot(
    client: AsyncClient, session: AsyncSession
) -> None:
    wid = await _workspace_id(session)
    vusa = await _fund_listing(session, "VUSA")
    session.add(_txn(wid, quantity=Decimal("10"), fund_id=vusa.fund_id, fund_listing_id=vusa.id))
    await session.commit()
    resp = await client.get(f"/api/v1/workspaces/{wid}/portfolio/valuation")
    assert resp.status_code == 200
    body = resp.json()
    assert body["cached"] is False
    assert body["status"] == "recompute_needed"
    assert body["rows"]


# --- planner / diagnostics / capabilities ------------------------------------


@pytest.mark.asyncio
async def test_planner_emits_recompute_item(session: AsyncSession) -> None:
    wid = await _workspace_id(session)
    vusa = await _fund_listing(session, "VUSA")
    session.add(_txn(wid, quantity=Decimal("10"), fund_id=vusa.fund_id, fund_listing_id=vusa.id))
    await session.commit()
    plan = await market_data_planner.build_plan(session, wid)
    item = next(i for i in plan.items if i.item_type == "recompute_portfolio_valuation")
    assert item.estimated_requests == 0  # local recompute, not a fetch
    assert plan.summary.portfolio_valuation_recompute_needed == 1


@pytest.mark.asyncio
async def test_planner_no_recompute_item_when_fresh_snapshot(session: AsyncSession) -> None:
    wid = await _workspace_id(session)
    vusa = await _fund_listing(session, "VUSA")
    session.add(_txn(wid, quantity=Decimal("10"), fund_id=vusa.fund_id, fund_listing_id=vusa.id))
    await session.commit()
    await pv.recompute_portfolio_valuation_snapshot(session, wid)
    await session.commit()
    plan = await market_data_planner.build_plan(session, wid)
    assert not any(i.item_type == "recompute_portfolio_valuation" for i in plan.items)
    assert plan.summary.portfolio_valuation_recompute_needed == 0


@pytest.mark.asyncio
async def test_diagnostics_counts(session: AsyncSession) -> None:
    wid = await _workspace_id(session)
    vusa = await _fund_listing(session, "VUSA")
    session.add(_txn(wid, quantity=Decimal("10"), fund_id=vusa.fund_id, fund_listing_id=vusa.id))
    session.add(
        _txn(
            wid, quantity=Decimal("1"), currency="USD", symbol="ZZZ", status="unresolved_instrument"
        )
    )
    await session.commit()
    await pv.recompute_portfolio_valuation_snapshot(session, wid)
    await session.commit()
    diag = await diagnostics_service.workspace_diagnostics(session, wid)
    assert diag.portfolio_positions >= 2
    assert diag.portfolio_positions_valued >= 1
    assert diag.portfolio_positions_unresolved == 1
    assert diag.latest_portfolio_valuation_snapshot_at is not None
    assert diag.portfolio_valuation_failures == 0


def test_capabilities_status() -> None:
    caps = capabilities_service.build_capabilities()
    assert caps.features["portfolio_valuation_recompute"] == capabilities_service.REAL
    assert caps.features["portfolio_valuation"] == capabilities_service.PARTIAL
    # PnL et al stay planned — never marked real.
    for planned in ("portfolio_pnl", "tax_lots", "total_return", "performance_attribution"):
        assert caps.features[planned] == capabilities_service.PLANNED


# --- compute-boundary safety -------------------------------------------------


@pytest.mark.asyncio
async def test_valuation_never_resolves_or_invents(session: AsyncSession) -> None:
    """Unresolved rows stay blocked; no instrument is created; no value invented."""
    wid = await _workspace_id(session)
    before = await session.scalar(select(func.count()).select_from(Instrument))
    session.add(
        _txn(
            wid, quantity=Decimal("1"), currency="USD", symbol="ZZZ", status="unresolved_instrument"
        )
    )
    await session.commit()
    computed = await pv.compute_valuation(session, wid)
    after = await session.scalar(select(func.count()).select_from(Instrument))
    assert after == before  # no identity resolution / instrument creation
    row = next(r for r in computed.rows if r.symbol == "ZZZ")
    assert row.market_value_base is None
    assert row.valuation_status == "unresolved_instrument"
    assert computed.total_market_value_base is None
