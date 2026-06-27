"""Fund holding read schema."""

from __future__ import annotations

from datetime import date, datetime

from app.schemas.common import DecimalStr, ORMModel
from app.schemas.constituent import InstrumentSummary


class HoldingRead(ORMModel):
    id: int
    fund_id: int
    as_of_date: date
    security_name: str
    security_ticker: str | None
    security_isin: str | None
    security_sedol: str | None = None
    security_cusip: str | None = None
    security_figi: str | None = None
    country: str | None
    sector: str | None
    industry: str | None = None
    currency: str | None = None
    weight: DecimalStr
    market_value: DecimalStr | None = None
    shares: DecimalStr | None = None
    status: str | None = None
    source: str
    # Constituent identity-resolution state (see app/services/constituent_identity).
    holding_instrument_id: int | None = None
    identity_status: str | None = None
    # Hydrated only when the caller asks for identity (include_identity=true).
    instrument: InstrumentSummary | None = None
    created_at: datetime


class FundHoldingsResponse(ORMModel):
    """The ``GET /api/v1/funds/{id}/holdings`` payload: a single coherent
    holdings snapshot for a fund (one source + as-of date), bounded."""

    fund_id: int
    fund_name: str
    as_of_date: date | None
    source: str | None
    status: str | None
    holdings: list[HoldingRead]
