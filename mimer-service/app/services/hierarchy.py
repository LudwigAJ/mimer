"""Investable hierarchy aggregation: Portfolio -> positions -> top holdings.

Values and weights are derived from latest prices + FX (same basis as the
portfolio summary). Holdings are bounded to the top N per position so the tree
stays small even for broad funds.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Fund, FundListing, PortfolioPosition, Price
from app.schemas.hierarchy import HierarchyNode, HierarchyResponse
from app.services import holdings_ingestion as holdings_service
from app.services import workspaces as workspaces_service
from app.services.conversion import convert, load_fx_map

_TOP_HOLDINGS = 10
_MONEY_Q = Decimal("0.01")
_WEIGHT_Q = Decimal("0.0001")
_ZERO = Decimal("0")


def _q(value: Decimal | None, quant: Decimal) -> Decimal | None:
    if value is None:
        return None
    return value.quantize(quant, rounding=ROUND_HALF_UP)


async def build_hierarchy(session: AsyncSession, workspace_id: int) -> HierarchyResponse:
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

    fx_map = await load_fx_map(session)
    listing_ids = [ln.id for _, ln, _ in rows]
    fund_ids = list({f.id for _, _, f in rows})

    latest_price: dict[int, Price] = {}
    if listing_ids:
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
        for price in price_rows:
            latest_price[price.fund_listing_id] = price
    holdings_by_fund = await holdings_service.latest_holdings_by_fund(session, fund_ids)

    # First pass: per-position market value (base) for weights.
    computed: list[tuple[PortfolioPosition, FundListing, Fund, Decimal | None, Price | None]] = []
    total_mv = _ZERO
    for position, listing, fund in rows:
        price = latest_price.get(listing.id)
        mv = None
        if price is not None:
            mv = convert(position.units * price.price, price.currency, base, fx_map)
            if mv is not None:
                total_mv += mv
        computed.append((position, listing, fund, mv, price))

    position_nodes: list[HierarchyNode] = []
    for position, listing, fund, mv, price in computed:
        weight = mv / total_mv if (mv is not None and total_mv > 0) else None

        holding_nodes: list[HierarchyNode] = []
        for holding in holdings_by_fund.get(fund.id, [])[:_TOP_HOLDINGS]:
            look_through = mv * holding.weight if mv is not None else None
            holding_nodes.append(
                HierarchyNode(
                    id=f"position:{position.id}:holding:{holding.id}",
                    kind="holding",
                    label=holding.security_name,
                    value=_q(look_through, _MONEY_Q),
                    currency=base,
                    weight=_q(holding.weight, _WEIGHT_Q),
                    status="derived",
                    source=holding.source,
                    children=[],
                )
            )

        position_nodes.append(
            HierarchyNode(
                id=f"position:{position.id}",
                kind="position",
                label=listing.ticker,
                value=_q(mv, _MONEY_Q),
                currency=base,
                weight=_q(weight, _WEIGHT_Q),
                status=listing.status,
                source=price.source if price is not None else "missing",
                children=holding_nodes,
            )
        )

    root = HierarchyNode(
        id=f"workspace:{workspace.id}",
        kind="portfolio",
        label=workspace.name,
        value=_q(total_mv, _MONEY_Q),
        currency=base,
        weight=Decimal("1.0") if position_nodes else _ZERO,
        status="derived",
        source="derived",
        children=position_nodes,
    )
    return HierarchyResponse(root=root)
