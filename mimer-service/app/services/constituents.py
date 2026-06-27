"""Constituent / instrument read shaping for the API.

Thin read layer over ``constituent_identity`` (persistence/orchestration) + the
holdings snapshot selection. Turns ORM rows into the GUI-facing identity view:
per-holding resolution state, the resolved instrument, and the next action.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFoundError
from app.db.models import FundHolding, Instrument, InstrumentListing, InstrumentPrice
from app.schemas.constituent import (
    ConstituentPriceSummary,
    ConstituentRead,
    FundConstituentsResponse,
    InstrumentDetailRead,
    InstrumentIdentifierRead,
    InstrumentListingRead,
    InstrumentPriceRead,
    InstrumentSummary,
)
from app.schemas.holding import FundHoldingsResponse, HoldingRead
from app.services import constituent_identity
from app.services import freshness as freshness_service
from app.services import funds as funds_service
from app.services import holdings_ingestion as holdings_service
from app.services import instrument_prices as instrument_prices_service


def _listing_label(listing: InstrumentListing) -> str:
    market = listing.mic or listing.exchange
    return f"{listing.ticker} / {market}" if (listing.ticker and market) else (listing.ticker or "")


def _price_summary(listing: InstrumentListing, price: InstrumentPrice) -> ConstituentPriceSummary:
    return ConstituentPriceSummary(
        instrument_listing_id=listing.id,
        listing_label=_listing_label(listing) or None,
        price_date=price.price_date,
        close=price.close,
        currency=price.currency,
        source=price.source,
        status=price.status,
        freshness=freshness_service.freshness_state(price.price_date, kind="price"),
    )


def _constituent_read(
    holding: FundHolding,
    instrument: Instrument | None,
    price_summary: ConstituentPriceSummary | None = None,
) -> ConstituentRead:
    state = constituent_identity.identity_state(holding)
    return ConstituentRead(
        holding_id=holding.id,
        fund_id=holding.fund_id,
        security_name=holding.security_name,
        security_ticker=holding.security_ticker,
        security_isin=holding.security_isin,
        country=holding.country,
        currency=holding.currency,
        weight=holding.weight,
        source=holding.source,
        identity_state=state,
        identity_resolved_at=holding.identity_resolved_at,
        holding_instrument_id=holding.holding_instrument_id,
        instrument=InstrumentSummary.model_validate(instrument) if instrument else None,
        latest_price=price_summary,
        next_action=constituent_identity.next_action(state),
    )


async def build_fund_constituents(
    session: AsyncSession,
    fund_id: int,
    *,
    status: str | None = None,
    include_prices: bool = False,
) -> FundConstituentsResponse:
    """A fund's constituents with identity-resolution state + rollup.

    ``include_prices=true`` hydrates each resolved constituent with the latest EOD
    price of its instrument's primary listing (deduped DB read, no fetch)."""
    fund = await funds_service.get_fund(session, fund_id)
    # Rollup is computed over the *whole* snapshot; the list is filtered by status.
    snapshot = await constituent_identity.constituents_for_fund(session, fund_id)
    filtered = (
        snapshot
        if status is None
        else await constituent_identity.constituents_for_fund(session, fund_id, status=status)
    )
    instruments = await constituent_identity.hydrate_holdings_with_instruments(session, filtered)

    prices: dict[int, tuple[InstrumentListing, InstrumentPrice | None]] = {}
    if include_prices:
        instrument_ids = [h.holding_instrument_id for h in filtered if h.holding_instrument_id]
        prices = await instrument_prices_service.latest_constituent_prices(session, instrument_ids)

    def _summary_for(holding: FundHolding) -> ConstituentPriceSummary | None:
        entry = prices.get(holding.holding_instrument_id) if holding.holding_instrument_id else None
        if entry is None or entry[1] is None:
            return None
        return _price_summary(entry[0], entry[1])

    top = snapshot[0] if snapshot else None
    states = [constituent_identity.identity_state(h) for h in snapshot]
    return FundConstituentsResponse(
        fund_id=fund.id,
        fund_name=fund.name,
        as_of_date=top.as_of_date if top else None,
        source=top.source if top else None,
        total=len(snapshot),
        resolved=sum(1 for s in states if s == "resolved"),
        unresolved=sum(1 for s in states if s == "unresolved"),
        ambiguous=sum(1 for s in states if s == "ambiguous"),
        not_found=sum(1 for s in states if s == "not_found"),
        constituents=[
            _constituent_read(h, instruments.get(h.holding_instrument_id), _summary_for(h))
            for h in filtered
        ],
    )


async def build_fund_holdings_with_identity(
    session: AsyncSession,
    fund_id: int,
    *,
    as_of_date=None,
    source: str | None = None,
    limit: int = 100,
    include_identity: bool = False,
) -> FundHoldingsResponse:
    """Holdings snapshot, optionally hydrated with resolved-instrument identity."""
    fund = await funds_service.get_fund(session, fund_id)
    holdings = await holdings_service.latest_holdings_for_fund(
        session, fund_id, as_of_date=as_of_date, source=source, limit=limit
    )
    instruments = (
        await constituent_identity.hydrate_holdings_with_instruments(session, holdings)
        if include_identity
        else {}
    )
    reads: list[HoldingRead] = []
    for holding in holdings:
        read = HoldingRead.model_validate(holding)
        if include_identity and holding.holding_instrument_id in instruments:
            read.instrument = InstrumentSummary.model_validate(
                instruments[holding.holding_instrument_id]
            )
        reads.append(read)
    top = holdings[0] if holdings else None
    return FundHoldingsResponse(
        fund_id=fund.id,
        fund_name=fund.name,
        as_of_date=top.as_of_date if top else None,
        source=top.source if top else None,
        status=top.status if top else None,
        holdings=reads,
    )


async def build_instrument_detail(
    session: AsyncSession, instrument_id: int
) -> InstrumentDetailRead:
    instrument = await constituent_identity.get_instrument(session, instrument_id)
    if instrument is None:
        raise NotFoundError("Instrument not found", code="instrument_not_found")
    listings = await constituent_identity.listings_for_instrument(session, instrument_id)
    identifiers = await constituent_identity.identifiers_for_instrument(session, instrument_id)
    # Build from the scalar columns (InstrumentSummary) so model_validate never
    # touches the ORM ``listings``/``identifiers`` relationships (async lazy load).
    return InstrumentDetailRead(
        **InstrumentSummary.model_validate(instrument).model_dump(),
        created_at=instrument.created_at,
        updated_at=instrument.updated_at,
        listings=[InstrumentListingRead.model_validate(ln) for ln in listings],
        identifiers=[InstrumentIdentifierRead.model_validate(i) for i in identifiers],
    )


async def list_instrument_listings(
    session: AsyncSession, instrument_id: int
) -> list[InstrumentListingRead]:
    instrument = await constituent_identity.get_instrument(session, instrument_id)
    if instrument is None:
        raise NotFoundError("Instrument not found", code="instrument_not_found")
    listings = await constituent_identity.listings_for_instrument(session, instrument_id)
    return [InstrumentListingRead.model_validate(ln) for ln in listings]


async def list_listing_prices(
    session: AsyncSession,
    instrument_listing_id: int,
    *,
    source: str | None = None,
    limit: int | None = None,
) -> list[InstrumentPriceRead]:
    """A constituent listing's stored EOD bars (oldest first). 404s if unknown."""
    listing = await session.get(InstrumentListing, instrument_listing_id)
    if listing is None:
        raise NotFoundError("Instrument listing not found", code="instrument_listing_not_found")
    rows = await instrument_prices_service.list_prices_for_listing(
        session, instrument_listing_id, source=source, limit=limit
    )
    return [InstrumentPriceRead.model_validate(p) for p in rows]


async def list_instrument_prices(
    session: AsyncSession,
    instrument_id: int,
    *,
    source: str | None = None,
    limit: int | None = None,
) -> list[InstrumentPriceRead]:
    """EOD bars for an instrument's primary listing. 404s if the instrument is unknown."""
    instrument = await constituent_identity.get_instrument(session, instrument_id)
    if instrument is None:
        raise NotFoundError("Instrument not found", code="instrument_not_found")
    listing = await session.scalar(
        select(InstrumentListing)
        .where(InstrumentListing.instrument_id == instrument_id)
        .order_by(InstrumentListing.id)
    )
    if listing is None:
        return []
    rows = await instrument_prices_service.list_prices_for_listing(
        session, listing.id, source=source, limit=limit
    )
    return [InstrumentPriceRead.model_validate(p) for p in rows]
