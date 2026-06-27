"""Fund-centric read services."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFoundError
from app.db.models import Distribution, Fund, FundListing


async def list_funds(session: AsyncSession) -> list[Fund]:
    result = await session.execute(select(Fund).order_by(Fund.name))
    return list(result.scalars().all())


async def get_fund(session: AsyncSession, fund_id: int) -> Fund:
    fund = await session.get(Fund, fund_id)
    if fund is None:
        raise NotFoundError("Fund not found", code="fund_not_found")
    return fund


async def list_listings(session: AsyncSession, fund_id: int) -> list[FundListing]:
    await get_fund(session, fund_id)
    result = await session.execute(
        select(FundListing).where(FundListing.fund_id == fund_id).order_by(FundListing.ticker)
    )
    return list(result.scalars().all())


async def list_fund_distributions(session: AsyncSession, fund_id: int) -> list[Distribution]:
    await get_fund(session, fund_id)
    result = await session.execute(
        select(Distribution)
        .where(Distribution.fund_id == fund_id)
        .order_by(Distribution.ex_date.desc())
    )
    return list(result.scalars().all())
