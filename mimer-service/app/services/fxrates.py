"""FX rate listing (shared reference data)."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import FxRate


async def list_fx_rates(
    session: AsyncSession,
    base_currency: str | None = None,
    quote_currency: str | None = None,
    source: str | None = None,
    limit: int = 200,
) -> list[FxRate]:
    stmt = select(FxRate).order_by(FxRate.rate_date.desc(), FxRate.id.desc()).limit(limit)
    if base_currency is not None:
        stmt = stmt.where(FxRate.base_currency == base_currency.upper())
    if quote_currency is not None:
        stmt = stmt.where(FxRate.quote_currency == quote_currency.upper())
    if source is not None:
        stmt = stmt.where(FxRate.source == source)
    return list((await session.execute(stmt)).scalars().all())
