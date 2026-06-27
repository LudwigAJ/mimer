"""FX rate endpoint (shared reference data)."""

from __future__ import annotations

from fastapi import APIRouter, Query

from app.api.deps import SessionDep
from app.schemas.common import ListResponse
from app.schemas.fxrate import FxRateRead
from app.services import fxrates as service

router = APIRouter(prefix="/fx-rates", tags=["fx-rates"])


@router.get("", response_model=ListResponse[FxRateRead])
async def list_fx_rates(
    session: SessionDep,
    base_currency: str | None = None,
    quote_currency: str | None = None,
    limit: int = Query(default=200, ge=1, le=1000),
):
    items = await service.list_fx_rates(
        session, base_currency=base_currency, quote_currency=quote_currency, limit=limit
    )
    return ListResponse.of([FxRateRead.model_validate(i) for i in items])
