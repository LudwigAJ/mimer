"""Global distribution endpoint."""

from __future__ import annotations

from fastapi import APIRouter, Query

from app.api.deps import SessionDep
from app.schemas.common import ListResponse
from app.schemas.distribution import DistributionRead
from app.services import distributions as service

router = APIRouter(prefix="/distributions", tags=["distributions"])


@router.get("", response_model=ListResponse[DistributionRead])
async def list_distributions(
    session: SessionDep,
    fund_id: int | None = None,
    limit: int = Query(default=200, ge=1, le=1000),
) -> ListResponse[DistributionRead]:
    items = await service.list_distributions(session, fund_id=fund_id, limit=limit)
    return ListResponse.of([DistributionRead.model_validate(i) for i in items])
