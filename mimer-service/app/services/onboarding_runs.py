"""Onboarding run history / observability read model.

A bounded, typed, GUI-friendly read layer over the parent ``instrument_onboarding``
``job_runs`` and their structured ``payload_json`` (written by
``app.services.instrument_onboarding.execute_onboarding_plan``). It answers
"what did this onboarding run do — which stages ran / were skipped / blocked /
failed, which child job_runs belong to each stage, how long did each take, what
scope + source mode, what to do next" *without* parsing the free-text message.

This is strictly a read model (see AGENTS.md): no writes, no network, no
per-instrument compute. Queries are bounded — ``job_type='instrument_onboarding'``
filtered by workspace/fund scope, latest-first, with a capped limit, served by
the ``(job_type, id)`` index. Pre-0015 runs (no ``payload_json``) are surfaced as
``legacy_metadata=true`` with empty stages and the human-readable message kept.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFoundError
from app.db.models import Fund, JobRun
from app.schemas.onboarding import (
    RUN_BLOCKED,
    RUN_FAILED,
    RUN_PARTIAL,
    RUN_SKIPPED,
    RUN_SUCCESS,
    OnboardingChildJobRun,
    OnboardingRunDetail,
    OnboardingRunListResponse,
    OnboardingRunSummary,
    OnboardingStageRunDetail,
)
from app.services import instrument_onboarding as onboarding_service

ONBOARDING_JOB = onboarding_service.ONBOARDING_JOB

DEFAULT_LIMIT = 50
MAX_LIMIT = 200


def clamp_limit(limit: int | None) -> int:
    return max(1, min(limit or DEFAULT_LIMIT, MAX_LIMIT))


# --- small payload parsing ---------------------------------------------------


def _payload(run: JobRun) -> dict | None:
    return run.payload_json if isinstance(run.payload_json, dict) else None


def _parse_dt(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None
    return None


def _stage_counts(stages: list[dict]) -> dict[str, int]:
    counts = {"success": 0, "failed": 0, "blocked": 0, "skipped": 0}
    for st in stages:
        status = st.get("status") if isinstance(st, dict) else None
        if status in (RUN_SUCCESS, RUN_PARTIAL):
            counts["success"] += 1
        elif status == RUN_FAILED:
            counts["failed"] += 1
        elif status == RUN_BLOCKED:
            counts["blocked"] += 1
        elif status == RUN_SKIPPED:
            counts["skipped"] += 1
    return counts


def _stages_of(run: JobRun) -> list[dict]:
    payload = _payload(run)
    if not payload:
        return []
    return [st for st in (payload.get("stages") or []) if isinstance(st, dict)]


def _summary(run: JobRun) -> OnboardingRunSummary:
    payload = _payload(run)
    legacy = payload is None
    stages = _stages_of(run)
    counts = _stage_counts(stages)
    child_ids = [cid for st in stages for cid in (st.get("child_run_ids") or [])]

    scope_type: str | None = None
    scope_id: int | None = None
    next_action: str | None = None
    source_mode: str | None = run.source
    if payload:
        scope = payload.get("scope") or {}
        scope_type = scope.get("type")
        scope_id = scope.get("id")
        next_action = payload.get("next_recommended_action")
        source_mode = payload.get("source_mode") or run.source
    elif run.workspace_id is not None:
        scope_type, scope_id = "workspace", run.workspace_id
    elif run.fund_id is not None:
        scope_type, scope_id = "fund", run.fund_id

    return OnboardingRunSummary(
        run_id=run.id,
        workspace_id=run.workspace_id,
        fund_id=run.fund_id,
        scope_type=scope_type,
        scope_id=scope_id,
        status=run.status,
        source_mode=source_mode,
        started_at=run.started_at,
        finished_at=run.finished_at,
        duration_ms=onboarding_service.run_duration_ms(run),
        stage_count=len(stages),
        success_count=counts["success"],
        failed_count=counts["failed"],
        blocked_count=counts["blocked"],
        skipped_count=counts["skipped"],
        child_run_count=len(child_ids),
        next_recommended_action=next_action,
        message=run.message,
        legacy_metadata=legacy,
    )


def _stage_detail(st: dict) -> OnboardingStageRunDetail:
    return OnboardingStageRunDetail(
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
        child_run_ids=list(st.get("child_run_ids") or []),
        records_inserted=st.get("records_inserted") or 0,
        records_updated=st.get("records_updated") or 0,
        records_failed=st.get("records_failed") or 0,
        blockers=list(st.get("blockers") or []),
        message=st.get("message"),
    )


def _child_run(run: JobRun) -> OnboardingChildJobRun:
    duration_ms: int | None = None
    if run.started_at and run.finished_at:
        duration_ms = max(0, int((run.finished_at - run.started_at).total_seconds() * 1000))
    return OnboardingChildJobRun(
        run_id=run.id,
        job_type=run.job_type,
        status=run.status,
        source=run.source,
        started_at=run.started_at,
        finished_at=run.finished_at,
        duration_ms=duration_ms,
        records_inserted=run.records_inserted or 0,
        records_updated=run.records_updated or 0,
        records_failed=run.records_failed or 0,
        message=run.message,
    )


async def _load_child_runs(session: AsyncSession, ids: list[int]) -> list[OnboardingChildJobRun]:
    if not ids:
        return []
    rows = (
        (await session.execute(select(JobRun).where(JobRun.id.in_(ids)).order_by(JobRun.id)))
        .scalars()
        .all()
    )
    return [_child_run(r) for r in rows]


# --- public read API ---------------------------------------------------------


async def list_onboarding_runs(
    session: AsyncSession,
    *,
    workspace_id: int | None = None,
    fund_id: int | None = None,
    limit: int | None = DEFAULT_LIMIT,
) -> list[OnboardingRunSummary]:
    """Latest-first, bounded onboarding run summaries for a workspace/fund scope."""
    bounded = clamp_limit(limit)
    stmt = select(JobRun).where(JobRun.job_type == ONBOARDING_JOB)
    if workspace_id is not None:
        stmt = stmt.where(JobRun.workspace_id == workspace_id)
    if fund_id is not None:
        stmt = stmt.where(JobRun.fund_id == fund_id)
    stmt = stmt.order_by(JobRun.id.desc()).limit(bounded)
    runs = (await session.execute(stmt)).scalars().all()
    return [_summary(r) for r in runs]


async def get_onboarding_run_detail(
    session: AsyncSession,
    run_id: int,
    *,
    workspace_id: int | None = None,
    fund_id: int | None = None,
) -> OnboardingRunDetail:
    """One onboarding run with typed stages + child job-run summaries.

    Enforces scope: a run is only visible through the matching workspace/fund
    (404 otherwise), so one workspace/fund never sees another's runs.
    """
    run = await session.get(JobRun, run_id)
    if run is None or run.job_type != ONBOARDING_JOB:
        raise NotFoundError("Onboarding run not found", code="onboarding_run_not_found")
    if workspace_id is not None and run.workspace_id != workspace_id:
        raise NotFoundError("Onboarding run not found", code="onboarding_run_not_found")
    if fund_id is not None and run.fund_id != fund_id:
        raise NotFoundError("Onboarding run not found", code="onboarding_run_not_found")

    payload = _payload(run)
    stages = _stages_of(run)
    child_ids = sorted({cid for st in stages for cid in (st.get("child_run_ids") or [])})

    summary = _summary(run)
    return OnboardingRunDetail(
        **summary.model_dump(),
        plan_only=bool(payload.get("plan_only")) if payload else False,
        blocking_issues=list(payload.get("blocking_issues") or []) if payload else [],
        stages=[_stage_detail(st) for st in stages],
        child_runs=await _load_child_runs(session, child_ids),
    )


async def _ensure_fund(session: AsyncSession, fund_id: int) -> None:
    if await session.get(Fund, fund_id) is None:
        raise NotFoundError("Fund not found", code="fund_not_found")


async def workspace_run_list(
    session: AsyncSession, workspace_id: int, *, limit: int | None = DEFAULT_LIMIT
) -> OnboardingRunListResponse:
    runs = await list_onboarding_runs(session, workspace_id=workspace_id, limit=limit)
    return OnboardingRunListResponse(
        scope_type="workspace",
        scope_id=workspace_id,
        limit=clamp_limit(limit),
        count=len(runs),
        runs=runs,
    )


async def fund_run_list(
    session: AsyncSession, fund_id: int, *, limit: int | None = DEFAULT_LIMIT
) -> OnboardingRunListResponse:
    await _ensure_fund(session, fund_id)
    runs = await list_onboarding_runs(session, fund_id=fund_id, limit=limit)
    return OnboardingRunListResponse(
        scope_type="fund",
        scope_id=fund_id,
        limit=clamp_limit(limit),
        count=len(runs),
        runs=runs,
    )
