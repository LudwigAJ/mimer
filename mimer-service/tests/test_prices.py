from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import FundListing, JobRun, Price
from app.schemas.instrument import InstrumentCandidate
from app.services import resolver
from app.sources.base import PricePoint
from app.workers import run as worker
from app.workers.run import run_job


class FakePriceSource:
    name = "faketest"

    def __init__(self, points: list[PricePoint]) -> None:
        self._points = points

    async def fetch(self, *, ticker, exchange=None, currency=None) -> list[PricePoint]:
        return self._points


async def _first_listing_id(session: AsyncSession) -> int:
    listing = await session.scalar(select(FundListing).order_by(FundListing.id))
    assert listing is not None
    return listing.id


async def test_price_ingestion_upserts_and_counts(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    listing_id = await _first_listing_id(session)
    points = [
        PricePoint(date(2026, 6, 1), Decimal("10.00"), "GBP"),
        PricePoint(date(2026, 6, 2), Decimal("11.00"), "GBP"),
    ]
    monkeypatch.setattr(worker, "get_price_source", lambda name=None: FakePriceSource(points))

    run1 = await run_job(session, "price_ingestion", fund_listing_id=listing_id)
    assert run1.status == "success"
    assert run1.records_inserted == 2
    assert run1.records_updated == 0
    assert run1.source == "faketest"

    rows = (
        (
            await session.execute(
                select(Price).where(Price.fund_listing_id == listing_id, Price.source == "faketest")
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 2

    # Re-running upserts the same dates (idempotent).
    run2 = await run_job(session, "price_ingestion", fund_listing_id=listing_id)
    assert run2.records_inserted == 0
    assert run2.records_updated == 2
    assert run2.status == "success"


async def test_price_ingestion_claims_queued_backfill_run(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    listing_id = await _first_listing_id(session)
    queued = JobRun(job_type="price_ingestion", status="queued", fund_listing_id=listing_id)
    session.add(queued)
    await session.commit()
    queued_id = queued.id

    points = [PricePoint(date(2026, 6, 3), Decimal("12.00"), "GBP")]
    monkeypatch.setattr(worker, "get_price_source", lambda name=None: FakePriceSource(points))

    run = await run_job(session, "price_ingestion", fund_listing_id=listing_id)
    assert run.id == queued_id  # claimed the existing queued run
    assert run.status == "success"
    assert run.records_inserted == 1


async def test_non_price_job_records_stub(session: AsyncSession) -> None:
    # rates_curve_ingestion has no worker yet -> records a stub run.
    run = await run_job(session, "rates_curve_ingestion")
    assert run.status == "success_stub"


async def test_end_to_end_resolve_then_ingest(
    client: AsyncClient, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """resolve a new instrument -> queued backfill -> worker ingests -> prices."""

    async def fake_resolve(query, provider_name=None, **_):
        return [
            InstrumentCandidate(
                isin="IE00E2E000001",
                ticker="E2EX",
                exchange="LSE",
                trading_currency="GBP",
                name="End-to-end ETF",
                confidence="high",
                source="stub",
            )
        ]

    monkeypatch.setattr(resolver, "resolve_identifier", fake_resolve)
    created = await client.post(
        "/api/v1/instruments",
        json={"symbol": "E2EX", "symbol_type": "ticker", "exchange": "LSE", "currency": "GBP"},
    )
    assert created.status_code == 202
    listing_id = created.json()["fund_listing_id"]

    points = [PricePoint(date(2026, 6, 5), Decimal("9.99"), "GBP")]
    monkeypatch.setattr(worker, "get_price_source", lambda name=None: FakePriceSource(points))

    run = await run_job(session, "price_ingestion", fund_listing_id=listing_id)
    assert run.status == "success"
    assert run.records_inserted == 1
    # The queued price_ingestion backfill run for this listing was claimed.
    assert run.fund_listing_id == listing_id

    rows = (
        (
            await session.execute(
                select(Price).where(Price.fund_listing_id == listing_id, Price.source == "faketest")
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
