"""Top-holding performance — price-context contribution over a date window.

The bridge from "what *changed* between two exposure snapshots?" (exposure_drift)
to "what likely *drove value* over this window?". For the heaviest resolved
constituents it pairs the base + comparison snapshot rows, looks up each
constituent's EOD price at the window endpoints, and reports a conservative
**price-context contribution estimate**:

    price_return                  = comparison_price / base_price - 1   (local ccy)
    price_context_contribution_base = base_implied_market_value_base × price_return

This is **not** PnL, total return or trade attribution, and it does **not** infer
buys/sells or ETF rebalance causes. ``base_implied_market_value_base`` is the
weight-based implied value from the snapshot; ``price_return`` is a *local*
currency return (same listing/currency at both endpoints, so the ratio is
currency-neutral) — FX drift between the two dates is **not** applied this slice
(``fx_rate_base`` / ``fx_rate_comparison`` are surfaced as context for a future
FX-adjusted return in the Rust GUI / local pricer).

Compute boundary (see AGENTS.md): bounded and database-driven. Snapshot rows are
already one-per-constituent; prices are fetched with two batched ``GROUP BY``
as-of-date queries (``instrument_prices.prices_asof_for_listings``) over the
*capped* top-weight listing set — never the whole price history, never an
unbounded per-instrument loop, no dataframe analytics.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ExposureRow, InstrumentListing, InstrumentPrice
from app.schemas.exposure import (
    TopHoldingPerformanceDashboard,
    TopHoldingPerformanceResponse,
    TopHoldingPerformanceRow,
    TopHoldingPerformanceSummary,
)
from app.services import exposure_drift
from app.services import exposure_recompute as exposure_service
from app.services import freshness as freshness_service
from app.services import instrument_prices as instrument_prices_service
from app.services import workspaces as workspaces_service
from app.services.conversion import normalise_currency
from app.services.fx import MISSING, load_fx_index

_WEIGHT_Q = Decimal("0.0001")
_MONEY_Q = Decimal("0.01")
_RETURN_Q = Decimal("0.000001")
_RATE_Q = Decimal("0.0000000001")
_ZERO = Decimal("0")

# Upper bound on constituents we price + score, so a few-hundred-holding ETF
# look-through stays bounded. The heaviest holdings dominate contribution.
_MAX_WORKING_SET = 500
_DEFAULT_LIMIT = 50

SORTS = (
    "abs_contribution",
    "contribution",
    "abs_weight_delta",
    "weight_delta",
    "market_value_delta",
)
_DEFAULT_SORT = "abs_contribution"

# Row statuses.
OK = "ok"
MISSING_BASE_PRICE = "missing_base_price"
MISSING_COMPARISON_PRICE = "missing_comparison_price"
STALE_PRICE = "stale_price"
UNRESOLVED = "unresolved"
PARTIAL = "partial"

_SYNTHETIC_BUCKETS = {"__unresolved__", "__unclassified__"}


def _q(value: Decimal | None, quant: Decimal) -> Decimal | None:
    if value is None:
        return None
    return value.quantize(quant, rounding=ROUND_HALF_UP)


@dataclass
class _Pair:
    instrument_id: int
    base: ExposureRow | None = None
    comparison: ExposureRow | None = None

    @property
    def ref(self) -> ExposureRow:
        row = self.comparison or self.base
        assert row is not None
        return row

    @property
    def order_weight(self) -> Decimal:
        return max(
            (self.comparison.weight if self.comparison else _ZERO) or _ZERO,
            (self.base.weight if self.base else _ZERO) or _ZERO,
        )


def _pair_rows(base_rows: list[ExposureRow], comp_rows: list[ExposureRow]) -> list[_Pair]:
    """Pair resolved constituent rows by ``instrument_id`` across the snapshots."""
    pairs: dict[int, _Pair] = {}
    for row in base_rows:
        if row.instrument_id is None or row.bucket in _SYNTHETIC_BUCKETS:
            continue
        pairs.setdefault(row.instrument_id, _Pair(instrument_id=row.instrument_id)).base = row
    for row in comp_rows:
        if row.instrument_id is None or row.bucket in _SYNTHETIC_BUCKETS:
            continue
        pairs.setdefault(row.instrument_id, _Pair(instrument_id=row.instrument_id)).comparison = row
    ordered = sorted(pairs.values(), key=lambda p: p.order_weight, reverse=True)
    return ordered[:_MAX_WORKING_SET]


def _sort_key(sort: str):
    def contribution(r: TopHoldingPerformanceRow) -> Decimal:
        return r.price_context_contribution_base or _ZERO

    def mv_delta(r: TopHoldingPerformanceRow) -> Decimal:
        return r.market_value_delta_base or _ZERO

    if sort == "contribution":
        return lambda r: (contribution(r),)
    if sort == "abs_weight_delta":
        return lambda r: (abs(r.weight_delta),)
    if sort == "weight_delta":
        return lambda r: (r.weight_delta,)
    if sort == "market_value_delta":
        return lambda r: (mv_delta(r),)
    return lambda r: (abs(contribution(r)),)  # abs_contribution (default)


async def compute_top_holding_performance(
    session: AsyncSession,
    workspace_id: int,
    *,
    base_snapshot_id: int | None = None,
    comparison_snapshot_id: int | None = None,
    start_date: date | None = None,
    end_date: date | None = None,
    limit: int = _DEFAULT_LIMIT,
    sort: str = _DEFAULT_SORT,
) -> TopHoldingPerformanceResponse:
    """Top constituents by price-context contribution over a date window."""
    await workspaces_service.get_workspace(session, workspace_id)  # 404s unknown workspace
    sort = sort if sort in SORTS else _DEFAULT_SORT
    limit = max(1, min(limit, _MAX_WORKING_SET))

    base, comparison = await exposure_drift.select_snapshots(
        session,
        workspace_id,
        base_snapshot_id=base_snapshot_id,
        comparison_snapshot_id=comparison_snapshot_id,
    )
    if comparison is None or base is None:
        return TopHoldingPerformanceResponse(
            workspace_id=workspace_id,
            status="insufficient_history",
            sort=sort,
            comparison_snapshot_id=comparison.id if comparison else None,
            comparison_as_of_date=comparison.as_of_date if comparison else None,
            base_currency=comparison.base_currency if comparison else None,
        )

    base_ccy = comparison.base_currency.upper()
    window_start = start_date or base.as_of_date
    window_end = end_date or comparison.as_of_date

    base_rows = await exposure_service.get_rows(session, base.id, dimension="constituent")
    comp_rows = await exposure_service.get_rows(session, comparison.id, dimension="constituent")
    pairs = _pair_rows(base_rows, comp_rows)

    common = dict(
        workspace_id=workspace_id,
        base_snapshot_id=base.id,
        comparison_snapshot_id=comparison.id,
        base_as_of_date=base.as_of_date,
        comparison_as_of_date=comparison.as_of_date,
        start_date=window_start,
        end_date=window_end,
        base_currency=base_ccy,
        sort=sort,
    )
    if not pairs:
        return TopHoldingPerformanceResponse(
            status="insufficient_price_data",
            summary=TopHoldingPerformanceSummary(),
            **common,
        )

    listing_ids = sorted(
        {p.ref.instrument_listing_id for p in pairs if p.ref.instrument_listing_id}
    )
    listings = await _listings_by_id(session, listing_ids)
    base_bars, comp_bars = await _resolve_bars(
        session,
        pairs,
        listing_ids,
        start_date=start_date,
        end_date=end_date,
        default_start=base.as_of_date,
        default_end=comparison.as_of_date,
    )
    fx_index = await load_fx_index(session)

    rows = [
        _build_row(
            p,
            listings,
            base_bars.get(p.instrument_id),
            comp_bars.get(p.instrument_id),
            fx_index,
            base_ccy,
            window_start,
            window_end,
        )
        for p in pairs
    ]
    rows.sort(key=_sort_key(sort), reverse=True)
    rows = rows[:limit]

    summary = _build_summary(rows, base_ccy)
    status = OK if summary.computed_count else "insufficient_price_data"
    return TopHoldingPerformanceResponse(status=status, summary=summary, rows=rows, **common)


async def _listings_by_id(
    session: AsyncSession, listing_ids: list[int]
) -> dict[int, InstrumentListing]:
    if not listing_ids:
        return {}
    rows = (
        (
            await session.execute(
                select(InstrumentListing).where(InstrumentListing.id.in_(listing_ids))
            )
        )
        .scalars()
        .all()
    )
    return {ln.id: ln for ln in rows}


async def _resolve_bars(
    session: AsyncSession,
    pairs: list[_Pair],
    listing_ids: list[int],
    *,
    start_date: date | None,
    end_date: date | None,
    default_start: date,
    default_end: date,
) -> tuple[dict[int, InstrumentPrice], dict[int, InstrumentPrice]]:
    """Resolve the base + comparison EOD bar for each pair (keyed by instrument_id).

    Default (no explicit dates): each constituent's *snapshot* price — the exact
    ``(listing, price_date)`` the base/comparison exposure row priced at — so price
    movement between two same-day snapshots is captured. Explicit dates: a uniform
    as-of window applied to every listing.
    """
    base_bars: dict[int, InstrumentPrice] = {}
    comp_bars: dict[int, InstrumentPrice] = {}

    if start_date is not None or end_date is not None:
        ws = start_date or default_start
        we = end_date or default_end
        base_asof = await instrument_prices_service.prices_asof_for_listings(
            session, listing_ids, ws
        )
        comp_asof = await instrument_prices_service.prices_asof_for_listings(
            session, listing_ids, we
        )
        for p in pairs:
            lid = p.ref.instrument_listing_id
            if lid is None:
                continue
            if lid in base_asof:
                base_bars[p.instrument_id] = base_asof[lid]
            if lid in comp_asof:
                comp_bars[p.instrument_id] = comp_asof[lid]
        return base_bars, comp_bars

    # Default: the exact price each snapshot row captured (per-constituent dates).
    dates: set = set()
    for p in pairs:
        if p.base is not None and p.base.price_date is not None:
            dates.add(p.base.price_date)
        if p.comparison is not None and p.comparison.price_date is not None:
            dates.add(p.comparison.price_date)
    exact = await instrument_prices_service.prices_on_dates_for_listings(
        session, listing_ids, sorted(dates)
    )
    for p in pairs:
        lid = p.ref.instrument_listing_id
        if lid is None:
            continue
        if p.base is not None and p.base.price_date is not None:
            bar = exact.get((lid, p.base.price_date))
            if bar is not None:
                base_bars[p.instrument_id] = bar
        if p.comparison is not None and p.comparison.price_date is not None:
            bar = exact.get((lid, p.comparison.price_date))
            if bar is not None:
                comp_bars[p.instrument_id] = bar
    return base_bars, comp_bars


def _build_row(
    pair: _Pair,
    listings: dict[int, InstrumentListing],
    base_bar: InstrumentPrice | None,
    comp_bar: InstrumentPrice | None,
    fx_index,
    base_ccy: str,
    window_start: date,
    window_end: date,
) -> TopHoldingPerformanceRow:
    ref = pair.ref
    lid = ref.instrument_listing_id
    listing = listings.get(lid) if lid is not None else None

    base_w = (pair.base.weight if pair.base else None) or _ZERO
    comp_w = (pair.comparison.weight if pair.comparison else None) or _ZERO
    base_mv = pair.base.market_value_base if pair.base else None
    comp_mv = pair.comparison.market_value_base if pair.comparison else None
    mv_delta = (comp_mv or _ZERO) - (base_mv or _ZERO) if (base_mv or comp_mv) is not None else None

    base_price = base_bar.close if base_bar else None
    comp_price = comp_bar.close if comp_bar else None
    currency = normalise_currency(
        (comp_bar.currency if comp_bar else None)
        or (base_bar.currency if base_bar else None)
        or (listing.currency if listing else None),
        base_ccy,
    )

    # Local-currency price return + price-context contribution (NOT PnL).
    price_return: Decimal | None = None
    contribution: Decimal | None = None
    if base_price is not None and comp_price is not None and base_price != 0:
        price_return = comp_price / base_price - Decimal(1)
        if base_mv is not None:
            contribution = base_mv * price_return

    # FX context only (currency -> base, as of each endpoint); never applied here.
    fx_rate_base = fx_rate_comp = None
    fx_source = None
    fx_missing = False
    if currency == base_ccy:
        fx_rate_base = fx_rate_comp = Decimal(1)
    else:
        rb = fx_index.get_fx_rate(currency, base_ccy, as_of_date=window_start)
        rc = fx_index.get_fx_rate(currency, base_ccy, as_of_date=window_end)
        fx_rate_base = rb.rate if rb.status != MISSING else None
        fx_rate_comp = rc.rate if rc.status != MISSING else None
        fx_source = rb.source or rc.source
        fx_missing = fx_rate_base is None or fx_rate_comp is None

    status, notes = _classify(
        lid, base_price, comp_price, base_mv, comp_bar, currency, base_ccy, fx_missing
    )

    return TopHoldingPerformanceRow(
        instrument_id=pair.instrument_id,
        instrument_listing_id=lid,
        label=ref.label,
        ticker=listing.ticker if listing else None,
        currency=currency,
        base_weight=_q(base_w, _WEIGHT_Q) or _ZERO,
        comparison_weight=_q(comp_w, _WEIGHT_Q) or _ZERO,
        weight_delta=_q(comp_w - base_w, _WEIGHT_Q) or _ZERO,
        base_implied_market_value_base=_q(base_mv, _MONEY_Q),
        comparison_implied_market_value_base=_q(comp_mv, _MONEY_Q),
        market_value_delta_base=_q(mv_delta, _MONEY_Q),
        base_price=_q(base_price, _MONEY_Q),
        comparison_price=_q(comp_price, _MONEY_Q),
        base_price_date=base_bar.price_date if base_bar else None,
        comparison_price_date=comp_bar.price_date if comp_bar else None,
        price_return=_q(price_return, _RETURN_Q),
        price_return_basis="base" if currency == base_ccy else "local",
        price_context_contribution_base=_q(contribution, _MONEY_Q),
        price_source=comp_bar.source if comp_bar else (base_bar.source if base_bar else None),
        price_status=pair.comparison.price_status if pair.comparison else None,
        fx_rate_base=_q(fx_rate_base, _RATE_Q),
        fx_rate_comparison=_q(fx_rate_comp, _RATE_Q),
        fx_source=fx_source,
        status=status,
        notes=notes,
    )


def _classify(
    lid: int | None,
    base_price: Decimal | None,
    comp_price: Decimal | None,
    base_mv: Decimal | None,
    comp_bar,
    currency: str,
    base_ccy: str,
    fx_missing: bool,
) -> tuple[str, str | None]:
    note_fx = (
        " local-currency return; FX drift not applied (see fx_rate_*)."
        if currency != base_ccy and not fx_missing
        else ""
    )
    note_fx_missing = " FX context unavailable for this currency." if fx_missing else ""
    if lid is None:
        return UNRESOLVED, "No tradable listing for this constituent."
    if base_price is None:
        return MISSING_BASE_PRICE, "No price on/before the window start." + note_fx_missing
    if comp_price is None:
        return MISSING_COMPARISON_PRICE, "No price on/before the window end." + note_fx_missing
    if base_mv is None:
        return PARTIAL, "No base snapshot implied value (constituent only in the comparison)."
    if comp_bar is not None and (
        freshness_service.freshness_state(comp_bar.price_date, kind="price")
        == freshness_service.STALE
    ):
        return STALE_PRICE, (
            "Comparison price is stale." + note_fx + note_fx_missing
        ).strip() or None
    return OK, (note_fx + note_fx_missing).strip() or None


def _build_summary(
    rows: list[TopHoldingPerformanceRow], base_ccy: str
) -> TopHoldingPerformanceSummary:
    computed = [r for r in rows if r.price_context_contribution_base is not None]
    contribs = [r.price_context_contribution_base for r in computed]
    base_priced_w = sum((r.comparison_weight for r in rows if r.base_price is not None), _ZERO)
    comp_priced_w = sum(
        (r.comparison_weight for r in rows if r.comparison_price is not None), _ZERO
    )
    return TopHoldingPerformanceSummary(
        row_count=len(rows),
        computed_count=len(computed),
        missing_price_count=sum(
            1 for r in rows if r.status in (MISSING_BASE_PRICE, MISSING_COMPARISON_PRICE)
        ),
        stale_price_count=sum(1 for r in rows if r.status == STALE_PRICE),
        fx_missing_count=sum(
            1
            for r in rows
            if r.currency != base_ccy and (r.fx_rate_base is None or r.fx_rate_comparison is None)
        ),
        total_price_context_contribution_base=_q(sum(contribs, _ZERO), _MONEY_Q)
        if contribs
        else None,
        total_abs_price_context_contribution_base=_q(
            sum((abs(c) for c in contribs), _ZERO), _MONEY_Q
        )
        if contribs
        else None,
        total_weight_delta=_q(sum((r.weight_delta for r in rows), _ZERO), _WEIGHT_Q),
        total_abs_weight_delta=_q(sum((abs(r.weight_delta) for r in rows), _ZERO), _WEIGHT_Q),
        base_price_coverage_weight=_q(base_priced_w, _WEIGHT_Q),
        comparison_price_coverage_weight=_q(comp_priced_w, _WEIGHT_Q),
    )


# --- compact dashboard block -------------------------------------------------


async def build_dashboard_performance(
    session: AsyncSession, workspace_id: int, *, top_n: int = 3
) -> TopHoldingPerformanceDashboard:
    """Compact top positive/negative price-context contributors for the dashboard."""
    perf = await compute_top_holding_performance(
        session, workspace_id, limit=_MAX_WORKING_SET, sort="contribution"
    )
    if perf.status == "insufficient_history" or perf.summary is None:
        return TopHoldingPerformanceDashboard(status=perf.status)
    contributors = [r for r in perf.rows if r.price_context_contribution_base is not None]
    positive = [r for r in contributors if (r.price_context_contribution_base or _ZERO) > 0]
    negative = [r for r in contributors if (r.price_context_contribution_base or _ZERO) < 0]
    positive.sort(key=lambda r: r.price_context_contribution_base or _ZERO, reverse=True)
    negative.sort(key=lambda r: r.price_context_contribution_base or _ZERO)
    return TopHoldingPerformanceDashboard(
        status=perf.status,
        base_snapshot_id=perf.base_snapshot_id,
        comparison_snapshot_id=perf.comparison_snapshot_id,
        total_price_context_contribution_base=perf.summary.total_price_context_contribution_base,
        missing_price_count=perf.summary.missing_price_count,
        fx_missing_count=perf.summary.fx_missing_count,
        top_positive_contributors=positive[:top_n],
        top_negative_contributors=negative[:top_n],
    )
