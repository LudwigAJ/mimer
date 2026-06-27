"""Workspace / user schemas."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel

from app.schemas.common import ORMModel


class UserRead(ORMModel):
    id: int
    email: str | None
    display_name: str | None
    is_active: bool
    created_at: datetime
    updated_at: datetime


class WorkspaceRead(ORMModel):
    id: int
    name: str
    base_currency: str
    created_at: datetime
    updated_at: datetime


class WorkspaceMemberRead(ORMModel):
    id: int
    workspace_id: int
    user_id: int
    role: str
    created_at: datetime


class MeResponse(BaseModel):
    user: UserRead
    workspaces: list[WorkspaceRead]


class WorkspaceSettingsRead(BaseModel):
    workspace_id: int
    settings: dict[str, Any]


class WorkspaceSettingsUpdate(BaseModel):
    # Keys provided here are upserted; existing keys not listed are left intact.
    settings: dict[str, Any]
