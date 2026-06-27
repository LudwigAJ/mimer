"""Instrument resolution endpoint.

`POST /api/v1/instruments` resolves a submitted identifier and, on a confident
single match, creates/reuses the fund + listing and queues backfill jobs.

Responses:
* 202 Accepted  — confident match; fund/listing ready, backfill queued.
* 409 Conflict  — ambiguous/low-confidence; candidates returned, nothing created.
* 404 Not Found — no match (structured error envelope).
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query, status
from fastapi.responses import JSONResponse

from app.api.deps import SessionDep
from app.schemas.common import ListResponse
from app.schemas.constituent import (
    InstrumentDetailRead,
    InstrumentListingRead,
    InstrumentPriceRead,
)
from app.schemas.instrument import (
    InstrumentAmbiguousResponse,
    InstrumentRequest,
    InstrumentResolveResponse,
)
from app.schemas.timeseries import SeriesKind, TimeRange, TimeSeriesResponse
from app.services import constituents as constituents_service
from app.services import identity as identity_service
from app.services import timeseries as timeseries_service

router = APIRouter(prefix="/instruments", tags=["instruments"])

KindQuery = Annotated[SeriesKind, Query()]
RangeQuery = Annotated[TimeRange, Query()]
SourceQuery = Annotated[str | None, Query()]


@router.post(
    "",
    responses={
        202: {"model": InstrumentResolveResponse},
        409: {"model": InstrumentAmbiguousResponse},
    },
)
async def resolve_instrument(payload: InstrumentRequest, session: SessionDep) -> JSONResponse:
    result = await identity_service.create_from_symbol(session, payload)
    if isinstance(result, InstrumentAmbiguousResponse):
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT, content=result.model_dump(mode="json")
        )
    return JSONResponse(
        status_code=status.HTTP_202_ACCEPTED, content=result.model_dump(mode="json")
    )


@router.get("/{instrument_id}", response_model=InstrumentDetailRead)
async def get_instrument(instrument_id: int, session: SessionDep) -> InstrumentDetailRead:
    """A canonical instrument (constituent) with its listings + identifiers."""
    return await constituents_service.build_instrument_detail(session, instrument_id)


@router.get("/{instrument_id}/listings", response_model=ListResponse[InstrumentListingRead])
async def list_instrument_listings(
    instrument_id: int, session: SessionDep
) -> ListResponse[InstrumentListingRead]:
    """An instrument's tradable listings (what EOD price ingestion fetches)."""
    listings = await constituents_service.list_instrument_listings(session, instrument_id)
    return ListResponse.of(listings)


@router.get("/{instrument_id}/prices", response_model=ListResponse[InstrumentPriceRead])
async def list_instrument_prices(
    instrument_id: int,
    session: SessionDep,
    source: SourceQuery = None,
    limit: int = Query(default=365, ge=1, le=3650),
) -> ListResponse[InstrumentPriceRead]:
    """Stored EOD bars for an instrument's primary listing (oldest first)."""
    prices = await constituents_service.list_instrument_prices(
        session, instrument_id, source=source, limit=limit
    )
    return ListResponse.of(prices)


@router.get("/{instrument_id}/time-series", response_model=TimeSeriesResponse)
async def instrument_time_series(
    instrument_id: int,
    session: SessionDep,
    kind: KindQuery = "price",
    range: RangeQuery = "1y",
    source: SourceQuery = None,
) -> TimeSeriesResponse:
    """Chart-friendly EOD price series for an instrument (its primary listing)."""
    return await timeseries_service.instrument_time_series(
        session, instrument_id, kind=kind, range_=range, source=source
    )
