"""Schemas for instrument resolution (POST /api/v1/instruments)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

SymbolType = Literal["ticker", "isin", "figi", "sedol", "cusip"]
Confidence = Literal["high", "medium", "low"]


class InstrumentRequest(BaseModel):
    symbol: str
    symbol_type: SymbolType
    # Optional hints — narrow ticker resolution to a single listing when given.
    exchange: str | None = None
    currency: str | None = None


class ResolvedInstrument(BaseModel):
    isin: str | None = None
    figi: str | None = None
    ticker: str | None = None
    exchange: str | None = None
    trading_currency: str | None = None
    name: str | None = None


class InstrumentCandidate(ResolvedInstrument):
    confidence: Confidence = "medium"
    source: str


class InstrumentCreated(BaseModel):
    fund: bool
    listing: bool


class InstrumentResolveResponse(BaseModel):
    """Returned on a confident single match (HTTP 202)."""

    status: Literal["pending", "active"] = "pending"
    fund_id: int
    fund_listing_id: int | None
    resolved: ResolvedInstrument
    created: InstrumentCreated
    job_run_ids: list[int]


class InstrumentAmbiguousResponse(BaseModel):
    """Returned when resolution is not confident (HTTP 409)."""

    status: Literal["ambiguous"] = "ambiguous"
    message: str
    candidates: list[InstrumentCandidate]
