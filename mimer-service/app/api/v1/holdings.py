"""Global holdings endpoint."""

from __future__ import annotations

from fastapi import APIRouter, Query

from app.api.deps import SessionDep
from app.schemas.common import ListResponse
from app.schemas.holding import HoldingRead
from app.services import holdings as service

router = APIRouter(prefix="/holdings", tags=["holdings"])


@router.get("", response_model=ListResponse[HoldingRead])
async def list_holdings(
    session: SessionDep,
    fund_id: int | None = None,
    limit: int = Query(default=500, ge=1, le=2000),
) -> ListResponse[HoldingRead]:
    items = await service.list_holdings(session, fund_id=fund_id, limit=limit)
    return ListResponse.of([HoldingRead.model_validate(i) for i in items])
