"""Scheduler operational endpoints (status / due jobs / manual one-shot).

Exposes the in-process scheduler's state to the GUI. ``run-once`` triggers a
single pass synchronously using the request session — the same code the
``app.workers.scheduler`` CLI runs in its loop.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter

from app.api.deps import SessionDep
from app.core.config import get_settings
from app.schemas.common import ListResponse
from app.schemas.job import ScheduledJobRead
from app.schemas.scheduler import SchedulerRunResult, SchedulerStatus
from app.services import job_leases as leases_service
from app.services import jobs as jobs_service
from app.workers import scheduler as scheduler_worker

router = APIRouter(prefix="/scheduler", tags=["scheduler"])


@router.get("/status", response_model=SchedulerStatus)
async def scheduler_status(session: SessionDep) -> SchedulerStatus:
    now = datetime.now(UTC)
    settings = get_settings()
    jobs = await jobs_service.list_jobs(session)
    due = await scheduler_worker.due_jobs(session, now=now)
    # One shared lease classifier feeds status / diagnostics / timeline so the
    # running / stuck / expired / blocked definitions never disagree.
    lease_summary = await leases_service.lease_summary_counts(session, now=now)

    def _leased(job) -> bool:
        expires = job.lock_expires_at
        if expires is None:
            return False
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=UTC)
        return expires > now

    def _next_run(job) -> datetime | None:
        nra = job.next_run_at
        if nra is None or not job.is_active or job.schedule_kind == "manual":
            return None
        return nra if nra.tzinfo is not None else nra.replace(tzinfo=UTC)

    upcoming = [dt for j in jobs if (dt := _next_run(j)) is not None]

    return SchedulerStatus(
        now=now,
        active_jobs=sum(1 for j in jobs if j.is_active),
        manual_jobs=sum(1 for j in jobs if j.schedule_kind == "manual"),
        due_jobs=len(due),
        leased_jobs=sum(1 for j in jobs if _leased(j)),
        running_leases=lease_summary.running_count,
        stuck_leases=lease_summary.stuck_lease_count,
        expired_leases=lease_summary.expired_lease_count,
        blocked_by_lease=lease_summary.blocked_by_lease_count,
        next_due_at=min(upcoming) if upcoming else None,
        poll_seconds=settings.scheduler_poll_seconds,
        lease_seconds=settings.scheduler_lease_seconds,
        jobs=[jobs_service.serialize_job(j) for j in jobs],
    )


@router.get("/due-jobs", response_model=ListResponse[ScheduledJobRead])
async def scheduler_due_jobs(session: SessionDep) -> ListResponse[ScheduledJobRead]:
    due = await scheduler_worker.due_jobs(session)
    return ListResponse.of([jobs_service.serialize_job(j) for j in due])


@router.post("/run-once", response_model=SchedulerRunResult)
async def scheduler_run_once(session: SessionDep) -> SchedulerRunResult:
    """Run one scheduler pass now (claim + run all due jobs), then return a summary."""
    result = await scheduler_worker.run_due_jobs(session, instance_id="api:run-once")
    return SchedulerRunResult.model_validate(result.as_dict())
