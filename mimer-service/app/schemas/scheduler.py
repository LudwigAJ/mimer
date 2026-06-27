"""Scheduler status / run-once schemas for the GUI."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel

from app.schemas.job import ScheduledJobRead


class SchedulerStatus(BaseModel):
    now: datetime
    active_jobs: int
    manual_jobs: int
    due_jobs: int
    # Active leases held right now (lock_expires_at > now) = running + stuck.
    leased_jobs: int
    # Live lease breakdown (shared lease classifier — see app/services/job_leases.py).
    # ``running_leases`` are healthy active leases; ``stuck_leases`` are active but
    # unhealthy (worker watchdog/heartbeat); ``expired_leases`` have passed
    # ``lock_expires_at`` and are reclaimable. ``blocked_by_lease`` are due jobs the
    # scheduler cannot claim because a lease is still held.
    running_leases: int = 0
    stuck_leases: int = 0
    expired_leases: int = 0
    blocked_by_lease: int = 0
    # Soonest upcoming next_run_at among active, non-manual jobs (None if none).
    next_due_at: datetime | None = None
    poll_seconds: int
    lease_seconds: int
    jobs: list[ScheduledJobRead]


class SchedulerRanItem(BaseModel):
    job: str
    job_type: str
    status: str


class SchedulerRunResult(BaseModel):
    instance_id: str
    due: int
    claimed: int
    succeeded: int
    failed: int
    skipped: int
    ran: list[SchedulerRanItem]
