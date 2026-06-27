"""In-process scheduler worker — claims due jobs, leases them, runs them.

    uv run python -m app.workers.scheduler            # loop forever (poll)
    uv run python -m app.workers.scheduler --once      # one pass, then exit
    uv run python -m app.workers.scheduler --poll-seconds 30

Design (see AGENTS.md / README "Scheduler"):

* No Celery/RQ/Kafka, no OS cron, no pg_cron, no subprocesses. The scheduler is a
  plain Python process that *imports and calls* the same ``app.workers.run.run_job``
  business logic as the CLI — it never shells out.
* Due = active, non-``manual``, ``next_run_at <= now``, and not currently leased.
* Each due job is claimed with a single atomic conditional UPDATE that stamps the
  lease only if the row is unlocked or its lease has expired. So several scheduler
  processes can exist but exactly one runs a given job; a crashed lease is
  reclaimable after ``lock_expires_at``. Runs are isolated: one failing job records
  ``last_status=failed`` and never kills the loop.
* After a run, ``next_run_at`` is recomputed from *now* (a simple
  run-once-then-schedule misfire policy) and the lease is released.

This is the operational foundation for future stock/constituent backfills; it
does not itself fetch market data.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import socket
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.logging import configure_logging, get_logger
from app.db.models import ScheduledJob
from app.db.session import get_engine, get_sessionmaker

logger = get_logger("app.scheduler")

MANUAL = "manual"
# schedule_kind -> fixed recurrence period in seconds (interval uses its own).
_KIND_INTERVALS: dict[str, int] = {
    "hourly": 3600,
    "daily": 86400,
    "weekly": 604800,
}


def schedule_interval_seconds(job: ScheduledJob) -> int | None:
    """Recurrence period for a job, or None for ``manual`` / unscheduled."""
    if job.schedule_kind == MANUAL:
        return None
    if job.schedule_kind == "interval":
        return job.interval_seconds
    return _KIND_INTERVALS.get(job.schedule_kind)


def compute_next_run_at(job: ScheduledJob, *, after: datetime) -> datetime | None:
    """Next run instant for a job, measured from ``after`` (None if manual).

    Computing from ``after`` (typically *now*) rather than the stale
    ``next_run_at`` is the misfire policy: a long-overdue job runs once then
    schedules its next occurrence relative to now, so it never piles up.
    """
    interval = schedule_interval_seconds(job)
    if interval is None or interval <= 0:
        return None
    return after + timedelta(seconds=interval)


def default_instance_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


@dataclass
class SchedulerPass:
    """Summary of one scheduler pass (for logs + the run-once API)."""

    instance_id: str
    due: int = 0
    claimed: int = 0
    succeeded: int = 0
    failed: int = 0
    skipped: int = 0
    ran: list[dict[str, object]] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return {
            "instance_id": self.instance_id,
            "due": self.due,
            "claimed": self.claimed,
            "succeeded": self.succeeded,
            "failed": self.failed,
            "skipped": self.skipped,
            "ran": self.ran,
        }


async def initialize_next_run_at(session: AsyncSession, *, now: datetime | None = None) -> int:
    """Give active, non-manual jobs a ``next_run_at`` if they have none.

    A freshly-seeded recurring job starts due immediately (``next_run_at = now``)
    so the first scheduler pass picks it up. Returns the number initialised.
    """
    now = now or datetime.now(UTC)
    jobs = (
        (
            await session.execute(
                select(ScheduledJob).where(
                    ScheduledJob.is_active.is_(True),
                    ScheduledJob.schedule_kind != MANUAL,
                    ScheduledJob.next_run_at.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    count = 0
    for job in jobs:
        if schedule_interval_seconds(job) is None:
            continue
        job.next_run_at = now
        count += 1
    if count:
        await session.commit()
    return count


async def due_jobs(session: AsyncSession, *, now: datetime | None = None) -> list[ScheduledJob]:
    """Active, non-manual jobs that are due and not currently leased."""
    now = now or datetime.now(UTC)
    stmt = (
        select(ScheduledJob)
        .where(
            ScheduledJob.is_active.is_(True),
            ScheduledJob.schedule_kind != MANUAL,
            ScheduledJob.next_run_at.is_not(None),
            ScheduledJob.next_run_at <= now,
            or_(
                ScheduledJob.lock_expires_at.is_(None),
                ScheduledJob.lock_expires_at <= now,
            ),
        )
        .order_by(ScheduledJob.next_run_at, ScheduledJob.id)
    )
    return list((await session.execute(stmt)).scalars().all())


async def claim_job(
    session: AsyncSession,
    job: ScheduledJob,
    *,
    instance_id: str,
    lease_seconds: int,
    now: datetime | None = None,
) -> bool:
    """Atomically lease ``job`` for ``instance_id``. Returns True iff we won it.

    The single conditional UPDATE is the whole concurrency story: the row is
    stamped only if it is unlocked or its lease has expired, so two schedulers
    racing the same job produce exactly one winner (rowcount == 1) regardless of
    backend (works on Postgres and SQLite). Expired leases are reclaimable.
    """
    now = now or datetime.now(UTC)
    lock_expires_at = now + timedelta(seconds=lease_seconds)
    stmt = (
        update(ScheduledJob)
        .where(
            ScheduledJob.id == job.id,
            ScheduledJob.is_active.is_(True),
            or_(
                ScheduledJob.lock_expires_at.is_(None),
                ScheduledJob.lock_expires_at <= now,
            ),
        )
        .values(
            locked_by=instance_id,
            locked_at=now,
            lock_expires_at=lock_expires_at,
            last_heartbeat_at=now,
        )
        # Let the DB evaluate the WHERE (atomic) rather than the ORM evaluating it
        # in Python — which would also choke on SQLite's naive datetimes.
        .execution_options(synchronize_session=False)
    )
    result = await session.execute(stmt)
    await session.commit()
    won = (result.rowcount or 0) == 1
    if won:
        await session.refresh(job)
    return won


async def release_lease(
    session: AsyncSession,
    job_id: int,
    *,
    last_status: str,
    next_run_at: datetime | None,
    now: datetime | None = None,
) -> None:
    """Clear the lease and record outcome + next occurrence (by id).

    Uses a direct UPDATE rather than mutating the ORM instance: a failed run
    rolls back and expires the instance, so touching its attributes would trigger
    lazy IO. Keyed by the captured ``job_id``, this is robust either way.
    """
    now = now or datetime.now(UTC)
    await session.execute(
        update(ScheduledJob)
        .where(ScheduledJob.id == job_id)
        .values(
            locked_by=None,
            locked_at=None,
            lock_expires_at=None,
            last_heartbeat_at=now,
            last_status=last_status,
            next_run_at=next_run_at,
        )
        .execution_options(synchronize_session=False)
    )
    await session.commit()


async def _run_one(
    session: AsyncSession,
    job: ScheduledJob,
    *,
    instance_id: str,
    lease_seconds: int,
    now: datetime,
) -> str | None:
    """Claim + run a single job. Returns the run status, or None if not claimed."""
    # Imported here to avoid a module-level cycle (run imports services).
    from app.workers.run import run_job

    # Capture identity + next run BEFORE running: a failed run rolls back and
    # expires the ORM instance, so reading its attributes afterwards is unsafe.
    job_id, job_name, job_type = job.id, job.name, job.job_type
    next_run_at = compute_next_run_at(job, after=now)

    if not await claim_job(
        session, job, instance_id=instance_id, lease_seconds=lease_seconds, now=now
    ):
        logger.info("scheduler skip job=%s reason=already_leased", job_name)
        return None

    logger.info("scheduler claim job=%s type=%s by=%s", job_name, job_type, instance_id)
    try:
        run = await run_job(session, job_type, scheduled_job_id=job_id, source_name=None)
        status = run.status
        logger.info(
            "scheduler ran job=%s type=%s status=%s run_id=%s", job_name, job_type, status, run.id
        )
    except Exception as exc:  # noqa: BLE001 - isolate one job; never kill the loop
        await session.rollback()
        status = "failed"
        logger.warning("scheduler job_failed job=%s error=%s", job_name, exc)

    await release_lease(session, job_id, last_status=status, next_run_at=next_run_at, now=now)
    return status


async def run_due_jobs(
    session: AsyncSession,
    *,
    instance_id: str | None = None,
    lease_seconds: int | None = None,
    now: datetime | None = None,
) -> SchedulerPass:
    """One scheduler pass: initialise, select due, claim+run each in isolation."""
    settings = get_settings()
    instance_id = instance_id or default_instance_id()
    lease_seconds = lease_seconds or settings.scheduler_lease_seconds
    now = now or datetime.now(UTC)

    await initialize_next_run_at(session, now=now)

    result = SchedulerPass(instance_id=instance_id)
    jobs = await due_jobs(session, now=now)
    result.due = len(jobs)
    # Capture ids while the instances are fresh: a failing run rolls back and
    # expires every instance in the session, so re-fetch each job per iteration.
    due_ids = [job.id for job in jobs]
    for job_id in due_ids:
        job = await session.get(ScheduledJob, job_id)
        if job is None:
            continue
        job_name, job_type = job.name, job.job_type
        status = await _run_one(
            session, job, instance_id=instance_id, lease_seconds=lease_seconds, now=now
        )
        if status is None:
            result.skipped += 1
            continue
        result.claimed += 1
        result.ran.append({"job": job_name, "job_type": job_type, "status": status})
        if status == "failed":
            result.failed += 1
        else:
            result.succeeded += 1
    logger.info(
        "scheduler pass instance=%s due=%d claimed=%d ok=%d failed=%d skipped=%d",
        result.instance_id,
        result.due,
        result.claimed,
        result.succeeded,
        result.failed,
        result.skipped,
    )
    return result


async def _run_forever(*, poll_seconds: int, instance_id: str, lease_seconds: int) -> None:
    sessionmaker = get_sessionmaker()
    logger.info(
        "scheduler start instance=%s poll=%ds lease=%ds", instance_id, poll_seconds, lease_seconds
    )
    try:
        while True:
            try:
                async with sessionmaker() as session:
                    await run_due_jobs(
                        session, instance_id=instance_id, lease_seconds=lease_seconds
                    )
            except Exception as exc:  # noqa: BLE001 - a bad pass must not stop the loop
                logger.warning("scheduler pass_error error=%s", exc)
            await asyncio.sleep(poll_seconds)
    except (KeyboardInterrupt, asyncio.CancelledError):  # pragma: no cover - signal path
        logger.info("scheduler stop instance=%s", instance_id)


async def _run_once(*, instance_id: str, lease_seconds: int) -> SchedulerPass:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        return await run_due_jobs(session, instance_id=instance_id, lease_seconds=lease_seconds)


async def _amain(once: bool, poll_seconds: int) -> None:
    settings = get_settings()
    instance_id = default_instance_id()
    lease_seconds = settings.scheduler_lease_seconds
    if once:
        result = await _run_once(instance_id=instance_id, lease_seconds=lease_seconds)
        print(
            f"scheduler once instance={result.instance_id} due={result.due} "
            f"claimed={result.claimed} ok={result.succeeded} failed={result.failed} "
            f"skipped={result.skipped}"
        )
    else:
        await _run_forever(
            poll_seconds=poll_seconds, instance_id=instance_id, lease_seconds=lease_seconds
        )
    await get_engine().dispose()


def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    parser = argparse.ArgumentParser(description="Run the in-process job scheduler.")
    parser.add_argument(
        "--once", action="store_true", help="Run one pass over due jobs, then exit."
    )
    parser.add_argument(
        "--poll-seconds",
        type=int,
        default=settings.scheduler_poll_seconds,
        help="Loop-mode poll interval (ignored with --once).",
    )
    args = parser.parse_args()
    asyncio.run(_amain(args.once, args.poll_seconds))


if __name__ == "__main__":
    main()
