"""Bounded job-run timeline / failure drilldown read model.

Generalises observability across *all* ``job_runs`` (every worker, not just
onboarding) for the GUI Data Operations page. It answers, without each page
querying many endpoints or parsing free text:

    What ran recently? Which failed? Which is running?
    What did this run do (scope / counts / stages / child runs)?
    Which source fetches happened during/near it?
    Was a source budget / backoff involved?
    What should the user inspect next?

This is strictly a **read model** (see AGENTS.md): no writes, no network, no
per-instrument compute. Queries are bounded — latest-first, capped ``limit``,
indexed scope/`(job_type, id)` filters. Orchestration runs
(``instrument_onboarding``) expand into typed stages + child runs from the
structured ``payload_json`` (never the message). Fetch-log correlation is
*approximate* (source + time window) and labelled honestly. Everything surfaced
is masked defensively (``app/services/secret_masking.py``). It is observability
only — never a workflow engine (no DAG runtime, no retries/branching).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFoundError
from app.db.models import JobRun, SourceFetchLog
from app.schemas.job_timeline import (
    JobRunChild,
    JobRunDetail,
    JobRunFailureResponse,
    JobRunFetchLog,
    JobRunRecommendedAction,
    JobRunRelatedEntity,
    JobRunSourceBudgetContext,
    JobRunStage,
    JobRunTimelineItem,
    JobRunTimelineResponse,
    RunningJobsSummary,
    RunningJobTimelineItem,
)
from app.services import instrument_onboarding as onboarding_service
from app.services import job_leases as job_leases_service
from app.services import source_budget as source_budget_service
from app.services import workspaces as workspaces_service
from app.services.secret_masking import mask_json, mask_text

ONBOARDING_JOB = onboarding_service.ONBOARDING_JOB

DEFAULT_LIMIT = 100
MAX_LIMIT = 500
FETCH_LOG_DEFAULT = 25
FETCH_LOG_MAX = 100

# Statuses that count as a failure for the failure drilldown.
FAILURE_STATUSES = ("failed", "partial_success")
_RUNNING_STATUSES = ("running", "queued")

# Small buffer so a fetch finishing just after the run still correlates.
_FETCH_BUFFER_SECONDS = 5
# Rolling window for the budget-context "recent" fetch-log counts.
_RECENT_FETCH_SECONDS = 86400

# fetch_log_correlation method codes (honest about exactness).
CORR_EXACT = "exact"
CORR_PAYLOAD = "payload"
CORR_TIME_WINDOW = "time_window_source"
CORR_UNAVAILABLE = "unavailable"


def clamp_limit(limit: int | None) -> int:
    return max(1, min(limit or DEFAULT_LIMIT, MAX_LIMIT))


def _clamp_fetch_limit(limit: int | None) -> int:
    return max(1, min(limit or FETCH_LOG_DEFAULT, FETCH_LOG_MAX))


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _parse_dt(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None
    return None


# --- derivations -------------------------------------------------------------


def _severity(status: str) -> str:
    if status == "failed":
        return "error"
    if status == "partial_success":
        return "warning"
    if status in _RUNNING_STATUSES:
        return "running"
    return "ok"


def _scope_label(run: JobRun) -> str:
    if run.workspace_id is not None:
        return f"workspace:{run.workspace_id}"
    if run.fund_id is not None:
        return f"fund:{run.fund_id}"
    if run.fund_listing_id is not None:
        return f"listing:{run.fund_listing_id}"
    return "global"


def _is_orchestration(run: JobRun) -> bool:
    return run.job_type == ONBOARDING_JOB


def _payload(run: JobRun) -> dict | None:
    return run.payload_json if isinstance(run.payload_json, dict) else None


def _onboarding_child_ids(run: JobRun) -> list[int]:
    payload = _payload(run)
    if not payload:
        return []
    ids: set[int] = set()
    for st in payload.get("stages") or []:
        if isinstance(st, dict):
            for cid in st.get("child_run_ids") or []:
                try:
                    ids.add(int(cid))
                except (TypeError, ValueError):
                    continue
    return sorted(ids)


# --- recommended actions -----------------------------------------------------

ACTION_LABELS: dict[str, str] = {
    "open_fetch_logs": "Open source fetch logs",
    "check_source_budget": "Check source budget / backoff",
    "open_source_budget": "Open source budget",
    "rerun_job": "Re-run this job",
    "rerun_identity_resolution": "Re-run constituent identity resolution",
    "rerun_price_ingestion": "Re-run constituent EOD price ingestion",
    "open_missing_prices": "Open missing constituent prices",
    "rerun_exposure": "Re-run exposure recompute",
    "check_missing_prices_fx": "Check missing prices / FX",
    "open_onboarding_run": "Open onboarding run detail",
    "open_diagnostics": "Open diagnostics",
    "run_next_recommended_stage": "Run next recommended onboarding stage",
    "wait_for_backoff": "Wait for source backoff to clear",
    "resolve_identity": "Resolve constituent identity",
    "fetch_prices": "Fetch constituent prices",
    "check_diagnostics": "Check diagnostics",
}


def recommended_action_codes(run: JobRun, *, source_in_backoff: bool = False) -> list[str]:
    """Deterministic next-action codes from job type / status / source context.

    Pure (no DB / network); the GUI maps codes to navigation. Nothing is executed
    from these — they are hints only (see AGENTS.md: observability, not a workflow
    engine).
    """
    failed = run.status == "failed"
    partial = run.status == "partial_success"
    jt = run.job_type
    actions: list[str] = []

    if source_in_backoff:
        actions += ["wait_for_backoff", "open_source_budget", "open_fetch_logs"]

    if not (failed or partial):
        # A clean (or running/stub/planned) run needs no remediation.
        return _dedupe(actions)

    if jt == ONBOARDING_JOB:
        actions += ["open_onboarding_run", "open_diagnostics", "run_next_recommended_stage"]
    elif jt == "constituent_identity_resolution":
        actions += ["check_source_budget", "open_fetch_logs", "rerun_identity_resolution"]
    elif jt in ("constituent_eod_price_ingestion", "instrument_eod_price_ingestion") and partial:
        actions += ["open_missing_prices", "open_source_budget", "rerun_price_ingestion"]
    elif jt in ("constituent_eod_price_ingestion", "instrument_eod_price_ingestion"):
        actions += ["check_source_budget", "open_fetch_logs", "rerun_price_ingestion"]
    elif jt == "exposure_recompute":
        actions += ["open_diagnostics", "check_missing_prices_fx", "rerun_exposure"]
    elif jt == "fx_ingestion":
        actions += ["open_fetch_logs", "check_source_budget", "rerun_job"]
    else:
        actions += ["open_fetch_logs", "check_source_budget", "rerun_job"]

    return _dedupe(actions)


def _dedupe(codes: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for code in codes:
        if code not in seen:
            seen.add(code)
            out.append(code)
    return out


def _actions(codes: list[str]) -> list[JobRunRecommendedAction]:
    return [JobRunRecommendedAction(code=c, label=ACTION_LABELS.get(c, c)) for c in codes]


# --- summary / child projections ---------------------------------------------


def summarise_job_run(run: JobRun, *, has_fetch_logs: bool = False) -> JobRunTimelineItem:
    """Bounded, masked summary of one run (uses only the run row)."""
    child_ids = _onboarding_child_ids(run)
    codes = recommended_action_codes(run)
    return JobRunTimelineItem(
        run_id=run.id,
        job_type=run.job_type,
        workspace_id=run.workspace_id,
        fund_id=run.fund_id,
        fund_listing_id=run.fund_listing_id,
        scheduled_job_id=run.scheduled_job_id,
        status=run.status,
        severity=_severity(run.status),
        source_name=run.source,
        scope_label=_scope_label(run),
        started_at=run.started_at,
        finished_at=run.finished_at,
        duration_ms=onboarding_service.run_duration_ms(run),
        records_inserted=run.records_inserted or 0,
        records_updated=run.records_updated or 0,
        records_failed=run.records_failed or 0,
        message=mask_text(run.message),
        is_orchestration=_is_orchestration(run),
        has_payload=_payload(run) is not None,
        has_children=bool(child_ids),
        child_run_count=len(child_ids),
        has_fetch_logs=has_fetch_logs,
        recommended_action=codes[0] if codes else None,
    )


def _child(run: JobRun) -> JobRunChild:
    duration_ms: int | None = None
    start, finish = _as_utc(run.started_at), _as_utc(run.finished_at)
    if start and finish:
        duration_ms = max(0, int((finish - start).total_seconds() * 1000))
    return JobRunChild(
        run_id=run.id,
        job_type=run.job_type,
        status=run.status,
        severity=_severity(run.status),
        source=run.source,
        started_at=run.started_at,
        finished_at=run.finished_at,
        duration_ms=duration_ms,
        records_inserted=run.records_inserted or 0,
        records_updated=run.records_updated or 0,
        records_failed=run.records_failed or 0,
        message=mask_text(run.message),
    )


def _fetch_log(log: SourceFetchLog) -> JobRunFetchLog:
    return JobRunFetchLog(
        id=log.id,
        source_name=log.source_name,
        request_kind=log.request_kind,
        request_key=mask_text(log.request_key) or "",
        endpoint_label=mask_text(log.endpoint_label),
        method=log.method,
        status=log.status,
        http_status=log.http_status,
        started_at=log.started_at,
        finished_at=log.finished_at,
        duration_ms=log.duration_ms,
        records_inserted=log.records_inserted,
        records_updated=log.records_updated,
        records_failed=log.records_failed,
        error_code=log.error_code,
        error_message=mask_text(log.error_message),
        rate_limited=log.rate_limited,
        backoff_until=log.backoff_until,
        cache_hit=log.cache_hit,
    )


def _stages(run: JobRun) -> list[JobRunStage]:
    payload = _payload(run)
    if not payload:
        return []
    rows: list[JobRunStage] = []
    for st in payload.get("stages") or []:
        if not isinstance(st, dict):
            continue
        rows.append(
            JobRunStage(
                stage=st.get("stage") or "",
                label=st.get("label"),
                status=st.get("status") or "",
                reason=st.get("reason"),
                source=st.get("source"),
                source_mode=st.get("source_mode"),
                expected_offline=bool(st.get("expected_offline", True)),
                started_at=_parse_dt(st.get("started_at")),
                finished_at=_parse_dt(st.get("finished_at")),
                duration_ms=st.get("duration_ms"),
                child_run_ids=[int(c) for c in (st.get("child_run_ids") or [])],
                records_inserted=st.get("records_inserted") or 0,
                records_updated=st.get("records_updated") or 0,
                records_failed=st.get("records_failed") or 0,
                blockers=list(st.get("blockers") or []),
                message=mask_text(st.get("message")),
            )
        )
    return rows


def _related_entities(run: JobRun) -> list[JobRunRelatedEntity]:
    out: list[JobRunRelatedEntity] = []
    if run.workspace_id is not None:
        out.append(JobRunRelatedEntity(entity_type="workspace", entity_id=run.workspace_id))
    if run.fund_id is not None:
        out.append(JobRunRelatedEntity(entity_type="fund", entity_id=run.fund_id))
    if run.fund_listing_id is not None:
        out.append(JobRunRelatedEntity(entity_type="fund_listing", entity_id=run.fund_listing_id))
    if run.scheduled_job_id is not None:
        out.append(JobRunRelatedEntity(entity_type="scheduled_job", entity_id=run.scheduled_job_id))
    return out


# --- fetch-log correlation + budget context ----------------------------------


def _correlatable_source(source: str | None) -> bool:
    """A run source that maps to a real fetch source (not a mode / pseudo-source)."""
    return bool(source) and source not in onboarding_service.SOURCE_MODES


async def correlate_fetch_logs(
    session: AsyncSession, run: JobRun, *, limit: int | None = FETCH_LOG_DEFAULT
) -> tuple[str, list[SourceFetchLog]]:
    """Associate a run with nearby ``source_fetch_logs`` (bounded, approximate).

    Correlation is by ``source_name`` + the run's time window (``started_at`` ..
    ``finished_at`` + small buffer). There is no exact run↔fetch FK yet, so this
    is honestly labelled ``time_window_source``; pseudo-sources (onboarding mode,
    DB-only producers without a budget row) yield ``unavailable``.
    """
    source = run.source
    start = _as_utc(run.started_at)
    if not _correlatable_source(source) or start is None:
        return CORR_UNAVAILABLE, []
    # Only correlate against sources we actually track a budget for — this skips
    # DB-only pseudo-sources like "alert_generation" / "exposure_recompute".
    budget = await source_budget_service.get_budget(session, source)
    if budget is None:
        return CORR_UNAVAILABLE, []

    end = (_as_utc(run.finished_at) or datetime.now(UTC)) + timedelta(seconds=_FETCH_BUFFER_SECONDS)
    bounded = _clamp_fetch_limit(limit)
    stmt = (
        select(SourceFetchLog)
        .where(
            SourceFetchLog.source_name == source,
            SourceFetchLog.started_at >= start,
            SourceFetchLog.started_at <= end,
        )
        .order_by(SourceFetchLog.id.desc())
        .limit(bounded)
    )
    logs = list((await session.execute(stmt)).scalars().all())
    return CORR_TIME_WINDOW, logs


async def _recent_fetch_count(
    session: AsyncSession, source: str, *, status: str, since: datetime
) -> int:
    return (
        await session.scalar(
            select(func.count())
            .select_from(SourceFetchLog)
            .where(
                SourceFetchLog.source_name == source,
                SourceFetchLog.status == status,
                SourceFetchLog.started_at >= since,
            )
        )
    ) or 0


async def build_source_budget_context(
    session: AsyncSession, run: JobRun, *, now: datetime | None = None
) -> JobRunSourceBudgetContext | None:
    """Read-only budget / backoff context for a run's source (None if N/A)."""
    source = run.source
    if not _correlatable_source(source):
        return None
    budget = await source_budget_service.get_budget(session, source)
    if budget is None:
        return None

    now = now or datetime.now(UTC)
    decision = await source_budget_service.check_budget(session, source, now=now)
    backoff_until = _as_utc(budget.backoff_until)
    next_allowed_at: datetime | None = None
    if not decision.allowed and decision.wait_seconds:
        next_allowed_at = now + timedelta(seconds=decision.wait_seconds)

    since = now - timedelta(seconds=_RECENT_FETCH_SECONDS)
    recent_failures = await _recent_fetch_count(session, source, status="failed", since=since)
    cache_hits = await _recent_fetch_count(session, source, status="cache_hit", since=since)
    rate_limited = await _recent_fetch_count(session, source, status="rate_limited", since=since)

    return JobRunSourceBudgetContext(
        source_name=source,
        enabled=budget.is_enabled,
        status=decision.reason,
        allowed=decision.allowed,
        wait_seconds=decision.wait_seconds,
        backoff_until=backoff_until,
        next_allowed_at=next_allowed_at,
        recent_failures=recent_failures,
        cache_hits=cache_hits,
        rate_limited_recently=rate_limited > 0,
    )


async def _load_children(session: AsyncSession, ids: list[int]) -> list[JobRunChild]:
    if not ids:
        return []
    rows = (
        (await session.execute(select(JobRun).where(JobRun.id.in_(ids)).order_by(JobRun.id)))
        .scalars()
        .all()
    )
    return [_child(r) for r in rows]


# --- list timeline -----------------------------------------------------------


async def _sources_with_logs(session: AsyncSession, runs: list[JobRun]) -> set[str]:
    """Distinct fetch-log sources within the listed runs' overall time window.

    One bounded query that lets the list set a coarse ``has_fetch_logs`` hint
    without an N+1 per-run correlation (the detail does the precise correlation).
    """
    starts = [dt for r in runs if (dt := _as_utc(r.started_at)) is not None]
    if not starts:
        return set()
    finishes = [dt for r in runs if (dt := _as_utc(r.finished_at)) is not None]
    window_start = min(starts)
    window_end = max([*finishes, *starts]) + timedelta(seconds=_FETCH_BUFFER_SECONDS)
    rows = (
        await session.execute(
            select(func.distinct(SourceFetchLog.source_name)).where(
                SourceFetchLog.started_at >= window_start,
                SourceFetchLog.started_at <= window_end,
            )
        )
    ).scalars()
    return set(rows)


async def list_job_runs_timeline(
    session: AsyncSession,
    *,
    workspace_id: int | None = None,
    fund_id: int | None = None,
    fund_listing_id: int | None = None,
    job_type: str | None = None,
    status: str | None = None,
    statuses: tuple[str, ...] | None = None,
    limit: int | None = DEFAULT_LIMIT,
) -> list[JobRunTimelineItem]:
    """Latest-first, bounded job-run summaries for a scope / filter set."""
    bounded = clamp_limit(limit)
    stmt = select(JobRun)
    if workspace_id is not None:
        stmt = stmt.where(JobRun.workspace_id == workspace_id)
    if fund_id is not None:
        stmt = stmt.where(JobRun.fund_id == fund_id)
    if fund_listing_id is not None:
        stmt = stmt.where(JobRun.fund_listing_id == fund_listing_id)
    if job_type is not None:
        stmt = stmt.where(JobRun.job_type == job_type)
    if statuses:
        stmt = stmt.where(JobRun.status.in_(statuses))
    elif status is not None:
        stmt = stmt.where(JobRun.status == status)
    stmt = stmt.order_by(JobRun.id.desc()).limit(bounded)
    runs = list((await session.execute(stmt)).scalars().all())

    fetch_sources = await _sources_with_logs(session, runs)
    return [summarise_job_run(r, has_fetch_logs=r.source in fetch_sources) for r in runs]


async def _live_jobs_block(
    session: AsyncSession, *, limit: int | None
) -> tuple[list[RunningJobTimelineItem], RunningJobsSummary]:
    """Currently-leased / due scheduled jobs + their summary (for include_running).

    ``scheduled_jobs`` are global shared infrastructure, so the same global live
    rows enrich both the global and workspace timelines (the scheduler health is
    global; see ``app/services/job_leases.py``). Delegates to the single shared
    lease classifier so the timeline never disagrees with ``/jobs/running``.
    """
    live = await job_leases_service.list_running_jobs(session, include_due=True, limit=limit)
    return live, job_leases_service.summarize(live)


async def global_timeline(
    session: AsyncSession,
    *,
    job_type: str | None = None,
    status: str | None = None,
    limit: int | None = DEFAULT_LIMIT,
    include_running: bool = False,
) -> JobRunTimelineResponse:
    runs = await list_job_runs_timeline(session, job_type=job_type, status=status, limit=limit)
    live, summary = ([], None)
    if include_running:
        live, summary = await _live_jobs_block(session, limit=limit)
    return JobRunTimelineResponse(
        scope_type="global",
        scope_id=None,
        limit=clamp_limit(limit),
        count=len(runs),
        runs=runs,
        include_running=include_running,
        live_jobs=live,
        running_summary=summary,
    )


async def workspace_timeline(
    session: AsyncSession,
    workspace_id: int,
    *,
    limit: int | None = DEFAULT_LIMIT,
    include_running: bool = False,
) -> JobRunTimelineResponse:
    await workspaces_service.get_workspace(session, workspace_id)  # 404s unknown workspace
    runs = await list_job_runs_timeline(session, workspace_id=workspace_id, limit=limit)
    live, summary = ([], None)
    if include_running:
        live, summary = await _live_jobs_block(session, limit=limit)
    return JobRunTimelineResponse(
        scope_type="workspace",
        scope_id=workspace_id,
        limit=clamp_limit(limit),
        count=len(runs),
        runs=runs,
        include_running=include_running,
        live_jobs=live,
        running_summary=summary,
    )


async def list_job_failures(
    session: AsyncSession, *, workspace_id: int | None = None, limit: int | None = DEFAULT_LIMIT
) -> JobRunFailureResponse:
    runs = await list_job_runs_timeline(
        session, workspace_id=workspace_id, statuses=FAILURE_STATUSES, limit=limit
    )
    return JobRunFailureResponse(
        scope_type="workspace" if workspace_id is not None else "global",
        scope_id=workspace_id,
        limit=clamp_limit(limit),
        count=len(runs),
        failures=runs,
    )


async def workspace_failures(
    session: AsyncSession, workspace_id: int, *, limit: int | None = DEFAULT_LIMIT
) -> JobRunFailureResponse:
    await workspaces_service.get_workspace(session, workspace_id)  # 404s unknown workspace
    return await list_job_failures(session, workspace_id=workspace_id, limit=limit)


# --- detail ------------------------------------------------------------------


async def get_job_run_detail(
    session: AsyncSession, run_id: int, *, workspace_id: int | None = None
) -> JobRunDetail:
    """One job run with scope, payload, stages, child runs, fetch-log + budget context.

    With ``workspace_id`` the run is only visible through the matching workspace
    (404 otherwise), so a workspace never drills into another's runs. The global
    route resolves by id (existing project style).
    """
    run = await session.get(JobRun, run_id)
    if run is None:
        raise NotFoundError("Job run not found", code="job_run_not_found")
    if workspace_id is not None and run.workspace_id != workspace_id:
        raise NotFoundError("Job run not found", code="job_run_not_found")

    method, logs = await correlate_fetch_logs(session, run, limit=FETCH_LOG_DEFAULT)
    budget_ctx = await build_source_budget_context(session, run)
    child_runs = await _load_children(session, _onboarding_child_ids(run))

    source_in_backoff = bool(
        budget_ctx and (budget_ctx.status == "in_backoff" or budget_ctx.rate_limited_recently)
    )
    codes = recommended_action_codes(run, source_in_backoff=source_in_backoff)

    payload = _payload(run)
    masked_payload = mask_json(payload) if payload is not None else None

    return JobRunDetail(
        summary=summarise_job_run(run, has_fetch_logs=bool(logs)),
        payload=masked_payload,
        stages=_stages(run),
        child_runs=child_runs,
        related_fetch_logs=[_fetch_log(log) for log in logs],
        fetch_log_correlation=method,
        source_budget_context=budget_ctx,
        related_entities=_related_entities(run),
        recommended_actions=_actions(codes),
        legacy_metadata=_is_orchestration(run) and payload is None,
    )
