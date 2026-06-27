"""Official / reference-rate endpoints (shared reference data).

Read-only, GUI-friendly access to the ``reference_rates`` observations:

* ``GET /api/v1/rates``             — filtered list of observations
* ``GET /api/v1/rates/latest``      — newest observation per series
* ``GET /api/v1/rates/sources``     — the rates-source catalogue (impl / planned)
* ``GET /api/v1/rates/time-series`` — one series' observations (chart shape)

These serve stored official observations only — never a constructed curve,
interpolated point or discount factor (see AGENTS.md compute boundary).
"""

from __future__ import annotations

from datetime import date
from typing import Annotated

from fastapi import APIRouter, Query

from app.api.deps import SessionDep
from app.schemas.common import ListResponse
from app.schemas.rate import (
    ReferenceRateRead,
    ReferenceRateSeriesRead,
    ReferenceRateSourceRead,
)
from app.services import rates as service
from app.sources.rates import list_rates_sources

router = APIRouter(prefix="/rates", tags=["rates"])


@router.get("/latest", response_model=ListResponse[ReferenceRateRead])
async def latest_rates(
    session: SessionDep,
    currency: str | None = None,
    country_or_region: str | None = None,
    rate_family: str | None = None,
    rate_name: str | None = None,
    source: str | None = None,
    limit: int = Query(default=500, ge=1, le=2000),
):
    items = await service.latest_reference_rates(
        session,
        currency=currency,
        country_or_region=country_or_region,
        rate_family=rate_family,
        rate_name=rate_name,
        source=source,
        limit=limit,
    )
    return ListResponse.of([ReferenceRateRead.model_validate(i) for i in items])


@router.get("/sources", response_model=ListResponse[ReferenceRateSourceRead])
async def rate_sources():
    infos = list_rates_sources()
    return ListResponse.of(
        [
            ReferenceRateSourceRead(
                source=i.source,
                adapter_status=i.adapter_status,
                is_fixture=i.is_fixture,
                requires_live_fetch=i.requires_live_fetch,
                is_default=i.is_default,
                description=i.description,
                currencies=list(i.currencies),
                rate_families=list(i.rate_families),
            )
            for i in infos
        ]
    )


@router.get("/time-series", response_model=ReferenceRateSeriesRead)
async def rate_time_series(
    session: SessionDep,
    rate_name: str = Query(..., description="e.g. ESTR, SONIA, US_TREASURY_PAR_YIELD"),
    currency: str | None = None,
    country_or_region: str | None = None,
    tenor: str | None = None,
    source: str | None = None,
    start_date: date | None = None,
    end_date: date | None = None,
    limit: int = Query(default=730, ge=1, le=2000),
):
    series = await service.reference_rate_time_series(
        session,
        rate_name=rate_name,
        currency=currency,
        country_or_region=country_or_region,
        tenor=tenor,
        source=source,
        start_date=start_date,
        end_date=end_date,
        limit=limit,
    )
    return ReferenceRateSeriesRead(
        rate_name=series.rate_name,
        currency=series.currency,
        country_or_region=series.country_or_region,
        tenor=series.tenor,
        source=series.source,
        unit=series.unit,
        points=[p.__dict__ for p in series.points],
    )


@router.get("", response_model=ListResponse[ReferenceRateRead])
async def list_rates(
    session: SessionDep,
    currency: str | None = None,
    country_or_region: str | None = None,
    rate_family: str | None = None,
    rate_name: str | None = None,
    tenor: str | None = None,
    source: str | None = None,
    on_date: Annotated[
        date | None, Query(alias="date", description="Exact observation date")
    ] = None,
    start_date: date | None = None,
    end_date: date | None = None,
    limit: int = Query(default=200, ge=1, le=1000),
):
    items = await service.list_reference_rates(
        session,
        currency=currency,
        country_or_region=country_or_region,
        rate_family=rate_family,
        rate_name=rate_name,
        tenor=tenor,
        source=source,
        rate_date=on_date,
        start_date=start_date,
        end_date=end_date,
        limit=limit,
    )
    return ListResponse.of([ReferenceRateRead.model_validate(i) for i in items])
