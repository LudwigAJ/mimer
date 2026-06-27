"""FX endpoints: rates, pair time-series, and a provenance-rich conversion.

These complement the existing ``/fx-rates`` list. ``/fx/rates`` and
``/fx/time-series`` read stored ``fx_rates`` (the latter inverts a pair when only
the opposite direction is stored). ``/fx/convert`` runs the lookup/triangulation
engine (`app/services/fx.py`) and returns full source/freshness metadata so a
source-selection aware GUI has what it needs.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Query

from app.api.deps import SessionDep
from app.schemas.common import ListResponse
from app.schemas.fx import FxConversionRead
from app.schemas.fxrate import FxRateRead
from app.schemas.timeseries import TimeRange, TimeSeriesResponse
from app.services import fxrates as fxrates_service
from app.services import timeseries as timeseries_service
from app.services.fx import load_fx_index

router = APIRouter(prefix="/fx", tags=["fx"])

OptStrQuery = Annotated[str | None, Query()]
RangeQuery = Annotated[TimeRange, Query()]
LimitQuery = Annotated[int, Query(ge=1, le=1000)]


@router.get("/rates", response_model=ListResponse[FxRateRead])
async def list_fx_rates(
    session: SessionDep,
    base: OptStrQuery = None,
    quote: OptStrQuery = None,
    source: OptStrQuery = None,
    limit: LimitQuery = 200,
) -> ListResponse[FxRateRead]:
    items = await fxrates_service.list_fx_rates(
        session, base_currency=base, quote_currency=quote, source=source, limit=limit
    )
    return ListResponse.of([FxRateRead.model_validate(i) for i in items])


@router.get("/time-series", response_model=TimeSeriesResponse)
async def fx_time_series(
    session: SessionDep,
    base: Annotated[str, Query()],
    quote: Annotated[str, Query()],
    range: RangeQuery = "1y",
    source: OptStrQuery = None,
) -> TimeSeriesResponse:
    return await timeseries_service.fx_time_series(
        session, base, quote, range_=range, source=source
    )


@router.get("/convert", response_model=FxConversionRead)
async def convert(
    session: SessionDep,
    from_currency: Annotated[str, Query(alias="from")],
    to: Annotated[str, Query()],
    amount: Annotated[Decimal, Query()] = Decimal(1),
    as_of: Annotated[date | None, Query()] = None,
    source: OptStrQuery = None,
) -> FxConversionRead:
    index = await load_fx_index(session)
    result = index.convert_amount(amount, from_currency, to, as_of_date=as_of, source_policy=source)
    return FxConversionRead(
        from_currency=result.from_currency,
        to_currency=result.to_currency,
        amount=result.amount,
        converted_amount=result.converted_amount,
        rate=result.rate,
        rate_date=result.rate_date,
        source=result.source,
        status=result.status,
        is_direct=result.is_direct,
        is_inverse=result.is_inverse,
        is_triangulated=result.is_triangulated,
        missing_reason=result.missing_reason,
        requested_source=result.requested_source,
        effective_source=result.effective_source,
        fallback_used=result.fallback_used,
        available_sources=result.available_sources,
    )
