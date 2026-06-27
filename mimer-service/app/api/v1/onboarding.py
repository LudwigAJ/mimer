"""Instrument-onboarding / data-readiness endpoints.

Plan (read-only) + run (orchestration) for a workspace or fund scope. The plan
reports readiness/coverage and the jobs that would run; the run executes only the
needed stages via the existing worker dispatch and records a parent job_run.

Source mode defaults to the safe offline ``fixture``; ``live`` must be explicit
and is still budgeted/cached/logged by the underlying workers.
"""

from __future__ import annotations

from fastapi import APIRouter, Query

from app.api.deps import PathWorkspaceId, SessionDep
from app.schemas.onboarding import (
    OnboardingPlanResponse,
    OnboardingRunDetail,
    OnboardingRunListResponse,
    OnboardingRunResponse,
    OnboardingStatus,
)
from app.services import instrument_onboarding as onboarding_service
from app.services import onboarding_runs as onboarding_runs_service

workspace_router = APIRouter(prefix="/workspaces/{workspace_id}", tags=["onboarding"])
fund_router = APIRouter(prefix="/funds/{fund_id}", tags=["onboarding"])

_MODE_DESC = "fixture (offline, default) | live (explicit, budgeted/cached/logged)"
_RUNS_LIMIT_DESC = "max onboarding runs to return (latest first; capped at 200)"


@workspace_router.get("/onboarding/plan", response_model=OnboardingPlanResponse)
async def get_workspace_onboarding_plan(
    workspace_id: PathWorkspaceId,
    session: SessionDep,
    source_mode: str = Query(default="fixture", description=_MODE_DESC),
    limit: int | None = Query(default=None, ge=1, le=1000),
) -> OnboardingPlanResponse:
    """Read-only data-readiness plan for a workspace (no writes, no network)."""
    return await onboarding_service.build_onboarding_plan(
        session, workspace_id=workspace_id, source_mode=source_mode, limit=limit
    )


@workspace_router.get("/onboarding/status", response_model=OnboardingStatus)
async def get_workspace_onboarding_status(
    workspace_id: PathWorkspaceId,
    session: SessionDep,
) -> OnboardingStatus:
    """Compact onboarding status (readiness + last run + next action)."""
    return await onboarding_service.build_status(session, workspace_id)


@workspace_router.get("/onboarding/runs", response_model=OnboardingRunListResponse)
async def list_workspace_onboarding_runs(
    workspace_id: PathWorkspaceId,
    session: SessionDep,
    limit: int = Query(default=50, ge=1, le=200, description=_RUNS_LIMIT_DESC),
) -> OnboardingRunListResponse:
    """Bounded, latest-first onboarding run history for a workspace (read model)."""
    return await onboarding_runs_service.workspace_run_list(session, workspace_id, limit=limit)


@workspace_router.get("/onboarding/runs/{run_id}", response_model=OnboardingRunDetail)
async def get_workspace_onboarding_run(
    workspace_id: PathWorkspaceId,
    run_id: int,
    session: SessionDep,
) -> OnboardingRunDetail:
    """One workspace onboarding run with typed stages + child job runs (404 if foreign)."""
    return await onboarding_runs_service.get_onboarding_run_detail(
        session, run_id, workspace_id=workspace_id
    )


@workspace_router.post("/onboarding/run", response_model=OnboardingRunResponse)
async def run_workspace_onboarding(
    workspace_id: PathWorkspaceId,
    session: SessionDep,
    source_mode: str = Query(default="fixture", description=_MODE_DESC),
    plan_only: bool = Query(default=False),
    limit: int | None = Query(default=None, ge=1, le=1000),
    skip_exposure: bool = Query(default=False),
    skip_alerts: bool = Query(default=False),
) -> OnboardingRunResponse:
    """Run the needed onboarding stages for a workspace (synchronous, job_run-backed)."""
    return await onboarding_service.execute_onboarding_plan(
        session,
        workspace_id=workspace_id,
        source_mode=source_mode,
        plan_only=plan_only,
        limit=limit,
        skip_exposure=skip_exposure,
        skip_alerts=skip_alerts,
    )


@fund_router.get("/onboarding/plan", response_model=OnboardingPlanResponse)
async def get_fund_onboarding_plan(
    fund_id: int,
    session: SessionDep,
    source_mode: str = Query(default="fixture", description=_MODE_DESC),
    limit: int | None = Query(default=None, ge=1, le=1000),
) -> OnboardingPlanResponse:
    """Read-only data-readiness plan for a fund (no writes, no network)."""
    return await onboarding_service.build_onboarding_plan(
        session, fund_id=fund_id, source_mode=source_mode, limit=limit
    )


@fund_router.get("/onboarding/runs", response_model=OnboardingRunListResponse)
async def list_fund_onboarding_runs(
    fund_id: int,
    session: SessionDep,
    limit: int = Query(default=50, ge=1, le=200, description=_RUNS_LIMIT_DESC),
) -> OnboardingRunListResponse:
    """Bounded, latest-first onboarding run history for a fund (read model)."""
    return await onboarding_runs_service.fund_run_list(session, fund_id, limit=limit)


@fund_router.get("/onboarding/runs/{run_id}", response_model=OnboardingRunDetail)
async def get_fund_onboarding_run(
    fund_id: int,
    run_id: int,
    session: SessionDep,
) -> OnboardingRunDetail:
    """One fund onboarding run with typed stages + child job runs (404 if foreign)."""
    return await onboarding_runs_service.get_onboarding_run_detail(session, run_id, fund_id=fund_id)


@fund_router.post("/onboarding/run", response_model=OnboardingRunResponse)
async def run_fund_onboarding(
    fund_id: int,
    session: SessionDep,
    source_mode: str = Query(default="fixture", description=_MODE_DESC),
    plan_only: bool = Query(default=False),
    limit: int | None = Query(default=None, ge=1, le=1000),
    skip_exposure: bool = Query(default=False),
    skip_alerts: bool = Query(default=False),
) -> OnboardingRunResponse:
    """Run the needed onboarding stages for a fund (synchronous, job_run-backed)."""
    return await onboarding_service.execute_onboarding_plan(
        session,
        fund_id=fund_id,
        source_mode=source_mode,
        plan_only=plan_only,
        limit=limit,
        skip_exposure=skip_exposure,
        skip_alerts=skip_alerts,
    )
