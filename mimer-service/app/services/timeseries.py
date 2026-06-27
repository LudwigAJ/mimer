"""Time-series derivation for the Charts page.

v1 builds what the existing tables allow:

* listing/fund **price** series from ``prices`` (stored).
* fund/listing **distribution** series from ``distributions`` (stored).
* workspace **portfolio_value** and **distribution** series, *derived* from
  positions + prices/distributions + FX (sparse when little price history
  exists — clearly marked ``status="derived"``).

Kinds with no backing data yet (``nav``, ``yield``, ``fx``, ``market_value``)
return an empty series with ``status="unavailable"`` rather than fabricated
numbers.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFoundError
from app.db.models import (
    Distribution,
    Fund,
    FundListing,
    FxRate,
    InstrumentListing,
    InstrumentPrice,
    PortfolioPosition,
    Price,
)
from app.schemas.timeseries import TimeSeriesPoint, TimeSeriesResponse, TimeSeriesSubject
from app.services import freshness as freshness_service
from app.services import funds as funds_service
from app.services import workspaces as workspaces_service
from app.services.conversion import convert, load_fx_map
from app.services.fx import fx_source_priority

_RANGE_DAYS: dict[str, int | None] = {
    "1m": 30,
    "3m": 90,
    "6m": 180,
    "1y": 365,
    "all": None,
}
_MONEY_Q = Decimal("0.01")
_ZERO = Decimal("0")


def _cutoff(range_: str) -> date | None:
    days = _RANGE_DAYS.get(range_)
    return None if days is None else date.today() - timedelta(days=days)


def _q(value: Decimal | None) -> Decimal:
    if value is None:
        return _ZERO
    return value.quantize(_MONEY_Q, rounding=ROUND_HALF_UP)


def _empty(
    subject: TimeSeriesSubject, kind: str, *, status: str, currency: str | None = None
) -> TimeSeriesResponse:
    return TimeSeriesResponse(
        subject=subject, kind=kind, currency=currency, source=None, status=status, points=[]
    )


def _price_status(price_date: date) -> str:
    return freshness_service.freshness_state(price_date, kind="price")


async def _listing_price_series(
    session: AsyncSession,
    listing: FundListing,
    *,
    range_: str,
    source: str | None,
    subject: TimeSeriesSubject,
) -> TimeSeriesResponse:
    cutoff = _cutoff(range_)
    stmt = select(Price).where(Price.fund_listing_id == listing.id)
    if cutoff is not None:
        stmt = stmt.where(Price.price_date >= cutoff)
    if source is not None:
        stmt = stmt.where(Price.source == source)
    stmt = stmt.order_by(Price.price_date.asc(), Price.id.asc())
    rows = list((await session.execute(stmt)).scalars().all())

    # Deduplicate to one point per date (last source wins) for a clean line.
    by_date: dict[date, Price] = {}
    for row in rows:
        by_date[row.price_date] = row
    ordered = [by_date[d] for d in sorted(by_date)]

    points = [
        TimeSeriesPoint(
            date=p.price_date, value=p.price, source=p.source, status=_price_status(p.price_date)
        )
        for p in ordered
    ]
    sources = {p.source for p in ordered}
    currency = (
        ordered[-1].currency if ordered else (listing.currency_unit or listing.trading_currency)
    )
    overall_source = source or (sources.pop() if len(sources) == 1 else None)
    return TimeSeriesResponse(
        subject=subject,
        kind="price",
        currency=currency,
        source=overall_source,
        status="active" if points else "empty",
        points=points,
    )


async def _fund_distribution_series(
    session: AsyncSession,
    fund_id: int,
    *,
    range_: str,
    subject: TimeSeriesSubject,
) -> TimeSeriesResponse:
    cutoff = _cutoff(range_)
    stmt = select(Distribution).where(Distribution.fund_id == fund_id)
    if cutoff is not None:
        stmt = stmt.where(Distribution.ex_date >= cutoff)
    stmt = stmt.order_by(Distribution.ex_date.asc(), Distribution.id.asc())
    rows = list((await session.execute(stmt)).scalars().all())
    points = [
        TimeSeriesPoint(
            date=d.ex_date, value=d.amount, source=d.source, status=d.status or "active"
        )
        for d in rows
    ]
    sources = {d.source for d in rows}
    currency = rows[-1].currency if rows else None
    return TimeSeriesResponse(
        subject=subject,
        kind="distribution",
        currency=currency,
        source=sources.pop() if len(sources) == 1 else None,
        status="active" if points else "empty",
        points=points,
    )


async def listing_time_series(
    session: AsyncSession,
    fund_listing_id: int,
    *,
    kind: str,
    range_: str,
    source: str | None,
) -> TimeSeriesResponse:
    listing = await session.get(FundListing, fund_listing_id)
    if listing is None:
        raise NotFoundError("Fund listing not found", code="fund_listing_not_found")
    subject = TimeSeriesSubject(type="fund_listing", id=listing.id, label=listing.ticker)

    if kind == "price":
        return await _listing_price_series(
            session, listing, range_=range_, source=source, subject=subject
        )
    if kind == "distribution":
        # Distributions belong to the fund; surface them for the listing too.
        return await _fund_distribution_series(
            session, listing.fund_id, range_=range_, subject=subject
        )
    return _empty(subject, kind, status="unavailable")


def _instrument_listing_label(listing: InstrumentListing) -> str:
    market = listing.mic or listing.exchange
    return f"{listing.ticker} / {market}" if (listing.ticker and market) else (listing.ticker or "")


async def _instrument_listing_price_series(
    session: AsyncSession,
    listing: InstrumentListing,
    *,
    range_: str,
    source: str | None,
    subject: TimeSeriesSubject,
) -> TimeSeriesResponse:
    cutoff = _cutoff(range_)
    stmt = select(InstrumentPrice).where(InstrumentPrice.instrument_listing_id == listing.id)
    if cutoff is not None:
        stmt = stmt.where(InstrumentPrice.price_date >= cutoff)
    if source is not None:
        stmt = stmt.where(InstrumentPrice.source == source)
    stmt = stmt.order_by(InstrumentPrice.price_date.asc(), InstrumentPrice.id.asc())
    rows = list((await session.execute(stmt)).scalars().all())

    # One point per date (last source wins) for a clean line.
    by_date: dict[date, InstrumentPrice] = {}
    for row in rows:
        by_date[row.price_date] = row
    ordered = [by_date[d] for d in sorted(by_date)]

    points = [
        TimeSeriesPoint(
            date=p.price_date, value=p.close, source=p.source, status=_price_status(p.price_date)
        )
        for p in ordered
    ]
    sources = {p.source for p in ordered}
    currency = ordered[-1].currency if ordered else listing.currency
    overall_source = source or (sources.pop() if len(sources) == 1 else None)
    return TimeSeriesResponse(
        subject=subject,
        kind="price",
        currency=currency,
        source=overall_source,
        status="active" if points else "empty",
        points=points,
    )


async def instrument_listing_time_series(
    session: AsyncSession,
    instrument_listing_id: int,
    *,
    kind: str,
    range_: str,
    source: str | None,
) -> TimeSeriesResponse:
    """EOD price series for a constituent ``instrument_listing``."""
    listing = await session.get(InstrumentListing, instrument_listing_id)
    if listing is None:
        raise NotFoundError("Instrument listing not found", code="instrument_listing_not_found")
    subject = TimeSeriesSubject(
        type="instrument_listing",
        id=listing.id,
        label=_instrument_listing_label(listing) or str(listing.id),
    )
    if kind == "price":
        return await _instrument_listing_price_series(
            session, listing, range_=range_, source=source, subject=subject
        )
    return _empty(subject, kind, status="unavailable")


async def instrument_time_series(
    session: AsyncSession,
    instrument_id: int,
    *,
    kind: str,
    range_: str,
    source: str | None,
) -> TimeSeriesResponse:
    """EOD price series for an instrument (its primary listing)."""
    from app.db.models import Instrument

    instrument = await session.get(Instrument, instrument_id)
    if instrument is None:
        raise NotFoundError("Instrument not found", code="instrument_not_found")
    subject = TimeSeriesSubject(type="instrument", id=instrument.id, label=instrument.name)
    if kind != "price":
        return _empty(subject, kind, status="unavailable")
    listing = await session.scalar(
        select(InstrumentListing)
        .where(InstrumentListing.instrument_id == instrument_id)
        .order_by(InstrumentListing.id)
    )
    if listing is None:
        return _empty(subject, kind, status="empty")
    subject.label = f"{instrument.name} ({_instrument_listing_label(listing)})"
    return await _instrument_listing_price_series(
        session, listing, range_=range_, source=source, subject=subject
    )


async def fund_time_series(
    session: AsyncSession,
    fund_id: int,
    *,
    kind: str,
    range_: str,
    source: str | None,
) -> TimeSeriesResponse:
    fund = await funds_service.get_fund(session, fund_id)
    subject = TimeSeriesSubject(type="fund", id=fund.id, label=fund.name)

    if kind == "distribution":
        return await _fund_distribution_series(session, fund.id, range_=range_, subject=subject)
    if kind == "price":
        # A fund has many listings/currencies; use the primary (first) listing,
        # clearly labelled. Listing-level series are more precise.
        listing = await session.scalar(
            select(FundListing).where(FundListing.fund_id == fund.id).order_by(FundListing.id)
        )
        if listing is None:
            return _empty(subject, kind, status="empty")
        subject.label = f"{fund.name} ({listing.ticker})"
        result = await _listing_price_series(
            session, listing, range_=range_, source=source, subject=subject
        )
        return result
    # nav has no backing table yet — do not fabricate it.
    return _empty(subject, kind, status="unavailable")


async def _held_positions(
    session: AsyncSession, workspace_id: int
) -> list[tuple[PortfolioPosition, FundListing, Fund]]:
    rows = (
        await session.execute(
            select(PortfolioPosition, FundListing, Fund)
            .join(FundListing, PortfolioPosition.fund_listing_id == FundListing.id)
            .join(Fund, FundListing.fund_id == Fund.id)
            .where(PortfolioPosition.workspace_id == workspace_id)
        )
    ).all()
    return [(p, ln, f) for p, ln, f in rows]


async def portfolio_time_series(
    session: AsyncSession,
    workspace_id: int,
    *,
    kind: str,
    range_: str,
    source: str | None,
) -> TimeSeriesResponse:
    workspace = await workspaces_service.get_workspace(session, workspace_id)
    base = workspace.base_currency
    subject = TimeSeriesSubject(type="portfolio", id=workspace.id, label=workspace.name)
    cutoff = _cutoff(range_)
    held = await _held_positions(session, workspace_id)
    fx_map = await load_fx_map(session)

    if kind in ("portfolio_value", "market_value"):
        if not held:
            return _empty(subject, "portfolio_value", status="empty", currency=base)
        listing_ids = [ln.id for _, ln, _ in held]
        stmt = select(Price).where(Price.fund_listing_id.in_(listing_ids))
        if cutoff is not None:
            stmt = stmt.where(Price.price_date >= cutoff)
        if source is not None:
            stmt = stmt.where(Price.source == source)
        stmt = stmt.order_by(Price.fund_listing_id, Price.price_date.asc(), Price.id.asc())
        price_rows = list((await session.execute(stmt)).scalars().all())

        # Per-listing date->price (last source wins on a date).
        series_by_listing: dict[int, dict[date, Price]] = {}
        for row in price_rows:
            series_by_listing.setdefault(row.fund_listing_id, {})[row.price_date] = row
        all_dates = sorted({row.price_date for row in price_rows})

        points: list[TimeSeriesPoint] = []
        for d in all_dates:
            total = _ZERO
            have_any = False
            for position, listing, _fund in held:
                day_map = series_by_listing.get(listing.id, {})
                # Most recent price on or before d.
                candidates = [pd for pd in day_map if pd <= d]
                if not candidates:
                    continue
                price = day_map[max(candidates)]
                converted = convert(position.units * price.price, price.currency, base, fx_map)
                if converted is not None:
                    total += converted
                    have_any = True
            if have_any:
                points.append(
                    TimeSeriesPoint(date=d, value=_q(total), source="derived", status="derived")
                )
        return TimeSeriesResponse(
            subject=subject,
            kind="portfolio_value",
            currency=base,
            source="derived",
            status="derived" if points else "empty",
            points=points,
        )

    if kind == "fx":
        # The portfolio is multi-currency; an FX pair needs an explicit base/quote.
        # Use the dedicated /api/v1/fx/time-series endpoint instead.
        return _empty(subject, "fx", status="unavailable", currency=base)

    if kind == "distribution":
        if not held:
            return _empty(subject, "distribution", status="empty", currency=base)
        # Total shares held per fund (positions are per listing; same fund = same shares).
        units_by_fund: dict[int, Decimal] = {}
        for position, _ln, fund in held:
            units_by_fund[fund.id] = units_by_fund.get(fund.id, _ZERO) + position.units
        stmt = select(Distribution).where(Distribution.fund_id.in_(list(units_by_fund)))
        if cutoff is not None:
            stmt = stmt.where(Distribution.ex_date >= cutoff)
        stmt = stmt.order_by(Distribution.ex_date.asc())
        dist_rows = list((await session.execute(stmt)).scalars().all())
        income_by_date: dict[date, Decimal] = {}
        for dist in dist_rows:
            per_share = convert(dist.amount, dist.currency, base, fx_map)
            if per_share is None:
                continue
            income = per_share * units_by_fund.get(dist.fund_id, _ZERO)
            income_by_date[dist.ex_date] = income_by_date.get(dist.ex_date, _ZERO) + income
        points = [
            TimeSeriesPoint(date=d, value=_q(income_by_date[d]), source="derived", status="derived")
            for d in sorted(income_by_date)
        ]
        return TimeSeriesResponse(
            subject=subject,
            kind="distribution",
            currency=base,
            source="derived",
            status="derived" if points else "empty",
            points=points,
        )

    return _empty(subject, kind, status="unavailable", currency=base)


async def _fx_rows(
    session: AsyncSession,
    base: str,
    quote: str,
    *,
    cutoff: date | None,
    source: str | None,
) -> list[FxRate]:
    stmt = select(FxRate).where(FxRate.base_currency == base, FxRate.quote_currency == quote)
    if cutoff is not None:
        stmt = stmt.where(FxRate.rate_date >= cutoff)
    if source is not None:
        stmt = stmt.where(FxRate.source == source)
    return list((await session.execute(stmt)).scalars().all())


def _dedupe_by_date(rows: list[FxRate]) -> list[FxRate]:
    """One row per date, preferring the higher-priority source."""
    best: dict[date, FxRate] = {}
    for row in rows:
        current = best.get(row.rate_date)
        if current is None or fx_source_priority(row.source) < fx_source_priority(current.source):
            best[row.rate_date] = row
    return [best[d] for d in sorted(best)]


async def fx_time_series(
    session: AsyncSession,
    base_currency: str,
    quote_currency: str,
    *,
    range_: str,
    source: str | None,
) -> TimeSeriesResponse:
    """FX-pair rate series from stored ``fx_rates`` (direct, else inverted)."""
    base, quote = base_currency.upper(), quote_currency.upper()
    pair = f"{base}/{quote}"
    subject = TimeSeriesSubject(type="fx_pair", id=pair, label=pair)

    if base == quote:
        return TimeSeriesResponse(
            subject=subject, kind="fx", currency=None, source=None, status="unavailable", points=[]
        )

    cutoff = _cutoff(range_)
    rows = _dedupe_by_date(await _fx_rows(session, base, quote, cutoff=cutoff, source=source))
    inverted = False
    if not rows:
        inverse = _dedupe_by_date(
            await _fx_rows(session, quote, base, cutoff=cutoff, source=source)
        )
        rows = inverse
        inverted = True

    points: list[TimeSeriesPoint] = []
    for row in rows:
        value = (Decimal(1) / row.rate) if (inverted and row.rate != 0) else row.rate
        points.append(
            TimeSeriesPoint(
                date=row.rate_date,
                value=value,
                source=row.source,
                status=freshness_service.freshness_state(row.rate_date, kind="fx"),
            )
        )
    sources = {row.source for row in rows}
    overall_source = source or (sources.pop() if len(sources) == 1 else None)
    return TimeSeriesResponse(
        subject=subject,
        kind="fx",
        currency=None,
        source=overall_source,
        status="active" if points else "empty",
        points=points,
    )
