"""True constituent look-through valuation — classification, coverage, rows, API.

Builds isolated workspaces with hand-resolved/priced constituents for precise
maths, and reuses the seeded workspace (1) for the end-to-end fixture flow
(holdings -> identity -> constituent prices -> exposure_recompute). Everything is
offline — valuation is derived purely from DB rows.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from httpx import AsyncClient
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

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
from app.services import alert_generation, constituent_valuation
from app.services import alerts as alerts_service
from app.services import exposure_recompute as service
from app.sources.holdings import holding_identity_key
from app.workers.run import run_job

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
    ticker: str | None = None,
    price: str | None = None,
    price_days_ago: int = 0,
) -> Instrument:
    """A resolved instrument + primary listing (+ optional latest EOD bar)."""
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
        listing_key=f"ticker:{ticker or name}",
        ticker=ticker or name[:6].upper(),
        currency=currency,
        source="manual",
        status="active",
    )
    session.add(listing)
    await session.flush()
    if price is not None:
        session.add(
            InstrumentPrice(
                instrument_listing_id=listing.id,
                price_date=_TODAY - timedelta(days=price_days_ago),
                close=Decimal(price),
                currency=currency,
                source=_FIXTURE,
                status="fixture",
            )
        )
        await session.flush()
    return instr


async def _fund_with_holdings(
    session: AsyncSession,
    ws: Workspace,
    *,
    isin: str,
    units: str,
    price: str,
    currency: str = "GBP",
    holdings: list[tuple[Instrument | None, str, str]],
) -> Fund:
    """A fund+listing+position whose holdings link (or not) to instruments.

    ``holdings`` tuples are ``(instrument | None, weight, security_name)``."""
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


def _rows(computed, dimension: str):
    return [r for r in computed.rows if r.dimension == dimension]


def _bucket(computed, dimension: str, bucket: str):
    return next(r for r in computed.rows if r.dimension == dimension and r.bucket == bucket)


# --- classification ----------------------------------------------------------


async def test_resolved_priced_constituent_row(session: AsyncSession) -> None:
    ws = await _workspace(session, "Priced")
    apple = await _instrument(session, "Apple Co", currency="USD", ticker="AAPL", price="190")
    # GBP position worth 1000; Apple is 50% of the fund's holdings weight.
    await _fund_with_holdings(
        session,
        ws,
        isin="IE00PRICED01",
        units="100",
        price="10",
        holdings=[(apple, "0.50", "Apple Co")],
    )
    await session.commit()

    computed = await service.compute_exposure(session, ws.id)
    assert computed is not None
    row = _bucket(computed, "constituent", f"instrument:{apple.id}")
    assert row.status == constituent_valuation.STATUS_OK
    assert row.weight == Decimal("0.5000")
    # Implied value is weight-based: 1000 * 0.50 (NOT shares * price).
    assert row.market_value_base == Decimal("500.00")
    assert row.instrument_id == apple.id
    assert row.instrument_listing_id is not None
    assert row.price_date == _TODAY
    assert row.price_source == _FIXTURE
    assert row.fx_rate is not None  # USD->GBP applied
    assert row.fx_source is not None
    assert row.valuation_method == constituent_valuation.METHOD_FUND_WEIGHT_PRICED
    # Price-status funnel.
    funnel = _bucket(computed, "constituent_price_status", "priced_fresh")
    assert funnel.weight == Decimal("0.5000")


async def test_unresolved_identity_classified(session: AsyncSession) -> None:
    ws = await _workspace(session, "Unresolved")
    await _fund_with_holdings(
        session,
        ws,
        isin="IE00UNRES001",
        units="100",
        price="10",
        holdings=[(None, "0.40", "Mystery Holdco")],
    )
    await session.commit()
    computed = await service.compute_exposure(session, ws.id)
    row = _bucket(computed, "constituent", constituent_valuation.BUCKET_UNRESOLVED)
    assert row.status == constituent_valuation.STATUS_UNRESOLVED
    assert row.weight == Decimal("0.4000")
    assert computed.identity_coverage_weight == Decimal("0.0000")
    funnel = _bucket(computed, "constituent_price_status", "unresolved_identity")
    assert funnel.weight == Decimal("0.4000")


async def test_resolved_missing_price(session: AsyncSession) -> None:
    ws = await _workspace(session, "NoPrice")
    inst = await _instrument(session, "Unpriced Inc", currency="USD", ticker="UNP")  # no price
    await _fund_with_holdings(
        session,
        ws,
        isin="IE00NOPRC001",
        units="100",
        price="10",
        holdings=[(inst, "0.30", "Unpriced Inc")],
    )
    await session.commit()
    computed = await service.compute_exposure(session, ws.id)
    row = _bucket(computed, "constituent", f"instrument:{inst.id}")
    assert row.status == constituent_valuation.STATUS_MISSING_PRICE
    assert computed.identity_coverage_weight == Decimal("0.3000")
    assert computed.price_coverage_weight == Decimal("0.0000")
    assert computed.missing_constituent_price_count == 1


async def test_resolved_priced_but_fx_missing(session: AsyncSession) -> None:
    ws = await _workspace(session, "FXmiss")
    # CHF has no FX path to GBP in the seed rates -> priced, but cannot value.
    nestle = await _instrument(session, "Nestle Co", currency="CHF", ticker="NESN", price="95")
    await _fund_with_holdings(
        session,
        ws,
        isin="IE00FXMISS01",
        units="100",
        price="10",
        holdings=[(nestle, "0.20", "Nestle Co")],
    )
    await session.commit()
    computed = await service.compute_exposure(session, ws.id)
    row = _bucket(computed, "constituent", f"instrument:{nestle.id}")
    assert row.status == constituent_valuation.STATUS_FX_MISSING
    assert computed.price_coverage_weight == Decimal("0.2000")  # priced
    assert computed.fx_coverage_weight == Decimal("0.0000")  # but not FX-convertible
    assert computed.constituent_fx_missing_count == 1


async def test_stale_constituent_price(session: AsyncSession) -> None:
    ws = await _workspace(session, "Stale")
    old = await _instrument(
        session, "Stale Co", currency="USD", ticker="STL", price="10", price_days_ago=30
    )
    await _fund_with_holdings(
        session,
        ws,
        isin="IE00STALE001",
        units="100",
        price="10",
        holdings=[(old, "0.50", "Stale Co")],
    )
    await session.commit()
    computed = await service.compute_exposure(session, ws.id)
    row = _bucket(computed, "constituent", f"instrument:{old.id}")
    assert row.status == constituent_valuation.STATUS_STALE_PRICE
    assert computed.stale_constituent_price_count == 1
    # Stale still counts as priced + FX-covered (it is usable, just old).
    assert computed.price_coverage_weight == Decimal("0.5000")
    assert computed.fx_coverage_weight == Decimal("0.5000")


# --- coverage nesting + counts ----------------------------------------------


async def test_coverage_weights_nested_and_weight_based(session: AsyncSession) -> None:
    ws = await _workspace(session, "Coverage")
    priced = await _instrument(session, "Priced Co", currency="USD", ticker="PRC", price="100")
    unpriced = await _instrument(session, "Unpriced Co", currency="USD", ticker="UPR")
    await _fund_with_holdings(
        session,
        ws,
        isin="IE00COVER001",
        units="100",
        price="10",
        holdings=[
            (priced, "0.50", "Priced Co"),
            (unpriced, "0.20", "Unpriced Co"),
            (None, "0.10", "Unresolved Co"),
            # remaining 0.20 weight has no holding row -> unclassified remainder
        ],
    )
    await session.commit()
    computed = await service.compute_exposure(session, ws.id)
    # holdings >= identity >= price >= fx, all weight-based fractions of total.
    assert computed.coverage_weight == Decimal("0.8000")  # 0.5+0.2+0.1
    assert computed.identity_coverage_weight == Decimal("0.7000")  # 0.5+0.2
    assert computed.price_coverage_weight == Decimal("0.5000")  # only Priced Co
    assert computed.fx_coverage_weight == Decimal("0.5000")
    assert computed.resolved_constituent_count == 2
    assert computed.priced_constituent_count == 1
    # Price-status funnel sums to the full portfolio (~1.0).
    funnel = {r.bucket: r.weight for r in _rows(computed, "constituent_price_status")}
    assert sum(funnel.values()) == Decimal("1.0000")
    assert funnel["unclassified"] == Decimal("0.2000")


async def test_no_double_counting_same_instrument_across_funds(session: AsyncSession) -> None:
    ws = await _workspace(session, "Shared")
    apple = await _instrument(session, "Apple Shared", currency="USD", ticker="AAPL", price="190")
    # Two equal GBP positions (50% each); both hold Apple at 0.10 weight.
    await _fund_with_holdings(
        session,
        ws,
        isin="IE00SHAREA01",
        units="100",
        price="10",
        holdings=[(apple, "0.10", "Apple Shared")],
    )
    await _fund_with_holdings(
        session,
        ws,
        isin="IE00SHAREB01",
        units="100",
        price="10",
        holdings=[(apple, "0.10", "Apple Shared")],
    )
    await session.commit()
    computed = await service.compute_exposure(session, ws.id)
    apple_rows = [r for r in _rows(computed, "constituent") if r.instrument_id == apple.id]
    assert len(apple_rows) == 1  # one bucket aggregating both funds
    # 0.5*0.10 + 0.5*0.10 = 0.10 total look-through weight.
    assert apple_rows[0].weight == Decimal("0.1000")
    assert computed.resolved_constituent_count == 1  # deduped instrument count


# --- idempotency / input hash -----------------------------------------------


async def test_constituent_price_change_creates_new_snapshot(session: AsyncSession) -> None:
    ws = await _workspace(session, "Idem")
    inst = await _instrument(session, "Hash Co", currency="USD", ticker="HSH", price="100")
    await _fund_with_holdings(
        session,
        ws,
        isin="IE00HASHC001",
        units="100",
        price="10",
        holdings=[(inst, "0.50", "Hash Co")],
    )
    await session.commit()

    first = await service.recompute_workspace(session, ws.id)
    await session.commit()
    assert first.inserted == 1

    # Idempotent rerun: same constituent price -> no new snapshot. (Guards the
    # Decimal-normalisation in the input hash: a value round-tripped through a
    # Numeric column must not look "changed" vs the in-session one.)
    second = await service.recompute_workspace(session, ws.id)
    await session.commit()
    assert second.unchanged == 1

    # Change the constituent EOD price -> new snapshot.
    await session.execute(
        update(InstrumentPrice)
        .where(InstrumentPrice.currency == "USD", InstrumentPrice.source == _FIXTURE)
        .values(close=Decimal("150"))
    )
    await session.commit()
    third = await service.recompute_workspace(session, ws.id)
    await session.commit()
    assert third.inserted == 1


# --- end-to-end fixture flow (seed workspace 1) ------------------------------


async def _full_flow(session: AsyncSession, workspace_id: int) -> None:
    await run_job(session, "issuer_holdings_ingestion")
    await run_job(
        session,
        "constituent_identity_resolution",
        workspace_id=workspace_id,
        source_name="constituent_identity_fixture",
    )
    await run_job(
        session, "constituent_eod_price_ingestion", workspace_id=workspace_id, source_name=_FIXTURE
    )


async def test_worker_exposure_recompute_includes_constituents(session: AsyncSession) -> None:
    await _full_flow(session, 1)
    run = await run_job(session, "exposure_recompute", workspace_id=1)
    assert run.status == "success"
    snap = await service.get_latest_snapshot(session, 1)
    assert snap is not None
    assert snap.resolved_constituent_count >= 1
    assert snap.priced_constituent_count >= 1
    assert snap.identity_coverage_weight is not None
    assert snap.price_coverage_weight is not None
    # Nestle (CHF) + Novo (DKK) have no FX path to GBP in the seed rates.
    assert snap.constituent_fx_missing_count >= 1


async def test_exposure_api_constituent_dimensions(
    client: AsyncClient, session: AsyncSession
) -> None:
    await _full_flow(session, 1)
    await service.recompute_workspace(session, 1)
    await session.commit()

    body = (await client.get("/api/v1/workspaces/1/exposure?dimension=constituent")).json()
    assert body["rows"]
    assert all(r["dimension"] == "constituent" for r in body["rows"])
    priced = [r for r in body["rows"] if r["valuation_method"] and r["instrument_id"]]
    assert priced
    assert body["constituent_coverage"]["resolved_constituent_count"] >= 1

    funnel = (
        await client.get("/api/v1/workspaces/1/exposure?dimension=constituent_price_status")
    ).json()
    assert funnel["rows"]
    assert all(r["dimension"] == "constituent_price_status" for r in funnel["rows"])


async def test_exposure_snapshots_carry_constituent_coverage(
    client: AsyncClient, session: AsyncSession
) -> None:
    await _full_flow(session, 1)
    await service.recompute_workspace(session, 1)
    await session.commit()
    body = (await client.get("/api/v1/workspaces/1/exposure/snapshots")).json()
    snap = body["data"][0]
    assert snap["constituent_coverage"] is not None
    assert "identity_coverage_weight" in snap["constituent_coverage"]


async def test_dashboard_top_constituents_and_coverage(
    client: AsyncClient, session: AsyncSession
) -> None:
    await _full_flow(session, 1)
    await service.recompute_workspace(session, 1)
    await session.commit()
    body = (await client.get("/api/v1/workspaces/1/dashboard")).json()
    exposure = body["exposure"]
    assert exposure["top_constituents"]
    # Top constituents are real resolved instruments, not the synthetic buckets.
    assert all(r["instrument_id"] for r in exposure["top_constituents"])
    assert exposure["constituent_coverage"]["resolved_constituent_count"] >= 1


async def test_diagnostics_constituent_valuation_fields(
    client: AsyncClient, session: AsyncSession
) -> None:
    await _full_flow(session, 1)
    await service.recompute_workspace(session, 1)
    await session.commit()
    diag = (await client.get("/api/v1/workspaces/1/diagnostics")).json()
    assert "low_constituent_identity_coverage" in diag
    assert "low_constituent_price_coverage" in diag
    assert diag["constituent_valuation_fx_missing"] >= 1  # CHF/DKK constituents
    assert diag["constituent_valuation_unclassified_weight"] is not None


async def test_planner_true_lookthrough_rollup(session: AsyncSession) -> None:
    await _full_flow(session, 1)
    from app.services import market_data_planner

    plan = await market_data_planner.build_plan(session, 1, include_constituents=True)
    assert plan.summary.true_lookthrough_ready >= 1
    assert plan.summary.blocked_by_missing_fx >= 1  # CHF/DKK constituents
    # Everything resolved + priced => no remaining price work.
    assert plan.summary.blocked_by_missing_price == 0


# --- alerts (conservative) ---------------------------------------------------


async def test_no_constituent_alerts_before_resolution(session: AsyncSession) -> None:
    # Workspace 1 has holdings but no resolution yet -> the clean pre-resolution
    # state must NOT raise constituent-coverage alerts.
    await service.recompute_workspace(session, 1)
    await session.commit()
    await alert_generation.generate_for_workspace(session, 1)
    await session.commit()
    alerts = await alerts_service.list_alerts(session, 1)
    keys = {a.dedupe_key for a in alerts}
    assert not any(k.startswith("constituent_identity_coverage_low") for k in keys)
    assert not any(k.startswith("constituent_price_coverage_low") for k in keys)


async def test_constituent_fx_missing_alert(session: AsyncSession) -> None:
    ws = await _workspace(session, "AlertFX")
    # All resolved + priced, but CHF has no FX path -> one grouped FX alert.
    nestle = await _instrument(session, "Nestle Alert", currency="CHF", ticker="NESN", price="95")
    await _fund_with_holdings(
        session,
        ws,
        isin="IE00ALRTFX01",
        units="100",
        price="10",
        holdings=[(nestle, "1.00", "Nestle Alert")],
    )
    await session.commit()
    await service.recompute_workspace(session, ws.id)
    await session.commit()
    await alert_generation.generate_for_workspace(session, ws.id)
    await session.commit()
    alerts = await alerts_service.list_alerts(session, ws.id, category="fx")
    assert any(a.dedupe_key == f"constituent_valuation_fx_missing:{ws.id}" for a in alerts)


async def test_low_constituent_price_coverage_alert(session: AsyncSession) -> None:
    ws = await _workspace(session, "AlertCov")
    # Resolved but unpriced => identity coverage high, price coverage 0 -> alert.
    unpriced = await _instrument(session, "Unpriced Alert", currency="USD", ticker="UPA")
    await _fund_with_holdings(
        session,
        ws,
        isin="IE00ALRTCV01",
        units="100",
        price="10",
        holdings=[(unpriced, "1.00", "Unpriced Alert")],
    )
    await session.commit()
    await service.recompute_workspace(session, ws.id)
    await session.commit()
    await alert_generation.generate_for_workspace(session, ws.id)
    await session.commit()
    alerts = await alerts_service.list_alerts(session, ws.id, category="exposure")
    keys = {a.dedupe_key for a in alerts}
    assert f"constituent_price_coverage_low:{ws.id}" in keys
