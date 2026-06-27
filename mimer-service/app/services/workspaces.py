"""Workspace / user resolution and settings.

v1 has no real auth. Workspace resolution is: explicit id (path or
``X-Workspace-ID`` header) → otherwise the default (lowest-id) workspace. The
"current user" is likewise the default seeded user. See README for the auth
roadmap.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFoundError
from app.db.models import User, Workspace, WorkspaceMember, WorkspaceSetting


async def get_workspace(session: AsyncSession, workspace_id: int) -> Workspace:
    workspace = await session.get(Workspace, workspace_id)
    if workspace is None:
        raise NotFoundError("Workspace not found", code="workspace_not_found")
    return workspace


async def get_default_workspace(session: AsyncSession) -> Workspace:
    workspace = await session.scalar(select(Workspace).order_by(Workspace.id).limit(1))
    if workspace is None:
        raise NotFoundError("No workspace configured", code="workspace_not_found")
    return workspace


async def resolve_workspace(session: AsyncSession, workspace_id: int | None) -> Workspace:
    """Resolve an explicit workspace id, or fall back to the default workspace."""
    if workspace_id is not None:
        return await get_workspace(session, workspace_id)
    return await get_default_workspace(session)


async def list_workspaces(session: AsyncSession) -> list[Workspace]:
    return list((await session.execute(select(Workspace).order_by(Workspace.id))).scalars().all())


async def get_default_user(session: AsyncSession) -> User:
    user = await session.scalar(select(User).order_by(User.id).limit(1))
    if user is None:
        raise NotFoundError("No user configured", code="user_not_found")
    return user


async def list_user_workspaces(session: AsyncSession, user_id: int) -> list[Workspace]:
    stmt = (
        select(Workspace)
        .join(WorkspaceMember, WorkspaceMember.workspace_id == Workspace.id)
        .where(WorkspaceMember.user_id == user_id)
        .order_by(Workspace.id)
    )
    return list((await session.execute(stmt)).scalars().all())


async def get_settings(session: AsyncSession, workspace_id: int) -> dict[str, Any]:
    await get_workspace(session, workspace_id)
    rows = (
        (
            await session.execute(
                select(WorkspaceSetting).where(WorkspaceSetting.workspace_id == workspace_id)
            )
        )
        .scalars()
        .all()
    )
    return {row.key: row.value_json for row in rows}


async def update_settings(
    session: AsyncSession, workspace_id: int, values: dict[str, Any]
) -> dict[str, Any]:
    await get_workspace(session, workspace_id)
    existing = {
        row.key: row
        for row in (
            await session.execute(
                select(WorkspaceSetting).where(WorkspaceSetting.workspace_id == workspace_id)
            )
        )
        .scalars()
        .all()
    }
    for key, value in values.items():
        if key in existing:
            existing[key].value_json = value
        else:
            session.add(WorkspaceSetting(workspace_id=workspace_id, key=key, value_json=value))
    await session.commit()
    return await get_settings(session, workspace_id)
