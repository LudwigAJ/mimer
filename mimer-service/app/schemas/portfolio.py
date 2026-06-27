"""Portfolio position and summary schemas."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel

from app.schemas.common import DecimalStr, ORMModel


class PositionRead(ORMModel):
    id: int
    workspace_id: int
    fund_listing_id: int
    account_name: str | None
    units: DecimalStr
    average_cost: DecimalStr | None
    cost_currency: str | None
    created_at: datetime
    updated_at: datetime


class PositionCreate(BaseModel):
    fund_listing_id: int
    units: Decimal
    account_name: str | None = None
    average_cost: Decimal | None = None
    cost_currency: str | None = None


class PositionUpdate(BaseModel):
    account_name: str | None = None
    units: Decimal | None = None
    average_cost: Decimal | None = None
    cost_currency: str | None = None


class SummaryPosition(BaseModel):
    fund_listing_id: int
    ticker: str
    fund_name: str
    isin: str
    units: DecimalStr
    price: DecimalStr | None
    # ``currency`` is the raw price/quote currency (e.g. GBX for pence-quoted
    # London lines); ``listing_currency`` is its normalised form (GBX -> GBP).
    currency: str | None
    # ``market_value`` is kept for backwards compatibility and equals
    # ``market_value_base`` (workspace base currency).
    market_value: DecimalStr | None
    portfolio_weight: DecimalStr | None
    trailing_yield: DecimalStr | None
    projected_income: DecimalStr | None
    # Currency-aware valuation (FX overlay for the GUI).
    listing_currency: str | None = None
    base_currency: str | None = None
    market_value_local: DecimalStr | None = None
    market_value_base: DecimalStr | None = None
    fx_rate: DecimalStr | None = None
    fx_source: str | None = None
    # fresh | stale | missing — freshness of the FX rate used (None if no value).
    fx_status: str | None = None


class PortfolioSummary(BaseModel):
    base_currency: str
    total_market_value: DecimalStr
    daily_change: DecimalStr | None
    unrealised_gain_loss: DecimalStr | None
    trailing_12m_income: DecimalStr | None
    projected_annual_income: DecimalStr | None
    positions: list[SummaryPosition]
