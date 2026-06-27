"""Data-quality / diagnostics endpoints.

* ``GET /api/v1/diagnostics`` — global counts across all reference data.
* ``GET /api/v1/workspaces/{workspace_id}/diagnostics`` — freshness/provenance
  scoped to the workspace's held funds (job-queue health stays global).
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.deps import PathWorkspaceId, SessionDep
from app.schemas.diagnostics import Diagnostics, WorkspaceDiagnostics
from app.services import diagnostics as service

router = APIRouter(tags=["diagnostics"])
workspace_router = APIRouter(prefix="/workspaces/{workspace_id}", tags=["diagnostics"])


@router.get("/diagnostics", response_model=Diagnostics)
async def get_diagnostics(session: SessionDep) -> Diagnostics:
    return await service.global_diagnostics(session)


@workspace_router.get("/diagnostics", response_model=WorkspaceDiagnostics)
async def get_workspace_diagnostics(
    workspace_id: PathWorkspaceId, session: SessionDep
) -> WorkspaceDiagnostics:
    return await service.workspace_diagnostics(session, workspace_id)
