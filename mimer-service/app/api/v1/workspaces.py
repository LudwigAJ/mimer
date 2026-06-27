"""Workspace and user endpoints."""

from __future__ import annotations

from fastapi import APIRouter

from app.api.deps import SessionDep
from app.schemas.common import ListResponse
from app.schemas.workspace import (
    MeResponse,
    UserRead,
    WorkspaceRead,
    WorkspaceSettingsRead,
    WorkspaceSettingsUpdate,
)
from app.services import workspaces as service

router = APIRouter(tags=["workspaces"])


@router.get("/me", response_model=MeResponse)
async def get_me(session: SessionDep):
    """Current user + their workspaces. v1 returns the default seeded user."""
    user = await service.get_default_user(session)
    workspaces = await service.list_user_workspaces(session, user.id)
    return MeResponse(
        user=UserRead.model_validate(user),
        workspaces=[WorkspaceRead.model_validate(w) for w in workspaces],
    )


@router.get("/workspaces", response_model=ListResponse[WorkspaceRead])
async def list_workspaces(session: SessionDep):
    items = await service.list_workspaces(session)
    return ListResponse.of([WorkspaceRead.model_validate(w) for w in items])


@router.get("/workspaces/{workspace_id}", response_model=WorkspaceRead)
async def get_workspace(workspace_id: int, session: SessionDep):
    workspace = await service.get_workspace(session, workspace_id)
    return WorkspaceRead.model_validate(workspace)


@router.get("/workspaces/{workspace_id}/settings", response_model=WorkspaceSettingsRead)
async def get_settings(workspace_id: int, session: SessionDep):
    settings = await service.get_settings(session, workspace_id)
    return WorkspaceSettingsRead(workspace_id=workspace_id, settings=settings)


@router.put("/workspaces/{workspace_id}/settings", response_model=WorkspaceSettingsRead)
async def update_settings(workspace_id: int, data: WorkspaceSettingsUpdate, session: SessionDep):
    settings = await service.update_settings(session, workspace_id, data.settings)
    return WorkspaceSettingsRead(workspace_id=workspace_id, settings=settings)
