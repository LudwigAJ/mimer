"""Portfolio endpoints.

Two routers expose the same logic:

* ``workspace_router`` — canonical, workspace-scoped under
  ``/workspaces/{workspace_id}/portfolio/...``.
* ``router`` — legacy ``/portfolio/...`` aliases that resolve the workspace from
  the ``X-Workspace-ID`` header (or the default workspace). Kept for backward
  compatibility; prefer the workspace-scoped routes.
"""

from __future__ import annotations

from fastapi import APIRouter, status

from app.api.deps import HeaderWorkspaceId, PathWorkspaceId, SessionDep
from app.schemas.common import ListResponse
from app.schemas.portfolio import (
    PortfolioSummary,
    PositionCreate,
    PositionRead,
    PositionUpdate,
)
from app.services import portfolio as portfolio_service

# Canonical, path-scoped router.
workspace_router = APIRouter(prefix="/workspaces/{workspace_id}/portfolio", tags=["portfolio"])
# Legacy, header/default-scoped router.
router = APIRouter(prefix="/portfolio", tags=["portfolio (legacy)"])


async def _list_positions(session: SessionDep, workspace_id: int) -> ListResponse[PositionRead]:
    items = await portfolio_service.list_positions(session, workspace_id)
    return ListResponse.of([PositionRead.model_validate(i) for i in items])


# --- canonical workspace-scoped endpoints -----------------------------------


@workspace_router.get("/positions", response_model=ListResponse[PositionRead])
async def list_positions(workspace_id: PathWorkspaceId, session: SessionDep):
    return await _list_positions(session, workspace_id)


@workspace_router.get("/summary", response_model=PortfolioSummary)
async def portfolio_summary(workspace_id: PathWorkspaceId, session: SessionDep):
    return await portfolio_service.build_summary(session, workspace_id)


@workspace_router.post(
    "/positions", response_model=PositionRead, status_code=status.HTTP_201_CREATED
)
async def create_position(workspace_id: PathWorkspaceId, data: PositionCreate, session: SessionDep):
    position = await portfolio_service.create_position(session, workspace_id, data)
    return PositionRead.model_validate(position)


@workspace_router.put("/positions/{position_id}", response_model=PositionRead)
async def update_position(
    workspace_id: PathWorkspaceId,
    position_id: int,
    data: PositionUpdate,
    session: SessionDep,
):
    position = await portfolio_service.update_position(session, workspace_id, position_id, data)
    return PositionRead.model_validate(position)


@workspace_router.delete("/positions/{position_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_position(workspace_id: PathWorkspaceId, position_id: int, session: SessionDep):
    await portfolio_service.delete_position(session, workspace_id, position_id)


# --- legacy header/default-scoped aliases -----------------------------------


@router.get("/positions", response_model=ListResponse[PositionRead])
async def list_positions_legacy(workspace_id: HeaderWorkspaceId, session: SessionDep):
    return await _list_positions(session, workspace_id)


@router.get("/summary", response_model=PortfolioSummary)
async def portfolio_summary_legacy(workspace_id: HeaderWorkspaceId, session: SessionDep):
    return await portfolio_service.build_summary(session, workspace_id)


@router.post("/positions", response_model=PositionRead, status_code=status.HTTP_201_CREATED)
async def create_position_legacy(
    workspace_id: HeaderWorkspaceId, data: PositionCreate, session: SessionDep
):
    position = await portfolio_service.create_position(session, workspace_id, data)
    return PositionRead.model_validate(position)


@router.put("/positions/{position_id}", response_model=PositionRead)
async def update_position_legacy(
    workspace_id: HeaderWorkspaceId,
    position_id: int,
    data: PositionUpdate,
    session: SessionDep,
):
    position = await portfolio_service.update_position(session, workspace_id, position_id, data)
    return PositionRead.model_validate(position)


@router.delete("/positions/{position_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_position_legacy(
    workspace_id: HeaderWorkspaceId, position_id: int, session: SessionDep
):
    await portfolio_service.delete_position(session, workspace_id, position_id)
