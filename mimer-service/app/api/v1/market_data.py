"""Market-data planning endpoint (workspace-scoped, read-only).

Returns the computed plan of what would need to be resolved/fetched for a
workspace's held funds and constituents — a dedupe + prioritise + estimate step
that runs *before* any external fetch. No network I/O happens here.
"""

from __future__ import annotations

from fastapi import APIRouter, Query

from app.api.deps import PathWorkspaceId, SessionDep
from app.schemas.market_data import MarketDataPlanResponse
from app.services import market_data_planner

workspace_router = APIRouter(prefix="/workspaces/{workspace_id}", tags=["market-data"])


@workspace_router.get("/market-data-plan", response_model=MarketDataPlanResponse)
async def get_market_data_plan(
    workspace_id: PathWorkspaceId,
    session: SessionDep,
    include_constituents: bool = Query(default=True),
) -> MarketDataPlanResponse:
    return await market_data_planner.build_plan(
        session, workspace_id, include_constituents=include_constituents
    )
