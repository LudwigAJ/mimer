"""Workspace alert endpoints.

Path-scoped under ``/api/v1/workspaces/{workspace_id}/alerts``. Alerts are
workspace-scoped rows produced by the `alert_generation` worker; these endpoints
list/filter them and apply the read / dismiss / resolve transitions.
"""

from __future__ import annotations

from fastapi import APIRouter, Query

from app.api.deps import PathWorkspaceId, SessionDep
from app.schemas.alert import AlertRead, MarkAllReadResponse
from app.schemas.common import ListResponse
from app.services import alerts as service

workspace_router = APIRouter(prefix="/workspaces/{workspace_id}", tags=["alerts"])


@workspace_router.get("/alerts", response_model=ListResponse[AlertRead])
async def list_alerts(
    workspace_id: PathWorkspaceId,
    session: SessionDep,
    status: str | None = Query(default=None, description="active | read | dismissed | resolved"),
    category: str | None = Query(default=None),
    severity: str | None = Query(default=None),
    limit: int = Query(default=200, ge=1, le=1000),
):
    items = await service.list_alerts(
        session,
        workspace_id,
        status=status,
        category=category,
        severity=severity,
        limit=limit,
    )
    return ListResponse.of(items)


@workspace_router.post("/alerts/mark-all-read", response_model=MarkAllReadResponse)
async def mark_all_alerts_read(workspace_id: PathWorkspaceId, session: SessionDep):
    marked = await service.mark_all_read(session, workspace_id)
    return MarkAllReadResponse(marked_read=marked)


@workspace_router.post("/alerts/{alert_id}/read", response_model=AlertRead)
async def mark_alert_read(alert_id: int, workspace_id: PathWorkspaceId, session: SessionDep):
    return await service.mark_read(session, workspace_id, alert_id)


@workspace_router.post("/alerts/{alert_id}/dismiss", response_model=AlertRead)
async def dismiss_alert(alert_id: int, workspace_id: PathWorkspaceId, session: SessionDep):
    return await service.mark_dismissed(session, workspace_id, alert_id)


@workspace_router.post("/alerts/{alert_id}/resolve", response_model=AlertRead)
async def resolve_alert(alert_id: int, workspace_id: PathWorkspaceId, session: SessionDep):
    return await service.mark_resolved(session, workspace_id, alert_id)
