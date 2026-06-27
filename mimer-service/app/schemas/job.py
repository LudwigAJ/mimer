"""Scheduled job / job run schemas."""

from __future__ import annotations

from datetime import datetime

from app.schemas.common import ORMModel


class ScheduledJobRead(ORMModel):
    id: int
    name: str
    job_type: str
    # ``source`` is the scheduled job's *category* hint (issuer/market_data/fx).
    source: str | None
    schedule_cron: str | None
    # manual | hourly | daily | weekly | interval.
    schedule_kind: str
    interval_seconds: int | None
    timezone: str
    is_active: bool
    last_run_at: datetime | None
    next_run_at: datetime | None
    # Outcome of the most recent scheduler-driven run.
    last_status: str | None
    # Lease / running state (for the GUI to show "running"/"locked").
    locked_by: str | None
    locked_at: datetime | None
    lock_expires_at: datetime | None
    last_heartbeat_at: datetime | None
    misfire_policy: str
    created_at: datetime
    updated_at: datetime
    # Derived (see app/services/capabilities.py); set by jobs.serialize_job.
    # real | fixture | stub | planned.
    implementation_status: str | None = None
    # The provider adapter a real/fixture worker would use (None for stub/planned).
    configured_source: str | None = None


class JobRunRead(ORMModel):
    id: int
    scheduled_job_id: int | None
    # Optional backfill target (set for instrument-scoped runs).
    fund_id: int | None
    fund_listing_id: int | None
    job_type: str
    source: str | None
    status: str
    started_at: datetime | None
    finished_at: datetime | None
    message: str | None
    records_inserted: int | None
    records_updated: int | None
    records_failed: int | None
