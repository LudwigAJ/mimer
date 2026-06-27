"""Exposure drift — compare two exposure snapshots to explain *what changed*.

This is the read/compute layer on top of the cached ``exposure_snapshots`` /
``exposure_rows`` the ``exposure_recompute`` worker writes. It diffs two snapshots
of the same workspace and reports, per dimension bucket, how the look-through
**weight** and **implied market value** moved, plus coverage / price-status drift.

Honesty (see AGENTS.md): this compares snapshots — it does **not** know trades,
ETF rebalances or realised PnL. ``delta_market_value_base`` is the change in the
weight-based *implied* value, not cash PnL. For constituents we can additionally
fetch the resolved EOD prices from ``instrument_prices`` and report a
``price_context_contribution`` — explicitly a *price-context estimate*, never
exact PnL or total return. There is no new table and no worker: drift is computed
on demand from snapshots already on disk. No network.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ExposureRow, ExposureSnapshot, InstrumentPrice
from app.schemas.exposure import (
    ExposureDriftDashboard,
    ExposureDriftResponse,
    ExposureDriftRow,
    ExposureDriftSummary,
)
from app.services import exposure_recompute as exposure_service
from app.services import workspaces as workspaces_service

_WEIGHT_Q = Decimal("0.0001")
_MONEY_Q = Decimal("0.01")
_RETURN_Q = Decimal("0.000001")
_ZERO = Decimal("0")
_EPS = Decimal("0.00005")  # below this, a weight delta is "unchanged"

# Dimensions drift supports today (all present on exposure_rows).
DIMENSIONS = (
    "constituent",
    "country",
    "sector",
    "industry",
    "currency",
    "source",
    "constituent_price_status",
)
_DEFAULT_DIMENSION = "constituent"

# Sort keys for the rows / top-movers view.
SORTS = ("abs_delta_weight", "abs_delta_market_value", "delta_weight", "delta_market_value")
_DEFAULT_SORT = "abs_delta_weight"

# Synthetic constituent buckets excluded from "movers" (not real securities).
_SYNTHETIC_BUCKETS = {"__unresolved__", "__unclassified__"}

# change_kind values.
APPEARED = "appeared"
DISAPPEARED = "disappeared"
INCREASED = "increased"
DECREASED = "decreased"
STATUS_CHANGED = "status_changed"
UNCHANGED = "unchanged"


def _q(value: Decimal | None, quant: Decimal) -> Decimal | None:
    if value is None:
        return None
    return value.quantize(quant, rounding=ROUND_HALF_UP)


# --- snapshot selection ------------------------------------------------------


async def previous_snapshot(
    session: AsyncSession, workspace_id: int, comparison: ExposureSnapshot
) -> ExposureSnapshot | None:
    """The snapshot immediately before ``comparison`` for the same workspace.

    Ordered by ``(as_of_date, id)`` so two snapshots written the same day (a hash
    change without a date change) still order deterministically.
    """
    return await session.scalar(
        select(ExposureSnapshot)
        .where(
            ExposureSnapshot.workspace_id == workspace_id,
            or_(
                ExposureSnapshot.as_of_date < comparison.as_of_date,
                (ExposureSnapshot.as_of_date == comparison.as_of_date)
                & (ExposureSnapshot.id < comparison.id),
            ),
        )
        .order_by(ExposureSnapshot.as_of_date.desc(), ExposureSnapshot.id.desc())
        .limit(1)
    )


async def select_snapshots(
    session: AsyncSession,
    workspace_id: int,
    *,
    base_snapshot_id: int | None = None,
    comparison_snapshot_id: int | None = None,
) -> tuple[ExposureSnapshot | None, ExposureSnapshot | None]:
    """Resolve ``(base, comparison)``. Default: previous vs latest.

    Explicit ids are loaded workspace-scoped (``get_snapshot`` 404s a snapshot
    belonging to another workspace), so cross-workspace comparison is impossible.
    Returns ``(None, comparison)`` when there is no prior snapshot to compare.
    """
    if comparison_snapshot_id is not None:
        comparison = await exposure_service.get_snapshot(
            session, workspace_id, comparison_snapshot_id
        )
    else:
        comparison = await exposure_service.get_latest_snapshot(session, workspace_id)
    if comparison is None:
        return None, None

    if base_snapshot_id is not None:
        base = await exposure_service.get_snapshot(session, workspace_id, base_snapshot_id)
    else:
        base = await previous_snapshot(session, workspace_id, comparison)
    return base, comparison


# --- row diff ----------------------------------------------------------------


def _match_key(row: ExposureRow, dimension: str) -> str:
    """Stable identity for matching a row across snapshots.

    Constituents match by resolved ``instrument_id`` (deduped across funds),
    falling back to ``bucket`` for synthetic/unresolved rows; every other
    dimension matches by ``bucket``.
    """
    if dimension == "constituent" and row.instrument_id is not None:
        return f"instrument:{row.instrument_id}"
    return row.bucket


@dataclass
class _Pair:
    base: ExposureRow | None = None
    comparison: ExposureRow | None = None


def _classify(base: ExposureRow | None, comparison: ExposureRow | None, delta_w: Decimal) -> str:
    if base is None:
        return APPEARED
    if comparison is None:
        return DISAPPEARED
    if delta_w > _EPS:
        return INCREASED
    if delta_w < -_EPS:
        return DECREASED
    if (base.status or "") != (comparison.status or "") or (base.price_status or "") != (
        comparison.price_status or ""
    ):
        return STATUS_CHANGED
    return UNCHANGED


def _drift_row(key: str, dimension: str, pair: _Pair) -> ExposureDriftRow:
    b, c = pair.base, pair.comparison
    ref = c or b  # the row we read identity/labels from (prefer comparison)
    assert ref is not None

    base_w = (b.weight if b else None) or _ZERO
    comp_w = (c.weight if c else None) or _ZERO
    delta_w = comp_w - base_w

    base_mv = b.market_value_base if b else None
    comp_mv = c.market_value_base if c else None
    delta_mv: Decimal | None = None
    if base_mv is not None or comp_mv is not None:
        delta_mv = (comp_mv or _ZERO) - (base_mv or _ZERO)

    base_status = b.status if b else None
    comp_status = c.status if c else None
    status_change = (b is not None and c is not None) and (base_status != comp_status)

    return ExposureDriftRow(
        key=key,
        label=ref.label,
        bucket=ref.bucket,
        dimension=dimension,
        instrument_id=ref.instrument_id,
        instrument_listing_id=ref.instrument_listing_id,
        fund_id=ref.fund_id,
        base_weight=_q(base_w, _WEIGHT_Q) or _ZERO,
        comparison_weight=_q(comp_w, _WEIGHT_Q) or _ZERO,
        delta_weight=_q(delta_w, _WEIGHT_Q) or _ZERO,
        abs_delta_weight=_q(abs(delta_w), _WEIGHT_Q) or _ZERO,
        base_market_value_base=_q(base_mv, _MONEY_Q),
        comparison_market_value_base=_q(comp_mv, _MONEY_Q),
        delta_market_value_base=_q(delta_mv, _MONEY_Q),
        abs_delta_market_value_base=_q(abs(delta_mv), _MONEY_Q) if delta_mv is not None else None,
        base_status=base_status,
        comparison_status=comp_status,
        status_change=status_change,
        base_price_status=b.price_status if b else None,
        comparison_price_status=c.price_status if c else None,
        base_price_date=b.price_date if b else None,
        comparison_price_date=c.price_date if c else None,
        base_price_source=b.price_source if b else None,
        comparison_price_source=c.price_source if c else None,
        valuation_method=(c.valuation_method if c else None) or (b.valuation_method if b else None),
        change_kind=_classify(b, c, delta_w),
    )


def _sort_rows(rows: list[ExposureDriftRow], sort: str) -> list[ExposureDriftRow]:
    def mv(r: ExposureDriftRow, attr: str) -> Decimal:
        return getattr(r, attr) or _ZERO

    if sort == "delta_weight":
        rows.sort(key=lambda r: (r.delta_weight, r.key), reverse=True)
    elif sort == "delta_market_value":
        rows.sort(key=lambda r: (mv(r, "delta_market_value_base"), r.key), reverse=True)
    elif sort == "abs_delta_market_value":
        rows.sort(key=lambda r: (mv(r, "abs_delta_market_value_base"), r.key), reverse=True)
    else:  # abs_delta_weight (default)
        rows.sort(key=lambda r: (r.abs_delta_weight, r.key), reverse=True)
    return rows


# --- price-context contribution (constituent only) ---------------------------


async def _price_context(session: AsyncSession, rows: list[ExposureDriftRow]) -> None:
    """Attach a *price-context contribution estimate* to constituent rows in place.

    Fetches the resolved EOD close from ``instrument_prices`` for the base and
    comparison ``(instrument_listing_id, price_date)`` of each row that has both,
    then sets ``price_return`` = comp/base − 1 and
    ``price_context_contribution_base`` = ``base_market_value_base × price_return``.
    Same listing ⇒ same currency ⇒ the ratio is currency-neutral. This is an
    estimate, NOT realised PnL (no shares, no trades).
    """
    wanted: set[tuple[int, date]] = set()
    for r in rows:
        if r.instrument_listing_id is None:
            continue
        if r.base_price_date is not None:
            wanted.add((r.instrument_listing_id, r.base_price_date))
        if r.comparison_price_date is not None:
            wanted.add((r.instrument_listing_id, r.comparison_price_date))
    if not wanted:
        return

    listing_ids = sorted({lid for lid, _ in wanted})
    dates = sorted({d for _, d in wanted})
    price_rows = (
        (
            await session.execute(
                select(InstrumentPrice).where(
                    InstrumentPrice.instrument_listing_id.in_(listing_ids),
                    InstrumentPrice.price_date.in_(dates),
                )
            )
        )
        .scalars()
        .all()
    )
    # (listing, date) -> close. A manual source wins, else last in (deterministic).
    by_key: dict[tuple[int, date], Decimal] = {}
    for p in sorted(price_rows, key=lambda p: (p.id,)):
        key = (p.instrument_listing_id, p.price_date)
        if p.source == "manual" or key not in by_key:
            by_key[key] = p.close

    for r in rows:
        lid = r.instrument_listing_id
        if lid is None or r.base_price_date is None or r.comparison_price_date is None:
            continue
        base_close = by_key.get((lid, r.base_price_date))
        comp_close = by_key.get((lid, r.comparison_price_date))
        if base_close is None or comp_close is None or base_close == 0:
            continue
        ret = comp_close / base_close - Decimal(1)
        r.base_price = base_close
        r.comparison_price = comp_close
        r.price_return = _q(ret, _RETURN_Q)
        if r.base_market_value_base is not None:
            contribution = r.base_market_value_base * ret
            r.price_context_contribution_base = _q(contribution, _MONEY_Q)


# --- summary -----------------------------------------------------------------


def _coverage(snapshot: ExposureSnapshot | None, attr: str) -> Decimal | None:
    if snapshot is None:
        return None
    return getattr(snapshot, attr)


def _coverage_delta(
    base: ExposureSnapshot | None, comparison: ExposureSnapshot | None, attr: str
) -> Decimal | None:
    b = _coverage(base, attr)
    c = _coverage(comparison, attr)
    if b is None or c is None:
        return _q(c - b, _WEIGHT_Q) if (b is not None and c is not None) else None
    return _q(c - b, _WEIGHT_Q)


def _build_summary(
    rows: list[ExposureDriftRow],
    base: ExposureSnapshot | None,
    comparison: ExposureSnapshot | None,
) -> ExposureDriftSummary:
    total_w = sum((r.abs_delta_weight for r in rows), _ZERO)
    total_mv = sum(
        (r.abs_delta_market_value_base or _ZERO for r in rows),
        _ZERO,
    )
    contrib = [r.price_context_contribution_base for r in rows if r.price_context_contribution_base]
    return ExposureDriftSummary(
        total_abs_weight_delta=_q(total_w, _WEIGHT_Q) or _ZERO,
        total_abs_market_value_delta_base=_q(total_mv, _MONEY_Q),
        appeared_count=sum(1 for r in rows if r.change_kind == APPEARED),
        disappeared_count=sum(1 for r in rows if r.change_kind == DISAPPEARED),
        changed_count=sum(1 for r in rows if r.change_kind in (INCREASED, DECREASED)),
        status_changed_count=sum(1 for r in rows if r.change_kind == STATUS_CHANGED),
        unchanged_count=sum(1 for r in rows if r.change_kind == UNCHANGED),
        base_coverage=_coverage(base, "coverage_weight"),
        comparison_coverage=_coverage(comparison, "coverage_weight"),
        identity_coverage_delta=_coverage_delta(base, comparison, "identity_coverage_weight"),
        price_coverage_delta=_coverage_delta(base, comparison, "price_coverage_weight"),
        fx_coverage_delta=_coverage_delta(base, comparison, "fx_coverage_weight"),
        total_price_context_contribution_base=_q(sum(contrib, _ZERO), _MONEY_Q)
        if contrib
        else None,
    )


# --- public compute ----------------------------------------------------------


async def compute_drift(
    session: AsyncSession,
    workspace_id: int,
    *,
    dimension: str = _DEFAULT_DIMENSION,
    base_snapshot_id: int | None = None,
    comparison_snapshot_id: int | None = None,
    sort: str = _DEFAULT_SORT,
    limit: int | None = None,
    exclude_unchanged: bool = False,
    movers_only: bool = False,
    with_price_context: bool = True,
) -> ExposureDriftResponse:
    """Diff two snapshots for one dimension. ``movers_only`` also drops the
    synthetic constituent buckets (for the top-movers view)."""
    await workspaces_service.get_workspace(session, workspace_id)  # 404s unknown workspace
    dimension = dimension if dimension in DIMENSIONS else _DEFAULT_DIMENSION
    sort = sort if sort in SORTS else _DEFAULT_SORT

    base, comparison = await select_snapshots(
        session,
        workspace_id,
        base_snapshot_id=base_snapshot_id,
        comparison_snapshot_id=comparison_snapshot_id,
    )
    if comparison is None or base is None:
        return ExposureDriftResponse(
            workspace_id=workspace_id,
            status="insufficient_history",
            dimension=dimension,
            sort=sort,
            comparison_snapshot_id=comparison.id if comparison else None,
            comparison_as_of_date=comparison.as_of_date if comparison else None,
            base_currency=comparison.base_currency if comparison else None,
        )

    base_rows = await exposure_service.get_rows(session, base.id, dimension=dimension)
    comp_rows = await exposure_service.get_rows(session, comparison.id, dimension=dimension)

    pairs: dict[str, _Pair] = {}
    for row in base_rows:
        pairs.setdefault(_match_key(row, dimension), _Pair()).base = row
    for row in comp_rows:
        pairs.setdefault(_match_key(row, dimension), _Pair()).comparison = row

    rows = [_drift_row(key, dimension, pair) for key, pair in pairs.items()]
    summary = _build_summary(rows, base, comparison)

    if dimension == "constituent" and with_price_context:
        await _price_context(session, rows)
        # Refresh the contribution total now that rows are populated.
        contrib = [
            r.price_context_contribution_base for r in rows if r.price_context_contribution_base
        ]
        summary.total_price_context_contribution_base = (
            _q(sum(contrib, _ZERO), _MONEY_Q) if contrib else None
        )

    if movers_only:
        rows = [r for r in rows if r.bucket not in _SYNTHETIC_BUCKETS]
    if exclude_unchanged or movers_only:
        rows = [r for r in rows if r.change_kind != UNCHANGED]

    rows = _sort_rows(rows, sort)
    if limit is not None:
        rows = rows[:limit]

    return ExposureDriftResponse(
        workspace_id=workspace_id,
        status="ok",
        dimension=dimension,
        sort=sort,
        base_snapshot_id=base.id,
        comparison_snapshot_id=comparison.id,
        base_as_of_date=base.as_of_date,
        comparison_as_of_date=comparison.as_of_date,
        base_currency=comparison.base_currency,
        summary=summary,
        rows=rows,
    )


# --- compact dashboard block -------------------------------------------------


async def build_dashboard_drift(
    session: AsyncSession, workspace_id: int, *, top_n: int = 5
) -> ExposureDriftDashboard:
    """Compact latest-vs-previous constituent drift for the dashboard."""
    drift = await compute_drift(
        session,
        workspace_id,
        dimension="constituent",
        sort="abs_delta_weight",
        limit=top_n,
        movers_only=True,
    )
    if drift.status != "ok" or drift.summary is None:
        return ExposureDriftDashboard(status="insufficient_history")
    s = drift.summary
    return ExposureDriftDashboard(
        status="ok",
        base_snapshot_id=drift.base_snapshot_id,
        comparison_snapshot_id=drift.comparison_snapshot_id,
        total_abs_constituent_weight_delta=s.total_abs_weight_delta,
        coverage_delta=(
            (s.comparison_coverage - s.base_coverage)
            if (s.comparison_coverage is not None and s.base_coverage is not None)
            else None
        ),
        identity_coverage_delta=s.identity_coverage_delta,
        price_coverage_delta=s.price_coverage_delta,
        fx_coverage_delta=s.fx_coverage_delta,
        top_constituent_movers=drift.rows,
    )
