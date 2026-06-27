"""Scheduler worker: due selection, leasing, next-run, run-once, isolation."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import JobRun, ScheduledJob
from app.workers import scheduler as sched


def _now() -> datetime:
    return datetime(2026, 6, 21, 12, 0, 0, tzinfo=UTC)


def _naive(value: datetime | None) -> datetime | None:
    # SQLite round-trips datetimes without tzinfo; compare on the wall-clock value.
    return value.replace(tzinfo=None) if value is not None else None


async def _add_job(
    session: AsyncSession,
    *,
    name: str,
    job_type: str = "rates_curve_ingestion",
    schedule_kind: str = "daily",
    next_run_at: datetime | None = None,
    is_active: bool = True,
    interval_seconds: int | None = None,
) -> ScheduledJob:
    job = ScheduledJob(
        name=name,
        job_type=job_type,
        schedule_kind=schedule_kind,
        interval_seconds=interval_seconds,
        is_active=is_active,
        next_run_at=next_run_at,
    )
    session.add(job)
    await session.commit()
    await session.refresh(job)
    return job


# --- schedule semantics ------------------------------------------------------


def test_schedule_interval_seconds_for_named_kinds() -> None:
    assert sched.schedule_interval_seconds(ScheduledJob(schedule_kind="hourly")) == 3600
    assert sched.schedule_interval_seconds(ScheduledJob(schedule_kind="daily")) == 86400
    assert sched.schedule_interval_seconds(ScheduledJob(schedule_kind="weekly")) == 604800
    assert sched.schedule_interval_seconds(ScheduledJob(schedule_kind="manual")) is None
    assert (
        sched.schedule_interval_seconds(
            ScheduledJob(schedule_kind="interval", interval_seconds=120)
        )
        == 120
    )


def test_compute_next_run_at_from_now_not_stale() -> None:
    now = _now()
    job = ScheduledJob(schedule_kind="daily")
    assert sched.compute_next_run_at(job, after=now) == now + timedelta(days=1)
    # Manual jobs never get a next run.
    assert sched.compute_next_run_at(ScheduledJob(schedule_kind="manual"), after=now) is None


# --- due selection -----------------------------------------------------------


async def test_due_jobs_excludes_manual_inactive_and_future(session: AsyncSession) -> None:
    now = _now()
    due = await _add_job(session, name="due", next_run_at=now - timedelta(minutes=1))
    await _add_job(session, name="future", next_run_at=now + timedelta(hours=1))
    await _add_job(
        session, name="manual", schedule_kind="manual", next_run_at=now - timedelta(hours=1)
    )
    await _add_job(session, name="inactive", is_active=False, next_run_at=now - timedelta(hours=1))

    names = {j.name for j in await sched.due_jobs(session, now=now)}
    assert "due" in names
    assert {"future", "manual", "inactive"}.isdisjoint(names)
    assert due.name == "due"


async def test_initialize_next_run_at_makes_active_jobs_due(session: AsyncSession) -> None:
    now = _now()
    job = await _add_job(session, name="needs_init", next_run_at=None)
    count = await sched.initialize_next_run_at(session, now=now)
    assert count >= 1
    await session.refresh(job)
    assert job.next_run_at is not None


# --- leasing / duplicate prevention -----------------------------------------


async def test_claim_job_is_exclusive(session: AsyncSession) -> None:
    now = _now()
    job = await _add_job(session, name="lease_me", next_run_at=now - timedelta(minutes=1))

    first = await sched.claim_job(session, job, instance_id="A", lease_seconds=300, now=now)
    second = await sched.claim_job(session, job, instance_id="B", lease_seconds=300, now=now)
    assert first is True
    assert second is False  # someone already holds the lease

    await session.refresh(job)
    assert job.locked_by == "A"
    assert job.lock_expires_at is not None


async def test_expired_lease_is_reclaimable(session: AsyncSession) -> None:
    now = _now()
    job = await _add_job(session, name="expire_me", next_run_at=now - timedelta(minutes=1))
    assert await sched.claim_job(session, job, instance_id="A", lease_seconds=300, now=now)

    later = now + timedelta(seconds=301)  # lease has expired
    reclaimed = await sched.claim_job(session, job, instance_id="B", lease_seconds=300, now=later)
    assert reclaimed is True
    await session.refresh(job)
    assert job.locked_by == "B"


# --- run-once / run path -----------------------------------------------------


async def test_run_due_jobs_runs_and_schedules_next(session: AsyncSession) -> None:
    now = _now()
    job = await _add_job(
        session,
        name="run_me",
        job_type="rates_curve_ingestion",
        next_run_at=now - timedelta(minutes=1),
    )

    result = await sched.run_due_jobs(session, instance_id="T", lease_seconds=300, now=now)
    assert result.claimed == 1
    assert result.succeeded == 1
    assert result.failed == 0

    await session.refresh(job)
    # Lease released, next_run scheduled one interval out, status recorded.
    assert job.locked_by is None
    assert job.lock_expires_at is None
    assert _naive(job.next_run_at) == _naive(now + timedelta(days=1))
    assert job.last_status == "success_stub"

    # A JobRun was created for the scheduled job.
    runs = (
        (await session.execute(select(JobRun).where(JobRun.scheduled_job_id == job.id)))
        .scalars()
        .all()
    )
    assert len(runs) == 1


async def test_run_due_jobs_isolates_failure_and_continues(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = _now()
    bad = await _add_job(session, name="bad", next_run_at=now - timedelta(minutes=2))
    good = await _add_job(session, name="good", next_run_at=now - timedelta(minutes=1))

    import app.workers.run as run_module

    real_run_job = run_module.run_job

    async def flaky_run_job(session, job_type, *, scheduled_job_id=None, **kwargs):
        job = await session.get(ScheduledJob, scheduled_job_id)
        if job is not None and job.name == "bad":
            raise RuntimeError("boom")
        return await real_run_job(session, job_type, scheduled_job_id=scheduled_job_id, **kwargs)

    monkeypatch.setattr(run_module, "run_job", flaky_run_job)

    result = await sched.run_due_jobs(session, instance_id="T", lease_seconds=300, now=now)
    assert result.failed == 1
    assert result.succeeded == 1  # the good job still ran despite the bad one

    await session.refresh(bad)
    await session.refresh(good)
    # Both leases released; failure recorded, next run still scheduled (misfire).
    assert bad.locked_by is None
    assert bad.last_status == "failed"
    assert _naive(bad.next_run_at) == _naive(now + timedelta(days=1))
    assert good.last_status == "success_stub"


async def test_run_once_does_not_duplicate_active_run(session: AsyncSession) -> None:
    now = _now()
    job = await _add_job(session, name="once", next_run_at=now - timedelta(minutes=1))
    # Pre-existing lease held by another instance, not yet expired.
    assert await sched.claim_job(session, job, instance_id="other", lease_seconds=300, now=now)

    result = await sched.run_due_jobs(session, instance_id="T", lease_seconds=300, now=now)
    # The job is leased, so this pass does not see it as due / claim it again.
    assert result.claimed == 0
