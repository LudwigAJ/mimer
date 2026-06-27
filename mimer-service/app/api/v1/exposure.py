"""Derived exposure endpoints (workspace-private).

Reads from the latest cached `exposure_snapshots` (written by the
`exposure_recompute` worker), falling back to an on-the-fly computation flagged
``cached=false`` when no snapshot exists yet. A legacy header/default-scoped
alias serves the old ad-hoc slice shape for backwards compatibility.
"""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Query

from app.api.deps import HeaderWorkspaceId, PathWorkspaceId, SessionDep
from app.schemas.common import ListResponse
from app.schemas.exposure import (
    ExposureDriftResponse,
    ExposureResponse,
    ExposureSnapshotResponse,
    ExposureSnapshotSummary,
    TopHoldingPerformanceResponse,
)
from app.services import exposure as legacy_service
from app.services import exposure_drift as drift_service
from app.services import exposure_recompute as service
from app.services import holding_performance as performance_service

workspace_router = APIRouter(prefix="/workspaces/{workspace_id}", tags=["exposure"])
router = APIRouter(tags=["exposure (legacy)"])


@workspace_router.get("/exposure", response_model=ExposureSnapshotResponse)
async def get_exposure(
    workspace_id: PathWorkspaceId,
    session: SessionDep,
    dimension: str | None = Query(default=None, description="country|sector|currency|holding|..."),
    snapshot_id: int | None = Query(default=None),
    limit: int | None = Query(default=None, ge=1, le=1000),
):
    return await service.build_response(
        session, workspace_id, dimension=dimension, snapshot_id=snapshot_id, limit=limit
    )


@workspace_router.get("/exposure/snapshots", response_model=ListResponse[ExposureSnapshotSummary])
async def list_exposure_snapshots(
    workspace_id: PathWorkspaceId,
    session: SessionDep,
    limit: int = Query(default=50, ge=1, le=500),
):
    rows = await service.list_snapshots(session, workspace_id, limit=limit)
    return ListResponse.of([service.summarize_snapshot(snap, count) for snap, count in rows])


@workspace_router.get("/exposure/drift", response_model=ExposureDriftResponse)
async def get_exposure_drift(
    workspace_id: PathWorkspaceId,
    session: SessionDep,
    dimension: str = Query(
        default="constituent",
        description="constituent|country|sector|industry|currency|source|constituent_price_status",
    ),
    base_snapshot_id: int | None = Query(default=None),
    comparison_snapshot_id: int | None = Query(default=None),
    sort: str = Query(
        default="abs_delta_weight",
        description="abs_delta_weight|abs_delta_market_value|delta_weight|delta_market_value",
    ),
    limit: int | None = Query(default=None, ge=1, le=1000),
):
    """What changed between two exposure snapshots (default: previous vs latest).

    Compares snapshots only — never infers trades or PnL. Returns
    ``status=insufficient_history`` when there is no prior snapshot to compare.
    Explicit snapshot ids are workspace-scoped (no cross-workspace comparison)."""
    return await drift_service.compute_drift(
        session,
        workspace_id,
        dimension=dimension,
        base_snapshot_id=base_snapshot_id,
        comparison_snapshot_id=comparison_snapshot_id,
        sort=sort,
        limit=limit,
    )


@workspace_router.get("/exposure/top-movers", response_model=ExposureDriftResponse)
async def get_exposure_top_movers(
    workspace_id: PathWorkspaceId,
    session: SessionDep,
    dimension: str = Query(default="constituent"),
    base_snapshot_id: int | None = Query(default=None),
    comparison_snapshot_id: int | None = Query(default=None),
    sort: str = Query(default="abs_delta_weight"),
    limit: int = Query(default=20, ge=1, le=500),
):
    """The biggest changes between two snapshots — drift with unchanged + synthetic
    buckets dropped, sorted and bounded for a "what moved most" view."""
    return await drift_service.compute_drift(
        session,
        workspace_id,
        dimension=dimension,
        base_snapshot_id=base_snapshot_id,
        comparison_snapshot_id=comparison_snapshot_id,
        sort=sort,
        limit=limit,
        movers_only=True,
    )


@workspace_router.get(
    "/exposure/top-holding-performance", response_model=TopHoldingPerformanceResponse
)
async def get_top_holding_performance(
    workspace_id: PathWorkspaceId,
    session: SessionDep,
    base_snapshot_id: int | None = Query(default=None),
    comparison_snapshot_id: int | None = Query(default=None),
    start_date: date | None = None,
    end_date: date | None = None,
    limit: int = Query(default=50, ge=1, le=500),
    sort: str = Query(
        default="abs_contribution",
        description="abs_contribution|contribution|abs_weight_delta|weight_delta|market_value_delta",
    ),
):
    """Which constituents likely drove value over a date window (price-context).

    A **price-context contribution estimate** (base implied value × local price
    return) from cached snapshots + instrument prices — NOT PnL, total return or
    trade attribution. Defaults to previous-vs-latest snapshot; returns
    ``insufficient_history`` (<2 snapshots) or ``insufficient_price_data`` (nothing
    priced). Explicit snapshot ids are workspace-scoped (no cross-workspace)."""
    return await performance_service.compute_top_holding_performance(
        session,
        workspace_id,
        base_snapshot_id=base_snapshot_id,
        comparison_snapshot_id=comparison_snapshot_id,
        start_date=start_date,
        end_date=end_date,
        limit=limit,
        sort=sort,
    )


@router.get("/exposure", response_model=ExposureResponse)
async def get_exposure_legacy(workspace_id: HeaderWorkspaceId, session: SessionDep):
    """Legacy ad-hoc look-through slices (country/sector/currency)."""
    return await legacy_service.build_exposure(session, workspace_id)
