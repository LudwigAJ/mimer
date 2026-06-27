"""Fund and listing read schemas."""

from __future__ import annotations

from datetime import datetime

from app.schemas.common import DecimalStr, ORMModel


class FundRead(ORMModel):
    id: int
    isin: str
    name: str
    provider: str | None
    domicile: str | None
    base_currency: str | None
    distribution_policy: str | None
    strategy: str | None
    ocf: DecimalStr | None
    source: str | None
    status: str
    last_refreshed_at: datetime | None
    created_at: datetime
    updated_at: datetime


class FundListingRead(ORMModel):
    id: int
    fund_id: int
    ticker: str
    exchange: str | None
    trading_currency: str | None
    currency_unit: str | None
    figi: str | None
    sedol: str | None
    status: str
    last_price_at: datetime | None
    last_resolved_at: datetime | None
    created_at: datetime
    updated_at: datetime
