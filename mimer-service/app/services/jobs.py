"""Scheduled job listing + execution.

Triggering a job delegates to `app.workers.run`. `price_ingestion`,
`issuer_facts_ingestion`, `distribution_ingestion`, `issuer_holdings_ingestion`,
`fx_ingestion` and `document_snapshot_ingestion` run real workers (all but prices
via offline fixture providers); any other job type records a `success_stub`
JobRun so clients can be wired end-to-end. No real scheduler/broker exists yet.

Read models are enriched with an `implementation_status` (real/fixture/stub/
planned) and the `configured_source` provider so the GUI can show which jobs do
real work — see `app/services/capabilities.py`.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError, NotFoundError
from app.db.models import JobRun, ScheduledJob
from app.schemas.job import ScheduledJobRead
from app.services import capabilities as capabilities_service


def serialize_job(job: ScheduledJob) -> ScheduledJobRead:
    """Build the read schema, enriched with implementation/source metadata."""
    read = ScheduledJobRead.model_validate(job)
    read.implementation_status = capabilities_service.worker_status(job.job_type)
    read.configured_source = capabilities_service.configured_source(job.job_type)
    return read


async def list_jobs(session: AsyncSession) -> list[ScheduledJob]:
    return list(
        (await session.execute(select(ScheduledJob).order_by(ScheduledJob.name))).scalars().all()
    )


async def get_job(session: AsyncSession, job_id: int) -> ScheduledJob:
    job = await session.get(ScheduledJob, job_id)
    if job is None:
        raise NotFoundError("Job not found", code="job_not_found")
    return job


async def list_runs(
    session: AsyncSession,
    *,
    limit: int = 100,
    job_type: str | None = None,
    fund_id: int | None = None,
    fund_listing_id: int | None = None,
    status: str | None = None,
) -> list[JobRun]:
    stmt = select(JobRun).order_by(JobRun.id.desc())
    if job_type is not None:
        stmt = stmt.where(JobRun.job_type == job_type)
    if fund_id is not None:
        stmt = stmt.where(JobRun.fund_id == fund_id)
    if fund_listing_id is not None:
        stmt = stmt.where(JobRun.fund_listing_id == fund_listing_id)
    if status is not None:
        stmt = stmt.where(JobRun.status == status)
    stmt = stmt.limit(limit)
    return list((await session.execute(stmt)).scalars().all())


async def trigger_job(session: AsyncSession, job_id: int) -> JobRun:
    # Imported here to avoid a module-level cycle (workers import services).
    from app.workers.run import run_job

    job = await get_job(session, job_id)
    # Best-effort guard against duplicate concurrent/leftover runs. Jobs run
    # synchronously today, so this mainly catches a previous run that crashed
    # mid-flight (status stuck at "running"); a real broker would lock instead.
    in_progress = await session.scalar(
        select(JobRun).where(JobRun.scheduled_job_id == job.id, JobRun.status == "running")
    )
    if in_progress is not None:
        raise ConflictError("A run for this job is already in progress", code="job_already_running")
    # ``scheduled_jobs.source`` is a *category* hint (issuer/market_data/fx/...),
    # not a provider id; let the worker pick its configured provider adapter.
    return await run_job(session, job.job_type, scheduled_job_id=job.id, source_name=None)
