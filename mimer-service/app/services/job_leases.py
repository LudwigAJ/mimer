"""Live running/leased scheduled-job read model.

The job-run timeline (``app/services/job_timeline.py``) covers *completed*
``job_runs``. This module is its live counterpart: a bounded, read-only view of
``scheduled_jobs`` lease state for the GUI Data Operations page —

    what is running right now?
    what is leased but possibly stuck?
    what lease expires soon, and which worker owns it?
    when did the last heartbeat happen?
    which scheduled jobs are due but not yet claimed?
    which due jobs are blocked by an active lease?

It is **observability only** (see AGENTS.md): no mutation, no unlock/kill, no
scheduler rewrite, no claim/release side effects. It does not re-implement the
scheduler — it classifies the existing lease columns the scheduler maintains
(``locked_at`` / ``locked_by`` / ``lock_expires_at`` / ``last_heartbeat_at`` /
``max_runtime_seconds`` / ``next_run_at`` / ``schedule_kind``). The single
``classify_lease`` / ``build_running_item`` pair is the *one* lease-classification
definition reused by ``/jobs/running``, the timeline (``include_running``),
``/scheduler/status`` and diagnostics, so those surfaces never disagree.

Queries are bounded — ``scheduled_jobs`` is a small, fixed set; we still fetch
only leased-or-due rows and cap the result (default 100 / max 500). Timezone
handling is explicit (SQLite round-trips naive datetimes).
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ScheduledJob
from app.schemas.job_timeline import (
    JobRunRecommendedAction,
    RunningJobsResponse,
    RunningJobsSummary,
    RunningJobTimelineItem,
)
from app.workers.scheduler import MANUAL

DEFAULT_LIMIT = 100
MAX_LIMIT = 500

# An *active* lease (not yet expired) whose worker appears dead: its heartbeat
# has not advanced for this long. Distinct from ``lock_expires_at`` (a hard,
# reclaimable expiry) — a stuck lease is still nominally held but unhealthy.
_STUCK_HEARTBEAT_SECONDS = 900

# Lease status codes (also documented on RunningJobTimelineItem.lease_status).
NOT_LEASED = "not_leased"
RUNNING = "running"
EXPIRED = "expired"
STUCK = "stuck"
DUE = "due"

# kind discriminator for a live row (``completed_run`` is the JobRunTimelineItem).
_KIND_BY_STATUS = {
    RUNNING: "running_lease",
    STUCK: "stuck_lease",
    EXPIRED: "expired_lease",
    DUE: "due_scheduled_job",
}
# Live statuses (everything except not_leased / unclassified).
_LIVE_STATUSES = (RUNNING, STUCK, EXPIRED, DUE)

# Urgency ordering for the live list (most urgent first): problems before
# healthy, healthy before merely due.
_URGENCY = {EXPIRED: 0, STUCK: 1, RUNNING: 2, DUE: 3}

# Recommended-action labels (codes -> human label). Labels only — nothing here is
# executed and there is deliberately NO unlock/kill/force action (see AGENTS.md).
ACTION_LABELS: dict[str, str] = {
    "wait_for_worker": "Wait for the worker to finish",
    "check_worker_health": "Check worker health",
    "open_scheduler": "Open scheduler status",
    "open_latest_run": "Open latest run",
    "open_job_detail": "Open scheduled job",
    "rerun_when_unlocked": "Re-run once the lease clears",
    "inspect_stuck_lease": "Inspect stuck lease",
}


def clamp_limit(limit: int | None) -> int:
    return max(1, min(limit or DEFAULT_LIMIT, MAX_LIMIT))


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


# --- classification ----------------------------------------------------------


def is_stuck_lease(job: ScheduledJob, *, now: datetime) -> bool:
    """An *active* lease (not expired) that looks unhealthy.

    Stuck = held past the job's own ``max_runtime_seconds`` watchdog, or the
    heartbeat has not advanced for ``_STUCK_HEARTBEAT_SECONDS`` — i.e. the worker
    probably died but the lease has not expired yet. A lease whose
    ``lock_expires_at`` has passed is classified ``expired`` (reclaimable), not
    stuck.
    """
    locked_at = _as_utc(job.locked_at)
    expires = _as_utc(job.lock_expires_at)
    if locked_at is None or expires is None or expires <= now:
        return False
    held = (now - locked_at).total_seconds()
    if job.max_runtime_seconds and held > job.max_runtime_seconds:
        return True
    heartbeat = _as_utc(job.last_heartbeat_at) or locked_at
    return (now - heartbeat).total_seconds() > _STUCK_HEARTBEAT_SECONDS


def _is_due(job: ScheduledJob, *, now: datetime) -> bool:
    """Active, non-manual, past its ``next_run_at`` (lease handled separately)."""
    next_run_at = _as_utc(job.next_run_at)
    return bool(
        job.is_active
        and job.schedule_kind != MANUAL
        and next_run_at is not None
        and next_run_at <= now
    )


def classify_lease(job: ScheduledJob, *, now: datetime) -> str:
    """Single source of truth: the lease status of one scheduled job.

    Buckets are mutually exclusive. A leased row is expired (hard, reclaimable) /
    stuck (active but unhealthy) / running (active, healthy); an unleased row is
    due (claimable now) / not_leased.
    """
    if job.locked_at is not None and job.locked_by is not None:
        expires = _as_utc(job.lock_expires_at)
        if expires is not None and expires <= now:
            return EXPIRED
        if is_stuck_lease(job, now=now):
            return STUCK
        return RUNNING
    return DUE if _is_due(job, now=now) else NOT_LEASED


def is_blocked_by_lease(job: ScheduledJob, lease_status: str, *, now: datetime) -> bool:
    """A leased (running/stuck) job whose ``next_run_at`` has already passed.

    The scheduler's ``due_jobs`` deliberately excludes leased rows, so these are
    *not* counted as due — they are held by an active lease and can only be
    (re)claimed once it clears.
    """
    if lease_status not in (RUNNING, STUCK):
        return False
    return _is_due(job, now=now)


def _severity(lease_status: str) -> str:
    if lease_status == EXPIRED:
        return "error"
    if lease_status == STUCK:
        return "warning"
    if lease_status == RUNNING:
        return "running"
    return "ok"


def _recommended_codes(lease_status: str, *, blocked: bool) -> list[str]:
    if lease_status == EXPIRED:
        codes = ["check_worker_health", "open_scheduler", "rerun_when_unlocked"]
    elif lease_status == STUCK:
        codes = ["inspect_stuck_lease", "check_worker_health", "open_scheduler"]
    elif lease_status == RUNNING:
        codes = ["wait_for_worker", "open_scheduler"]
    elif lease_status == DUE:
        codes = ["open_scheduler"]
    else:
        codes = []
    if blocked and "rerun_when_unlocked" not in codes:
        codes.append("rerun_when_unlocked")
    return codes


def _actions(codes: list[str]) -> list[JobRunRecommendedAction]:
    return [JobRunRecommendedAction(code=c, label=ACTION_LABELS.get(c, c)) for c in codes]


def build_running_item(job: ScheduledJob, *, now: datetime) -> RunningJobTimelineItem:
    """Project one scheduled job into a live timeline row (read-only)."""
    lease_status = classify_lease(job, now=now)
    blocked = is_blocked_by_lease(job, lease_status, now=now)
    locked_at = _as_utc(job.locked_at)
    expires = _as_utc(job.lock_expires_at)
    next_run_at = _as_utc(job.next_run_at)

    if lease_status == DUE:
        anchor = next_run_at
    else:
        anchor = locked_at
    age_seconds = int((now - anchor).total_seconds()) if anchor is not None else None
    seconds_until_expiry = (
        int((expires - now).total_seconds())
        if expires is not None and lease_status in (RUNNING, STUCK, EXPIRED)
        else None
    )

    return RunningJobTimelineItem(
        kind=_KIND_BY_STATUS.get(lease_status, "running_lease"),
        scheduled_job_id=job.id,
        name=job.name,
        job_type=job.job_type,
        source_name=job.source,
        lease_status=lease_status,
        severity=_severity(lease_status),
        schedule_kind=job.schedule_kind,
        locked_at=job.locked_at,
        locked_by=job.locked_by,
        lock_expires_at=job.lock_expires_at,
        last_heartbeat_at=job.last_heartbeat_at,
        max_runtime_seconds=job.max_runtime_seconds,
        next_run_at=job.next_run_at,
        last_status=job.last_status,
        started_at_for_timeline=anchor,
        age_seconds=age_seconds,
        seconds_until_expiry=seconds_until_expiry,
        is_expired=lease_status == EXPIRED,
        is_stuck=lease_status == STUCK,
        is_blocked_by_lease=blocked,
        recommended_actions=_actions(_recommended_codes(lease_status, blocked=blocked)),
    )


def _sort_key(item: RunningJobTimelineItem) -> tuple[int, int, int]:
    """Most urgent first: by lease-status urgency, then largest age, then id."""
    return (
        _URGENCY.get(item.lease_status, 99),
        -(item.age_seconds or 0),
        item.scheduled_job_id,
    )


# --- bounded reads -----------------------------------------------------------


async def _fetch_live_jobs(
    session: AsyncSession, *, now: datetime, include_due: bool
) -> list[ScheduledJob]:
    """Leased (any state) and, optionally, due scheduled jobs — bounded by design.

    ``scheduled_jobs`` is a small fixed set; we still constrain the WHERE to
    leased-or-due rows so we never project the whole table.
    """
    conditions = [ScheduledJob.locked_at.is_not(None)]
    if include_due:
        conditions.append(
            and_(
                ScheduledJob.is_active.is_(True),
                ScheduledJob.schedule_kind != MANUAL,
                ScheduledJob.next_run_at.is_not(None),
                ScheduledJob.next_run_at <= now,
            )
        )
    stmt = select(ScheduledJob).where(or_(*conditions)).order_by(ScheduledJob.id)
    return list((await session.execute(stmt)).scalars().all())


async def list_running_jobs(
    session: AsyncSession,
    *,
    include_due: bool = True,
    limit: int | None = DEFAULT_LIMIT,
    now: datetime | None = None,
) -> list[RunningJobTimelineItem]:
    """Live rows (running/stuck/expired, plus due when ``include_due``), urgent first.

    ``scheduled_jobs`` are global infrastructure, so there is no workspace filter:
    the workspace-scoped view returns the same global scheduler health (see the
    module docstring / AGENTS.md).
    """
    now = now or datetime.now(UTC)
    jobs = await _fetch_live_jobs(session, now=now, include_due=include_due)
    items = [build_running_item(j, now=now) for j in jobs]
    items = [i for i in items if i.lease_status in _LIVE_STATUSES]
    items.sort(key=_sort_key)
    return items[: clamp_limit(limit)]


async def list_due_scheduled_jobs(
    session: AsyncSession, *, limit: int | None = DEFAULT_LIMIT, now: datetime | None = None
) -> list[RunningJobTimelineItem]:
    """Due (active, non-manual, past next_run_at, not leased) scheduled jobs."""
    now = now or datetime.now(UTC)
    items = await list_running_jobs(session, include_due=True, limit=MAX_LIMIT, now=now)
    due = [i for i in items if i.lease_status == DUE]
    return due[: clamp_limit(limit)]


async def list_job_leases(
    session: AsyncSession,
    *,
    status: str | None = None,
    include_expired: bool = True,
    limit: int | None = DEFAULT_LIMIT,
    now: datetime | None = None,
) -> list[RunningJobTimelineItem]:
    """Lease rows, optionally filtered to a single ``status`` (running/stuck/expired/due)."""
    now = now or datetime.now(UTC)
    items = await list_running_jobs(session, include_due=True, limit=MAX_LIMIT, now=now)
    if not include_expired:
        items = [i for i in items if i.lease_status != EXPIRED]
    if status is not None:
        items = [i for i in items if i.lease_status == status]
    return items[: clamp_limit(limit)]


def summarize(items: list[RunningJobTimelineItem]) -> RunningJobsSummary:
    """Bounded counts over already-classified live rows."""
    return RunningJobsSummary(
        running_count=sum(1 for i in items if i.lease_status == RUNNING),
        expired_lease_count=sum(1 for i in items if i.lease_status == EXPIRED),
        stuck_lease_count=sum(1 for i in items if i.lease_status == STUCK),
        due_count=sum(1 for i in items if i.lease_status == DUE),
        blocked_by_lease_count=sum(1 for i in items if i.is_blocked_by_lease),
        total=len(items),
    )


async def lease_summary_counts(
    session: AsyncSession, *, now: datetime | None = None
) -> RunningJobsSummary:
    """The shared count helper for scheduler/status + diagnostics (all live rows)."""
    now = now or datetime.now(UTC)
    items = await list_running_jobs(session, include_due=True, limit=MAX_LIMIT, now=now)
    return summarize(items)


async def running_jobs_response(
    session: AsyncSession,
    *,
    scope_type: str,
    scope_id: int | None,
    include_due: bool = True,
    limit: int | None = DEFAULT_LIMIT,
    now: datetime | None = None,
) -> RunningJobsResponse:
    """Assemble the ``/jobs/running`` response (rows + summary) for a scope.

    The summary always reflects the full live set (so counts are stable regardless
    of ``include_due`` / ``limit``); ``jobs`` is the bounded, filtered list.
    """
    now = now or datetime.now(UTC)
    summary = await lease_summary_counts(session, now=now)
    jobs = await list_running_jobs(session, include_due=include_due, limit=limit, now=now)
    return RunningJobsResponse(
        scope_type=scope_type,
        scope_id=scope_id,
        now=now,
        limit=clamp_limit(limit),
        summary=summary,
        jobs=jobs,
    )


async def leases_response(
    session: AsyncSession,
    *,
    status: str | None = None,
    limit: int | None = DEFAULT_LIMIT,
    now: datetime | None = None,
) -> RunningJobsResponse:
    """Assemble the ``/jobs/leases`` response (status-filtered rows + full summary)."""
    now = now or datetime.now(UTC)
    summary = await lease_summary_counts(session, now=now)
    rows = await list_job_leases(session, status=status, limit=limit, now=now)
    return RunningJobsResponse(
        scope_type="global",
        scope_id=None,
        now=now,
        limit=clamp_limit(limit),
        summary=summary,
        jobs=rows,
    )
