"""Top-holding performance — price-context contribution over a date window.

All offline. Each test builds an isolated workspace, recomputes a base exposure
snapshot, then changes an input (price bar / weight) and recomputes again so two
snapshots with price movement exist. Contribution is a *price-context estimate*
(base implied value × local price return) — never PnL.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy import delete, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFoundError
from app.db.models import (
    Fund,
    FundHolding,
    FundListing,
    Instrument,
    InstrumentListing,
    InstrumentPrice,
    PortfolioPosition,
    Price,
    Workspace,
)
from app.services import alert_generation
from app.services import alerts as alerts_service
from app.services import exposure_recompute as service
from app.services import holding_performance as perf
from app.sources.holdings import holding_identity_key

_TODAY = date.today()
_FIXTURE = "instrument_price_fixture"


# --- builders ----------------------------------------------------------------


async def _workspace(session: AsyncSession, name: str, base: str = "GBP") -> Workspace:
    ws = Workspace(name=name, base_currency=base)
    session.add(ws)
    await session.flush()
    return ws


async def _instrument(
    session: AsyncSession, name: str, *, currency: str, ticker: str
) -> Instrument:
    instr = Instrument(
        identity_key=f"name:{name.lower()}",
        instrument_type="equity",
        name=name,
        currency=currency,
        status="active",
        source="manual",
    )
    session.add(instr)
    await session.flush()
    listing = InstrumentListing(
        instrument_id=instr.id,
        listing_key=f"ticker:{ticker}",
        ticker=ticker,
        currency=currency,
        source="manual",
        status="active",
    )
    session.add(listing)
    await session.flush()
    instr._listing_id = listing.id  # type: ignore[attr-defined]
    return instr


async def _add_price(
    session: AsyncSession, listing_id: int, close: str, currency: str, days_ago: int
) -> None:
    session.add(
        InstrumentPrice(
            instrument_listing_id=listing_id,
            price_date=_TODAY - timedelta(days=days_ago),
            close=Decimal(close),
            currency=currency,
            source=_FIXTURE,
            status="fixture",
        )
    )
    await session.flush()


async def _fund_with_holdings(
    session: AsyncSession,
    ws: Workspace,
    *,
    isin: str,
    units: str,
    price: str,
    currency: str = "GBP",
    holdings: list[tuple],
) -> Fund:
    """holdings tuples: (instrument|None, weight, name)."""
    listing = FundListing(
        ticker=isin[-4:],
        trading_currency=currency,
        currency_unit=currency,
        status="active",
        prices=[Price(price_date=_TODAY, price=Decimal(price), currency=currency, source="stooq")],
    )
    fund = Fund(isin=isin, name=f"Fund {isin}", status="active", source="seed", listings=[listing])
    session.add(fund)
    await session.flush()
    session.add(
        PortfolioPosition(workspace_id=ws.id, fund_listing_id=listing.id, units=Decimal(units))
    )
    for instr, weight, name in holdings:
        session.add(
            FundHolding(
                fund_id=fund.id,
                as_of_date=_TODAY,
                security_name=name,
                weight=Decimal(weight),
                source="holdings_fixture",
                holding_key=holding_identity_key(name=name),
                holding_instrument_id=instr.id if instr else None,
                identity_status="resolved" if instr else None,
            )
        )
    await session.flush()
    return fund


async def _recompute(session: AsyncSession, ws_id: int) -> int:
    res = await service.recompute_workspace(session, ws_id)
    await session.commit()
    assert res.snapshot_id is not None
    return res.snapshot_id


def _row(resp, instrument_id: int):
    return next(r for r in resp.rows if r.instrument_id == instrument_id)


# --- insufficient states -----------------------------------------------------


async def test_insufficient_history_single_snapshot(session: AsyncSession) -> None:
    ws = await _workspace(session, "Solo")
    apple = await _instrument(session, "Apple Co", currency="USD", ticker="AAPL")
    await _add_price(session, apple._listing_id, "100", "USD", 1)  # type: ignore[attr-defined]
    await _fund_with_holdings(
        session,
        ws,
        isin="IE00SOLO0001",
        units="100",
        price="10",
        holdings=[(apple, "0.5", "Apple Co")],
    )
    await session.commit()
    await _recompute(session, ws.id)

    out = await perf.compute_top_holding_performance(session, ws.id)
    assert out.status == "insufficient_history"
    assert out.rows == []
    assert out.base_snapshot_id is None


async def test_insufficient_price_data(session: AsyncSession) -> None:
    # Two snapshots but the only constituent is unresolved -> nothing to price.
    ws = await _workspace(session, "NoPrice")
    fund = await _fund_with_holdings(
        session,
        ws,
        isin="IE00NOPR0001",
        units="100",
        price="10",
        holdings=[(None, "0.5", "Mystery Co")],
    )
    await session.commit()
    await _recompute(session, ws.id)
    await session.execute(
        update(FundHolding).where(FundHolding.fund_id == fund.id).values(weight=Decimal("0.6"))
    )
    await session.commit()
    await _recompute(session, ws.id)

    out = await perf.compute_top_holding_performance(session, ws.id)
    assert out.status == "insufficient_price_data"


# --- contribution math + selection -------------------------------------------


async def _two_priced_snapshots(session: AsyncSession, ws: Workspace, apple: Instrument) -> None:
    """Snapshot 1 prices Apple at 100 (D1); snapshot 2 at 120 (D2 = today)."""
    await _add_price(session, apple._listing_id, "100", "USD", 3)  # type: ignore[attr-defined]
    await _fund_with_holdings(
        session,
        ws,
        isin="IE00PERF0001",
        units="100",
        price="10",
        holdings=[(apple, "0.5", "Apple Co")],
    )
    await session.commit()
    await _recompute(session, ws.id)
    await _add_price(session, apple._listing_id, "120", "USD", 0)  # type: ignore[attr-defined]
    await session.commit()
    await _recompute(session, ws.id)


async def test_contribution_and_deltas_default_selection(session: AsyncSession) -> None:
    ws = await _workspace(session, "Perf")
    apple = await _instrument(session, "Apple Co", currency="USD", ticker="AAPL")
    await _two_priced_snapshots(session, ws, apple)

    out = await perf.compute_top_holding_performance(session, ws.id)
    assert out.status == "ok"
    row = _row(out, apple.id)
    # Base implied value = 1000 total * 0.5 weight = 500 (weight-based, NOT shares).
    assert row.base_implied_market_value_base == Decimal("500.00")
    assert row.comparison_implied_market_value_base == Decimal("500.00")
    assert row.market_value_delta_base == Decimal("0.00")  # weight unchanged
    assert row.weight_delta == Decimal("0.0000")
    assert row.base_price == Decimal("100.00")
    assert row.comparison_price == Decimal("120.00")
    assert row.price_return == Decimal("0.200000")  # local return
    assert row.price_return_basis == "local"  # USD priced, GBP base
    assert row.price_context_contribution_base == Decimal("100.00")  # 500 * 0.2
    assert row.status == "ok"
    assert out.summary.computed_count == 1
    assert out.summary.total_price_context_contribution_base == Decimal("100.00")


async def test_explicit_snapshot_ids(session: AsyncSession) -> None:
    ws = await _workspace(session, "Explicit")
    apple = await _instrument(session, "Apple Co", currency="USD", ticker="AAPL")
    await _add_price(session, apple._listing_id, "100", "USD", 3)  # type: ignore[attr-defined]
    await _fund_with_holdings(
        session,
        ws,
        isin="IE00EXPL0001",
        units="100",
        price="10",
        holdings=[(apple, "0.5", "Apple Co")],
    )
    await session.commit()
    snap1 = await _recompute(session, ws.id)
    await _add_price(session, apple._listing_id, "120", "USD", 0)  # type: ignore[attr-defined]
    await session.commit()
    snap2 = await _recompute(session, ws.id)

    out = await perf.compute_top_holding_performance(
        session, ws.id, base_snapshot_id=snap1, comparison_snapshot_id=snap2
    )
    assert out.base_snapshot_id == snap1 and out.comparison_snapshot_id == snap2
    assert _row(out, apple.id).price_context_contribution_base == Decimal("100.00")


async def test_cross_workspace_rejected(session: AsyncSession) -> None:
    ws_a = await _workspace(session, "A")
    ws_b = await _workspace(session, "B")
    a = await _instrument(session, "Apple A", currency="USD", ticker="AAPL")
    b = await _instrument(session, "Beta B", currency="USD", ticker="MSFT")
    await _add_price(session, a._listing_id, "100", "USD", 1)  # type: ignore[attr-defined]
    await _add_price(session, b._listing_id, "100", "USD", 1)  # type: ignore[attr-defined]
    await _fund_with_holdings(
        session,
        ws_a,
        isin="IE00WSA00001",
        units="100",
        price="10",
        holdings=[(a, "0.5", "Apple A")],
    )
    await _fund_with_holdings(
        session, ws_b, isin="IE00WSB00001", units="100", price="10", holdings=[(b, "0.5", "Beta B")]
    )
    await session.commit()
    await _recompute(session, ws_a.id)
    b_snap = await _recompute(session, ws_b.id)
    with pytest.raises(NotFoundError):
        await perf.compute_top_holding_performance(session, ws_a.id, comparison_snapshot_id=b_snap)


async def test_limit_and_sort(session: AsyncSession) -> None:
    ws = await _workspace(session, "Sort")
    big = await _instrument(session, "Big Co", currency="GBP", ticker="BIG")
    small = await _instrument(session, "Small Co", currency="GBP", ticker="SML")
    for inst in (big, small):
        await _add_price(session, inst._listing_id, "100", "GBP", 3)  # type: ignore[attr-defined]
    await _fund_with_holdings(
        session,
        ws,
        isin="IE00SORT0001",
        units="100",
        price="10",
        holdings=[(big, "0.5", "Big Co"), (small, "0.3", "Small Co")],
    )
    await session.commit()
    await _recompute(session, ws.id)
    # Big moves +20%, Small +5% -> Big has the larger contribution.
    await _add_price(session, big._listing_id, "120", "GBP", 0)  # type: ignore[attr-defined]
    await _add_price(session, small._listing_id, "105", "GBP", 0)  # type: ignore[attr-defined]
    await session.commit()
    await _recompute(session, ws.id)

    out = await perf.compute_top_holding_performance(
        session, ws.id, sort="abs_contribution", limit=1
    )
    assert len(out.rows) == 1
    assert out.rows[0].instrument_id == big.id


# --- missing / stale / fx / zero-price handling ------------------------------


async def test_missing_base_price(session: AsyncSession) -> None:
    ws = await _workspace(session, "MissBase")
    apple = await _instrument(session, "Apple Co", currency="USD", ticker="AAPL")
    # Snapshot 1: resolved but UNPRICED -> base row has no price_date.
    await _fund_with_holdings(
        session,
        ws,
        isin="IE00MISS0001",
        units="100",
        price="10",
        holdings=[(apple, "0.5", "Apple Co")],
    )
    await session.commit()
    await _recompute(session, ws.id)
    await _add_price(session, apple._listing_id, "120", "USD", 0)  # type: ignore[attr-defined]
    await session.commit()
    await _recompute(session, ws.id)

    out = await perf.compute_top_holding_performance(session, ws.id)
    row = _row(out, apple.id)
    assert row.status == "missing_base_price"
    assert row.price_context_contribution_base is None
    assert out.summary.missing_price_count == 1


async def test_missing_comparison_price(session: AsyncSession) -> None:
    ws = await _workspace(session, "MissComp")
    apple = await _instrument(session, "Apple Co", currency="USD", ticker="AAPL")
    await _add_price(session, apple._listing_id, "100", "USD", 3)  # type: ignore[attr-defined]
    await _fund_with_holdings(
        session,
        ws,
        isin="IE00MISC0001",
        units="100",
        price="10",
        holdings=[(apple, "0.5", "Apple Co")],
    )
    await session.commit()
    await _recompute(session, ws.id)
    # Snapshot 2 prices off a newer bar, then that bar is removed (correction).
    await _add_price(session, apple._listing_id, "120", "USD", 0)  # type: ignore[attr-defined]
    await session.commit()
    await _recompute(session, ws.id)
    await session.execute(delete(InstrumentPrice).where(InstrumentPrice.price_date == _TODAY))
    await session.commit()

    out = await perf.compute_top_holding_performance(session, ws.id)
    row = _row(out, apple.id)
    assert row.status == "missing_comparison_price"
    assert row.base_price == Decimal("100.00")
    assert row.price_context_contribution_base is None


async def test_stale_price(session: AsyncSession) -> None:
    ws = await _workspace(session, "Stale")
    apple = await _instrument(session, "Apple Co", currency="USD", ticker="AAPL")
    await _add_price(session, apple._listing_id, "100", "USD", 40)  # type: ignore[attr-defined]
    await _fund_with_holdings(
        session,
        ws,
        isin="IE00STAL0001",
        units="100",
        price="10",
        holdings=[(apple, "0.5", "Apple Co")],
    )
    await session.commit()
    await _recompute(session, ws.id)
    await _add_price(session, apple._listing_id, "120", "USD", 30)  # newer but still stale
    await session.commit()
    await _recompute(session, ws.id)

    out = await perf.compute_top_holding_performance(session, ws.id)
    row = _row(out, apple.id)
    assert row.status == "stale_price"
    # Contribution is still computed off the (stale) prices.
    assert row.price_context_contribution_base == Decimal("100.00")
    assert out.summary.stale_price_count == 1


async def test_fx_missing_local_return_still_computed(session: AsyncSession) -> None:
    ws = await _workspace(session, "FX")
    # CHF has no FX path to GBP in the seed rates -> fx context missing, but the
    # local-currency return still yields a contribution.
    nestle = await _instrument(session, "Nestle Co", currency="CHF", ticker="NESN")
    await _add_price(session, nestle._listing_id, "100", "CHF", 3)  # type: ignore[attr-defined]
    await _fund_with_holdings(
        session,
        ws,
        isin="IE00FXMS0001",
        units="100",
        price="10",
        holdings=[(nestle, "0.5", "Nestle Co")],
    )
    await session.commit()
    await _recompute(session, ws.id)
    await _add_price(session, nestle._listing_id, "110", "CHF", 0)  # type: ignore[attr-defined]
    await session.commit()
    await _recompute(session, ws.id)

    out = await perf.compute_top_holding_performance(session, ws.id)
    row = _row(out, nestle.id)
    assert row.currency == "CHF"
    assert row.fx_rate_base is None and row.fx_rate_comparison is None
    assert row.price_return_basis == "local"
    assert row.price_context_contribution_base == Decimal("50.00")  # 500 * 0.10
    assert out.summary.fx_missing_count == 1


async def test_fx_context_present_for_usd(session: AsyncSession) -> None:
    ws = await _workspace(session, "FXok")
    apple = await _instrument(session, "Apple Co", currency="USD", ticker="AAPL")
    await _two_priced_snapshots(session, ws, apple)
    out = await perf.compute_top_holding_performance(session, ws.id)
    row = _row(out, apple.id)
    # GBP/USD is seeded, so USD->GBP FX context is available (informational only).
    assert row.fx_rate_base is not None
    assert row.fx_source is not None
    assert out.summary.fx_missing_count == 0


async def test_zero_base_price_handled_safely(session: AsyncSession) -> None:
    ws = await _workspace(session, "Zero")
    apple = await _instrument(session, "Apple Co", currency="GBP", ticker="AAPL")
    await _add_price(session, apple._listing_id, "0", "GBP", 3)  # type: ignore[attr-defined]
    await _fund_with_holdings(
        session,
        ws,
        isin="IE00ZERO0001",
        units="100",
        price="10",
        holdings=[(apple, "0.5", "Apple Co")],
    )
    await session.commit()
    await _recompute(session, ws.id)
    await _add_price(session, apple._listing_id, "120", "GBP", 0)  # type: ignore[attr-defined]
    await session.commit()
    await _recompute(session, ws.id)

    out = await perf.compute_top_holding_performance(session, ws.id)  # must not raise
    row = _row(out, apple.id)
    assert row.price_return is None
    assert row.price_context_contribution_base is None


# --- API ---------------------------------------------------------------------


async def _seed_two_snapshots(session: AsyncSession, ws_id: int) -> None:
    from app.workers.run import run_job

    await run_job(session, "issuer_holdings_ingestion")
    await run_job(
        session,
        "constituent_identity_resolution",
        workspace_id=ws_id,
        source_name="constituent_identity_fixture",
    )
    await run_job(
        session, "constituent_eod_price_ingestion", workspace_id=ws_id, source_name=_FIXTURE
    )
    await service.recompute_workspace(session, ws_id)
    await session.commit()
    await session.execute(
        update(FundHolding)
        .where(FundHolding.security_name == "Apple Inc")
        .values(weight=Decimal("0.20000000"))
    )
    await session.commit()
    await service.recompute_workspace(session, ws_id)
    await session.commit()


async def test_api_default(client: AsyncClient, session: AsyncSession) -> None:
    await _seed_two_snapshots(session, 1)
    body = (
        await client.get("/api/v1/workspaces/1/exposure/top-holding-performance?limit=20")
    ).json()
    assert body["status"] == "ok"
    assert body["base_snapshot_id"] and body["comparison_snapshot_id"]
    assert len(body["rows"]) <= 20
    assert body["summary"]["computed_count"] >= 1
    # Schema never claims PnL.
    assert "price_context_contribution_base" in body["rows"][0]


async def test_api_explicit_snapshot_ids(client: AsyncClient, session: AsyncSession) -> None:
    await _seed_two_snapshots(session, 1)
    snaps = (await client.get("/api/v1/workspaces/1/exposure/snapshots")).json()["data"]
    comp_id = snaps[0]["snapshot_id"]
    base_id = snaps[1]["snapshot_id"]
    body = (
        await client.get(
            f"/api/v1/workspaces/1/exposure/top-holding-performance"
            f"?base_snapshot_id={base_id}&comparison_snapshot_id={comp_id}"
        )
    ).json()
    assert body["base_snapshot_id"] == base_id
    assert body["comparison_snapshot_id"] == comp_id


async def test_api_insufficient_history(client: AsyncClient, session: AsyncSession) -> None:
    await service.recompute_workspace(session, 1)
    await session.commit()
    body = (await client.get("/api/v1/workspaces/1/exposure/top-holding-performance")).json()
    assert body["status"] == "insufficient_history"
    assert body["rows"] == []


async def test_api_invalid_snapshot_id(client: AsyncClient, session: AsyncSession) -> None:
    await _seed_two_snapshots(session, 1)
    resp = await client.get(
        "/api/v1/workspaces/1/exposure/top-holding-performance?comparison_snapshot_id=999999"
    )
    assert resp.status_code == 404


async def test_api_dashboard_performance_block(client: AsyncClient, session: AsyncSession) -> None:
    await _seed_two_snapshots(session, 1)
    body = (await client.get("/api/v1/workspaces/1/dashboard")).json()
    block = body["exposure"]["top_holding_performance"]
    assert block["status"] == "ok"
    assert block["comparison_snapshot_id"]
    assert "top_positive_contributors" in block
    assert "top_negative_contributors" in block


async def test_api_dashboard_insufficient_history(
    client: AsyncClient, session: AsyncSession
) -> None:
    await service.recompute_workspace(session, 1)
    await session.commit()
    body = (await client.get("/api/v1/workspaces/1/dashboard")).json()
    assert body["exposure"]["top_holding_performance"]["status"] == "insufficient_history"


# --- diagnostics + alert quietness -------------------------------------------


async def test_diagnostics_missing_prices(client: AsyncClient, session: AsyncSession) -> None:
    ws = await _workspace(session, "DiagMiss")
    apple = await _instrument(session, "Apple Co", currency="USD", ticker="AAPL")
    # Unpriced at snapshot 1, priced at snapshot 2 -> missing base price.
    await _fund_with_holdings(
        session,
        ws,
        isin="IE00DGM00001",
        units="100",
        price="10",
        holdings=[(apple, "0.5", "Apple Co")],
    )
    await session.commit()
    await _recompute(session, ws.id)
    await _add_price(session, apple._listing_id, "120", "USD", 0)  # type: ignore[attr-defined]
    await session.commit()
    await _recompute(session, ws.id)

    diag = (await client.get(f"/api/v1/workspaces/{ws.id}/diagnostics")).json()
    assert diag["top_holding_performance_missing_prices"] == 1
    assert diag["top_holding_performance_insufficient_history"] == 0


async def test_diagnostics_fx_missing(client: AsyncClient, session: AsyncSession) -> None:
    ws = await _workspace(session, "DiagFX")
    nestle = await _instrument(session, "Nestle Co", currency="CHF", ticker="NESN")
    await _add_price(session, nestle._listing_id, "100", "CHF", 3)  # type: ignore[attr-defined]
    await _fund_with_holdings(
        session,
        ws,
        isin="IE00DGF00001",
        units="100",
        price="10",
        holdings=[(nestle, "0.5", "Nestle Co")],
    )
    await session.commit()
    await _recompute(session, ws.id)
    await _add_price(session, nestle._listing_id, "110", "CHF", 0)  # type: ignore[attr-defined]
    await session.commit()
    await _recompute(session, ws.id)

    diag = (await client.get(f"/api/v1/workspaces/{ws.id}/diagnostics")).json()
    assert diag["top_holding_performance_fx_missing"] == 1


async def test_diagnostics_insufficient_history(client: AsyncClient, session: AsyncSession) -> None:
    ws = await _workspace(session, "DiagOne")
    apple = await _instrument(session, "Apple Co", currency="USD", ticker="AAPL")
    await _add_price(session, apple._listing_id, "100", "USD", 1)  # type: ignore[attr-defined]
    await _fund_with_holdings(
        session,
        ws,
        isin="IE00DGO00001",
        units="100",
        price="10",
        holdings=[(apple, "0.5", "Apple Co")],
    )
    await session.commit()
    await _recompute(session, ws.id)  # one snapshot only
    diag = (await client.get(f"/api/v1/workspaces/{ws.id}/diagnostics")).json()
    assert diag["top_holding_performance_insufficient_history"] == 1


async def test_no_performance_alert_for_ordinary_price_move(session: AsyncSession) -> None:
    # A large price move must NOT raise any performance/price-move alert this slice.
    ws = await _workspace(session, "Quiet")
    apple = await _instrument(session, "Apple Co", currency="USD", ticker="AAPL")
    await _add_price(session, apple._listing_id, "100", "USD", 3)  # type: ignore[attr-defined]
    await _fund_with_holdings(
        session,
        ws,
        isin="IE00QUIE0001",
        units="100",
        price="10",
        holdings=[(apple, "0.5", "Apple Co")],
    )
    await session.commit()
    await _recompute(session, ws.id)
    await _add_price(session, apple._listing_id, "150", "USD", 0)  # +50% move
    await session.commit()
    await _recompute(session, ws.id)

    await alert_generation.generate_for_workspace(session, ws.id)
    await session.commit()
    alerts = await alerts_service.list_alerts(session, ws.id)
    assert not any("performance" in a.dedupe_key or "price_move" in a.dedupe_key for a in alerts)
