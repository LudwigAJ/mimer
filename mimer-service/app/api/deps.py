"""Shared FastAPI dependencies."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Header
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_session
from app.services import workspaces as workspaces_service

SessionDep = Annotated[AsyncSession, Depends(get_session)]


async def resolve_header_workspace_id(
    session: SessionDep,
    x_workspace_id: Annotated[int | None, Header(alias="X-Workspace-ID")] = None,
) -> int:
    """Resolve the workspace for legacy (non-path-scoped) private endpoints.

    Uses the ``X-Workspace-ID`` header if present, otherwise the default
    workspace. v1 dev convenience only — real auth is future work.
    """
    workspace = await workspaces_service.resolve_workspace(session, x_workspace_id)
    return workspace.id


async def require_path_workspace_id(workspace_id: int, session: SessionDep) -> int:
    """Validate a workspace id taken from the URL path (404 if it does not exist)."""
    await workspaces_service.get_workspace(session, workspace_id)
    return workspace_id


# Legacy endpoints (/api/v1/portfolio, /exposure, /alerts) → header/default workspace.
HeaderWorkspaceId = Annotated[int, Depends(resolve_header_workspace_id)]
# Path-scoped endpoints (/api/v1/workspaces/{workspace_id}/...) → validated path id.
PathWorkspaceId = Annotated[int, Depends(require_path_workspace_id)]
