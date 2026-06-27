"""Exposure (look-through) schemas.

``ExposureResponse`` is the legacy ad-hoc slice shape (kept for the dashboard's
``exposures`` block and the holdings exposure tests). ``ExposureSnapshotResponse``
+ ``ExposureRowRead`` are the new derived/cached shape served from
``exposure_snapshots`` / ``exposure_rows``.
"""

from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel

from app.schemas.common import DecimalStr


class ConstituentCoverage(BaseModel):
    """Weight-based true look-through coverage, nested under holdings coverage.

    All weights are fractions of *total portfolio value* and nest:
    ``holdings_coverage_weight`` >= ``identity_coverage_weight`` >=
    ``price_coverage_weight`` >= ``fx_coverage_weight``. The counts are by
    *distinct resolved instrument* (deduped across funds). See
    ``app/services/constituent_valuation.py``.
    """

    holdings_coverage_weight: DecimalStr | None = None
    identity_coverage_weight: DecimalStr | None = None
    price_coverage_weight: DecimalStr | None = None
    fx_coverage_weight: DecimalStr | None = None
    constituent_count: int = 0
    resolved_constituent_count: int = 0
    priced_constituent_count: int = 0
    stale_constituent_price_count: int = 0
    missing_constituent_price_count: int = 0
    constituent_fx_missing_count: int = 0


class ExposureSlice(BaseModel):
    key: str
    weight: DecimalStr


class ExposureResponse(BaseModel):
    country: list[ExposureSlice]
    sector: list[ExposureSlice]
    currency: list[ExposureSlice]
    # How to read the slices. Position weights use base-currency market values
    # (FX applied where available); currency exposure is a *listing-level*
    # approximation (each position's quote currency), not look-through to the
    # underlying holdings' currencies.
    currency_basis: str = "listing_currency_approximation"
    base_currency: str | None = None


class ExposureRowRead(BaseModel):
    dimension: str
    bucket: str
    label: str
    weight: DecimalStr
    # Weight-based *implied* market value (position value x holding weight); for
    # constituent rows it is NOT a share/price-derived notional. See
    # ``valuation_method``.
    market_value_base: DecimalStr | None = None
    currency: str | None = None
    source: str | None = None
    status: str | None = None
    # Constituent look-through context (None on fund-level rows).
    instrument_id: int | None = None
    instrument_listing_id: int | None = None
    fund_id: int | None = None
    price_date: date | None = None
    price_source: str | None = None
    price_status: str | None = None
    fx_rate: DecimalStr | None = None
    fx_source: str | None = None
    valuation_method: str | None = None


class ExposureSnapshotResponse(BaseModel):
    """Derived exposure for a workspace (from the latest cached snapshot)."""

    workspace_id: int
    # None when computed on the fly (no cached snapshot yet).
    snapshot_id: int | None = None
    as_of_date: date | None = None
    base_currency: str
    source: str
    # ok | partial | empty | recompute_needed
    status: str
    total_market_value_base: DecimalStr | None = None
    coverage_weight: DecimalStr | None = None
    unclassified_weight: DecimalStr | None = None
    missing_holdings_count: int = 0
    missing_fx_count: int = 0
    # True constituent look-through coverage (None/0 on pre-0014 snapshots).
    constituent_coverage: ConstituentCoverage | None = None
    created_at: datetime | None = None
    # True when served from a stored snapshot; False when computed on the fly.
    cached: bool = True
    dimensions: list[str] = []
    rows: list[ExposureRowRead] = []


class ExposureDriftRow(BaseModel):
    """One bucket's change between a base and a comparison exposure snapshot.

    Deltas are ``comparison - base``. ``market_value_base`` is the weight-based
    *implied* value (see ``ExposureRowRead``); a change in it is NOT realised PnL
    and does NOT imply a trade — it is how the look-through estimate moved. The
    optional ``price_context_contribution_base`` is a *price-context estimate*
    only (constituent dimension), never exact PnL.
    """

    key: str
    label: str
    bucket: str
    dimension: str
    instrument_id: int | None = None
    instrument_listing_id: int | None = None
    fund_id: int | None = None
    base_weight: DecimalStr
    comparison_weight: DecimalStr
    delta_weight: DecimalStr
    abs_delta_weight: DecimalStr
    base_market_value_base: DecimalStr | None = None
    comparison_market_value_base: DecimalStr | None = None
    delta_market_value_base: DecimalStr | None = None
    abs_delta_market_value_base: DecimalStr | None = None
    base_status: str | None = None
    comparison_status: str | None = None
    status_change: bool = False
    base_price_status: str | None = None
    comparison_price_status: str | None = None
    base_price_date: date | None = None
    comparison_price_date: date | None = None
    base_price_source: str | None = None
    comparison_price_source: str | None = None
    valuation_method: str | None = None
    # appeared | disappeared | increased | decreased | status_changed | unchanged
    change_kind: str
    # --- price-context contribution (constituent dimension only; estimate) -----
    # Resolved constituent EOD prices in both snapshots, when available. The
    # contribution is ``base_market_value_base × price_return`` — a *price-context
    # estimate*, NOT exact PnL and NOT total return.
    base_price: DecimalStr | None = None
    comparison_price: DecimalStr | None = None
    price_return: DecimalStr | None = None
    price_context_contribution_base: DecimalStr | None = None


class ExposureDriftSummary(BaseModel):
    total_abs_weight_delta: DecimalStr
    total_abs_market_value_delta_base: DecimalStr | None = None
    appeared_count: int = 0
    disappeared_count: int = 0
    changed_count: int = 0
    unchanged_count: int = 0
    status_changed_count: int = 0
    base_coverage: DecimalStr | None = None
    comparison_coverage: DecimalStr | None = None
    identity_coverage_delta: DecimalStr | None = None
    price_coverage_delta: DecimalStr | None = None
    fx_coverage_delta: DecimalStr | None = None
    # Sum of price-context contribution estimates over the rows (constituent only).
    total_price_context_contribution_base: DecimalStr | None = None


class ExposureDriftResponse(BaseModel):
    """What changed between two exposure snapshots for a workspace + dimension.

    Compares snapshots only — it does not infer trades, ETF rebalances or PnL.
    ``status`` is ``ok`` (two snapshots compared) or ``insufficient_history``
    (fewer than two snapshots / no prior to compare against).
    """

    workspace_id: int
    status: str = "ok"
    dimension: str
    sort: str
    base_snapshot_id: int | None = None
    comparison_snapshot_id: int | None = None
    base_as_of_date: date | None = None
    comparison_as_of_date: date | None = None
    base_currency: str | None = None
    summary: ExposureDriftSummary | None = None
    rows: list[ExposureDriftRow] = []


class ExposureDriftDashboard(BaseModel):
    """Compact drift block for the dashboard (latest vs previous snapshot)."""

    status: str  # ok | insufficient_history
    base_snapshot_id: int | None = None
    comparison_snapshot_id: int | None = None
    total_abs_constituent_weight_delta: DecimalStr | None = None
    coverage_delta: DecimalStr | None = None
    identity_coverage_delta: DecimalStr | None = None
    price_coverage_delta: DecimalStr | None = None
    fx_coverage_delta: DecimalStr | None = None
    top_constituent_movers: list[ExposureDriftRow] = []


class TopHoldingPerformanceRow(BaseModel):
    """One constituent's price-context contribution over a date window.

    ``price_context_contribution_base`` = ``base_implied_market_value_base ×
    price_return`` — a *price-context estimate*, NOT realised PnL, total return or
    trade attribution. ``price_return`` is a **local-currency** return (the same
    listing/currency in both snapshots, so the ratio is currency-neutral); FX
    drift between the two dates is NOT applied this slice (see ``fx_rate_base`` /
    ``fx_rate_comparison`` for the context a GUI/local pricer would need)."""

    instrument_id: int | None = None
    instrument_listing_id: int | None = None
    label: str
    ticker: str | None = None
    currency: str | None = None
    base_weight: DecimalStr
    comparison_weight: DecimalStr
    weight_delta: DecimalStr
    base_implied_market_value_base: DecimalStr | None = None
    comparison_implied_market_value_base: DecimalStr | None = None
    market_value_delta_base: DecimalStr | None = None
    base_price: DecimalStr | None = None
    comparison_price: DecimalStr | None = None
    base_price_date: date | None = None
    comparison_price_date: date | None = None
    # Local-currency price return (comparison/base − 1); FX drift not applied.
    price_return: DecimalStr | None = None
    price_return_basis: str = "local"  # local | base (base only when currency == base)
    price_context_contribution_base: DecimalStr | None = None
    price_source: str | None = None
    price_status: str | None = None
    # FX context only (currency -> base, as of each date); not applied to the return.
    fx_rate_base: DecimalStr | None = None
    fx_rate_comparison: DecimalStr | None = None
    fx_source: str | None = None
    # ok | missing_base_price | missing_comparison_price | stale_price |
    # fx_missing | unresolved | partial
    status: str
    notes: str | None = None


class TopHoldingPerformanceSummary(BaseModel):
    row_count: int = 0
    computed_count: int = 0
    missing_price_count: int = 0
    stale_price_count: int = 0
    fx_missing_count: int = 0
    total_price_context_contribution_base: DecimalStr | None = None
    total_abs_price_context_contribution_base: DecimalStr | None = None
    total_weight_delta: DecimalStr | None = None
    total_abs_weight_delta: DecimalStr | None = None
    base_price_coverage_weight: DecimalStr | None = None
    comparison_price_coverage_weight: DecimalStr | None = None


class TopHoldingPerformanceResponse(BaseModel):
    """Which constituents likely drove value over a date window (price-context).

    Built from cached exposure snapshots + instrument price history — it is NOT
    PnL, total return or trade attribution. ``status`` is ``ok`` /
    ``insufficient_history`` (need two snapshots) / ``insufficient_price_data``
    (two snapshots but nothing priced to compute a contribution)."""

    workspace_id: int
    status: str = "ok"
    base_snapshot_id: int | None = None
    comparison_snapshot_id: int | None = None
    base_as_of_date: date | None = None
    comparison_as_of_date: date | None = None
    start_date: date | None = None
    end_date: date | None = None
    base_currency: str | None = None
    sort: str = "abs_contribution"
    summary: TopHoldingPerformanceSummary | None = None
    rows: list[TopHoldingPerformanceRow] = []


class TopHoldingPerformanceDashboard(BaseModel):
    """Compact top-holding price-context block for the dashboard."""

    status: str  # ok | insufficient_history | insufficient_price_data
    base_snapshot_id: int | None = None
    comparison_snapshot_id: int | None = None
    total_price_context_contribution_base: DecimalStr | None = None
    missing_price_count: int = 0
    fx_missing_count: int = 0
    top_positive_contributors: list[TopHoldingPerformanceRow] = []
    top_negative_contributors: list[TopHoldingPerformanceRow] = []


class ExposureDashboardBlock(BaseModel):
    """Compact exposure block for the dashboard (top buckets + coverage/age)."""

    # cached | stale | recompute_needed | missing
    status: str
    cached: bool
    snapshot_id: int | None = None
    as_of_date: date | None = None
    age_days: int | None = None
    base_currency: str
    total_market_value_base: DecimalStr | None = None
    coverage_weight: DecimalStr | None = None
    unclassified_weight: DecimalStr | None = None
    missing_holdings_count: int = 0
    missing_fx_count: int = 0
    # True constituent look-through coverage + top resolved constituents.
    constituent_coverage: ConstituentCoverage | None = None
    top_sectors: list[ExposureRowRead] = []
    top_countries: list[ExposureRowRead] = []
    top_currencies: list[ExposureRowRead] = []
    top_holdings: list[ExposureRowRead] = []
    top_constituents: list[ExposureRowRead] = []
    # Compact latest-vs-previous drift (``insufficient_history`` if <2 snapshots).
    drift: ExposureDriftDashboard | None = None
    # Compact top-holding price-context performance (latest vs previous snapshot).
    top_holding_performance: TopHoldingPerformanceDashboard | None = None


class ExposureSnapshotSummary(BaseModel):
    """Metadata-only row for the snapshots listing endpoint."""

    snapshot_id: int
    workspace_id: int
    as_of_date: date
    base_currency: str
    source: str
    status: str
    input_hash: str
    total_market_value_base: DecimalStr | None = None
    coverage_weight: DecimalStr | None = None
    unclassified_weight: DecimalStr | None = None
    missing_holdings_count: int = 0
    missing_fx_count: int = 0
    constituent_coverage: ConstituentCoverage | None = None
    created_at: datetime
    row_count: int = 0
