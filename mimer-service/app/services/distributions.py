"""Global distribution listing service."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Distribution


async def list_distributions(
    session: AsyncSession,
    fund_id: int | None = None,
    limit: int = 200,
) -> list[Distribution]:
    stmt = select(Distribution).order_by(Distribution.ex_date.desc()).limit(limit)
    if fund_id is not None:
        stmt = stmt.where(Distribution.fund_id == fund_id)
    result = await session.execute(stmt)
    return list(result.scalars().all())
