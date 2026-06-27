"""Constituent identity + instrument read schemas.

The GUI-facing view of the canonical instrument master (``instruments`` /
``instrument_listings`` / ``instrument_identifiers``) and of a fund's
constituents' identity-resolution state. No secrets here.
"""

from __future__ import annotations

from datetime import date, datetime

from app.schemas.common import DecimalStr, ORMModel


class InstrumentIdentifierRead(ORMModel):
    id: int
    scheme: str
    value: str
    source: str
    status: str


class InstrumentListingRead(ORMModel):
    id: int
    instrument_id: int
    ticker: str | None
    exchange: str | None
    mic: str | None
    currency: str | None
    country: str | None
    figi: str | None
    composite_figi: str | None
    share_class_figi: str | None
    source: str
    status: str


class InstrumentPriceRead(ORMModel):
    """One stored EOD bar for a constituent ``instrument_listing``."""

    id: int
    instrument_listing_id: int
    price_date: date
    open: DecimalStr | None = None
    high: DecimalStr | None = None
    low: DecimalStr | None = None
    close: DecimalStr
    adjusted_close: DecimalStr | None = None
    volume: DecimalStr | None = None
    currency: str | None = None
    source: str
    status: str | None = None


class ConstituentPriceSummary(ORMModel):
    """Latest EOD price for a constituent, embedded into a constituent row."""

    instrument_listing_id: int
    # "AAPL / XNAS"-style label of the listing the price is for.
    listing_label: str | None = None
    price_date: date
    close: DecimalStr
    currency: str | None = None
    source: str
    status: str | None = None
    # Derived read-side freshness: fresh | stale | missing.
    freshness: str


class InstrumentSummary(ORMModel):
    """Compact instrument view embedded into a holding / constituent row."""

    id: int
    instrument_type: str
    name: str
    legal_name: str | None = None
    country: str | None = None
    currency: str | None = None
    status: str
    source: str


class InstrumentRead(InstrumentSummary):
    created_at: datetime
    updated_at: datetime


class InstrumentDetailRead(InstrumentRead):
    listings: list[InstrumentListingRead] = []
    identifiers: list[InstrumentIdentifierRead] = []


class ConstituentRead(ORMModel):
    """One constituent holding with its identity-resolution state for the GUI."""

    holding_id: int
    fund_id: int
    security_name: str
    security_ticker: str | None = None
    security_isin: str | None = None
    country: str | None = None
    currency: str | None = None
    weight: DecimalStr
    source: str
    # Derived state: resolved | ambiguous | not_found | failed | unresolved.
    identity_state: str
    identity_resolved_at: datetime | None = None
    holding_instrument_id: int | None = None
    instrument: InstrumentSummary | None = None
    # Latest EOD price for the resolved instrument's primary listing. Hydrated
    # only when the caller asks (include_prices=true) and the constituent is
    # resolved and priced.
    latest_price: ConstituentPriceSummary | None = None
    # What the client/operator should do next for this constituent.
    next_action: str


class FundConstituentsResponse(ORMModel):
    fund_id: int
    fund_name: str
    as_of_date: date | None = None
    source: str | None = None
    # Identity-resolution rollup for the returned constituents.
    total: int = 0
    resolved: int = 0
    unresolved: int = 0
    ambiguous: int = 0
    not_found: int = 0
    constituents: list[ConstituentRead]
