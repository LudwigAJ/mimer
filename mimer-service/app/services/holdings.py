"""Holdings read services (global list + per-fund snapshot)."""

from __future__ import annotations

from datetime import date

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import FundHolding
from app.schemas.holding import FundHoldingsResponse, HoldingRead
from app.services import funds as funds_service
from app.services import holdings_ingestion as holdings_service


async def list_holdings(
    session: AsyncSession,
    fund_id: int | None = None,
    limit: int = 500,
) -> list[FundHolding]:
    stmt = select(FundHolding).order_by(FundHolding.weight.desc()).limit(limit)
    if fund_id is not None:
        stmt = stmt.where(FundHolding.fund_id == fund_id)
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def build_fund_holdings(
    session: AsyncSession,
    fund_id: int,
    *,
    as_of_date: date | None = None,
    source: str | None = None,
    limit: int = 100,
) -> FundHoldingsResponse:
    """One coherent holdings snapshot for a fund (single source + as-of date).

    Returns the latest/best snapshot by default; ``source`` and ``as_of_date``
    pin a specific snapshot. Response carries the snapshot's provenance so the
    GUI can badge it (source/status/as_of_date). Bounded by ``limit``.
    """
    fund = await funds_service.get_fund(session, fund_id)
    holdings = await holdings_service.latest_holdings_for_fund(
        session, fund_id, as_of_date=as_of_date, source=source, limit=limit
    )
    top = holdings[0] if holdings else None
    return FundHoldingsResponse(
        fund_id=fund.id,
        fund_name=fund.name,
        as_of_date=top.as_of_date if top else None,
        source=top.source if top else None,
        status=top.status if top else None,
        holdings=[HoldingRead.model_validate(h) for h in holdings],
    )
