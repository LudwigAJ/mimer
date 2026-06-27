from __future__ import annotations

from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Fund, JobRun, ScheduledJob
from app.sources.issuer import IssuerFacts, StaticIssuerFactsSource
from app.workers.run import run_job


class FakeIssuerSource:
    """A controlled issuer-facts provider for one ISIN (no network)."""

    name = "issuer_fixture"

    async def fetch(self, *, isin: str) -> IssuerFacts | None:
        if isin != "IE00PENDING01":
            return None
        return IssuerFacts(
            isin=isin,
            source=self.name,
            official_name="Pending Test UCITS ETF",
            provider="Test Issuer",
            domicile="IE",
            base_currency="USD",
            distribution_policy="accumulating",
            strategy="Test strategy",
            ocf=Decimal("0.12000"),
        )


async def test_static_source_returns_facts_for_seeded_isin() -> None:
    facts = await StaticIssuerFactsSource().fetch(isin="IE00B3XXRP09")
    assert facts is not None
    assert facts.provider == "Vanguard"
    assert facts.source == "issuer_fixture"
    assert await StaticIssuerFactsSource().fetch(isin="ZZ0000000000") is None


async def test_issuer_facts_enriches_pending_fund(session: AsyncSession, monkeypatch) -> None:
    import app.workers.run as worker

    monkeypatch.setattr(worker, "get_issuer_facts_source", lambda name=None: FakeIssuerSource())

    fund = Fund(isin="IE00PENDING01", name="IE00PENDING01", status="pending")
    session.add(fund)
    await session.commit()

    run = await run_job(session, "issuer_facts_ingestion", fund_id=fund.id)
    assert run.status == "success"
    assert run.records_inserted == 1  # pending -> active
    assert run.records_updated >= 1
    assert run.records_failed == 0

    await session.refresh(fund)
    assert fund.status == "active"
    assert fund.source == "issuer_fixture"
    assert fund.provider == "Test Issuer"
    assert fund.name == "Pending Test UCITS ETF"
    assert fund.last_refreshed_at is not None


async def test_issuer_facts_is_idempotent(session: AsyncSession, monkeypatch) -> None:
    import app.workers.run as worker

    monkeypatch.setattr(worker, "get_issuer_facts_source", lambda name=None: FakeIssuerSource())
    fund = Fund(isin="IE00PENDING01", name="IE00PENDING01", status="pending")
    session.add(fund)
    await session.commit()

    await run_job(session, "issuer_facts_ingestion", fund_id=fund.id)
    run2 = await run_job(session, "issuer_facts_ingestion", fund_id=fund.id)
    assert run2.records_updated == 0  # nothing left to change


async def test_issuer_facts_does_not_overwrite_manual(session: AsyncSession, monkeypatch) -> None:
    import app.workers.run as worker

    monkeypatch.setattr(worker, "get_issuer_facts_source", lambda name=None: FakeIssuerSource())
    fund = Fund(
        isin="IE00PENDING01",
        name="IE00PENDING01",
        provider="Hand-entered Provider",
        source="manual",
        status="active",
    )
    session.add(fund)
    await session.commit()

    await run_job(session, "issuer_facts_ingestion", fund_id=fund.id)
    await session.refresh(fund)
    # Manual outranks the issuer fixture: the provider is preserved...
    assert fund.provider == "Hand-entered Provider"
    assert fund.source == "manual"
    # ...but empty fields are still filled in.
    assert fund.strategy == "Test strategy"


async def test_issuer_facts_records_miss(session: AsyncSession, monkeypatch) -> None:
    import app.workers.run as worker

    monkeypatch.setattr(worker, "get_issuer_facts_source", lambda name=None: FakeIssuerSource())
    fund = Fund(isin="IE00NOFACTS01", name="No Facts", status="pending")
    session.add(fund)
    await session.commit()

    run = await run_job(session, "issuer_facts_ingestion", fund_id=fund.id)
    assert run.status == "failed"
    assert run.records_failed == 1


async def test_issuer_facts_claims_queued_backfill_run(session: AsyncSession, monkeypatch) -> None:
    import app.workers.run as worker

    monkeypatch.setattr(worker, "get_issuer_facts_source", lambda name=None: FakeIssuerSource())
    fund = Fund(isin="IE00PENDING01", name="IE00PENDING01", status="pending")
    session.add(fund)
    await session.flush()
    queued = JobRun(job_type="issuer_facts_ingestion", status="queued", fund_id=fund.id)
    session.add(queued)
    await session.commit()
    queued_id = queued.id

    run = await run_job(session, "issuer_facts_ingestion", fund_id=fund.id)
    assert run.id == queued_id  # reused the queued backfill run
    assert run.status == "success"


async def test_scheduled_issuer_facts_job_runs_real(session: AsyncSession) -> None:
    from app.services import jobs as jobs_service

    job = ScheduledJob(
        name="weekly_issuer_facts",
        job_type="issuer_facts_ingestion",
        source="issuer",
        is_active=True,
    )
    session.add(job)
    await session.commit()

    run = await jobs_service.trigger_job(session, job.id)
    # Real worker path: not a stub. Seed funds are source=seed, so they are
    # enriched (or already enriched) rather than recording a success_stub.
    assert run.status in {"success", "partial_success"}
    assert run.status != "success_stub"
