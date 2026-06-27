"""Exposure drift — compare two snapshots: deltas, top movers, price-context, API.

All offline. Each test builds an isolated workspace, recomputes one exposure
snapshot, mutates an input (weight / price / holding membership / FX), then
recomputes again — the previous-slice input-hash idempotency guarantees a *new*
snapshot only when an input actually changed.
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
from app.services import diagnostics as diagnostics_service
from app.services import exposure_drift as drift
from app.services import exposure_recompute as service
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
    session: AsyncSession,
    name: str,
    *,
    currency: str,
    ticker: str,
    price: str | None = None,
    price_days_ago: int = 0,
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
    instr._listing_id = listing.id  # type: ignore[attr-defined]  # test convenience
    if price is not None:
        await _add_price(session, listing.id, price, currency, price_days_ago)
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
    """holdings tuples: (instrument|None, weight, name[, sector[, country]])."""
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
    for h in holdings:
        instr, weight, name = h[0], h[1], h[2]
        sector = h[3] if len(h) > 3 else None
        country = h[4] if len(h) > 4 else None
        session.add(
            FundHolding(
                fund_id=fund.id,
                as_of_date=_TODAY,
                security_name=name,
                sector=sector,
                country=country,
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
    assert res.snapshot_id is not None  # an input changed -> a snapshot was written
    return res.snapshot_id


def _row(resp, key: str):
    return next(r for r in resp.rows if r.key == key)


# --- snapshot selection / insufficient history -------------------------------


async def test_insufficient_history_single_snapshot(session: AsyncSession) -> None:
    ws = await _workspace(session, "Solo")
    apple = await _instrument(session, "Apple Co", currency="USD", ticker="AAPL", price="190")
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

    out = await drift.compute_drift(session, ws.id, dimension="constituent")
    assert out.status == "insufficient_history"
    assert out.rows == []
    assert out.comparison_snapshot_id is not None
    assert out.base_snapshot_id is None


async def test_no_snapshots_at_all(session: AsyncSession) -> None:
    ws = await _workspace(session, "Empty")
    await session.commit()
    out = await drift.compute_drift(session, ws.id, dimension="constituent")
    assert out.status == "insufficient_history"
    assert out.comparison_snapshot_id is None


async def test_latest_vs_previous_default_selection(session: AsyncSession) -> None:
    ws = await _workspace(session, "Two")
    apple = await _instrument(session, "Apple Co", currency="USD", ticker="AAPL", price="190")
    await _fund_with_holdings(
        session,
        ws,
        isin="IE00TWO00001",
        units="100",
        price="10",
        holdings=[(apple, "0.5", "Apple Co")],
    )
    await session.commit()
    snap1 = await _recompute(session, ws.id)
    # Bump Apple's weight -> a new snapshot.
    await session.execute(
        update(FundHolding)
        .where(FundHolding.security_name == "Apple Co")
        .values(weight=Decimal("0.7"))
    )
    await session.commit()
    snap2 = await _recompute(session, ws.id)

    out = await drift.compute_drift(session, ws.id, dimension="constituent")
    assert out.status == "ok"
    assert out.base_snapshot_id == snap1
    assert out.comparison_snapshot_id == snap2


async def test_explicit_snapshot_ids(session: AsyncSession) -> None:
    ws = await _workspace(session, "Explicit")
    apple = await _instrument(session, "Apple Co", currency="USD", ticker="AAPL", price="190")
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
    await session.execute(
        update(FundHolding)
        .where(FundHolding.security_name == "Apple Co")
        .values(weight=Decimal("0.7"))
    )
    await session.commit()
    snap2 = await _recompute(session, ws.id)

    out = await drift.compute_drift(
        session,
        ws.id,
        dimension="constituent",
        base_snapshot_id=snap1,
        comparison_snapshot_id=snap2,
    )
    assert out.base_snapshot_id == snap1 and out.comparison_snapshot_id == snap2
    # Reversed: base/comparison swapped -> deltas invert sign.
    reversed_out = await drift.compute_drift(
        session,
        ws.id,
        dimension="constituent",
        base_snapshot_id=snap2,
        comparison_snapshot_id=snap1,
    )
    apple_fwd = _row(out, f"instrument:{apple.id}")
    apple_rev = _row(reversed_out, f"instrument:{apple.id}")
    assert apple_fwd.delta_weight == Decimal("0.2000")
    assert apple_rev.delta_weight == Decimal("-0.2000")


async def test_cross_workspace_comparison_rejected(session: AsyncSession) -> None:
    ws_a = await _workspace(session, "A")
    ws_b = await _workspace(session, "B")
    a_apple = await _instrument(session, "Apple A", currency="USD", ticker="AAPL", price="190")
    b_apple = await _instrument(session, "Apple B", currency="USD", ticker="MSFT", price="200")
    await _fund_with_holdings(
        session,
        ws_a,
        isin="IE00WSA00001",
        units="100",
        price="10",
        holdings=[(a_apple, "0.5", "Apple A")],
    )
    await _fund_with_holdings(
        session,
        ws_b,
        isin="IE00WSB00001",
        units="100",
        price="10",
        holdings=[(b_apple, "0.5", "Apple B")],
    )
    await session.commit()
    await _recompute(session, ws_a.id)
    b_snap = await _recompute(session, ws_b.id)

    # Asking workspace A to compare against workspace B's snapshot must 404.
    with pytest.raises(NotFoundError):
        await drift.compute_drift(session, ws_a.id, comparison_snapshot_id=b_snap)


# --- row classification ------------------------------------------------------


async def test_increased_decreased_appeared_disappeared(session: AsyncSession) -> None:
    ws = await _workspace(session, "Kinds")
    apple = await _instrument(session, "Apple Co", currency="USD", ticker="AAPL", price="190")
    shell = await _instrument(session, "Shell Co", currency="GBP", ticker="SHEL", price="28")
    msft = await _instrument(session, "MSFT Co", currency="USD", ticker="MSFT", price="400")
    fund = await _fund_with_holdings(
        session,
        ws,
        isin="IE00KIND0001",
        units="100",
        price="10",
        holdings=[(apple, "0.4", "Apple Co"), (shell, "0.3", "Shell Co")],
    )
    await session.commit()
    await _recompute(session, ws.id)

    # Apple up, Shell removed (disappeared), MSFT added (appeared).
    await session.execute(
        update(FundHolding)
        .where(FundHolding.security_name == "Apple Co")
        .values(weight=Decimal("0.5"))
    )
    await session.execute(delete(FundHolding).where(FundHolding.security_name == "Shell Co"))
    session.add(
        FundHolding(
            fund_id=fund.id,
            as_of_date=_TODAY,
            security_name="MSFT Co",
            weight=Decimal("0.2"),
            source="holdings_fixture",
            holding_key=holding_identity_key(name="MSFT Co"),
            holding_instrument_id=msft.id,
            identity_status="resolved",
        )
    )
    await session.commit()
    await _recompute(session, ws.id)

    out = await drift.compute_drift(session, ws.id, dimension="constituent")
    assert _row(out, f"instrument:{apple.id}").change_kind == "increased"
    assert _row(out, f"instrument:{shell.id}").change_kind == "disappeared"
    assert _row(out, f"instrument:{msft.id}").change_kind == "appeared"
    assert _row(out, f"instrument:{apple.id}").delta_weight == Decimal("0.1000")
    assert _row(out, f"instrument:{shell.id}").comparison_weight == Decimal("0.0000")
    assert _row(out, f"instrument:{msft.id}").base_weight == Decimal("0.0000")


async def test_unchanged_and_totals_and_match_by_instrument(session: AsyncSession) -> None:
    ws = await _workspace(session, "Totals")
    apple = await _instrument(session, "Apple Co", currency="USD", ticker="AAPL", price="190")
    shell = await _instrument(session, "Shell Co", currency="GBP", ticker="SHEL", price="28")
    await _fund_with_holdings(
        session,
        ws,
        isin="IE00TOTL0001",
        units="100",
        price="10",
        holdings=[(apple, "0.4", "Apple Co"), (shell, "0.3", "Shell Co")],
    )
    await session.commit()
    await _recompute(session, ws.id)
    # Move 0.1 weight from the unclassified remainder into Apple (Shell unchanged).
    await session.execute(
        update(FundHolding)
        .where(FundHolding.security_name == "Apple Co")
        .values(weight=Decimal("0.5"))
    )
    await session.commit()
    await _recompute(session, ws.id)

    out = await drift.compute_drift(session, ws.id, dimension="constituent")
    # Matched by instrument_id (key uses the resolved instrument).
    assert _row(out, f"instrument:{shell.id}").change_kind == "unchanged"
    assert _row(out, f"instrument:{apple.id}").instrument_id == apple.id
    # Apple +0.1, unclassified remainder -0.1 => total abs weight delta = 0.2.
    assert out.summary.total_abs_weight_delta == Decimal("0.2000")
    # 1000 total * 0.1 = 100 implied value moved into Apple, 100 out of unclassified.
    assert out.summary.total_abs_market_value_delta_base == Decimal("200.00")


async def test_status_changed_row(session: AsyncSession) -> None:
    ws = await _workspace(session, "Status")
    # Fresh price now; a stale price after re-dating the only bar to long ago.
    apple = await _instrument(session, "Apple Co", currency="USD", ticker="AAPL", price="190")
    await _fund_with_holdings(
        session,
        ws,
        isin="IE00STAT0001",
        units="100",
        price="10",
        holdings=[(apple, "0.5", "Apple Co")],
    )
    await session.commit()
    await _recompute(session, ws.id)
    # Age the price out of the freshness window (status ok -> stale_price), no weight change.
    await session.execute(
        update(InstrumentPrice)
        .where(InstrumentPrice.currency == "USD")
        .values(price_date=_TODAY - timedelta(days=60))
    )
    await session.commit()
    await _recompute(session, ws.id)

    out = await drift.compute_drift(session, ws.id, dimension="constituent")
    apple_row = _row(out, f"instrument:{apple.id}")
    assert apple_row.change_kind == "status_changed"
    assert apple_row.base_status == "ok"
    assert apple_row.comparison_status == "stale_price"
    assert apple_row.status_change is True


# --- coverage deltas + sort + limit ------------------------------------------


async def test_coverage_deltas(session: AsyncSession) -> None:
    ws = await _workspace(session, "Cov")
    apple = await _instrument(session, "Apple Co", currency="USD", ticker="AAPL", price="190")
    await _fund_with_holdings(
        session,
        ws,
        isin="IE00COV00001",
        units="100",
        price="10",
        holdings=[(apple, "0.5", "Apple Co")],
    )
    await session.commit()
    await _recompute(session, ws.id)
    # Age the price -> still priced (price coverage holds) but stale.
    await session.execute(
        update(InstrumentPrice)
        .where(InstrumentPrice.currency == "USD")
        .values(close=Decimal("250"))
    )
    await session.commit()
    await _recompute(session, ws.id)
    out = await drift.compute_drift(session, ws.id, dimension="constituent")
    assert out.summary.base_coverage is not None
    assert out.summary.comparison_coverage is not None
    assert out.summary.identity_coverage_delta == Decimal("0.0000")


async def test_sort_and_limit(session: AsyncSession) -> None:
    ws = await _workspace(session, "Sort")
    big = await _instrument(session, "Big Co", currency="GBP", ticker="BIG", price="10")
    small = await _instrument(session, "Small Co", currency="GBP", ticker="SML", price="10")
    await _fund_with_holdings(
        session,
        ws,
        isin="IE00SORT0001",
        units="100",
        price="10",
        holdings=[(big, "0.3", "Big Co"), (small, "0.3", "Small Co")],
    )
    await session.commit()
    await _recompute(session, ws.id)
    await session.execute(
        update(FundHolding)
        .where(FundHolding.security_name == "Big Co")
        .values(weight=Decimal("0.6"))
    )
    await session.execute(
        update(FundHolding)
        .where(FundHolding.security_name == "Small Co")
        .values(weight=Decimal("0.35"))
    )
    await session.commit()
    await _recompute(session, ws.id)

    out = await drift.compute_drift(
        session, ws.id, dimension="constituent", sort="abs_delta_weight", limit=1, movers_only=True
    )
    assert len(out.rows) == 1
    assert out.rows[0].key == f"instrument:{big.id}"  # biggest absolute weight move first


# --- price-context contribution ----------------------------------------------


async def test_price_context_contribution(session: AsyncSession) -> None:
    ws = await _workspace(session, "Perf")
    # Apple priced 100 yesterday; weight stays flat across snapshots.
    apple = await _instrument(
        session, "Apple Co", currency="USD", ticker="AAPL", price="100", price_days_ago=1
    )
    listing_id = apple._listing_id  # type: ignore[attr-defined]
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
    # Add a NEW, newer bar at 120 -> the comparison snapshot references it.
    await _add_price(session, listing_id, "120", "USD", days_ago=0)
    await session.commit()
    await _recompute(session, ws.id)

    out = await drift.compute_drift(session, ws.id, dimension="constituent")
    apple_row = _row(out, f"instrument:{apple.id}")
    assert apple_row.base_price == Decimal("100")
    assert apple_row.comparison_price == Decimal("120")
    assert apple_row.price_return == Decimal("0.200000")
    # base implied value (1000 * 0.5 = 500) * 0.2 = 100 price-context contribution.
    assert apple_row.price_context_contribution_base == Decimal("100.00")
    assert out.summary.total_price_context_contribution_base == Decimal("100.00")


async def test_price_context_absent_for_non_constituent(session: AsyncSession) -> None:
    ws = await _workspace(session, "Sector")
    apple = await _instrument(session, "Apple Co", currency="USD", ticker="AAPL", price="190")
    await _fund_with_holdings(
        session,
        ws,
        isin="IE00SECT0001",
        units="100",
        price="10",
        holdings=[(apple, "0.5", "Apple Co", "Technology", "US")],
    )
    await session.commit()
    await _recompute(session, ws.id)
    await session.execute(
        update(FundHolding)
        .where(FundHolding.security_name == "Apple Co")
        .values(weight=Decimal("0.7"))
    )
    await session.commit()
    await _recompute(session, ws.id)

    out = await drift.compute_drift(session, ws.id, dimension="sector")
    tech = _row(out, "Technology")
    assert tech.price_context_contribution_base is None
    assert tech.delta_weight == Decimal("0.2000")


# --- API ---------------------------------------------------------------------


async def _seed_two_snapshots(session: AsyncSession, ws_id: int) -> None:
    """Resolve + price + recompute, mutate a weight, recompute again (workspace 1)."""
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
    # Nudge a holding weight to force a second snapshot.
    await session.execute(
        update(FundHolding)
        .where(FundHolding.security_name == "Apple Inc")
        .values(weight=Decimal("0.20000000"))
    )
    await session.commit()
    await service.recompute_workspace(session, ws_id)
    await session.commit()


async def test_api_drift_default(client: AsyncClient, session: AsyncSession) -> None:
    await _seed_two_snapshots(session, 1)
    body = (await client.get("/api/v1/workspaces/1/exposure/drift?dimension=constituent")).json()
    assert body["status"] == "ok"
    assert body["dimension"] == "constituent"
    assert body["base_snapshot_id"] and body["comparison_snapshot_id"]
    assert body["summary"]["total_abs_weight_delta"]
    assert any(r["change_kind"] in {"increased", "decreased"} for r in body["rows"])


async def test_api_drift_insufficient_history(client: AsyncClient, session: AsyncSession) -> None:
    await service.recompute_workspace(session, 1)
    await session.commit()
    body = (await client.get("/api/v1/workspaces/1/exposure/drift?dimension=constituent")).json()
    assert body["status"] == "insufficient_history"
    assert body["rows"] == []


async def test_api_top_movers(client: AsyncClient, session: AsyncSession) -> None:
    await _seed_two_snapshots(session, 1)
    body = (
        await client.get("/api/v1/workspaces/1/exposure/top-movers?dimension=constituent&limit=5")
    ).json()
    assert body["status"] == "ok"
    assert len(body["rows"]) <= 5
    assert all(r["change_kind"] != "unchanged" for r in body["rows"])
    # Synthetic buckets are excluded from movers.
    assert all(not r["bucket"].startswith("__") for r in body["rows"])


async def test_api_drift_invalid_snapshot_id(client: AsyncClient, session: AsyncSession) -> None:
    await _seed_two_snapshots(session, 1)
    resp = await client.get(
        "/api/v1/workspaces/1/exposure/drift?dimension=constituent&comparison_snapshot_id=999999"
    )
    assert resp.status_code == 404


async def test_api_dashboard_drift_summary(client: AsyncClient, session: AsyncSession) -> None:
    await _seed_two_snapshots(session, 1)
    body = (await client.get("/api/v1/workspaces/1/dashboard")).json()
    drift_block = body["exposure"]["drift"]
    assert drift_block["status"] == "ok"
    assert drift_block["comparison_snapshot_id"]
    assert drift_block["total_abs_constituent_weight_delta"] is not None


async def test_api_dashboard_drift_insufficient_history(
    client: AsyncClient, session: AsyncSession
) -> None:
    await service.recompute_workspace(session, 1)
    await session.commit()
    body = (await client.get("/api/v1/workspaces/1/dashboard")).json()
    assert body["exposure"]["drift"]["status"] == "insufficient_history"


# --- diagnostics + alerts ----------------------------------------------------


async def _big_drift_workspace(session: AsyncSession) -> Workspace:
    """A workspace whose constituent exposure swings hard between snapshots."""
    ws = await _workspace(session, "BigDrift")
    a = await _instrument(session, "Alpha Co", currency="GBP", ticker="ALP", price="10")
    b = await _instrument(session, "Beta Co", currency="GBP", ticker="BET", price="10")
    await _fund_with_holdings(
        session,
        ws,
        isin="IE00BIGD0001",
        units="100",
        price="10",
        holdings=[(a, "0.8", "Alpha Co"), (b, "0.1", "Beta Co", "Energy", "GB")],
    )
    await session.commit()
    await _recompute(session, ws.id)
    # Swap the weights -> ~1.4 absolute weight moved (huge drift).
    await session.execute(
        update(FundHolding)
        .where(FundHolding.security_name == "Alpha Co")
        .values(weight=Decimal("0.1"))
    )
    await session.execute(
        update(FundHolding)
        .where(FundHolding.security_name == "Beta Co")
        .values(weight=Decimal("0.8"))
    )
    await session.commit()
    await _recompute(session, ws.id)
    return ws


async def test_diagnostics_large_drift(client: AsyncClient, session: AsyncSession) -> None:
    ws = await _big_drift_workspace(session)
    diag = (await client.get(f"/api/v1/workspaces/{ws.id}/diagnostics")).json()
    assert diag["large_constituent_exposure_drift"] == 1
    assert diag["no_prior_exposure_snapshot_for_drift"] == 0


async def test_diagnostics_no_prior_snapshot(client: AsyncClient, session: AsyncSession) -> None:
    ws = await _workspace(session, "OnePrior")
    apple = await _instrument(session, "Apple Co", currency="USD", ticker="AAPL", price="190")
    await _fund_with_holdings(
        session,
        ws,
        isin="IE00ONEP0001",
        units="100",
        price="10",
        holdings=[(apple, "0.5", "Apple Co")],
    )
    await session.commit()
    await _recompute(session, ws.id)
    diag = (await client.get(f"/api/v1/workspaces/{ws.id}/diagnostics")).json()
    assert diag["no_prior_exposure_snapshot_for_drift"] == 1
    assert diag["large_constituent_exposure_drift"] == 0


async def test_alert_large_drift_and_autoresolve(session: AsyncSession) -> None:
    ws = await _big_drift_workspace(session)
    await alert_generation.generate_for_workspace(session, ws.id)
    await session.commit()
    alerts = await alerts_service.list_alerts(session, ws.id, category="exposure")
    keys = {a.dedupe_key for a in alerts}
    assert f"exposure_drift_constituent:{ws.id}" in keys

    # A tiny subsequent change -> the *latest vs previous* drift is now small
    # (below threshold) -> the alert auto-resolves on the next generation.
    await session.execute(
        update(FundHolding)
        .where(FundHolding.security_name == "Alpha Co")
        .values(weight=Decimal("0.11"))
    )
    await session.commit()
    await service.recompute_workspace(session, ws.id)
    await session.commit()
    await alert_generation.generate_for_workspace(session, ws.id)
    await session.commit()
    alerts = await alerts_service.list_alerts(session, ws.id, category="exposure")
    drift_alert = next(a for a in alerts if a.dedupe_key == f"exposure_drift_constituent:{ws.id}")
    assert drift_alert.status == "resolved"


async def test_no_drift_alert_without_prior_snapshot(session: AsyncSession) -> None:
    ws = await _workspace(session, "Quiet")
    apple = await _instrument(session, "Apple Co", currency="USD", ticker="AAPL", price="190")
    await _fund_with_holdings(
        session,
        ws,
        isin="IE00QUIET001",
        units="100",
        price="10",
        holdings=[(apple, "0.5", "Apple Co")],
    )
    await session.commit()
    await _recompute(session, ws.id)  # one snapshot only
    await alert_generation.generate_for_workspace(session, ws.id)
    await session.commit()
    alerts = await alerts_service.list_alerts(session, ws.id)
    assert not any(a.dedupe_key.startswith("exposure_drift") for a in alerts)


async def test_global_diagnostics_smoke(session: AsyncSession) -> None:
    # The global diagnostics path iterates every workspace's drift; ensure it runs.
    await _big_drift_workspace(session)
    diag = await diagnostics_service.global_diagnostics(session)
    assert diag.large_constituent_exposure_drift >= 1
