"""Instrument-onboarding / data-readiness schemas.

Onboarding is an *orchestration* concept, not a new analytics engine: it takes a
workspace/fund from "not data-ready" to "data-ready enough for charts / exposure
/ performance" by coordinating the existing ingestion + recompute workers
(holdings -> constituent identity -> constituent prices -> FX -> exposure ->
alerts). The plan is read-only and driven by current DB state + the market-data
planner; execution calls the existing worker dispatch (never duplicates it).

Readiness here is **data-quality / coverage**, never investment quality.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel

from app.schemas.common import DecimalStr

# --- vocabulary --------------------------------------------------------------

# Conceptual onboarding stages, in dependency order.
STAGE_HOLDINGS = "holdings"
STAGE_IDENTITY = "constituent_identity"
STAGE_PRICES = "constituent_prices"
STAGE_FX = "fx"
STAGE_EXPOSURE = "exposure_recompute"
STAGE_ALERTS = "alerts"
STAGES = (
    STAGE_HOLDINGS,
    STAGE_IDENTITY,
    STAGE_PRICES,
    STAGE_FX,
    STAGE_EXPOSURE,
    STAGE_ALERTS,
)

# Plan-time stage status.
STATUS_READY = "ready"  # nothing to do for this stage
STATUS_NEEDED = "needed"  # work would run
STATUS_SKIPPED = "skipped"  # intentionally not run (flag / not applicable)
STATUS_BLOCKED = "blocked"  # cannot proceed without a human / upstream fix
STATUS_COMPLETE = "complete"  # already satisfied/fresh

# Plan-level status.
PLAN_READY = "ready"
PLAN_NEEDS_WORK = "needs_work"
PLAN_BLOCKED = "blocked"
PLAN_EMPTY = "empty"  # nothing to onboard (no positions / no holdings)

# Run-time stage status (GUI-friendly; persisted in the parent run payload).
RUN_PLANNED = "planned"
RUN_RUNNING = "running"
RUN_SUCCESS = "success"
RUN_PARTIAL = "partial_success"
RUN_FAILED = "failed"
RUN_SKIPPED = "skipped"
RUN_BLOCKED = "blocked"

# Structured stage reasons (machine-readable; the human-readable text stays in
# the stage ``message``). Prefer these over parsing free text.
REASON_ALREADY_READY = "already_ready"
REASON_NOT_NEEDED = "not_needed"
REASON_NO_POSITIONS = "blocked_by_no_positions"
REASON_MISSING_HOLDINGS = "blocked_by_missing_holdings"
REASON_UNRESOLVED_IDENTITY = "blocked_by_unresolved_identity"
REASON_MISSING_PRICES = "blocked_by_missing_prices"
REASON_MISSING_FX = "blocked_by_missing_fx"
REASON_SOURCE_BUDGET_BLOCKED = "source_budget_blocked"
REASON_WORKER_FAILED = "worker_failed"
REASON_SKIPPED_BY_FLAG = "skipped_by_flag"
REASON_BLOCKED = "blocked"  # generic fallback when no specific code maps

# ``payload_json`` discriminator + schema version for parent onboarding runs.
ONBOARDING_PAYLOAD_KIND = "instrument_onboarding"
ONBOARDING_PAYLOAD_VERSION = 1


class OnboardingStage(BaseModel):
    """One conceptual data-readiness stage in the plan."""

    name: str
    # The worker the stage would run (None for purely-derived/no-op stages).
    job_type: str | None = None
    # ready | needed | skipped | blocked | complete
    status: str
    reason: str
    # Machine-readable blocker codes (e.g. missing_holdings, ambiguous_identity).
    blockers: list[str] = []
    # The source the stage would use in the chosen source mode (None = DB-only).
    source: str | None = None
    # True when this stage makes no external network call (fixture / DB-only).
    expected_offline: bool = True
    # Estimated units of work (deduped requests / funds), bounded.
    estimated_requests: int = 0
    # Small bounded counts (funds_needing, unresolved, missing, stale, ...).
    detail: dict[str, int] = {}


class OnboardingReadiness(BaseModel):
    """Data-readiness / coverage summary (NOT investment quality)."""

    holdings_ready: bool = False
    identity_ready: bool = False
    constituent_prices_ready: bool = False
    fx_ready: bool = False
    exposure_ready: bool = False
    top_holding_performance_ready: bool = False
    # Weight-based coverage fractions from the latest exposure snapshot (nested
    # holdings >= identity >= price >= fx). None when there is no snapshot / for
    # fund scope where coverage is per holding workspace.
    holdings_coverage_weight: DecimalStr | None = None
    identity_coverage_weight: DecimalStr | None = None
    price_coverage_weight: DecimalStr | None = None
    fx_coverage_weight: DecimalStr | None = None
    exposure_snapshot_count: int = 0
    latest_exposure_snapshot_at: datetime | None = None
    missing_top_constituent_prices: int = 0
    ambiguous_constituents: int = 0
    # Fraction of the six readiness booleans that are satisfied (0..1).
    score: DecimalStr | None = None


class OnboardingPlanResponse(BaseModel):
    """Read-only onboarding plan for a workspace or fund scope."""

    scope: str  # workspace | fund
    workspace_id: int | None = None
    fund_id: int | None = None
    base_currency: str | None = None
    source_mode: str  # fixture | live
    # ready | needs_work | blocked | empty
    status: str
    stages: list[OnboardingStage]
    readiness: OnboardingReadiness
    blocking_issues: list[str] = []
    warnings: list[str] = []
    estimated_requests_by_source: dict[str, int] = {}
    jobs_that_would_run: list[str] = []
    next_recommended_action: str | None = None


class OnboardingStageRun(BaseModel):
    """Outcome of one stage during an execution run."""

    name: str
    job_type: str | None = None
    # complete-state mirrors plan statuses plus run statuses:
    # success | partial_success | failed | skipped | blocked
    status: str
    reason: str | None = None
    # Child job_run ids this stage produced (correlation; queryable via /jobs/runs).
    child_run_ids: list[int] = []
    records_inserted: int = 0
    records_updated: int = 0
    records_failed: int = 0
    message: str | None = None


class OnboardingRunResponse(BaseModel):
    """Result of executing (or planning) an onboarding run."""

    scope: str
    workspace_id: int | None = None
    fund_id: int | None = None
    source_mode: str
    plan_only: bool = False
    # The parent orchestration job_run id (None for plan-only).
    parent_job_run_id: int | None = None
    # planned | success | partial_success | failed
    status: str
    stages: list[OnboardingStageRun] = []
    readiness: OnboardingReadiness
    message: str | None = None
    next_recommended_action: str | None = None


class OnboardingStatus(BaseModel):
    """Compact onboarding status block for the dashboard / status endpoint."""

    scope: str = "workspace"
    workspace_id: int | None = None
    # ready | needs_work | blocked | empty
    status: str = "empty"
    readiness: OnboardingReadiness
    last_run_id: int | None = None
    last_run_status: str | None = None
    last_run_at: datetime | None = None
    # Latest-run observability overlay (from the parent run's structured payload;
    # None for legacy runs without stage metadata).
    last_run_duration_ms: int | None = None
    last_run_failed_stage: str | None = None
    stages_needing_attention: list[str] = []
    next_recommended_action: str | None = None


# --- onboarding run history / observability read model -----------------------


class OnboardingChildJobRun(BaseModel):
    """A child worker ``job_run`` produced by one onboarding stage."""

    run_id: int
    job_type: str
    status: str
    source: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    duration_ms: int | None = None
    records_inserted: int = 0
    records_updated: int = 0
    records_failed: int = 0
    message: str | None = None


class OnboardingStageRunDetail(BaseModel):
    """One stage of a recorded onboarding run (typed; not parsed from text)."""

    stage: str
    label: str | None = None
    # planned | running | success | partial_success | failed | skipped | blocked
    status: str
    # Structured reason code (see REASON_* in this module); None when not set.
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


class OnboardingRunSummary(BaseModel):
    """Bounded summary row of a recorded onboarding run (list view)."""

    run_id: int
    workspace_id: int | None = None
    fund_id: int | None = None
    scope_type: str | None = None  # workspace | fund
    scope_id: int | None = None
    # success | partial_success | failed | running | planned (parent run status)
    status: str
    source_mode: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    duration_ms: int | None = None
    stage_count: int = 0
    success_count: int = 0
    failed_count: int = 0
    blocked_count: int = 0
    skipped_count: int = 0
    child_run_count: int = 0
    next_recommended_action: str | None = None
    message: str | None = None
    # True for pre-0015 runs (no structured stage payload); stages will be empty.
    legacy_metadata: bool = False


class OnboardingRunDetail(OnboardingRunSummary):
    """A recorded onboarding run with its stages + child job runs."""

    plan_only: bool = False
    blocking_issues: list[str] = []
    stages: list[OnboardingStageRunDetail] = []
    child_runs: list[OnboardingChildJobRun] = []


class OnboardingRunListResponse(BaseModel):
    """Bounded list of onboarding runs for a workspace or fund scope."""

    scope_type: str  # workspace | fund
    scope_id: int
    limit: int
    count: int
    runs: list[OnboardingRunSummary] = []
