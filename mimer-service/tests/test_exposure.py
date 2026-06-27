"""Derived exposure: computation, input hash, idempotent recompute, worker, API.

Tests build their own isolated workspaces/positions/holdings for precise maths,
and reuse the seeded workspace (1) for the worker/endpoint/dashboard/diagnostics
paths. Everything is offline — exposure is derived purely from DB rows.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from httpx import AsyncClient
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    ExposureSnapshot,
    Fund,
    FundHolding,
    FundListing,
    JobRun,
    PortfolioPosition,
    Price,
    ScheduledJob,
    Workspace,
)
from app.services import alert_generation, alert_rules
from app.services import alerts as alerts_service
from app.services import exposure_recompute as service
from app.sources.holdings import holding_identity_key
from app.workers.run import run_job

_TODAY = date.today()


# --- helpers -----------------------------------------------------------------


async def _workspace(session: AsyncSession, name: str, base: str = "GBP") -> Workspace:
    ws = Workspace(name=name, base_currency=base)
    session.add(ws)
    await session.flush()
    return ws


async def _add_position(
    session: AsyncSession,
    ws: Workspace,
    *,
    isin: str,
    units: str,
    price: str | None,
    currency: str = "GBP",
    holdings: list[tuple] | None = None,
) -> Fund:
    """Add a fund+listing+position (+optional holdings). holdings tuples:
    (name, country, sector, weight[, holding_currency[, industry]])."""
    prices = []
    if price is not None:
        prices = [Price(price_date=_TODAY, price=Decimal(price), currency=currency, source="stooq")]
    listing = FundListing(
        ticker=isin[-4:],
        trading_currency=currency,
        currency_unit=currency,
        status="active",
        prices=prices,
    )
    fund = Fund(isin=isin, name=f"Fund {isin}", status="active", source="seed", listings=[listing])
    session.add(fund)
    await session.flush()
    session.add(
        PortfolioPosition(workspace_id=ws.id, fund_listing_id=listing.id, units=Decimal(units))
    )
    for h in holdings or []:
        name, country, sector, weight = h[0], h[1], h[2], h[3]
        hccy = h[4] if len(h) > 4 else None
        industry = h[5] if len(h) > 5 else None
        session.add(
            FundHolding(
                fund_id=fund.id,
                as_of_date=_TODAY,
                security_name=name,
                country=country,
                sector=sector,
                industry=industry,
                currency=hccy,
                weight=Decimal(weight),
                source="holdings_fixture",
                holding_key=holding_identity_key(name=name),
            )
        )
    await session.flush()
    return fund


def _by_bucket(rows, dimension: str) -> dict[str, Decimal]:
    return {r.bucket: r.weight for r in rows if r.dimension == dimension}


def _rows_for(computed, dimension: str):
    return [r for r in computed.rows if r.dimension == dimension]


# --- input hash --------------------------------------------------------------


async def test_input_hash_deterministic_and_input_sensitive(session: AsyncSession) -> None:
    ws = await _workspace(session, "Hash")
    await _add_position(
        session,
        ws,
        isin="IE00HASH0001",
        units="100",
        price="10",
        holdings=[("Apple", "US", "Technology", "0.5")],
    )
    await session.commit()

    a = await service.compute_exposure(session, ws.id)
    b = await service.compute_exposure(session, ws.id)
    assert a is not None and b is not None
    assert a.input_hash == b.input_hash
    assert a.position_snapshot_hash == b.position_snapshot_hash

    # Changing units must change the input hash.
    await session.execute(
        update(PortfolioPosition)
        .where(PortfolioPosition.workspace_id == ws.id)
        .values(units=Decimal("200"))
    )
    await session.commit()
    c = await service.compute_exposure(session, ws.id)
    assert c is not None and c.input_hash != a.input_hash


# --- computation rules -------------------------------------------------------


async def test_single_position_country_sector_coverage(session: AsyncSession) -> None:
    ws = await _workspace(session, "Solo")
    await _add_position(
        session,
        ws,
        isin="IE00SOLO0001",
        units="100",
        price="10",
        holdings=[
            ("Apple", "US", "Technology", "0.07"),
            ("Shell", "GB", "Energy", "0.05"),
        ],
    )
    await session.commit()

    computed = await service.compute_exposure(session, ws.id)
    assert computed is not None
    country = _by_bucket(computed.rows, "country")
    sector = _by_bucket(computed.rows, "sector")
    assert country["US"] == Decimal("0.0700")
    assert country["GB"] == Decimal("0.0500")
    assert sector["Technology"] == Decimal("0.0700")
    # Look-through covers 12%; the rest is Unclassified.
    assert computed.coverage_weight == Decimal("0.1200")
    assert computed.unclassified_weight == Decimal("0.8800")
    assert country["Unclassified"] == Decimal("0.8800")
    assert computed.status == "partial"  # coverage < 1


async def test_lookthrough_multiplies_position_and_holding_weight(session: AsyncSession) -> None:
    ws = await _workspace(session, "Two")
    # Two GBP positions with identical market value => 50% each.
    await _add_position(
        session,
        ws,
        isin="IE00TWOA0001",
        units="100",
        price="10",
        holdings=[("Apple", "US", "Technology", "0.10")],
    )
    await _add_position(
        session,
        ws,
        isin="IE00TWOB0001",
        units="100",
        price="10",
        holdings=[("Shell", "GB", "Energy", "0.10")],
    )
    await session.commit()

    computed = await service.compute_exposure(session, ws.id)
    assert computed is not None
    country = _by_bucket(computed.rows, "country")
    assert country["US"] == Decimal("0.0500")  # 0.5 * 0.10
    assert country["GB"] == Decimal("0.0500")
    fund = _by_bucket(computed.rows, "fund")
    assert fund["IE00TWOA0001"] == Decimal("0.5000")
    assert fund["IE00TWOB0001"] == Decimal("0.5000")


async def test_top_holding_exposure(session: AsyncSession) -> None:
    ws = await _workspace(session, "Top")
    await _add_position(
        session,
        ws,
        isin="IE00TOPH0001",
        units="100",
        price="10",
        holdings=[("Apple Inc", "US", "Technology", "0.20")],
    )
    await session.commit()
    computed = await service.compute_exposure(session, ws.id)
    holding = _rows_for(computed, "holding")
    apple = next(r for r in holding if r.label == "Apple Inc")
    assert apple.weight == Decimal("0.2000")
    assert apple.market_value_base == Decimal("200.00")  # 1000 * 0.20


async def test_currency_lookthrough_and_fallback(session: AsyncSession) -> None:
    ws = await _workspace(session, "Ccy")
    # Holding carries USD currency for 0.5; remainder falls back to listing GBP.
    await _add_position(
        session,
        ws,
        isin="IE00CCY00001",
        units="100",
        price="10",
        currency="GBP",
        holdings=[("Apple", "US", "Technology", "0.50", "USD")],
    )
    await session.commit()
    computed = await service.compute_exposure(session, ws.id)
    currency = _by_bucket(computed.rows, "currency")
    assert currency["USD"] == Decimal("0.5000")
    assert currency["GBP"] == Decimal("0.5000")  # remainder fallback
    usd_row = next(r for r in computed.rows if r.dimension == "currency" and r.bucket == "USD")
    gbp_row = next(r for r in computed.rows if r.dimension == "currency" and r.bucket == "GBP")
    assert usd_row.status == "ok"
    assert gbp_row.status == "approximate"


async def test_missing_holdings_unclassified_bucket(session: AsyncSession) -> None:
    ws = await _workspace(session, "NoHold")
    await _add_position(session, ws, isin="IE00NOHO0001", units="100", price="10", holdings=[])
    await session.commit()
    computed = await service.compute_exposure(session, ws.id)
    assert computed.missing_holdings_count == 1
    assert computed.coverage_weight == Decimal("0.0000")
    assert computed.unclassified_weight == Decimal("1.0000")
    country = _rows_for(computed, "country")
    uncl = next(r for r in country if r.bucket == "Unclassified")
    assert uncl.weight == Decimal("1.0000")
    assert uncl.status == "missing_holdings"


async def test_missing_fx_marks_status(session: AsyncSession) -> None:
    ws = await _workspace(session, "FXmiss")
    # One valued GBP position + one JPY position with no FX path to GBP.
    await _add_position(
        session,
        ws,
        isin="IE00FXGB0001",
        units="100",
        price="10",
        currency="GBP",
        holdings=[("Apple", "US", "Technology", "0.10")],
    )
    await _add_position(session, ws, isin="IE00FXJP0001", units="100", price="10", currency="JPY")
    await session.commit()

    computed = await service.compute_exposure(session, ws.id)
    assert computed.missing_fx_count == 1
    assert computed.status == "partial"
    fund_rows = _rows_for(computed, "fund")
    jpy = next(r for r in fund_rows if r.bucket == "IE00FXJP0001")
    assert jpy.status == "fx_missing"
    assert jpy.market_value_base is None
    assert jpy.weight == Decimal("0.0000")


# --- idempotent persistence --------------------------------------------------


async def _count_snapshots(session: AsyncSession, ws_id: int) -> int:
    return len(
        (
            await session.execute(
                select(ExposureSnapshot).where(ExposureSnapshot.workspace_id == ws_id)
            )
        )
        .scalars()
        .all()
    )


async def test_recompute_inserts_then_idempotent(session: AsyncSession) -> None:
    ws = await _workspace(session, "Idem")
    await _add_position(
        session,
        ws,
        isin="IE00IDEM0001",
        units="100",
        price="10",
        holdings=[("Apple", "US", "Technology", "0.10")],
    )
    await session.commit()

    first = await service.recompute_workspace(session, ws.id)
    await session.commit()
    assert first.inserted == 1
    assert await _count_snapshots(session, ws.id) == 1

    second = await service.recompute_workspace(session, ws.id)
    await session.commit()
    assert second.inserted == 0
    assert second.unchanged == 1
    assert await _count_snapshots(session, ws.id) == 1


async def test_changed_input_creates_new_snapshot(session: AsyncSession) -> None:
    ws = await _workspace(session, "Changed")
    await _add_position(
        session,
        ws,
        isin="IE00CHNG0001",
        units="100",
        price="10",
        holdings=[("Apple", "US", "Technology", "0.10")],
    )
    await session.commit()
    await service.recompute_workspace(session, ws.id)
    await session.commit()

    await session.execute(
        update(PortfolioPosition)
        .where(PortfolioPosition.workspace_id == ws.id)
        .values(units=Decimal("250"))
    )
    await session.commit()
    res = await service.recompute_workspace(session, ws.id)
    await session.commit()
    assert res.inserted == 1
    assert await _count_snapshots(session, ws.id) == 2
    latest = await service.get_latest_snapshot(session, ws.id)
    assert latest is not None and latest.id == res.snapshot_id


async def test_recompute_no_positions_skipped(session: AsyncSession) -> None:
    ws = await _workspace(session, "Empty")
    await session.commit()
    res = await service.recompute_workspace(session, ws.id)
    await session.commit()
    assert res.skipped == 1
    assert await _count_snapshots(session, ws.id) == 0


# --- worker ------------------------------------------------------------------


async def test_worker_single_workspace(session: AsyncSession) -> None:
    run = await run_job(session, "exposure_recompute", workspace_id=1)
    assert run.job_type == "exposure_recompute"
    assert run.status == "success"
    assert (run.records_inserted or 0) >= 1
    assert "workspaces=1" in (run.message or "")


async def test_worker_all_workspaces_then_idempotent(session: AsyncSession) -> None:
    run = await run_job(session, "exposure_recompute")
    assert run.status == "success"
    assert (run.records_inserted or 0) >= 1
    rerun = await run_job(session, "exposure_recompute")
    assert rerun.status == "success"
    assert rerun.records_inserted == 0
    assert "unchanged=" in (rerun.message or "")


async def test_worker_isolates_workspace_failure(session: AsyncSession, monkeypatch) -> None:
    await _workspace(session, "Second")
    await session.commit()
    ids = await service.active_workspace_ids(session)
    assert len(ids) >= 2

    real = service.recompute_workspace

    async def flaky(sess, wid):
        if wid == ids[0]:
            raise RuntimeError("boom")
        return await real(sess, wid)

    monkeypatch.setattr("app.workers.run.exposure_service.recompute_workspace", flaky)
    run = await run_job(session, "exposure_recompute")
    assert run.records_failed == 1
    assert run.status == "partial_success"


# --- job trigger wiring ------------------------------------------------------


async def test_trigger_exposure_recompute_is_real(
    client: AsyncClient, session: AsyncSession
) -> None:
    session.add(
        ScheduledJob(name="nightly_exposure", job_type="exposure_recompute", is_active=True)
    )
    await session.commit()
    job = next(
        j
        for j in (await client.get("/api/v1/jobs")).json()["data"]
        if j["job_type"] == "exposure_recompute"
    )
    assert job["implementation_status"] == "real"
    run = await client.post(f"/api/v1/jobs/{job['id']}/run")
    assert run.status_code == 201
    assert run.json()["status"] == "success"


# --- API ---------------------------------------------------------------------


async def test_exposure_endpoint_shape_and_filter(
    client: AsyncClient, session: AsyncSession
) -> None:
    await service.recompute_workspace(session, 1)
    await session.commit()

    body = (await client.get("/api/v1/workspaces/1/exposure")).json()
    assert body["cached"] is True
    assert body["snapshot_id"] is not None
    assert body["base_currency"] == "GBP"
    assert "fund" in body["dimensions"]
    assert any(r["dimension"] == "sector" for r in body["rows"])

    sector = (await client.get("/api/v1/workspaces/1/exposure?dimension=sector")).json()
    assert sector["rows"]
    assert all(r["dimension"] == "sector" for r in sector["rows"])

    limited = (await client.get("/api/v1/workspaces/1/exposure?dimension=holding&limit=2")).json()
    assert len(limited["rows"]) <= 2


async def test_exposure_endpoint_uncached_fallback(client: AsyncClient) -> None:
    # Workspace 1 has positions but no snapshot yet -> on-the-fly, flagged.
    body = (await client.get("/api/v1/workspaces/1/exposure")).json()
    assert body["cached"] is False
    assert body["status"] == "recompute_needed"
    assert body["rows"]


async def test_exposure_snapshots_endpoint(client: AsyncClient, session: AsyncSession) -> None:
    await service.recompute_workspace(session, 1)
    await session.commit()
    body = (await client.get("/api/v1/workspaces/1/exposure/snapshots")).json()
    assert body["meta"]["count"] >= 1
    snap = body["data"][0]
    assert snap["row_count"] >= 1
    assert snap["input_hash"]


# --- dashboard / diagnostics -------------------------------------------------


async def test_dashboard_uses_latest_exposure_snapshot(
    client: AsyncClient, session: AsyncSession
) -> None:
    before = (await client.get("/api/v1/workspaces/1/dashboard")).json()
    assert before["exposure"]["status"] == "recompute_needed"
    assert before["exposure"]["cached"] is False

    await service.recompute_workspace(session, 1)
    await session.commit()

    after = (await client.get("/api/v1/workspaces/1/dashboard")).json()
    assert after["exposure"]["cached"] is True
    assert after["exposure"]["snapshot_id"] is not None
    assert after["exposure"]["top_sectors"]


async def test_diagnostics_exposure_fields(client: AsyncClient, session: AsyncSession) -> None:
    missing = (await client.get("/api/v1/workspaces/1/diagnostics")).json()
    assert missing["missing_exposure_snapshots"] == 1  # positions, no snapshot

    await service.recompute_workspace(session, 1)
    await session.commit()

    body = (await client.get("/api/v1/workspaces/1/diagnostics")).json()
    assert body["missing_exposure_snapshots"] == 0
    # Seed holdings only sum to a small fraction => low coverage.
    assert body["low_exposure_coverage"] == 1
    assert body["unclassified_exposure_weight"] is not None


# --- alert integration -------------------------------------------------------


async def test_alert_low_coverage_and_recompute_failed(session: AsyncSession) -> None:
    # Seed workspace 1 has low look-through coverage after a recompute.
    await service.recompute_workspace(session, 1)
    # A recent failed recompute run should also raise an alert.
    session.add(
        JobRun(
            job_type="exposure_recompute",
            status="failed",
            started_at=datetime.now(UTC),
            finished_at=datetime.now(UTC),
            message="boom",
        )
    )
    await session.commit()

    await alert_generation.generate_for_workspace(session, 1)
    await session.commit()

    alerts = await alerts_service.list_alerts(session, 1, category="exposure")
    keys = {a.dedupe_key for a in alerts}
    assert any(k.startswith("exposure_low_coverage") for k in keys)
    assert any(k.startswith("exposure_recompute_failed") for k in keys)


async def test_alert_exposure_stale(session: AsyncSession) -> None:
    await service.recompute_workspace(session, 1)
    await session.commit()
    snap = await service.get_latest_snapshot(session, 1)
    old = datetime.now(UTC) - timedelta(days=alert_rules.EXPOSURE_STALE_DAYS + 5)
    await session.execute(
        update(ExposureSnapshot).where(ExposureSnapshot.id == snap.id).values(created_at=old)
    )
    await session.commit()

    await alert_generation.generate_for_workspace(session, 1)
    await session.commit()
    alerts = await alerts_service.list_alerts(session, 1, category="exposure")
    assert any(a.dedupe_key == "exposure_stale:1" for a in alerts)
