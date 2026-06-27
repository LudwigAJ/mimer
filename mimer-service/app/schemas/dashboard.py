"""Workspace dashboard aggregate schema.

One bounded, GUI-friendly payload that hydrates the main workstation view in a
single call. It composes existing read schemas and adds latest-price/freshness
overlays so the client does not need to fan out to a dozen endpoints on load.
Deeper history is fetched separately (fund detail / time-series).
"""

from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel

from app.schemas.alert import AlertRead, AlertSummary
from app.schemas.common import DecimalStr, ORMModel
from app.schemas.diagnostics import Diagnostics
from app.schemas.distribution import DistributionRead
from app.schemas.document import DocumentRead
from app.schemas.exposure import ExposureDashboardBlock, ExposureResponse
from app.schemas.fund import FundRead
from app.schemas.fxrate import FxRateRead
from app.schemas.holding import HoldingRead
from app.schemas.job import JobRunRead, ScheduledJobRead
from app.schemas.onboarding import OnboardingStatus
from app.schemas.portfolio import SummaryPosition
from app.schemas.portfolio_valuation import PortfolioValuationDashboardBlock


class DashboardWorkspace(BaseModel):
    id: int
    name: str
    base_currency: str


class PortfolioSummaryBlock(BaseModel):
    base_currency: str
    total_market_value: DecimalStr
    daily_change: DecimalStr | None
    unrealised_gain_loss: DecimalStr | None
    trailing_12m_income: DecimalStr | None
    projected_annual_income: DecimalStr | None
    # "empty" | "seed" | "active" — whether the numbers rest on real data.
    status: str
    # Always "derived" — the summary is computed, not stored.
    source: str = "derived"


class ListingWithPrice(ORMModel):
    """A fund listing plus its latest price and a derived freshness state."""

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
    # Latest-price overlay (computed in the service).
    latest_price: DecimalStr | None = None
    latest_price_date: date | None = None
    latest_price_currency: str | None = None
    price_source: str | None = None
    freshness: str = "missing"


class FreshnessSummary(BaseModel):
    """Representative freshness per data domain (state of the newest record)."""

    prices: str
    distributions: str
    holdings: str
    documents: str
    fx: str
    fund_facts: str


class DashboardResponse(BaseModel):
    workspace: DashboardWorkspace
    portfolio_summary: PortfolioSummaryBlock
    positions: list[SummaryPosition]
    funds: list[FundRead]
    fund_listings: list[ListingWithPrice]
    distributions: list[DistributionRead]
    holdings: list[HoldingRead]
    # Legacy ad-hoc look-through slices (kept for backwards compatibility).
    exposures: ExposureResponse
    # Cached/derived exposure from the latest exposure_recompute snapshot.
    exposure: ExposureDashboardBlock
    documents: list[DocumentRead]
    # Latest portfolio valuation/readiness snapshot context (market value, coverage,
    # readiness, blockers, next action). Read-only over the latest snapshot — never
    # recomputed here, and NOT PnL/returns/performance. ``status=missing`` if none.
    portfolio_valuation: PortfolioValuationDashboardBlock
    # Recent open (active/read) alerts, most-severe-then-newest first.
    alerts: list[AlertRead]
    # Compact open-alert rollup (counts, highest severity, breakdowns).
    alert_summary: AlertSummary
    scheduled_jobs: list[ScheduledJobRead]
    job_runs: list[JobRunRead]
    fx_rates: list[FxRateRead]
    data_quality: Diagnostics
    freshness: FreshnessSummary
    # Data-readiness / onboarding status for the operations panel (readiness +
    # last run + next recommended action). Coverage, not investment quality.
    onboarding: OnboardingStatus
