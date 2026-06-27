"""FX rate read schema."""

from __future__ import annotations

from datetime import date, datetime

from app.schemas.common import DecimalStr, ORMModel


class FxRateRead(ORMModel):
    id: int
    rate_date: date
    base_currency: str
    quote_currency: str
    rate: DecimalStr
    source: str
    # Provider/derivation provenance (fixture | official | estimated | manual).
    status: str | None = None
    created_at: datetime
