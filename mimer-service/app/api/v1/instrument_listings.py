"""Constituent instrument-listing endpoints (EOD prices + time-series).

Read-only views of stored constituent prices (``instrument_prices``), populated
by the ``constituent_eod_price_ingestion`` worker. No network I/O here.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query

from app.api.deps import SessionDep
from app.schemas.common import ListResponse
from app.schemas.constituent import InstrumentPriceRead
from app.schemas.timeseries import SeriesKind, TimeRange, TimeSeriesResponse
from app.services import constituents as constituents_service
from app.services import timeseries as timeseries_service

router = APIRouter(prefix="/instrument-listings", tags=["instrument-prices"])

KindQuery = Annotated[SeriesKind, Query()]
RangeQuery = Annotated[TimeRange, Query()]
SourceQuery = Annotated[str | None, Query()]


@router.get("/{instrument_listing_id}/prices", response_model=ListResponse[InstrumentPriceRead])
async def list_listing_prices(
    instrument_listing_id: int,
    session: SessionDep,
    source: SourceQuery = None,
    limit: int = Query(default=365, ge=1, le=3650),
) -> ListResponse[InstrumentPriceRead]:
    """Stored EOD bars for a constituent listing (oldest first)."""
    prices = await constituents_service.list_listing_prices(
        session, instrument_listing_id, source=source, limit=limit
    )
    return ListResponse.of(prices)


@router.get("/{instrument_listing_id}/time-series", response_model=TimeSeriesResponse)
async def listing_time_series(
    instrument_listing_id: int,
    session: SessionDep,
    kind: KindQuery = "price",
    range: RangeQuery = "1y",
    source: SourceQuery = None,
) -> TimeSeriesResponse:
    """Chart-friendly EOD price series for a constituent listing."""
    return await timeseries_service.instrument_listing_time_series(
        session, instrument_listing_id, kind=kind, range_=range, source=source
    )
