"""Investable hierarchy endpoint.

``GET /api/v1/workspaces/{workspace_id}/hierarchy`` returns a bounded tree:
Portfolio -> positions -> top holdings, with derived values/weights.
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.deps import PathWorkspaceId, SessionDep
from app.schemas.hierarchy import HierarchyResponse
from app.services import hierarchy as service

workspace_router = APIRouter(prefix="/workspaces/{workspace_id}", tags=["hierarchy"])


@workspace_router.get("/hierarchy", response_model=HierarchyResponse)
async def get_hierarchy(workspace_id: PathWorkspaceId, session: SessionDep) -> HierarchyResponse:
    return await service.build_hierarchy(session, workspace_id)
