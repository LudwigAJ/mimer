"""Holdings: fixture provider, ingestion, idempotency, reads and exposure.

All offline — the fixture provider never touches the network, mirroring the
distribution/issuer-facts test pattern.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    Fund,
    FundHolding,
    FundListing,
    JobRun,
    PortfolioPosition,
    Price,
    ScheduledJob,
    Workspace,
)
from app.services import diagnostics as diagnostics_service
from app.services import exposure as exposure_service
from app.services import holdings_ingestion as holdings_service
from app.sources.holdings import (
    HoldingRecord,
    StaticHoldingsSource,
    fixture_as_of,
    holding_identity_key,
)
from app.workers.run import run_job

_VUSA = "IE00B3XXRP09"


# --- identity key + fixture provider ----------------------------------------


def test_identity_key_prefers_strongest_identifier() -> None:
    # ISIN beats everything; then FIGI > CUSIP > SEDOL.
    assert holding_identity_key(name="Apple", isin="US0378331005", sedol="2046251") == (
        "isin:US0378331005"
    )
    assert holding_identity_key(name="Apple", figi="BBG000B9XRY4") == "figi:BBG000B9XRY4"
    assert holding_identity_key(name="Apple", cusip="037833100") == "cusip:037833100"
    assert holding_identity_key(name="Apple", sedol="2046251") == "sedol:2046251"


def test_identity_key_name_fallback_is_normalised() -> None:
    # No identifier -> normalised name+ticker, stable across whitespace/case.
    a = holding_identity_key(name="  Apple   Inc ", ticker="AAPL")
    b = holding_identity_key(name="apple inc", ticker="aapl")
    assert a == b == "name:apple inc|aapl"


async def test_static_source_returns_records_for_seeded_isin() -> None:
    records = await StaticHoldingsSource().fetch(isin=_VUSA)
    assert len(records) == 10
    assert all(r.source == "holdings_fixture" for r in records)
    assert all(r.as_of_date == fixture_as_of() for r in records)
    # Mixed classification present, real-looking identifiers carried through.
    assert {r.country for r in records} == {"US"}
    assert any(r.sector == "Technology" for r in records)
    assert all(r.holding_isin and r.holding_sedol for r in records)
    # Top-holdings subset: weights sum to a known partial fraction (< 1.0).
    total = sum((r.weight for r in records), Decimal("0"))
    assert Decimal("0.30") < total < Decimal("0.40")
    # Unknown ISIN -> empty, not an error.
    assert await StaticHoldingsSource().fetch(isin="ZZ0000000000") == []


async def test_fixture_as_of_is_recent_previous_month_end() -> None:
    today = date(2026, 6, 21)
    assert fixture_as_of(today) == date(2026, 5, 31)
    # Always within the holdings freshness window (so it reads as fresh).
    assert (date.today() - fixture_as_of()).days <= 40


# --- ingestion via the worker ------------------------------------------------


async def _vusa(session: AsyncSession) -> Fund:
    fund = await session.scalar(select(Fund).where(Fund.isin == _VUSA))
    assert fund is not None
    return fund


async def test_holdings_ingestion_single_fund_inserts_and_counts(session: AsyncSession) -> None:
    fund = await _vusa(session)
    run = await run_job(session, "issuer_holdings_ingestion", fund_id=fund.id)
    assert run.status == "success"
    assert run.records_inserted == 10
    assert run.records_updated == 0
    assert run.records_failed == 0
    assert run.source == "holdings_fixture"

    rows = (
        (
            await session.execute(
                select(FundHolding).where(
                    FundHolding.fund_id == fund.id,
                    FundHolding.source == "holdings_fixture",
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 10
    assert all(h.holding_key for h in rows)
    # Seed holdings are left untouched (different source = separate snapshot).
    seed_count = await session.scalar(
        select(func.count())
        .select_from(FundHolding)
        .where(FundHolding.fund_id == fund.id, FundHolding.source == "seed")
    )
    assert seed_count == 5


async def test_holdings_ingestion_is_idempotent(session: AsyncSession) -> None:
    fund = await _vusa(session)
    await run_job(session, "issuer_holdings_ingestion", fund_id=fund.id)
    run2 = await run_job(session, "issuer_holdings_ingestion", fund_id=fund.id)
    assert run2.records_inserted == 0  # no duplicates on rerun
    assert run2.records_updated == 0  # nothing changed
    assert run2.status == "success"

    count = await session.scalar(
        select(func.count())
        .select_from(FundHolding)
        .where(FundHolding.fund_id == fund.id, FundHolding.source == "holdings_fixture")
    )
    assert count == 10


async def test_holdings_ingestion_updates_only_changed_rows(
    session: AsyncSession, monkeypatch
) -> None:
    import app.workers.run as worker

    as_of = fixture_as_of()

    def _rec(weight: str, *, sector: str = "Technology") -> HoldingRecord:
        return HoldingRecord(
            as_of_date=as_of,
            holding_name="Apple Inc",
            holding_ticker="AAPL",
            holding_isin="US0378331005",
            country="US",
            sector=sector,
            weight=Decimal(weight),
            source="holdings_fixture",
        )

    class Fake:
        name = "holdings_fixture"

        def __init__(self, records):
            self._records = records

        async def fetch(self, *, isin, session=None, url=None):
            return self._records if isin == _VUSA else []

    fund = await _vusa(session)
    monkeypatch.setattr(worker, "get_holdings_source", lambda name=None: Fake([_rec("0.07")]))
    first = await run_job(session, "issuer_holdings_ingestion", fund_id=fund.id)
    assert first.records_inserted == 1

    # Same identity (ISIN) but a corrected weight -> update, not insert.
    monkeypatch.setattr(worker, "get_holdings_source", lambda name=None: Fake([_rec("0.08")]))
    second = await run_job(session, "issuer_holdings_ingestion", fund_id=fund.id)
    assert second.records_inserted == 0
    assert second.records_updated == 1

    # Identical payload again -> no change counted.
    third = await run_job(session, "issuer_holdings_ingestion", fund_id=fund.id)
    assert third.records_inserted == 0
    assert third.records_updated == 0

    row = await session.scalar(
        select(FundHolding).where(
            FundHolding.fund_id == fund.id, FundHolding.source == "holdings_fixture"
        )
    )
    assert row is not None and row.weight == Decimal("0.08")


async def test_holdings_ingestion_counts_failed_rows_cleanly(
    session: AsyncSession, monkeypatch
) -> None:
    import app.workers.run as worker

    as_of = fixture_as_of()
    good = HoldingRecord(
        as_of_date=as_of, holding_name="Good", weight=Decimal("0.05"), source="holdings_fixture"
    )
    # A nameless, identifier-less record cannot produce an identity key: the
    # ingester counts it as failed (cleanly) and moves on, adding nothing.
    bad = HoldingRecord(
        as_of_date=as_of,
        holding_name=None,  # type: ignore[arg-type]
        weight=Decimal("0.04"),
        source="holdings_fixture",
    )

    class Fake:
        name = "holdings_fixture"

        async def fetch(self, *, isin, session=None, url=None):
            return [good, bad] if isin == _VUSA else []

    fund = await _vusa(session)
    monkeypatch.setattr(worker, "get_holdings_source", lambda name=None: Fake())
    run = await run_job(session, "issuer_holdings_ingestion", fund_id=fund.id)
    # One good row in, one bad row counted as failed -> partial success.
    assert run.records_inserted == 1
    assert run.records_failed == 1
    assert run.status == "partial_success"


async def test_holdings_ingestion_bulk_runs_all_eligible_funds(session: AsyncSession) -> None:
    run = await run_job(session, "issuer_holdings_ingestion")
    assert run.status == "success"
    # Three seeded funds, ten fixture holdings each.
    assert run.records_inserted == 30

    total = await session.scalar(
        select(func.count())
        .select_from(FundHolding)
        .where(FundHolding.source == "holdings_fixture")
    )
    assert total == 30


async def test_holdings_ingestion_missing_fund_records_failure(session: AsyncSession) -> None:
    run = await run_job(session, "issuer_holdings_ingestion", fund_id=999999)
    assert run.status == "failed"
    assert "not found" in (run.message or "")


async def test_holdings_ingestion_claims_queued_backfill_run(session: AsyncSession) -> None:
    fund = await _vusa(session)
    queued = JobRun(job_type="issuer_holdings_ingestion", status="queued", fund_id=fund.id)
    session.add(queued)
    await session.commit()
    queued_id = queued.id

    run = await run_job(session, "issuer_holdings_ingestion", fund_id=fund.id)
    assert run.id == queued_id  # reused the queued backfill run
    assert run.status == "success"
    assert run.records_inserted == 10


async def test_scheduled_holdings_job_runs_real_not_stub(session: AsyncSession) -> None:
    from app.services import jobs as jobs_service

    job = ScheduledJob(
        name="weekly_holdings",
        job_type="issuer_holdings_ingestion",
        source="issuer",
        is_active=True,
    )
    session.add(job)
    await session.commit()

    run = await jobs_service.trigger_job(session, job.id)
    assert run.status in {"success", "partial_success"}
    assert run.status != "success_stub"
    assert (run.records_inserted or 0) > 0


# --- snapshot selection (read side) ------------------------------------------


async def test_latest_snapshot_prefers_fixture_over_seed(session: AsyncSession) -> None:
    fund = await _vusa(session)
    await run_job(session, "issuer_holdings_ingestion", fund_id=fund.id)

    snapshot = (await holdings_service.latest_holdings_by_fund(session, [fund.id]))[fund.id]
    # Fixture (priority 20) wins over seed (priority 100) — never a mix.
    assert {h.source for h in snapshot} == {"holdings_fixture"}
    assert len(snapshot) == 10


# --- read APIs ---------------------------------------------------------------


async def test_fund_holdings_endpoint_shape_and_provenance(
    client: AsyncClient, session: AsyncSession
) -> None:
    fund = await _vusa(session)
    await run_job(session, "issuer_holdings_ingestion", fund_id=fund.id)

    body = (await client.get(f"/api/v1/funds/{fund.id}/holdings")).json()
    assert body["fund_id"] == fund.id
    assert body["fund_name"] == fund.name
    assert body["source"] == "holdings_fixture"
    assert body["status"] == "current"
    assert body["as_of_date"] == fixture_as_of().isoformat()
    assert len(body["holdings"]) == 10
    top = body["holdings"][0]
    # Each holding carries identifiers + classification + provenance.
    for field in (
        "security_name",
        "security_ticker",
        "security_isin",
        "security_sedol",
        "sector",
        "country",
        "currency",
        "weight",
        "source",
    ):
        assert field in top
    assert top["security_name"]
    assert top["security_isin"]
    assert top["source"] == "holdings_fixture"
    # Weights are descending (bounded, ordered).
    weights = [Decimal(h["weight"]) for h in body["holdings"]]
    assert weights == sorted(weights, reverse=True)


async def test_fund_holdings_endpoint_filters_and_limit(
    client: AsyncClient, session: AsyncSession
) -> None:
    fund = await _vusa(session)
    await run_job(session, "issuer_holdings_ingestion", fund_id=fund.id)

    # Pin the seed snapshot explicitly via source.
    seed = (await client.get(f"/api/v1/funds/{fund.id}/holdings?source=seed")).json()
    assert seed["source"] == "seed"
    assert len(seed["holdings"]) == 5

    # Bound the response.
    limited = (await client.get(f"/api/v1/funds/{fund.id}/holdings?limit=3")).json()
    assert len(limited["holdings"]) == 3
    assert limited["source"] == "holdings_fixture"


async def test_fund_detail_includes_latest_holdings_snapshot(
    client: AsyncClient, session: AsyncSession
) -> None:
    fund = await _vusa(session)
    await run_job(session, "issuer_holdings_ingestion", fund_id=fund.id)

    detail = (await client.get(f"/api/v1/funds/{fund.id}/detail")).json()
    assert {h["source"] for h in detail["holdings"]} == {"holdings_fixture"}
    assert len(detail["holdings"]) == 10
    assert detail["freshness"]["holdings"] == "fresh"


async def test_dashboard_includes_holdings(client: AsyncClient, session: AsyncSession) -> None:
    await run_job(session, "issuer_holdings_ingestion")
    dashboard = (await client.get("/api/v1/workspaces/1/dashboard")).json()
    assert dashboard["holdings"]
    sources = {h["source"] for h in dashboard["holdings"]}
    # Held funds now surface fixture holdings (never mixed with seed at read).
    assert sources == {"holdings_fixture"}
    assert dashboard["freshness"]["holdings"] in {"fresh", "stale"}


async def test_hierarchy_includes_top_holdings(client: AsyncClient, session: AsyncSession) -> None:
    await run_job(session, "issuer_holdings_ingestion")
    body = (await client.get("/api/v1/workspaces/1/hierarchy")).json()
    positions = body["root"]["children"]
    assert positions
    holding_nodes = [
        child for pos in positions for child in pos["children"] if child["kind"] == "holding"
    ]
    assert holding_nodes
    # Bounded to the top-N per position and sourced from the fixture snapshot.
    assert all(len(pos["children"]) <= 10 for pos in positions)
    assert any(n["source"] == "holdings_fixture" for n in holding_nodes)


async def test_workspace_diagnostics_reflect_holdings(
    client: AsyncClient, session: AsyncSession
) -> None:
    await run_job(session, "issuer_holdings_ingestion")
    diag = (await client.get("/api/v1/workspaces/1/diagnostics")).json()
    # Every held seeded fund has a (fresh) holdings snapshot.
    assert diag["missing_holdings"] == 0
    assert diag["stale_holdings"] == 0


async def test_global_diagnostics_count_missing_and_stale_holdings(
    session: AsyncSession,
) -> None:
    # A fund with no holdings at all -> missing.
    bare = Fund(isin="IE00MISSING01", name="No Holdings ETF", status="active")
    # A fund whose only snapshot is long stale.
    stale = Fund(isin="IE00STALE0001", name="Stale Holdings ETF", status="active")
    session.add_all([bare, stale])
    await session.flush()
    session.add(
        FundHolding(
            fund_id=stale.id,
            as_of_date=date.today() - timedelta(days=300),
            security_name="Old Co",
            weight=Decimal("0.10"),
            source="holdings_fixture",
            holding_key="isin:OLD",
        )
    )
    await session.commit()

    diag = await diagnostics_service.global_diagnostics(session)
    assert diag.missing_holdings >= 1
    assert diag.stale_holdings >= 1


# --- exposure aggregation from holdings --------------------------------------


async def _isolated_position(
    session: AsyncSession,
    *,
    isin: str,
    units: str = "100",
    price: str = "10",
    currency: str = "GBP",
    workspace_id: int,
) -> Fund:
    listing = FundListing(
        ticker=isin[-4:],
        trading_currency=currency,
        currency_unit=currency,
        status="active",
        prices=[
            Price(price_date=date.today(), price=Decimal(price), currency=currency, source="stooq")
        ],
    )
    fund = Fund(isin=isin, name=f"Fund {isin}", status="active", listings=[listing])
    session.add(fund)
    await session.flush()
    session.add(
        PortfolioPosition(
            workspace_id=workspace_id, fund_listing_id=listing.id, units=Decimal(units)
        )
    )
    await session.flush()
    return fund


def _holding(fund_id: int, name: str, country: str, sector: str, weight: str) -> FundHolding:
    return FundHolding(
        fund_id=fund_id,
        as_of_date=date.today(),
        security_name=name,
        country=country,
        sector=sector,
        weight=Decimal(weight),
        source="holdings_fixture",
        holding_key=holding_identity_key(name=name),
    )


async def test_exposure_single_position_equals_holding_weights(session: AsyncSession) -> None:
    ws = Workspace(name="Solo", base_currency="GBP")
    session.add(ws)
    await session.flush()
    fund = await _isolated_position(session, isin="IE00EXPOSE01", workspace_id=ws.id)
    session.add_all(
        [
            _holding(fund.id, "Apple", "US", "Technology", "0.07"),
            _holding(fund.id, "Shell", "GB", "Energy", "0.05"),
        ]
    )
    await session.commit()

    exposure = await exposure_service.build_exposure(session, ws.id)
    country = {s.key: s.weight for s in exposure.country}
    sector = {s.key: s.weight for s in exposure.sector}
    currency = {s.key: s.weight for s in exposure.currency}
    # Single position => position weight 1.0, so look-through == holding weight.
    assert country["US"] == Decimal("0.0700")
    assert country["GB"] == Decimal("0.0500")
    assert sector["Technology"] == Decimal("0.0700")
    assert currency["GBP"] == Decimal("1.0000")


async def test_exposure_look_through_multiplies_position_and_holding_weight(
    session: AsyncSession,
) -> None:
    ws = Workspace(name="Two", base_currency="GBP")
    session.add(ws)
    await session.flush()
    # Two positions with identical market value => 50% each.
    fund_a = await _isolated_position(session, isin="IE00EXPOSEA1", workspace_id=ws.id)
    fund_b = await _isolated_position(session, isin="IE00EXPOSEB1", workspace_id=ws.id)
    session.add_all(
        [
            _holding(fund_a.id, "Apple", "US", "Technology", "0.10"),
            _holding(fund_b.id, "Shell", "GB", "Energy", "0.10"),
        ]
    )
    await session.commit()

    exposure = await exposure_service.build_exposure(session, ws.id)
    country = {s.key: s.weight for s in exposure.country}
    # 0.5 position weight * 0.10 holding weight = 0.05 look-through.
    assert country["US"] == Decimal("0.0500")
    assert country["GB"] == Decimal("0.0500")


async def test_exposure_handles_missing_holdings_gracefully(session: AsyncSession) -> None:
    ws = Workspace(name="Empty", base_currency="GBP")
    session.add(ws)
    await session.flush()
    await _isolated_position(session, isin="IE00NOHOLD01", workspace_id=ws.id)
    await session.commit()

    exposure = await exposure_service.build_exposure(session, ws.id)
    # No holdings -> no country/sector slices, but currency still resolves.
    assert exposure.country == []
    assert exposure.sector == []
    assert {s.key for s in exposure.currency} == {"GBP"}


# --- capability registry -----------------------------------------------------


async def test_data_source_capabilities_holdings_filter(client: AsyncClient) -> None:
    body = (await client.get("/api/v1/data-sources/capabilities?data_type=holdings")).json()
    names = {c["source_name"] for c in body["data"]}
    assert "holdings_fixture" in names
    fixture = next(c for c in body["data"] if c["source_name"] == "holdings_fixture")
    assert fixture["adapter_status"] == "implemented"
    assert "holdings" in fixture["data_types"]
