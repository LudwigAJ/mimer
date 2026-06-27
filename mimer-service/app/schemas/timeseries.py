"""Chart-friendly time-series schema.

A single explicit shape for every series the GUI Charts page plots, regardless
of whether the underlying data is a stored column (prices, distributions) or a
derived/sparse computation (portfolio value). Decimal values serialise as
strings, consistent with the rest of the API.
"""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel

from app.schemas.common import DecimalStr

SeriesKind = Literal[
    "price",
    "nav",
    "market_value",
    "distribution",
    "yield",
    "portfolio_value",
    "fx",
]
TimeRange = Literal["1m", "3m", "6m", "1y", "all"]


class TimeSeriesSubject(BaseModel):
    # "fund" | "fund_listing" | "portfolio" | "fx_pair"
    type: str
    # int for DB-backed subjects; a string id (e.g. "GBP/USD") for FX pairs.
    id: int | str
    label: str


class TimeSeriesPoint(BaseModel):
    date: date
    value: DecimalStr
    source: str | None = None
    status: str | None = None


class TimeSeriesResponse(BaseModel):
    subject: TimeSeriesSubject
    kind: str
    currency: str | None
    source: str | None
    # "active" (stored data) | "derived" (computed) | "empty" (no data) |
    # "unavailable" (kind not supported for this subject yet, e.g. NAV).
    status: str
    points: list[TimeSeriesPoint]
