"""Portfolio valuation/readiness snapshot schemas.

A bounded, cacheable read model: it joins the reconciled positions (net quantity
per instrument; cash per currency) to the *latest already-ingested* fund/instrument
price + FX at/before ``as_of_date`` and reports which rows can be valued and what
is blocking the rest. NOT PnL — there is no cost-basis, realised/unrealised gain,
tax-lot, total-return or performance-attribution field here (see AGENTS.md compute
boundary). Decimals serialise to JSON as strings.
"""

from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel

from app.schemas.common import DecimalStr

# valuation_status vocabulary (per row).
VALUATION_STATUSES = (
    "valued",
    "missing_price",
    "missing_fx",
    "unresolved_instrument",
    "ambiguous_instrument",
    "cash_only",
    "zero_quantity",
    "stale_price",
    "stale_fx",
)

# Snapshot-level readiness rollup (history/summary/dashboard). Distinct from the
# per-row ``readiness_status`` vocabulary (ready/blocked/stale/cash) above: this is
# a roll-up over a whole snapshot's blocker/stale counts.
#   ready   — every selected (non-cash) position valued, no stale blockers
#   partial — some valued, but missing-price/FX or unresolved/ambiguous remain
#   blocked — nothing valued and hard blockers dominate
#   stale   — valued with no hard blockers, only stale price/FX
#   empty   — no positions and no cash (or no snapshot at all)
READINESS_STATUSES = ("ready", "partial", "blocked", "stale", "empty")


class PortfolioValuationRowRead(BaseModel):
    """One valued / blocked position (or cash balance) for the GUI."""

    position_key: str
    # fund_listing | instrument_listing | instrument | fund | cash | unresolved |
    # ambiguous
    position_type: str
    fund_id: int | None = None
    fund_listing_id: int | None = None
    instrument_id: int | None = None
    instrument_listing_id: int | None = None
    symbol: str | None = None
    isin: str | None = None
    name: str | None = None
    quantity: DecimalStr
    local_currency: str | None = None
    base_currency: str | None = None
    latest_price: DecimalStr | None = None
    latest_price_date: date | None = None
    latest_price_source: str | None = None
    # fresh | stale | missing
    latest_price_status: str | None = None
    fx_rate_to_base: DecimalStr | None = None
    fx_rate_date: date | None = None
    fx_rate_source: str | None = None
    # fresh | stale | missing | same_currency
    fx_status: str | None = None
    market_value_local: DecimalStr | None = None
    market_value_base: DecimalStr | None = None
    # See VALUATION_STATUSES.
    valuation_status: str
    # ready | blocked | stale | cash
    readiness_status: str
    # Human-readable codes for *why* a row is not (fully) valued (empty when valued).
    blocking_reasons: list[str] = []
    source: str | None = None


class PortfolioValuationSummary(BaseModel):
    """Roll-up counts the GUI can badge without scanning the rows."""

    positions_selected: int = 0
    positions_valued: int = 0
    missing_price: int = 0
    missing_fx: int = 0
    unresolved: int = 0
    ambiguous: int = 0
    cash_rows: int = 0
    stale_price: int = 0
    stale_fx: int = 0
    total_market_value_base: DecimalStr | None = None


class PortfolioValuationResponse(BaseModel):
    """A valuation snapshot (cached) or an on-the-fly computation (``cached=false``)."""

    workspace_id: int
    snapshot_id: int | None = None
    as_of_date: date | None = None
    base_currency: str
    broker_account_id: int | None = None
    source: str = "portfolio_valuation"
    # ok | partial | empty | recompute_needed
    status: str = "empty"
    cached: bool = False
    created_at: datetime | None = None
    summary: PortfolioValuationSummary = PortfolioValuationSummary()
    rows: list[PortfolioValuationRowRead] = []


class PortfolioValuationCoverage(BaseModel):
    """Coverage-only view (no rows) for a compact dashboard/diagnostics widget."""

    workspace_id: int
    snapshot_id: int | None = None
    as_of_date: date | None = None
    base_currency: str
    status: str = "empty"
    cached: bool = False
    created_at: datetime | None = None
    summary: PortfolioValuationSummary = PortfolioValuationSummary()


class PortfolioValuationHistoryPoint(BaseModel):
    """One snapshot rendered as a point in the bounded readiness/coverage series.

    Coverage/readiness only — NO returns, PnL or performance between points (those
    live in the Rust GUI / local pricer; see the AGENTS.md compute boundary).
    ``total_market_value_base`` is a coverage figure (sum of *valued* rows), never a
    total-return number, and consecutive points are never differenced here.
    """

    snapshot_id: int
    as_of_date: date
    created_at: datetime
    base_currency: str
    broker_account_id: int | None = None
    positions_selected: int = 0
    positions_valued: int = 0
    missing_price_count: int = 0
    missing_fx_count: int = 0
    unresolved_count: int = 0
    ambiguous_count: int = 0
    stale_price_count: int = 0
    stale_fx_count: int = 0
    cash_row_count: int = 0
    total_market_value_base: DecimalStr | None = None
    # positions_valued / positions_selected (None when nothing selected).
    valuation_coverage_ratio: DecimalStr | None = None
    # See READINESS_STATUSES.
    readiness_status: str


class PortfolioValuationHistory(BaseModel):
    """A bounded, oldest-first series of valuation snapshots for charting."""

    workspace_id: int
    base_currency: str
    broker_account_id: int | None = None
    count: int = 0
    # Oldest-first (chart-friendly); bounded by ``limit`` (most-recent window).
    points: list[PortfolioValuationHistoryPoint] = []


class PortfolioValuationBrokerAccountSummary(BaseModel):
    """Latest-snapshot coverage roll-up for one broker account (when present)."""

    broker_account_id: int | None = None
    latest_snapshot_id: int
    as_of_date: date
    total_market_value_base: DecimalStr | None = None
    positions_selected: int = 0
    positions_valued: int = 0
    valuation_coverage_ratio: DecimalStr | None = None
    # See READINESS_STATUSES.
    readiness_status: str


class PortfolioValuationSummaryResponse(BaseModel):
    """Compact latest-context + readiness roll-up over the workspace's snapshots.

    Cheap SQL over the snapshots table (latest snapshot + a bounded count + the
    per-broker-account latest). NO rows, NO PnL/returns/performance fields.
    """

    workspace_id: int
    # present (a snapshot exists) | empty (none yet).
    status: str = "empty"
    latest_snapshot_id: int | None = None
    latest_as_of_date: date | None = None
    latest_created_at: datetime | None = None
    base_currency: str
    broker_account_id: int | None = None
    total_market_value_base: DecimalStr | None = None
    positions_selected: int = 0
    positions_valued: int = 0
    valuation_coverage_ratio: DecimalStr | None = None
    missing_price_count: int = 0
    missing_fx_count: int = 0
    unresolved_count: int = 0
    ambiguous_count: int = 0
    stale_price_count: int = 0
    stale_fx_count: int = 0
    cash_row_count: int = 0
    # See READINESS_STATUSES.
    readiness_status: str = "empty"
    # Human-readable blocker codes present in the latest snapshot (empty when ready).
    blocking_reasons: list[str] = []
    # How many snapshots are in scope (the history series length available).
    history_points: int = 0
    # Per-broker-account latest coverage (only populated for account-scoped snapshots).
    broker_accounts: list[PortfolioValuationBrokerAccountSummary] = []


class PortfolioValuationDashboardBlock(BaseModel):
    """Compact valuation/readiness block for the workspace dashboard.

    Reads the latest snapshot only — it never recomputes valuation, never fetches,
    never differences snapshots. ``status=missing`` means no snapshot exists yet.
    """

    # present (latest snapshot read) | missing (no snapshot yet).
    status: str = "missing"
    latest_snapshot_id: int | None = None
    latest_as_of_date: date | None = None
    base_currency: str | None = None
    total_market_value_base: DecimalStr | None = None
    positions_selected: int = 0
    positions_valued: int = 0
    valuation_coverage_ratio: DecimalStr | None = None
    # See READINESS_STATUSES.
    readiness_status: str = "empty"
    missing_price_count: int = 0
    missing_fx_count: int = 0
    unresolved_count: int = 0
    ambiguous_count: int = 0
    stale_price_count: int = 0
    stale_fx_count: int = 0
    # True when there is a ledger but no snapshot, or the latest snapshot is stale.
    needs_recompute: bool = False
    # e.g. "run portfolio_valuation_recompute" / "resolve imported instruments" /
    # "fetch missing prices" / "fetch missing FX" (None when ready).
    recommended_action: str | None = None


class PortfolioValuationRecomputeResponse(BaseModel):
    """Outcome of a recompute (idempotent: created XOR updated XOR skipped)."""

    workspace_id: int
    snapshot_id: int | None = None
    as_of_date: date | None = None
    base_currency: str
    broker_account_id: int | None = None
    status: str = "empty"
    positions_selected: int = 0
    positions_valued: int = 0
    missing_price: int = 0
    missing_fx: int = 0
    unresolved: int = 0
    ambiguous: int = 0
    cash_rows: int = 0
    stale_price: int = 0
    stale_fx: int = 0
    snapshot_created: bool = False
    snapshot_updated: bool = False
    snapshot_skipped: bool = False
    message: str = ""
