"""Workspace dashboard aggregate endpoint.

``GET /api/v1/workspaces/{workspace_id}/dashboard`` returns one bounded payload
that hydrates the GUI's main workstation view (summary, positions, held
funds/listings with latest prices, recent distributions/holdings/documents,
exposures, alerts, jobs, FX, and data-quality/freshness) in a single call.
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.deps import PathWorkspaceId, SessionDep
from app.schemas.dashboard import DashboardResponse
from app.services import dashboard as service

workspace_router = APIRouter(prefix="/workspaces/{workspace_id}", tags=["dashboard"])


@workspace_router.get("/dashboard", response_model=DashboardResponse)
async def get_dashboard(workspace_id: PathWorkspaceId, session: SessionDep) -> DashboardResponse:
    return await service.build_dashboard(session, workspace_id)
