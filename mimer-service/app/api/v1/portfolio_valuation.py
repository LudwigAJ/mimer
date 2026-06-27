"""Portfolio valuation/readiness snapshot endpoints.

All workspace-scoped under ``/workspaces/{workspace_id}``. These serve a bounded,
cacheable read model: which positions can be valued from already-ingested
prices/FX, and what is blocking the rest. It is NOT PnL / tax lots / total return
(see AGENTS.md compute boundary) and it never fetches a price/FX source.
"""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Query

from app.api.deps import PathWorkspaceId, SessionDep
from app.schemas.portfolio_valuation import (
    PortfolioValuationCoverage,
    PortfolioValuationHistory,
    PortfolioValuationRecomputeResponse,
    PortfolioValuationResponse,
    PortfolioValuationSummaryResponse,
)
from app.services import portfolio_valuation as service

workspace_router = APIRouter(prefix="/workspaces/{workspace_id}", tags=["portfolio valuation"])


@workspace_router.get("/portfolio/valuation", response_model=PortfolioValuationResponse)
async def get_portfolio_valuation(
    workspace_id: PathWorkspaceId,
    session: SessionDep,
    valuation_status: str | None = Query(default=None),
    limit: int = Query(default=500, ge=1, le=5000),
) -> PortfolioValuationResponse:
    """Latest valuation snapshot, or an on-the-fly computation when none exists yet.

    ``cached=false`` / ``status=recompute_needed`` flags an on-the-fly result so the
    GUI knows a recompute is due. ``valuation_status`` filters rows (e.g.
    ``missing_price`` / ``missing_fx`` / ``unresolved_instrument``).
    """
    return await service.build_latest_response(
        session, workspace_id, valuation_status=valuation_status, limit=limit
    )


@workspace_router.get("/portfolio/valuation/latest", response_model=PortfolioValuationResponse)
async def get_latest_portfolio_valuation(
    workspace_id: PathWorkspaceId,
    session: SessionDep,
    valuation_status: str | None = Query(default=None),
    limit: int = Query(default=500, ge=1, le=5000),
) -> PortfolioValuationResponse:
    """The latest *persisted* valuation snapshot only (``cached=false`` if none)."""
    return await service.build_latest_response(
        session,
        workspace_id,
        valuation_status=valuation_status,
        limit=limit,
        compute_if_missing=False,
    )


@workspace_router.get("/portfolio/valuation/coverage", response_model=PortfolioValuationCoverage)
async def get_portfolio_valuation_coverage(
    workspace_id: PathWorkspaceId, session: SessionDep
) -> PortfolioValuationCoverage:
    """Coverage-only roll-up (summary counts, no rows) for a compact widget."""
    return await service.build_coverage(session, workspace_id)


@workspace_router.get("/portfolio/valuation/history", response_model=PortfolioValuationHistory)
async def get_portfolio_valuation_history(
    workspace_id: PathWorkspaceId,
    session: SessionDep,
    broker_account_id: int | None = None,
    start_date: date | None = None,
    end_date: date | None = None,
    base_currency: str | None = None,
    limit: int = Query(default=250, ge=1, le=500),
) -> PortfolioValuationHistory:
    """Bounded, oldest-first series of already-persisted valuation snapshots.

    Reads ``portfolio_valuation_snapshots`` only — no recompute, no live fetch, no
    PnL/return/performance fields, no differencing of points. ``limit`` bounds the
    most-recent window returned (default 250, max 500)."""
    return await service.get_portfolio_valuation_history(
        session,
        workspace_id,
        broker_account_id=broker_account_id,
        start_date=start_date,
        end_date=end_date,
        base_currency=base_currency,
        limit=limit,
    )


@workspace_router.get(
    "/portfolio/valuation/summary", response_model=PortfolioValuationSummaryResponse
)
async def get_portfolio_valuation_summary(
    workspace_id: PathWorkspaceId,
    session: SessionDep,
    broker_account_id: int | None = None,
    base_currency: str | None = None,
) -> PortfolioValuationSummaryResponse:
    """Compact latest-context + readiness roll-up over the workspace's snapshots.

    Cheap SQL over the snapshots table (latest snapshot + a bounded count + the
    per-broker-account latest). No rows, no PnL/return/performance fields, no
    recompute on the read path."""
    return await service.build_summary(
        session,
        workspace_id,
        broker_account_id=broker_account_id,
        base_currency=base_currency,
    )


@workspace_router.post(
    "/portfolio/valuation/recompute", response_model=PortfolioValuationRecomputeResponse
)
async def recompute_portfolio_valuation(
    workspace_id: PathWorkspaceId,
    session: SessionDep,
    base_currency: str | None = Query(default=None),
    broker_account_id: int | None = Query(default=None),
    force: bool = Query(default=False),
) -> PortfolioValuationRecomputeResponse:
    """Recompute + idempotently persist the valuation snapshot (consumes existing
    prices/FX only — no live fetch, no identity resolution)."""
    result = await service.recompute_portfolio_valuation_snapshot(
        session,
        workspace_id,
        base_currency=base_currency,
        broker_account_id=broker_account_id,
        force=force,
    )
    await session.commit()
    return PortfolioValuationRecomputeResponse(
        workspace_id=result.workspace_id,
        snapshot_id=result.snapshot_id,
        as_of_date=result.as_of_date,
        base_currency=result.base_currency,
        broker_account_id=result.broker_account_id,
        status=result.status,
        positions_selected=result.positions_selected,
        positions_valued=result.positions_valued,
        missing_price=result.missing_price,
        missing_fx=result.missing_fx,
        unresolved=result.unresolved,
        ambiguous=result.ambiguous,
        cash_rows=result.cash_rows,
        stale_price=result.stale_price,
        stale_fx=result.stale_fx,
        snapshot_created=result.snapshot_created,
        snapshot_updated=result.snapshot_updated,
        snapshot_skipped=result.snapshot_skipped,
        message=result.message(),
    )
