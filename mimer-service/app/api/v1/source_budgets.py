"""Source rate-budget endpoints.

Read-only view of each source's request budget + its current fetch decision
(allowed / why / how long to wait / in backoff). No secrets are exposed.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter

from app.api.deps import SessionDep
from app.core.errors import NotFoundError
from app.db.models import SourceRateLimit
from app.schemas.common import ListResponse
from app.schemas.source_ops import SourceBudgetRead
from app.services import source_budget as budget_service

router = APIRouter(prefix="/source-budgets", tags=["source-budgets"])


async def _to_read(session: SessionDep, row: SourceRateLimit, now: datetime) -> SourceBudgetRead:
    decision = await budget_service.check_budget(session, row.source_name, now=now)
    read = SourceBudgetRead.model_validate(row)
    read.allowed = decision.allowed
    read.reason = decision.reason
    read.wait_seconds = decision.wait_seconds
    read.in_backoff = decision.reason == "in_backoff"
    return read


@router.get("", response_model=ListResponse[SourceBudgetRead])
async def list_source_budgets(session: SessionDep) -> ListResponse[SourceBudgetRead]:
    now = datetime.now(UTC)
    rows = await budget_service.list_budgets(session)
    return ListResponse.of([await _to_read(session, r, now) for r in rows])


@router.get("/{source_name}", response_model=SourceBudgetRead)
async def get_source_budget(source_name: str, session: SessionDep) -> SourceBudgetRead:
    row = await budget_service.get_budget(session, source_name)
    if row is None:
        raise NotFoundError("No budget for source", code="source_budget_not_found")
    return await _to_read(session, row, datetime.now(UTC))
