"""Job-run timeline / failure drilldown read schemas.

A bounded, GUI-friendly read model over *all* ``job_runs`` (not just onboarding):
"what ran recently, which failed, what did it do, which source fetches happened
near it, was a budget/backoff involved, what to inspect next". These shapes are a
contract for the GUI Data Operations page — keep them stable and bounded.

Everything surfaced here is secrets-safe: messages / payloads / fetch-log request
keys / error strings are masked defensively (see ``app/services/secret_masking.py``).
This is observability only — never a workflow engine (see AGENTS.md).
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class JobRunRecommendedAction(BaseModel):
    """A next-action *hint* for the GUI (a code + label; never executed here)."""

    code: str
    label: str


class JobRunRelatedEntity(BaseModel):
    """A typed deep-link target inferred from the run's scope columns."""

    entity_type: str  # workspace | fund | fund_listing | scheduled_job
    entity_id: int
    label: str | None = None


class JobRunFetchLog(BaseModel):
    """A bounded, masked projection of one ``source_fetch_logs`` row."""

    id: int
    source_name: str
    request_kind: str
    request_key: str
    endpoint_label: str | None = None
    method: str | None = None
    # started | success | failed | rate_limited | cache_hit
    status: str
    http_status: int | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    duration_ms: int | None = None
    records_inserted: int | None = None
    records_updated: int | None = None
    records_failed: int | None = None
    error_code: str | None = None
    error_message: str | None = None
    rate_limited: bool = False
    backoff_until: datetime | None = None
    cache_hit: bool = False


class JobRunSourceBudgetContext(BaseModel):
    """Read-only source budget / backoff state relevant to a run's source."""

    source_name: str
    enabled: bool
    # Current fetch decision reason: ok | in_backoff | min_delay | rate_limited_* |
    # disabled | no_budget_configured.
    status: str
    allowed: bool
    wait_seconds: float = 0.0
    backoff_until: datetime | None = None
    next_allowed_at: datetime | None = None
    # Recent (rolling 24h) fetch-log signals for this source.
    recent_failures: int = 0
    cache_hits: int = 0
    rate_limited_recently: bool = False


class JobRunStage(BaseModel):
    """One stage of an orchestration run (typed; from ``payload_json``).

    Populated for ``instrument_onboarding`` parent runs from their structured
    payload — never parsed from the free-text message.
    """

    stage: str
    label: str | None = None
    # planned | running | success | partial_success | failed | skipped | blocked
    status: str
    reason: str | None = None
    source: str | None = None
    source_mode: str | None = None
    expected_offline: bool = True
    started_at: datetime | None = None
    finished_at: datetime | None = None
    duration_ms: int | None = None
    child_run_ids: list[int] = []
    records_inserted: int = 0
    records_updated: int = 0
    records_failed: int = 0
    blockers: list[str] = []
    message: str | None = None


class JobRunChild(BaseModel):
    """A child worker ``job_run`` produced by an orchestration run's stage."""

    run_id: int
    job_type: str
    status: str
    severity: str
    source: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    duration_ms: int | None = None
    records_inserted: int = 0
    records_updated: int = 0
    records_failed: int = 0
    message: str | None = None


class JobRunTimelineItem(BaseModel):
    """Bounded summary row of one job run (timeline / failure list view)."""

    run_id: int
    job_type: str
    workspace_id: int | None = None
    fund_id: int | None = None
    fund_listing_id: int | None = None
    scheduled_job_id: int | None = None
    # queued | running | success | partial_success | failed | success_stub | planned
    status: str
    # error | warning | running | ok (derived from status)
    severity: str
    source_name: str | None = None
    # workspace:<id> | fund:<id> | listing:<id> | global
    scope_label: str
    started_at: datetime | None = None
    finished_at: datetime | None = None
    duration_ms: int | None = None
    records_inserted: int = 0
    records_updated: int = 0
    records_failed: int = 0
    message: str | None = None
    # True for orchestration runs (instrument_onboarding) with child stages.
    is_orchestration: bool = False
    has_payload: bool = False
    has_children: bool = False
    child_run_count: int = 0
    # Coarse hint: a correlatable source produced fetch logs in this run's window
    # (the run detail does the precise, bounded correlation).
    has_fetch_logs: bool = False
    # Primary next-action code (full list + labels on the detail).
    recommended_action: str | None = None


class JobRunDetail(BaseModel):
    """One job run with scope, payload, stages, child runs, fetch-log + budget context."""

    summary: JobRunTimelineItem
    # Masked structured payload (orchestration metadata) — None for runs without one.
    payload: dict | None = None
    # Typed stage rows (orchestration runs only; empty otherwise).
    stages: list[JobRunStage] = []
    # Child worker runs (orchestration runs only; empty otherwise).
    child_runs: list[JobRunChild] = []
    # Source fetches near this run (bounded, latest-first, masked).
    related_fetch_logs: list[JobRunFetchLog] = []
    # exact | payload | time_window_source | unavailable (honest about exactness).
    fetch_log_correlation: str = "unavailable"
    source_budget_context: JobRunSourceBudgetContext | None = None
    related_entities: list[JobRunRelatedEntity] = []
    recommended_actions: list[JobRunRecommendedAction] = []
    # True for an orchestration run predating structured payloads (pre-0015).
    legacy_metadata: bool = False


class RunningJobTimelineItem(BaseModel):
    """A currently-leased / due ``scheduled_jobs`` row, as a live timeline row.

    Complements the completed-run ``JobRunTimelineItem`` so the GUI Data
    Operations page can show *what is happening now* (running / leased / stuck /
    expired / due) alongside *what ran recently*. Derived purely from the
    ``scheduled_jobs`` lease columns by ``app/services/job_leases.py`` — read-only,
    no mutation, no unlock/kill. ``scheduled_jobs`` are global shared
    infrastructure, so these rows carry no ``workspace_id`` even in a
    workspace-scoped view (the scheduler health is global; see AGENTS.md).
    """

    # running_lease | due_scheduled_job | expired_lease | stuck_lease
    # (``completed_run`` is the discriminator for the JobRunTimelineItem rows).
    kind: str
    scheduled_job_id: int
    name: str
    job_type: str
    # Always None today (scheduled_jobs are global) — kept for shape symmetry with
    # the completed-run rows so the GUI can treat both uniformly.
    workspace_id: int | None = None
    fund_id: int | None = None
    fund_listing_id: int | None = None
    # The scheduled job's *category* hint (issuer/market_data/fx); not a fetch source.
    source_name: str | None = None
    # not_leased | leased | running | expired | stuck | due | blocked_by_lease | unknown
    lease_status: str
    # error | warning | running | ok (derived; mirrors JobRunTimelineItem.severity).
    severity: str
    schedule_kind: str
    locked_at: datetime | None = None
    locked_by: str | None = None
    lock_expires_at: datetime | None = None
    last_heartbeat_at: datetime | None = None
    max_runtime_seconds: int | None = None
    next_run_at: datetime | None = None
    last_status: str | None = None
    # The timestamp used to place this row on the timeline (locked_at for a lease,
    # next_run_at for a due job).
    started_at_for_timeline: datetime | None = None
    # Age of the live state in seconds (since locked_at for a lease; since
    # next_run_at for a due job — i.e. how overdue it is).
    age_seconds: int | None = None
    # Seconds until the lease expires (negative once expired); None for due rows.
    seconds_until_expiry: int | None = None
    is_expired: bool = False
    is_stuck: bool = False
    # A leased (running/stuck) job whose next_run_at has passed: the scheduler
    # cannot claim it until the lease clears.
    is_blocked_by_lease: bool = False
    # Next-action *hints* (codes + labels). Labels only — never unlock/kill; the
    # GUI navigates, nothing is executed (see AGENTS.md).
    recommended_actions: list[JobRunRecommendedAction] = []


class RunningJobsSummary(BaseModel):
    """Bounded counts of live ``scheduled_jobs`` lease states."""

    running_count: int = 0
    expired_lease_count: int = 0
    stuck_lease_count: int = 0
    due_count: int = 0
    blocked_by_lease_count: int = 0
    total: int = 0


class RunningJobsResponse(BaseModel):
    """Live running/leased/stuck/expired/due scheduled jobs for a scope."""

    scope_type: str  # global | workspace
    scope_id: int | None = None
    now: datetime
    limit: int
    summary: RunningJobsSummary
    jobs: list[RunningJobTimelineItem] = []


class JobRunTimelineResponse(BaseModel):
    """Bounded, latest-first job-run timeline for a scope.

    ``runs`` is the completed ``job_runs`` view (unchanged, backward compatible).
    With ``include_running=true`` the response is enriched with ``live_jobs``
    (currently-leased / due scheduled jobs, urgent first) and a ``running_summary``
    — both empty/None otherwise so existing clients are unaffected.
    """

    scope_type: str  # global | workspace
    scope_id: int | None = None
    limit: int
    count: int
    runs: list[JobRunTimelineItem] = []
    # Populated only when include_running=true (see RunningJobTimelineItem).
    include_running: bool = False
    live_jobs: list[RunningJobTimelineItem] = []
    running_summary: RunningJobsSummary | None = None


class JobRunFailureResponse(BaseModel):
    """Bounded, latest-first failed/partial job runs for a scope."""

    scope_type: str  # global | workspace
    scope_id: int | None = None
    limit: int
    count: int
    failures: list[JobRunTimelineItem] = []
