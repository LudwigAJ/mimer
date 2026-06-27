"""Approximate look-through exposure derived from seeded holdings.

Each portfolio position contributes its market-value weight to the portfolio.
That weight is then distributed across the fund's latest holdings to produce
country and sector exposure. Currency exposure is taken from each listing's
trading unit. This is intentionally approximate for the first version — only
modelled holdings are counted, so look-through slices sum to the modelled
fraction, not necessarily 1.0.
"""

from __future__ import annotations

from collections import defaultdict
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Fund, FundListing, PortfolioPosition, Price
from app.schemas.exposure import ExposureResponse, ExposureSlice
from app.services import holdings_ingestion as holdings_service
from app.services import workspaces as workspaces_service
from app.services.conversion import convert, load_fx_map, normalise_currency

_WEIGHT_Q = Decimal("0.0001")
_ZERO = Decimal("0")


def _slices(totals: dict[str, Decimal]) -> list[ExposureSlice]:
    items = [
        ExposureSlice(key=key, weight=weight.quantize(_WEIGHT_Q, rounding=ROUND_HALF_UP))
        for key, weight in totals.items()
    ]
    items.sort(key=lambda s: s.weight, reverse=True)
    return items


async def build_exposure(session: AsyncSession, workspace_id: int) -> ExposureResponse:
    workspace = await workspaces_service.get_workspace(session, workspace_id)
    base = workspace.base_currency

    rows = (
        await session.execute(
            select(PortfolioPosition, FundListing, Fund)
            .join(FundListing, PortfolioPosition.fund_listing_id == FundListing.id)
            .join(Fund, FundListing.fund_id == Fund.id)
            .where(PortfolioPosition.workspace_id == workspace_id)
        )
    ).all()
    if not rows:
        return ExposureResponse(country=[], sector=[], currency=[], base_currency=base)

    fx_map = await load_fx_map(session)
    listing_ids = [listing.id for (_, listing, _) in rows]
    fund_ids = list({fund.id for (_, _, fund) in rows})

    price_rows = (
        (
            await session.execute(
                select(Price)
                .where(Price.fund_listing_id.in_(listing_ids))
                .order_by(Price.fund_listing_id, Price.price_date.asc())
            )
        )
        .scalars()
        .all()
    )
    latest_price: dict[int, Price] = {p.fund_listing_id: p for p in price_rows}
    holdings_by_fund = await holdings_service.latest_holdings_by_fund(session, fund_ids)

    # Market value per position (fallback to equal weighting if no prices).
    values: list[tuple[PortfolioPosition, FundListing, Fund, Decimal]] = []
    total = _ZERO
    for position, listing, fund in rows:
        price = latest_price.get(listing.id)
        mv = _ZERO
        if price is not None:
            converted = convert(position.units * price.price, price.currency, base, fx_map)
            mv = converted if converted is not None else _ZERO
        values.append((position, listing, fund, mv))
        total += mv

    use_equal = total <= 0
    n = Decimal(len(values))

    country: dict[str, Decimal] = defaultdict(lambda: _ZERO)
    sector: dict[str, Decimal] = defaultdict(lambda: _ZERO)
    currency: dict[str, Decimal] = defaultdict(lambda: _ZERO)

    for _position, listing, fund, mv in values:
        pos_weight = (Decimal(1) / n) if use_equal else (mv / total)

        cur = normalise_currency(listing.currency_unit or listing.trading_currency, base)
        currency[cur] += pos_weight

        for holding in holdings_by_fund.get(fund.id, []):
            contribution = pos_weight * holding.weight
            if holding.country:
                country[holding.country] += contribution
            if holding.sector:
                sector[holding.sector] += contribution

    return ExposureResponse(
        country=_slices(country),
        sector=_slices(sector),
        currency=_slices(currency),
        base_currency=base,
    )
