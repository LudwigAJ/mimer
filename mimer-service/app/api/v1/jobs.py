"""Scheduled job endpoints (design + stub execution) + job-run observability.

The ``/jobs`` router serves both the scheduled-job design/trigger surface and the
generic, bounded **job-run timeline / failure drilldown** read model (over *all*
``job_runs``) for the GUI Data Operations page. The rich timeline/detail/failure
endpoints are read-only and secrets-masked (see ``app/services/job_timeline.py``);
the simple ``/jobs/runs`` list is kept unchanged for backward compatibility.
"""

from __future__ import annotations

from fastapi import APIRouter, Query, status

from app.api.deps import PathWorkspaceId, SessionDep
from app.schemas.common import ListResponse
from app.schemas.job import JobRunRead, ScheduledJobRead
from app.schemas.job_timeline import (
    JobRunDetail,
    JobRunFailureResponse,
    JobRunTimelineResponse,
    RunningJobsResponse,
)
from app.services import job_leases as leases_service
from app.services import job_timeline as timeline_service
from app.services import jobs as service
from app.services import workspaces as workspaces_service

router = APIRouter(prefix="/jobs", tags=["jobs"])
# Workspace-scoped job observability (/api/v1/workspaces/{workspace_id}/jobs/...).
workspace_router = APIRouter(prefix="/workspaces/{workspace_id}", tags=["jobs"])

_TIMELINE_LIMIT_DESC = "max runs to return (latest first; capped at 500)"
_FAILURE_LIMIT_DESC = "max failed/partial runs to return (latest first; capped at 500)"
_RUNNING_LIMIT_DESC = "max live rows to return (most urgent first; capped at 500)"
_INCLUDE_RUNNING_DESC = (
    "also include currently-leased / due scheduled jobs as live rows (read-only)"
)
_LEASE_STATUS_DESC = "filter to a single lease status: running | stuck | expired | due"


@router.get("", response_model=ListResponse[ScheduledJobRead])
async def list_jobs(session: SessionDep):
    items = await service.list_jobs(session)
    return ListResponse.of([service.serialize_job(i) for i in items])


# Declared before "/{job_id}" so "runs" is not captured as a job id.
@router.get("/runs", response_model=ListResponse[JobRunRead])
async def list_runs(
    session: SessionDep,
    limit: int = Query(default=100, ge=1, le=1000),
    job_type: str | None = Query(default=None),
    fund_id: int | None = Query(default=None),
    fund_listing_id: int | None = Query(default=None),
    status: str | None = Query(default=None),
):
    items = await service.list_runs(
        session,
        limit=limit,
        job_type=job_type,
        fund_id=fund_id,
        fund_listing_id=fund_listing_id,
        status=status,
    )
    return ListResponse.of([JobRunRead.model_validate(i) for i in items])


# --- generic job-run timeline / failure drilldown (all job types) ------------
# All declared before "/{job_id}" so "timeline"/"failures"/"runs" are not
# captured as a scheduled-job id.


@router.get("/timeline", response_model=JobRunTimelineResponse)
async def get_jobs_timeline(
    session: SessionDep,
    limit: int = Query(default=100, ge=1, le=500, description=_TIMELINE_LIMIT_DESC),
    job_type: str | None = Query(default=None),
    status: str | None = Query(default=None),
    include_running: bool = Query(default=False, description=_INCLUDE_RUNNING_DESC),
) -> JobRunTimelineResponse:
    """Bounded, latest-first job-run timeline across all job types (read model).

    With ``include_running=true`` the response is enriched with ``live_jobs``
    (currently-leased / due ``scheduled_jobs``) + a ``running_summary``; the
    completed ``runs`` list is unchanged so existing clients are unaffected.
    """
    return await timeline_service.global_timeline(
        session, job_type=job_type, status=status, limit=limit, include_running=include_running
    )


@router.get("/running", response_model=RunningJobsResponse)
async def get_jobs_running(
    session: SessionDep,
    limit: int = Query(default=100, ge=1, le=500, description=_RUNNING_LIMIT_DESC),
    include_due: bool = Query(default=True, description="include due-but-unclaimed jobs"),
) -> RunningJobsResponse:
    """Live running/leased/stuck/expired/due scheduled jobs + summary (read-only).

    Derived from the ``scheduled_jobs`` lease columns the scheduler maintains. This
    is observability only — there is no unlock/kill/force endpoint (see AGENTS.md).
    """
    return await leases_service.running_jobs_response(
        session, scope_type="global", scope_id=None, include_due=include_due, limit=limit
    )


@router.get("/leases", response_model=RunningJobsResponse)
async def get_jobs_leases(
    session: SessionDep,
    status: str | None = Query(default=None, description=_LEASE_STATUS_DESC),
    limit: int = Query(default=100, ge=1, le=500, description=_RUNNING_LIMIT_DESC),
) -> RunningJobsResponse:
    """Scheduled-job leases, optionally filtered to one ``status`` (read-only)."""
    return await leases_service.leases_response(session, status=status, limit=limit)


@router.get("/failures", response_model=JobRunFailureResponse)
async def get_jobs_failures(
    session: SessionDep,
    limit: int = Query(default=50, ge=1, le=500, description=_FAILURE_LIMIT_DESC),
) -> JobRunFailureResponse:
    """Recent failed/partial job runs with recommended next actions (read model)."""
    return await timeline_service.list_job_failures(session, limit=limit)


@router.get("/runs/{run_id}", response_model=JobRunDetail)
async def get_job_run_detail(run_id: int, session: SessionDep) -> JobRunDetail:
    """One job run: scope, payload, stages, child runs, fetch-log + budget context."""
    return await timeline_service.get_job_run_detail(session, run_id)


@router.get("/{job_id}", response_model=ScheduledJobRead)
async def get_job(job_id: int, session: SessionDep):
    job = await service.get_job(session, job_id)
    return service.serialize_job(job)


@router.post("/{job_id}/run", response_model=JobRunRead, status_code=status.HTTP_201_CREATED)
async def run_job(job_id: int, session: SessionDep):
    """Trigger a job. ``price_ingestion`` runs the real worker; others are stubs.

    Runs synchronously for now — this will move to a background worker later.
    """
    run = await service.trigger_job(session, job_id)
    return JobRunRead.model_validate(run)


# --- workspace-scoped job observability --------------------------------------


@workspace_router.get("/jobs/timeline", response_model=JobRunTimelineResponse)
async def get_workspace_jobs_timeline(
    workspace_id: PathWorkspaceId,
    session: SessionDep,
    limit: int = Query(default=100, ge=1, le=500, description=_TIMELINE_LIMIT_DESC),
    include_running: bool = Query(default=False, description=_INCLUDE_RUNNING_DESC),
) -> JobRunTimelineResponse:
    """Bounded, latest-first job-run timeline scoped to one workspace (read model).

    Completed ``runs`` are limited to this ``workspace_id``. With
    ``include_running=true`` the response also carries ``live_jobs`` — the
    currently-leased / due ``scheduled_jobs``, which are **global shared
    infrastructure** (the scheduler health is the same for every workspace), plus
    a ``running_summary``.
    """
    return await timeline_service.workspace_timeline(
        session, workspace_id, limit=limit, include_running=include_running
    )


@workspace_router.get("/jobs/running", response_model=RunningJobsResponse)
async def get_workspace_jobs_running(
    workspace_id: PathWorkspaceId,
    session: SessionDep,
    limit: int = Query(default=100, ge=1, le=500, description=_RUNNING_LIMIT_DESC),
    include_due: bool = Query(default=True, description="include due-but-unclaimed jobs"),
) -> RunningJobsResponse:
    """Live running/leased/stuck/expired/due scheduled jobs for a workspace view.

    ``scheduled_jobs`` are global shared infrastructure, so this returns the same
    global scheduler health as ``/jobs/running`` (it validates the workspace
    exists). Read-only — no unlock/kill endpoint exists (see AGENTS.md).
    """
    await workspaces_service.get_workspace(session, workspace_id)  # 404s unknown workspace
    return await leases_service.running_jobs_response(
        session,
        scope_type="workspace",
        scope_id=workspace_id,
        include_due=include_due,
        limit=limit,
    )


@workspace_router.get("/jobs/failures", response_model=JobRunFailureResponse)
async def get_workspace_jobs_failures(
    workspace_id: PathWorkspaceId,
    session: SessionDep,
    limit: int = Query(default=50, ge=1, le=500, description=_FAILURE_LIMIT_DESC),
) -> JobRunFailureResponse:
    """Recent failed/partial job runs for a workspace with recommended actions."""
    return await timeline_service.workspace_failures(session, workspace_id, limit=limit)


@workspace_router.get("/jobs/runs/{run_id}", response_model=JobRunDetail)
async def get_workspace_job_run_detail(
    workspace_id: PathWorkspaceId,
    run_id: int,
    session: SessionDep,
) -> JobRunDetail:
    """One workspace job run with full drilldown (404 if it belongs to another)."""
    return await timeline_service.get_job_run_detail(session, run_id, workspace_id=workspace_id)
