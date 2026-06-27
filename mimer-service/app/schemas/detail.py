"""Fund detail schema — hydrates the GUI's Fund Detail page (Overview, Prices,
Distributions, Holdings, Documents, Jobs, Diffs tabs) in one bounded call."""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel

from app.schemas.common import DecimalStr
from app.schemas.dashboard import ListingWithPrice
from app.schemas.distribution import DistributionRead
from app.schemas.document import DocumentRead
from app.schemas.fund import FundRead
from app.schemas.holding import HoldingRead
from app.schemas.identifier import SecurityIdentifierRead
from app.schemas.job import JobRunRead


class PricePointRead(BaseModel):
    date: date
    value: DecimalStr
    currency: str
    source: str


class PriceHistorySummary(BaseModel):
    points: int
    start_date: date | None = None
    end_date: date | None = None
    first: DecimalStr | None = None
    last: DecimalStr | None = None
    change_pct: DecimalStr | None = None


class ListingDetail(ListingWithPrice):
    price_summary: PriceHistorySummary
    # Bounded recent points; empty unless ``include_prices`` is set.
    prices: list[PricePointRead] = []


class FundFreshness(BaseModel):
    prices: str
    distributions: str
    holdings: str
    documents: str
    fund_facts: str


class FundDetailResponse(BaseModel):
    fund: FundRead
    listings: list[ListingDetail]
    distributions: list[DistributionRead]
    holdings: list[HoldingRead]
    documents: list[DocumentRead]
    job_runs: list[JobRunRead]
    identifiers: list[SecurityIdentifierRead]
    freshness: FundFreshness
