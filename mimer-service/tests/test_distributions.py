from __future__ import annotations

from datetime import date
from decimal import Decimal

from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Distribution, Fund, JobRun, ScheduledJob
from app.sources.distributions import DistributionRecord, StaticDistributionSource
from app.workers.run import run_job


class FakeDistributionSource:
    """A controlled distribution provider for one ISIN (no network)."""

    name = "distribution_fixture"
    is_fixture = True

    def __init__(self, records: list[DistributionRecord]) -> None:
        self._records = records

    async def fetch(self, *, isin: str, session=None, url=None) -> list[DistributionRecord]:  # noqa: ANN001
        if isin != "IE00DIST00001":
            return []
        return self._records


def _record(ex: str, amount: str, *, status: str = "paid") -> DistributionRecord:
    return DistributionRecord(
        ex_date=date.fromisoformat(ex),
        record_date=date.fromisoformat(ex),
        payment_date=date.fromisoformat(ex),
        amount=Decimal(amount),
        currency="USD",
        source="distribution_fixture",
        status=status,
    )


async def test_static_source_returns_records_for_seeded_isin() -> None:
    records = await StaticDistributionSource().fetch(isin="IE00B3XXRP09")
    assert records
    assert all(r.source == "distribution_fixture" for r in records)
    assert all(r.currency == "USD" for r in records)
    # Unknown ISIN -> empty, not an error.
    assert await StaticDistributionSource().fetch(isin="ZZ0000000000") == []


async def _make_fund(session: AsyncSession, isin: str = "IE00DIST00001") -> Fund:
    fund = Fund(
        isin=isin, name="Dist Test ETF", status="active", distribution_policy="distributing"
    )
    session.add(fund)
    await session.commit()
    return fund


async def test_distribution_ingestion_inserts_and_counts(
    session: AsyncSession, monkeypatch
) -> None:
    import app.workers.run as worker

    records = [_record("2025-03-20", "0.31"), _record("2025-06-19", "0.33")]
    monkeypatch.setattr(
        worker, "get_distribution_source", lambda name=None: FakeDistributionSource(records)
    )
    fund = await _make_fund(session)

    run = await run_job(session, "distribution_ingestion", fund_id=fund.id)
    assert run.status == "success"
    assert run.records_inserted == 2
    assert run.records_updated == 0
    assert run.records_failed == 0
    assert run.source == "distribution_fixture"

    rows = (
        (await session.execute(select(Distribution).where(Distribution.fund_id == fund.id)))
        .scalars()
        .all()
    )
    assert len(rows) == 2
    assert {r.source for r in rows} == {"distribution_fixture"}


async def test_distribution_ingestion_is_idempotent(session: AsyncSession, monkeypatch) -> None:
    import app.workers.run as worker

    records = [_record("2025-03-20", "0.31"), _record("2025-06-19", "0.33")]
    monkeypatch.setattr(
        worker, "get_distribution_source", lambda name=None: FakeDistributionSource(records)
    )
    fund = await _make_fund(session)

    await run_job(session, "distribution_ingestion", fund_id=fund.id)
    run2 = await run_job(session, "distribution_ingestion", fund_id=fund.id)
    assert run2.records_inserted == 0  # no duplicates on rerun
    assert run2.records_updated == 0  # nothing changed
    assert run2.status == "success"

    count = await session.scalar(
        select(func.count()).select_from(Distribution).where(Distribution.fund_id == fund.id)
    )
    assert count == 2


async def test_distribution_ingestion_updates_changed_amount(
    session: AsyncSession, monkeypatch
) -> None:
    import app.workers.run as worker

    fund = await _make_fund(session)
    monkeypatch.setattr(
        worker,
        "get_distribution_source",
        lambda name=None: FakeDistributionSource([_record("2025-03-20", "0.31")]),
    )
    await run_job(session, "distribution_ingestion", fund_id=fund.id)

    # Same (fund, ex_date, source) but a corrected amount -> update, not insert.
    monkeypatch.setattr(
        worker,
        "get_distribution_source",
        lambda name=None: FakeDistributionSource([_record("2025-03-20", "0.40")]),
    )
    run = await run_job(session, "distribution_ingestion", fund_id=fund.id)
    assert run.records_inserted == 0
    assert run.records_updated == 1

    row = await session.scalar(select(Distribution).where(Distribution.fund_id == fund.id))
    assert row is not None
    assert row.amount == Decimal("0.40")


async def test_distribution_ingestion_claims_queued_backfill_run(
    session: AsyncSession, monkeypatch
) -> None:
    import app.workers.run as worker

    monkeypatch.setattr(
        worker,
        "get_distribution_source",
        lambda name=None: FakeDistributionSource([_record("2025-03-20", "0.31")]),
    )
    fund = await _make_fund(session)
    queued = JobRun(job_type="distribution_ingestion", status="queued", fund_id=fund.id)
    session.add(queued)
    await session.commit()
    queued_id = queued.id

    run = await run_job(session, "distribution_ingestion", fund_id=fund.id)
    assert run.id == queued_id  # reused the queued backfill run
    assert run.status == "success"
    assert run.records_inserted == 1


async def test_distribution_ingestion_missing_fund_records_failure(
    session: AsyncSession,
) -> None:
    run = await run_job(session, "distribution_ingestion", fund_id=999999)
    assert run.status == "failed"
    assert "not found" in (run.message or "")


async def test_scheduled_distribution_job_runs_real(session: AsyncSession) -> None:
    from app.services import jobs as jobs_service

    job = ScheduledJob(
        name="weekly_distributions",
        job_type="distribution_ingestion",
        source="issuer",
        is_active=True,
    )
    session.add(job)
    await session.commit()

    run = await jobs_service.trigger_job(session, job.id)
    # Real worker path against the offline fixture — never a stub.
    assert run.status in {"success", "partial_success"}
    assert run.status != "success_stub"
    # Seeded distributing funds get fixture distributions ingested.
    assert (run.records_inserted or 0) > 0


async def test_distributions_appear_in_detail_after_ingestion(
    client: AsyncClient, session: AsyncSession
) -> None:
    # Ingest fixture distributions for the seeded VUSA fund (id 1).
    fund = await session.scalar(select(Fund).where(Fund.isin == "IE00B3XXRP09"))
    assert fund is not None
    run = await run_job(session, "distribution_ingestion", fund_id=fund.id)
    assert run.records_inserted > 0

    detail = (await client.get(f"/api/v1/funds/{fund.id}/detail")).json()
    sources = {d["source"] for d in detail["distributions"]}
    assert "distribution_fixture" in sources
    # Aggregate endpoints carry fund_name for the GUI.
    assert all(d["fund_name"] == fund.name for d in detail["distributions"])

    series = (
        await client.get(f"/api/v1/funds/{fund.id}/time-series?kind=distribution&range=all")
    ).json()
    assert series["status"] == "active"
    assert len(series["points"]) > 0
