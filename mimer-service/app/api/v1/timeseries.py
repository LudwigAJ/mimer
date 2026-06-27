"""Chart time-series endpoints.

* ``GET /api/v1/fund-listings/{id}/time-series`` — listing price/distribution.
* ``GET /api/v1/funds/{id}/time-series`` — fund price (primary listing)/distribution.
* ``GET /api/v1/workspaces/{id}/portfolio/time-series`` — derived portfolio value/income.

Query params: ``kind`` (price|nav|market_value|distribution|yield|portfolio_value|fx),
``range`` (1m|3m|6m|1y|all), optional ``source``.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query

from app.api.deps import PathWorkspaceId, SessionDep
from app.schemas.timeseries import SeriesKind, TimeRange, TimeSeriesResponse
from app.services import timeseries as service

listing_router = APIRouter(prefix="/fund-listings", tags=["time-series"])
fund_router = APIRouter(prefix="/funds", tags=["time-series"])
portfolio_router = APIRouter(prefix="/workspaces/{workspace_id}/portfolio", tags=["time-series"])

KindQuery = Annotated[SeriesKind, Query()]
RangeQuery = Annotated[TimeRange, Query()]
SourceQuery = Annotated[str | None, Query()]


@listing_router.get("/{fund_listing_id}/time-series", response_model=TimeSeriesResponse)
async def listing_time_series(
    fund_listing_id: int,
    session: SessionDep,
    kind: KindQuery = "price",
    range: RangeQuery = "1y",
    source: SourceQuery = None,
) -> TimeSeriesResponse:
    return await service.listing_time_series(
        session, fund_listing_id, kind=kind, range_=range, source=source
    )


@fund_router.get("/{fund_id}/time-series", response_model=TimeSeriesResponse)
async def fund_time_series(
    fund_id: int,
    session: SessionDep,
    kind: KindQuery = "distribution",
    range: RangeQuery = "1y",
    source: SourceQuery = None,
) -> TimeSeriesResponse:
    return await service.fund_time_series(session, fund_id, kind=kind, range_=range, source=source)


@portfolio_router.get("/time-series", response_model=TimeSeriesResponse)
async def portfolio_time_series(
    workspace_id: PathWorkspaceId,
    session: SessionDep,
    kind: KindQuery = "portfolio_value",
    range: RangeQuery = "1y",
    source: SourceQuery = None,
) -> TimeSeriesResponse:
    return await service.portfolio_time_series(
        session, workspace_id, kind=kind, range_=range, source=source
    )
