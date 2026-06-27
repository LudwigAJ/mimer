"""Data-source endpoints.

* ``GET /api/v1/data-sources`` — the runtime ``data_sources`` registry rows
  (source type + priority + activation), used for source ranking.
* ``GET /api/v1/data-sources/capabilities`` — the code capability catalogue
  (`app/sources/registry.py`): what each candidate source can provide and
  whether an adapter is implemented yet.
"""

from __future__ import annotations

from fastapi import APIRouter, Query
from sqlalchemy import select

from app.api.deps import SessionDep
from app.db.models import DataSource
from app.schemas.capability import SourceCapabilityRead
from app.schemas.common import ListResponse, Meta
from app.schemas.data_source import DataSourceRead
from app.schemas.fund_coverage import FundCoverageMatrix
from app.schemas.source_readiness import SourceReadinessMatrix
from app.services import capabilities as capabilities_service

router = APIRouter(prefix="/data-sources", tags=["data-sources"])


# Declared before "" is matched generically; readiness is a sibling path.
@router.get("/readiness", response_model=SourceReadinessMatrix)
async def source_readiness(
    data_type: str | None = Query(default=None),
    status: str | None = Query(default=None),
    scheduler_safe: bool | None = Query(default=None),
    requires_secret: bool | None = Query(default=None),
) -> SourceReadinessMatrix:
    """Production data-source readiness matrix: can each source ingest real data on a VPS,
    and is it safe to schedule — or fixture-only / blocked / not implemented?"""
    matrix = capabilities_service.build_source_readiness_matrix()
    rows = matrix.data
    if data_type is not None:
        rows = [r for r in rows if r.data_type == data_type]
    if status is not None:
        rows = [r for r in rows if r.status == status]
    if scheduler_safe is not None:
        rows = [r for r in rows if r.safe_for_scheduler == scheduler_safe]
    if requires_secret is not None:
        rows = [r for r in rows if r.requires_secret == requires_secret]
    return SourceReadinessMatrix(data=rows, meta=Meta(count=len(rows)), summary=matrix.summary)


# Declared before "" is matched generically; fund-coverage is a sibling path.
@router.get("/fund-coverage", response_model=FundCoverageMatrix)
async def fund_coverage(
    fund_symbol: str | None = Query(default=None),
    data_type: str | None = Query(default=None),
    status: str | None = Query(default=None),
) -> FundCoverageMatrix:
    """Target-fund (VUSA/ISF/JEPG) live-readiness matrix: per *(fund, data type)* — can the
    backend fetch/parse/store this data type live, is it scheduler-safe, or what blocks it?"""
    matrix = capabilities_service.build_fund_coverage_matrix(fund_symbol)
    rows = matrix.data
    if data_type is not None:
        rows = [r for r in rows if r.data_type == data_type]
    if status is not None:
        rows = [r for r in rows if r.status == status]
    return FundCoverageMatrix(data=rows, meta=Meta(count=len(rows)), summary=matrix.summary)


# Declared before "" is matched generically; capabilities is a sibling path.
@router.get("/capabilities", response_model=ListResponse[SourceCapabilityRead])
async def list_capabilities(
    source_type: str | None = Query(default=None),
    data_type: str | None = Query(default=None),
    adapter_status: str | None = Query(default=None),
) -> ListResponse[SourceCapabilityRead]:
    items = capabilities_service.build_capabilities().sources
    if source_type is not None:
        items = [c for c in items if c.source_type == source_type]
    if data_type is not None:
        items = [c for c in items if data_type in c.data_types]
    if adapter_status is not None:
        items = [c for c in items if c.adapter_status == adapter_status]
    return ListResponse.of(items)


@router.get("", response_model=ListResponse[DataSourceRead])
async def list_data_sources(
    session: SessionDep,
    source_type: str | None = Query(default=None),
    is_active: bool | None = Query(default=None),
) -> ListResponse[DataSourceRead]:
    stmt = select(DataSource).order_by(DataSource.priority, DataSource.name)
    if source_type is not None:
        stmt = stmt.where(DataSource.source_type == source_type)
    if is_active is not None:
        stmt = stmt.where(DataSource.is_active.is_(is_active))
    rows = (await session.execute(stmt)).scalars().all()
    return ListResponse.of([DataSourceRead.model_validate(r) for r in rows])
