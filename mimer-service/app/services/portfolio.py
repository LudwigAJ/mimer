"""Portfolio position CRUD and the GUI-friendly summary."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFoundError
from app.db.models import Distribution, Fund, FundListing, PortfolioPosition, Price
from app.schemas.portfolio import (
    PortfolioSummary,
    PositionCreate,
    PositionUpdate,
    SummaryPosition,
)
from app.services import workspaces as workspaces_service
from app.services.fx import FxIndex, load_fx_index

_MONEY_Q = Decimal("0.01")
_WEIGHT_Q = Decimal("0.0001")
_RATE_Q = Decimal("0.0000000001")
_ZERO = Decimal("0")


def _q(value: Decimal | None, quant: Decimal) -> Decimal | None:
    if value is None:
        return None
    return value.quantize(quant, rounding=ROUND_HALF_UP)


# --- CRUD --------------------------------------------------------------------


async def list_positions(session: AsyncSession, workspace_id: int) -> list[PortfolioPosition]:
    result = await session.execute(
        select(PortfolioPosition)
        .where(PortfolioPosition.workspace_id == workspace_id)
        .order_by(PortfolioPosition.id)
    )
    return list(result.scalars().all())


async def _get_owned_position(
    session: AsyncSession, workspace_id: int, position_id: int
) -> PortfolioPosition:
    """Fetch a position, 404ing if it is missing or owned by another workspace."""
    position = await session.get(PortfolioPosition, position_id)
    if position is None or position.workspace_id != workspace_id:
        raise NotFoundError("Position not found", code="position_not_found")
    return position


async def create_position(
    session: AsyncSession, workspace_id: int, data: PositionCreate
) -> PortfolioPosition:
    listing = await session.get(FundListing, data.fund_listing_id)
    if listing is None:
        raise NotFoundError("Fund listing not found", code="fund_listing_not_found")
    position = PortfolioPosition(
        workspace_id=workspace_id,
        fund_listing_id=data.fund_listing_id,
        account_name=data.account_name,
        units=data.units,
        average_cost=data.average_cost,
        cost_currency=data.cost_currency,
    )
    session.add(position)
    await session.commit()
    await session.refresh(position)
    return position


async def update_position(
    session: AsyncSession, workspace_id: int, position_id: int, data: PositionUpdate
) -> PortfolioPosition:
    position = await _get_owned_position(session, workspace_id, position_id)
    for field, value in data.model_dump(exclude_unset=True).items():
        setattr(position, field, value)
    await session.commit()
    await session.refresh(position)
    return position


async def delete_position(session: AsyncSession, workspace_id: int, position_id: int) -> None:
    position = await _get_owned_position(session, workspace_id, position_id)
    await session.delete(position)
    await session.commit()


# --- Summary -----------------------------------------------------------------


async def _latest_prices(session: AsyncSession, listing_ids: list[int]) -> dict[int, Price]:
    """Return the most recent price row per listing."""
    if not listing_ids:
        return {}
    rows = (
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
    latest: dict[int, Price] = {}
    for price in rows:
        latest[price.fund_listing_id] = price  # ascending date => last wins
    return latest


async def _income_per_share_by_fund(
    session: AsyncSession, fund_ids: list[int], base_currency: str, fx_index: FxIndex
) -> dict[int, Decimal]:
    """Sum trailing-12-month per-share distributions per fund, in base currency.

    Each distribution is converted as of its ex-date (latest rate on/before),
    falling back to the latest available rate inside the FX index helper.
    """
    if not fund_ids:
        return {}
    cutoff = date.today() - timedelta(days=365)
    rows = (
        (
            await session.execute(
                select(Distribution).where(
                    Distribution.fund_id.in_(fund_ids), Distribution.ex_date >= cutoff
                )
            )
        )
        .scalars()
        .all()
    )
    totals: dict[int, Decimal] = {}
    for dist in rows:
        conv = fx_index.convert_amount(
            dist.amount, dist.currency, base_currency, as_of_date=dist.ex_date
        )
        if conv.converted_amount is None:
            continue
        totals[dist.fund_id] = totals.get(dist.fund_id, _ZERO) + conv.converted_amount
    return totals


async def build_summary(session: AsyncSession, workspace_id: int) -> PortfolioSummary:
    workspace = await workspaces_service.get_workspace(session, workspace_id)
    base = workspace.base_currency

    rows = (
        await session.execute(
            select(PortfolioPosition, FundListing, Fund)
            .join(FundListing, PortfolioPosition.fund_listing_id == FundListing.id)
            .join(Fund, FundListing.fund_id == Fund.id)
            .where(PortfolioPosition.workspace_id == workspace_id)
            .order_by(Fund.name)
        )
    ).all()

    fx_index = await load_fx_index(session)
    listing_ids = [listing.id for (_, listing, _) in rows]
    fund_ids = list({fund.id for (_, _, fund) in rows})
    latest_prices = await _latest_prices(session, listing_ids)
    income_per_share = await _income_per_share_by_fund(session, fund_ids, base, fx_index)

    # First pass: compute market value (needed for portfolio weights). Each
    # position is valued in its local/listing currency, then converted to the
    # workspace base currency via FX (carrying rate/source/freshness).
    computed: list[dict] = []
    total_mv = _ZERO
    for position, listing, fund in rows:
        price = latest_prices.get(listing.id)
        price_value = price.price if price else None
        price_currency = price.currency if price else listing.currency_unit

        local_value = None
        market_value = None  # base currency
        conv = None
        if price_value is not None:
            conv = fx_index.convert_amount(position.units * price_value, price_currency, base)
            local_value = conv.amount
            market_value = conv.converted_amount
            if market_value is not None:
                total_mv += market_value

        cost_base = None
        if position.average_cost is not None:
            cost_conv = fx_index.convert_amount(
                position.units * position.average_cost,
                position.cost_currency or base,
                base,
            )
            cost_base = cost_conv.converted_amount

        per_share_income = income_per_share.get(fund.id, _ZERO)
        position_income = per_share_income * position.units if per_share_income else _ZERO

        computed.append(
            {
                "position": position,
                "listing": listing,
                "fund": fund,
                "price_value": price_value,
                "price_currency": price_currency,
                "local_value": local_value,
                "market_value": market_value,
                "conversion": conv,
                "cost_base": cost_base,
                "income": position_income,
            }
        )

    # Second pass: build position breakdown + portfolio totals.
    positions: list[SummaryPosition] = []
    total_unrealised: Decimal | None = None
    total_income = _ZERO
    for entry in computed:
        mv = entry["market_value"]
        weight = None
        if mv is not None and total_mv > 0:
            weight = mv / total_mv

        income = entry["income"]
        trailing_yield = None
        if income and mv and mv > 0:
            trailing_yield = income / mv
        total_income += income

        if entry["cost_base"] is not None and mv is not None:
            unrealised = mv - entry["cost_base"]
            total_unrealised = (total_unrealised or _ZERO) + unrealised

        conv = entry["conversion"]
        positions.append(
            SummaryPosition(
                fund_listing_id=entry["listing"].id,
                ticker=entry["listing"].ticker,
                fund_name=entry["fund"].name,
                isin=entry["fund"].isin,
                units=entry["position"].units,
                price=entry["price_value"],
                currency=entry["price_currency"],
                market_value=_q(mv, _MONEY_Q),
                portfolio_weight=_q(weight, _WEIGHT_Q),
                trailing_yield=_q(trailing_yield, _WEIGHT_Q),
                projected_income=_q(income, _MONEY_Q),
                listing_currency=(conv.from_currency if conv else None),
                base_currency=base,
                market_value_local=_q(entry["local_value"], _MONEY_Q),
                market_value_base=_q(mv, _MONEY_Q),
                fx_rate=(_q(conv.rate, _RATE_Q) if conv and conv.rate is not None else None),
                fx_source=(conv.source if conv else None),
                fx_status=(conv.status if conv else None),
            )
        )

    return PortfolioSummary(
        base_currency=base,
        total_market_value=_q(total_mv, _MONEY_Q) or _ZERO,
        daily_change=None,  # placeholder: requires previous-day prices.
        unrealised_gain_loss=_q(total_unrealised, _MONEY_Q),
        trailing_12m_income=_q(total_income, _MONEY_Q),
        # Simple projection: carry the trailing 12m income forward.
        projected_annual_income=_q(total_income, _MONEY_Q),
        positions=positions,
    )
