"""Derived look-through exposure: compute, hash, and cache as snapshots.

This turns exposure from an ad-hoc read computation (`app/services/exposure.py`,
kept for the legacy slice shape) into an inspectable, cacheable, timestamped,
provenance/freshness-aware dataset in ``exposure_snapshots`` / ``exposure_rows``.

Pipeline:

1. ``compute_exposure`` — read current positions, latest prices, FX and the
   selected holdings snapshots, value each position in base currency (reusing the
   `FxIndex`), then distribute look-through across dimensions
   (fund/holding/country/sector/industry/currency/source). It is read-only and
   returns a `ComputedExposure` (rows + coverage metadata + a deterministic
   ``input_hash``).
2. ``recompute_workspace`` — idempotent persist: if the latest snapshot already
   has this ``input_hash`` nothing is written; otherwise a new snapshot+rows are
   inserted (old snapshots are kept as history for drift detection later).

Generic by design (``dimension``/``bucket``/``label``) so direct equities, bonds,
cash, etc. slot in later without a schema change. No network — all inputs are DB
rows.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFoundError
from app.db.models import (
    ExposureRow,
    ExposureSnapshot,
    Fund,
    FundListing,
    PortfolioPosition,
    Price,
    Workspace,
)
from app.schemas.exposure import (
    ConstituentCoverage,
    ExposureDashboardBlock,
    ExposureRowRead,
    ExposureSnapshotResponse,
    ExposureSnapshotSummary,
)
from app.services import alert_rules, constituent_valuation
from app.services import holdings_ingestion as holdings_service
from app.services import workspaces as workspaces_service
from app.services.conversion import normalise_currency
from app.services.fx import load_fx_index

_WEIGHT_Q = Decimal("0.0001")
_MONEY_Q = Decimal("0.01")
_ZERO = Decimal("0")
_ONE = Decimal("1")
_EPS = Decimal("0.00000001")

EXPOSURE_SOURCE = "exposure_recompute"

# Dimension names + the order rows are emitted in. The ``constituent*`` dimensions
# (added with true look-through valuation) are derived from resolved constituent
# instruments + their EOD prices; the rest are the original fund-level slices.
DIMENSIONS = (
    "fund",
    "holding",
    "constituent",
    "country",
    "sector",
    "industry",
    "currency",
    "constituent_price_status",
    "constituent_source",
    "source",
)
_UNCLASSIFIED = "Unclassified"

# Row status precedence: a more severe status wins when a bucket aggregates rows.
_STATUS_RANK = {
    "ok": 0,
    "approximate": 1,
    "unclassified": 2,
    "stale_price": 3,
    "missing_holdings": 4,
    "missing_listing": 5,
    "unresolved_identity": 6,
    "price_missing": 7,
    "fx_missing": 8,
}


def _q(value: Decimal | None, quant: Decimal) -> Decimal | None:
    if value is None:
        return None
    return value.quantize(quant, rounding=ROUND_HALF_UP)


def _sha(payload: Any) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str, separators=(",", ":")).encode()
    ).hexdigest()


def _dec(value: Decimal | None) -> str:
    """Canonical Decimal string for hashing (trailing-zero / scale stable).

    The same value can arrive as ``Decimal("10")`` (freshly built) or
    ``Decimal("10.00000000")`` (round-tripped through a Numeric column), so a bare
    ``str()`` would make the input hash depend on read timing and break
    idempotency. Normalising collapses both to one canonical form (``"10"``).
    """
    if value is None:
        return ""
    normalized = value.normalize()
    # Avoid exponent notation (e.g. 1E+1) so the canonical form is human-stable.
    return f"{normalized:f}"


# --- computation result types ------------------------------------------------


@dataclass
class ComputedRow:
    dimension: str
    bucket: str
    label: str
    weight: Decimal
    market_value_base: Decimal | None
    currency: str | None
    source: str | None
    status: str
    # Constituent look-through context (None on fund-level rows).
    instrument_id: int | None = None
    instrument_listing_id: int | None = None
    fund_id: int | None = None
    price_date: date | None = None
    price_source: str | None = None
    price_status: str | None = None
    fx_rate: Decimal | None = None
    fx_source: str | None = None
    valuation_method: str | None = None


@dataclass
class ComputedExposure:
    workspace_id: int
    as_of_date: date
    base_currency: str
    status: str
    input_hash: str
    holdings_snapshot_hash: str
    fx_snapshot_hash: str
    position_snapshot_hash: str
    total_market_value_base: Decimal
    coverage_weight: Decimal
    unclassified_weight: Decimal
    missing_holdings_count: int
    missing_fx_count: int
    rows: list[ComputedRow]
    # --- true constituent look-through coverage (weight-based, nested) --------
    identity_coverage_weight: Decimal = _ZERO
    price_coverage_weight: Decimal = _ZERO
    fx_coverage_weight: Decimal = _ZERO
    constituent_count: int = 0
    resolved_constituent_count: int = 0
    priced_constituent_count: int = 0
    stale_constituent_price_count: int = 0
    missing_constituent_price_count: int = 0
    constituent_fx_missing_count: int = 0


class _Bucket:
    __slots__ = (
        "weight",
        "mv",
        "has_value",
        "label",
        "currency",
        "source",
        "status",
        "meta",
    )

    def __init__(self, label: str, currency: str | None, source: str | None, status: str) -> None:
        self.weight = _ZERO
        self.mv = _ZERO
        self.has_value = False
        self.label = label
        self.currency = currency
        self.source = source
        self.status = status
        # Constituent context (first non-null write wins); empty for fund-level.
        self.meta: dict[str, object] = {}


# --- valuation helpers -------------------------------------------------------


async def _latest_prices(session: AsyncSession, listing_ids: list[int]) -> dict[int, Price]:
    if not listing_ids:
        return {}
    rows = (
        (
            await session.execute(
                select(Price)
                .where(Price.fund_listing_id.in_(listing_ids))
                .order_by(Price.fund_listing_id, Price.price_date.asc())
            )
        )
        .scalars()
        .all()
    )
    latest: dict[int, Price] = {}
    for price in rows:  # ascending date => last wins
        latest[price.fund_listing_id] = price
    return latest


# --- core computation --------------------------------------------------------


async def compute_exposure(session: AsyncSession, workspace_id: int) -> ComputedExposure | None:
    """Compute (but do not persist) look-through exposure. ``None`` if no positions."""
    workspace = await workspaces_service.get_workspace(session, workspace_id)
    base = workspace.base_currency.upper()
    as_of = date.today()

    rows = (
        await session.execute(
            select(PortfolioPosition, FundListing, Fund)
            .join(FundListing, PortfolioPosition.fund_listing_id == FundListing.id)
            .join(Fund, FundListing.fund_id == Fund.id)
            .where(PortfolioPosition.workspace_id == workspace_id)
            .order_by(PortfolioPosition.id)
        )
    ).all()
    if not rows:
        return None

    listing_ids = [ln.id for _, ln, _ in rows]
    fund_ids = sorted({f.id for _, _, f in rows})
    fx_index = await load_fx_index(session)
    latest_prices = await _latest_prices(session, listing_ids)
    holdings_by_fund = await holdings_service.latest_holdings_by_fund(session, fund_ids)

    # Resolved constituents -> instrument + primary listing + latest EOD price +
    # FX (deduped across funds). Feeds the true look-through valuation layer.
    resolved_instrument_ids = sorted(
        {
            h.holding_instrument_id
            for items in holdings_by_fund.values()
            for h in items
            if h.holding_instrument_id is not None
        }
    )
    constituent_infos = await constituent_valuation.load_constituent_infos(
        session, resolved_instrument_ids, fx_index=fx_index, base=base
    )

    # Input collections for deterministic hashing.
    position_inputs = sorted((ln.id, _dec(pos.units)) for pos, ln, _ in rows)
    price_inputs: list[tuple] = []
    fx_inputs: set[tuple] = set()
    holdings_inputs: list[tuple] = []
    for fid in fund_ids:
        for h in holdings_by_fund.get(fid) or []:
            holdings_inputs.append(
                (
                    fid,
                    h.source,
                    h.as_of_date.isoformat(),
                    h.holding_key,
                    _dec(h.weight),
                    h.country or "",
                    h.sector or "",
                    h.industry or "",
                    h.currency or "",
                    # Identity link/state so a (re)resolution changes the hash.
                    h.holding_instrument_id or 0,
                    h.identity_status or "",
                )
            )
    holdings_inputs.sort()

    # Constituent prices + their FX folded into the hash so a changed/new
    # constituent price (or its FX) yields a new snapshot.
    constituent_price_inputs: list[tuple] = []
    for iid in resolved_instrument_ids:
        info = constituent_infos.get(iid)
        if info is None or info.price is None or info.listing is None:
            continue
        price = info.price
        constituent_price_inputs.append(
            (
                info.listing.id,
                price.price_date.isoformat(),
                _dec(price.close),
                price.currency or "",
                price.source,
            )
        )
        fx = info.fx
        if fx is not None and fx.rate is not None and fx.rate_date is not None:
            fx_inputs.add(
                (
                    fx.from_currency,
                    fx.to_currency,
                    _dec(fx.rate),
                    fx.rate_date.isoformat(),
                    fx.source or "",
                )
            )

    # First pass: value each position in base currency.
    @dataclass
    class _Entry:
        fund: Fund
        listing: FundListing
        units: Decimal
        local_ccy: str
        mv_base: Decimal | None
        status: str  # ok | fx_missing | price_missing

    entries: list[_Entry] = []
    total_mv = _ZERO
    missing_fx_count = 0
    for pos, listing, fund in rows:
        price = latest_prices.get(listing.id)
        local_ccy = normalise_currency(
            (price.currency if price else listing.currency_unit) or listing.trading_currency, base
        )
        if price is None:
            entries.append(_Entry(fund, listing, pos.units, local_ccy, None, "price_missing"))
            continue
        price_inputs.append(
            (
                listing.id,
                price.price_date.isoformat(),
                _dec(price.price),
                price.currency,
                price.source,
            )
        )
        conv = fx_index.convert_amount(pos.units * price.price, price.currency, base)
        if conv.rate is not None and conv.rate_date is not None:
            fx_inputs.add(
                (
                    conv.from_currency,
                    conv.to_currency,
                    _dec(conv.rate),
                    conv.rate_date.isoformat(),
                    conv.source or "",
                )
            )
        mv = conv.converted_amount
        if mv is None:
            missing_fx_count += 1
            entries.append(_Entry(fund, listing, pos.units, local_ccy, None, "fx_missing"))
            continue
        entries.append(_Entry(fund, listing, pos.units, local_ccy, mv, "ok"))
        total_mv += mv

    missing_holdings_count = sum(1 for fid in fund_ids if not (holdings_by_fund.get(fid) or []))

    # Second pass: distribute look-through weight across dimensions.
    dims: dict[str, dict[str, _Bucket]] = {d: {} for d in DIMENSIONS}

    def bump(
        dim: str,
        bucket: str,
        weight: Decimal,
        mv: Decimal | None,
        *,
        label: str,
        currency: str | None = None,
        source: str | None = None,
        status: str = "ok",
        meta: dict[str, object] | None = None,
    ) -> None:
        b = dims[dim].get(bucket)
        if b is None:
            b = _Bucket(label, currency, source, status)
            dims[dim][bucket] = b
        b.weight += weight
        if mv is not None:
            b.mv += mv
            b.has_value = True
        if _STATUS_RANK.get(status, 0) > _STATUS_RANK.get(b.status, 0):
            b.status = status
        if b.currency is None and currency is not None:
            b.currency = currency
        if b.source is None and source is not None:
            b.source = source
        if meta:
            for key, value in meta.items():
                if value is not None and b.meta.get(key) is None:
                    b.meta[key] = value

    # --- true constituent look-through coverage (weight-based, deduped counts) -
    coverage_w = _ZERO
    identity_cov_w = _ZERO
    price_cov_w = _ZERO
    fx_cov_w = _ZERO
    resolved_iids: set[int] = set()
    priced_iids: set[int] = set()
    stale_iids: set[int] = set()
    missing_price_iids: set[int] = set()
    fx_missing_iids: set[int] = set()
    unresolved_keys: set[str] = set()
    if total_mv > 0:
        for e in entries:
            if e.mv_base is None:
                # Cannot value (no price / no FX): surface on the fund dimension.
                bump(
                    "fund",
                    e.fund.isin,
                    _ZERO,
                    None,
                    label=e.fund.name,
                    currency=base,
                    source=e.fund.source,
                    status=e.status,
                    meta={"fund_id": e.fund.id},
                )
                continue
            pos_weight = e.mv_base / total_mv
            bump(
                "fund",
                e.fund.isin,
                pos_weight,
                e.mv_base,
                label=e.fund.name,
                currency=base,
                source=e.fund.source,
                meta={"fund_id": e.fund.id},
            )

            holdings = holdings_by_fund.get(e.fund.id) or []
            looked = _ZERO
            for h in holdings:
                w = pos_weight * h.weight
                m = e.mv_base * h.weight
                looked += w
                coverage_w += w

                # --- true constituent look-through (price + FX aware) ---------
                cls = constituent_valuation.classify(h, constituent_infos)
                constituent_meta = {
                    "instrument_id": cls.instrument_id,
                    "instrument_listing_id": cls.instrument_listing_id,
                    "price_date": cls.price_date,
                    "price_source": cls.price_source,
                    "price_status": cls.price_status,
                    "fx_rate": cls.fx_rate,
                    "fx_source": cls.fx_source,
                    "valuation_method": cls.valuation_method,
                }
                if cls.is_resolved and cls.instrument_id is not None:
                    iid = cls.instrument_id
                    resolved_iids.add(iid)
                    identity_cov_w += w
                    if cls.is_priced:
                        priced_iids.add(iid)
                        price_cov_w += w
                        if cls.is_fx_ok:
                            fx_cov_w += w
                            if cls.status == constituent_valuation.STATUS_STALE_PRICE:
                                stale_iids.add(iid)
                        else:
                            fx_missing_iids.add(iid)
                    else:
                        missing_price_iids.add(iid)
                else:
                    unresolved_keys.add(h.holding_key)
                bump(
                    "constituent",
                    cls.bucket,
                    w,
                    m,
                    label=cls.label,
                    currency=cls.currency,
                    source=cls.price_source,
                    status=cls.status,
                    meta=constituent_meta,
                )
                bump(
                    "constituent_price_status",
                    cls.price_status_bucket,
                    w,
                    m,
                    label=constituent_valuation.price_bucket_label(cls.price_status_bucket),
                    status=cls.status,
                )
                if cls.is_priced and cls.price_source:
                    bump(
                        "constituent_source",
                        cls.price_source,
                        w,
                        m,
                        label=cls.price_source,
                        source=cls.price_source,
                        status=cls.status,
                    )
                bump(
                    "country",
                    h.country or _UNCLASSIFIED,
                    w,
                    m,
                    label=h.country or _UNCLASSIFIED,
                    status="ok" if h.country else "unclassified",
                )
                bump(
                    "sector",
                    h.sector or _UNCLASSIFIED,
                    w,
                    m,
                    label=h.sector or _UNCLASSIFIED,
                    status="ok" if h.sector else "unclassified",
                )
                bump(
                    "industry",
                    h.industry or _UNCLASSIFIED,
                    w,
                    m,
                    label=h.industry or _UNCLASSIFIED,
                    status="ok" if h.industry else "unclassified",
                )
                bump(
                    "holding",
                    h.holding_key,
                    w,
                    m,
                    label=h.security_name,
                    currency=normalise_currency(h.currency, base) if h.currency else None,
                    source=h.source,
                    meta=constituent_meta,
                )
                bump("source", h.source, w, m, label=h.source, source=h.source)
                if h.currency:
                    ccy = normalise_currency(h.currency, base)
                    bump("currency", ccy, w, m, label=ccy, currency=ccy)
                else:
                    bump(
                        "currency",
                        e.local_ccy,
                        w,
                        m,
                        label=e.local_ccy,
                        currency=e.local_ccy,
                        status="approximate",
                    )

            # Un-looked-through remainder (no holdings, or weights summing < 1).
            remainder_w = pos_weight - looked
            if remainder_w > _EPS:
                remainder_mv = e.mv_base - (
                    e.mv_base * (looked / pos_weight if pos_weight else _ZERO)
                )
                has_holdings = bool(holdings)
                uncl_status = "unclassified" if has_holdings else "missing_holdings"
                uncl_label = _UNCLASSIFIED if has_holdings else "Missing holdings"
                for dim in ("country", "sector", "industry"):
                    bump(
                        dim,
                        _UNCLASSIFIED,
                        remainder_w,
                        remainder_mv,
                        label=uncl_label,
                        status=uncl_status,
                    )
                bump(
                    "holding",
                    "__unclassified__",
                    remainder_w,
                    remainder_mv,
                    label=uncl_label,
                    status=uncl_status,
                )
                # Constituent funnel: the remainder is weight we cannot look
                # through to a priced constituent (no holdings, or weights < 1).
                bump(
                    "constituent",
                    constituent_valuation.BUCKET_UNCLASSIFIED,
                    remainder_w,
                    remainder_mv,
                    label=uncl_label,
                    status=uncl_status,
                    meta={"valuation_method": constituent_valuation.METHOD_UNCLASSIFIED},
                )
                bump(
                    "constituent_price_status",
                    constituent_valuation.PRICE_BUCKET_UNCLASSIFIED,
                    remainder_w,
                    remainder_mv,
                    label=uncl_label,
                    status=uncl_status,
                )
                bump(
                    "source",
                    "unclassified",
                    remainder_w,
                    remainder_mv,
                    label=uncl_label,
                    status=uncl_status,
                )
                # Currency: fall back to the listing currency (approximation).
                bump(
                    "currency",
                    e.local_ccy,
                    remainder_w,
                    remainder_mv,
                    label=e.local_ccy,
                    currency=e.local_ccy,
                    status="approximate",
                )

    coverage_weight = (coverage_w if coverage_w <= _ONE else _ONE) if total_mv > 0 else _ZERO
    unclassified_weight = (_ONE - coverage_weight) if total_mv > 0 else _ZERO

    def _cap(value: Decimal) -> Decimal:
        return value if value <= coverage_weight else coverage_weight

    # Constituent coverage is weight-based and nested under holdings coverage:
    # holdings >= identity >= price >= fx (each is a fraction of total value).
    identity_coverage_weight = _cap(identity_cov_w) if total_mv > 0 else _ZERO
    price_coverage_weight = _cap(price_cov_w) if total_mv > 0 else _ZERO
    fx_coverage_weight = _cap(fx_cov_w) if total_mv > 0 else _ZERO

    if total_mv <= 0:
        status = "empty"
    elif missing_fx_count or missing_holdings_count or coverage_weight < _ONE:
        status = "partial"
    else:
        status = "ok"

    # Build rows (sorted by weight desc within each dimension).
    computed_rows: list[ComputedRow] = []
    for dim in DIMENSIONS:
        buckets = sorted(dims[dim].items(), key=lambda kv: kv[1].weight, reverse=True)
        for bucket, b in buckets:
            meta = b.meta
            computed_rows.append(
                ComputedRow(
                    dimension=dim,
                    bucket=bucket,
                    label=b.label,
                    weight=_q(b.weight, _WEIGHT_Q) or _ZERO,
                    market_value_base=_q(b.mv, _MONEY_Q) if b.has_value else None,
                    currency=b.currency,
                    source=b.source,
                    status=b.status,
                    instrument_id=meta.get("instrument_id"),  # type: ignore[arg-type]
                    instrument_listing_id=meta.get("instrument_listing_id"),  # type: ignore[arg-type]
                    fund_id=meta.get("fund_id"),  # type: ignore[arg-type]
                    price_date=meta.get("price_date"),  # type: ignore[arg-type]
                    price_source=meta.get("price_source"),  # type: ignore[arg-type]
                    price_status=meta.get("price_status"),  # type: ignore[arg-type]
                    fx_rate=meta.get("fx_rate"),  # type: ignore[arg-type]
                    fx_source=meta.get("fx_source"),  # type: ignore[arg-type]
                    valuation_method=meta.get("valuation_method"),  # type: ignore[arg-type]
                )
            )

    position_snapshot_hash = _sha(position_inputs)
    holdings_snapshot_hash = _sha(holdings_inputs)
    fx_snapshot_hash = _sha(sorted(fx_inputs))
    input_hash = _sha(
        {
            "base": base,
            "as_of": as_of.isoformat(),
            "source_policy": None,
            "positions": position_snapshot_hash,
            "prices": _sha(sorted(price_inputs)),
            "holdings": holdings_snapshot_hash,
            "fx": fx_snapshot_hash,
            "constituent_prices": _sha(sorted(constituent_price_inputs)),
        }
    )

    return ComputedExposure(
        workspace_id=workspace_id,
        as_of_date=as_of,
        base_currency=base,
        status=status,
        input_hash=input_hash,
        holdings_snapshot_hash=holdings_snapshot_hash,
        fx_snapshot_hash=fx_snapshot_hash,
        position_snapshot_hash=position_snapshot_hash,
        total_market_value_base=_q(total_mv, _MONEY_Q) or _ZERO,
        coverage_weight=_q(coverage_weight, _WEIGHT_Q) or _ZERO,
        unclassified_weight=_q(unclassified_weight, _WEIGHT_Q) or _ZERO,
        missing_holdings_count=missing_holdings_count,
        missing_fx_count=missing_fx_count,
        rows=computed_rows,
        identity_coverage_weight=_q(identity_coverage_weight, _WEIGHT_Q) or _ZERO,
        price_coverage_weight=_q(price_coverage_weight, _WEIGHT_Q) or _ZERO,
        fx_coverage_weight=_q(fx_coverage_weight, _WEIGHT_Q) or _ZERO,
        constituent_count=len(resolved_iids) + len(unresolved_keys),
        resolved_constituent_count=len(resolved_iids),
        priced_constituent_count=len(priced_iids),
        stale_constituent_price_count=len(stale_iids),
        missing_constituent_price_count=len(missing_price_iids),
        constituent_fx_missing_count=len(fx_missing_iids),
    )


# --- idempotent persistence --------------------------------------------------


@dataclass
class WorkspaceExposureResult:
    workspace_id: int
    inserted: int = 0
    unchanged: int = 0
    skipped: int = 0  # no positions -> no snapshot
    snapshot_id: int | None = None
    status: str | None = None


@dataclass
class ExposureRecomputeResult:
    inserted: int = 0
    unchanged: int = 0
    skipped: int = 0
    failed: int = 0
    processed_workspaces: list[int] = field(default_factory=list)
    failures: dict[int, str] = field(default_factory=dict)

    def add(self, res: WorkspaceExposureResult) -> None:
        self.inserted += res.inserted
        self.unchanged += res.unchanged
        self.skipped += res.skipped
        self.processed_workspaces.append(res.workspace_id)


async def get_latest_snapshot(session: AsyncSession, workspace_id: int) -> ExposureSnapshot | None:
    return await session.scalar(
        select(ExposureSnapshot)
        .where(ExposureSnapshot.workspace_id == workspace_id)
        .order_by(ExposureSnapshot.as_of_date.desc(), ExposureSnapshot.id.desc())
        .limit(1)
    )


async def recompute_workspace(session: AsyncSession, workspace_id: int) -> WorkspaceExposureResult:
    """Compute + idempotently persist one workspace's exposure. Does not commit."""
    computed = await compute_exposure(session, workspace_id)
    if computed is None:
        return WorkspaceExposureResult(workspace_id=workspace_id, skipped=1)

    latest = await get_latest_snapshot(session, workspace_id)
    if latest is not None and latest.input_hash == computed.input_hash:
        return WorkspaceExposureResult(
            workspace_id=workspace_id, unchanged=1, snapshot_id=latest.id, status=latest.status
        )
    # Same-day re-emit of a previously-seen hash (e.g. an input revert): respect
    # the unique key and treat as unchanged rather than erroring.
    existing = await session.scalar(
        select(ExposureSnapshot).where(
            ExposureSnapshot.workspace_id == workspace_id,
            ExposureSnapshot.as_of_date == computed.as_of_date,
            ExposureSnapshot.input_hash == computed.input_hash,
        )
    )
    if existing is not None:
        return WorkspaceExposureResult(
            workspace_id=workspace_id, unchanged=1, snapshot_id=existing.id, status=existing.status
        )

    snapshot = ExposureSnapshot(
        workspace_id=workspace_id,
        as_of_date=computed.as_of_date,
        base_currency=computed.base_currency,
        source=EXPOSURE_SOURCE,
        status=computed.status,
        input_hash=computed.input_hash,
        holdings_snapshot_hash=computed.holdings_snapshot_hash,
        fx_snapshot_hash=computed.fx_snapshot_hash,
        position_snapshot_hash=computed.position_snapshot_hash,
        total_market_value_base=computed.total_market_value_base,
        coverage_weight=computed.coverage_weight,
        unclassified_weight=computed.unclassified_weight,
        missing_holdings_count=computed.missing_holdings_count,
        missing_fx_count=computed.missing_fx_count,
        identity_coverage_weight=computed.identity_coverage_weight,
        price_coverage_weight=computed.price_coverage_weight,
        fx_coverage_weight=computed.fx_coverage_weight,
        constituent_count=computed.constituent_count,
        resolved_constituent_count=computed.resolved_constituent_count,
        priced_constituent_count=computed.priced_constituent_count,
        stale_constituent_price_count=computed.stale_constituent_price_count,
        missing_constituent_price_count=computed.missing_constituent_price_count,
        constituent_fx_missing_count=computed.constituent_fx_missing_count,
    )
    session.add(snapshot)
    await session.flush()
    for r in computed.rows:
        session.add(
            ExposureRow(
                exposure_snapshot_id=snapshot.id,
                dimension=r.dimension,
                bucket=r.bucket,
                label=r.label,
                weight=r.weight,
                market_value_base=r.market_value_base,
                currency=r.currency,
                source=r.source,
                status=r.status,
                instrument_id=r.instrument_id,
                instrument_listing_id=r.instrument_listing_id,
                fund_id=r.fund_id,
                price_date=r.price_date,
                price_source=r.price_source,
                price_status=r.price_status,
                fx_rate=r.fx_rate,
                fx_source=r.fx_source,
                valuation_method=r.valuation_method,
            )
        )
    await session.flush()
    return WorkspaceExposureResult(
        workspace_id=workspace_id, inserted=1, snapshot_id=snapshot.id, status=computed.status
    )


async def active_workspace_ids(session: AsyncSession) -> list[int]:
    return list(
        (await session.execute(select(Workspace.id).order_by(Workspace.id))).scalars().all()
    )


# --- read helpers ------------------------------------------------------------


async def get_snapshot(
    session: AsyncSession, workspace_id: int, snapshot_id: int
) -> ExposureSnapshot:
    snapshot = await session.scalar(
        select(ExposureSnapshot).where(
            ExposureSnapshot.id == snapshot_id, ExposureSnapshot.workspace_id == workspace_id
        )
    )
    if snapshot is None:
        raise NotFoundError("Exposure snapshot not found", code="exposure_snapshot_not_found")
    return snapshot


async def list_snapshots(
    session: AsyncSession, workspace_id: int, *, limit: int = 50
) -> list[tuple[ExposureSnapshot, int]]:
    """Return ``(snapshot, row_count)`` newest-first for the snapshots endpoint."""
    stmt = (
        select(ExposureSnapshot, func.count(ExposureRow.id))
        .outerjoin(ExposureRow, ExposureRow.exposure_snapshot_id == ExposureSnapshot.id)
        .where(ExposureSnapshot.workspace_id == workspace_id)
        .group_by(ExposureSnapshot.id)
        .order_by(ExposureSnapshot.as_of_date.desc(), ExposureSnapshot.id.desc())
        .limit(limit)
    )
    return [(snap, count) for snap, count in (await session.execute(stmt)).all()]


async def get_rows(
    session: AsyncSession,
    snapshot_id: int,
    *,
    dimension: str | None = None,
    limit: int | None = None,
) -> list[ExposureRow]:
    stmt = select(ExposureRow).where(ExposureRow.exposure_snapshot_id == snapshot_id)
    if dimension is not None:
        stmt = stmt.where(ExposureRow.dimension == dimension)
    stmt = stmt.order_by(ExposureRow.weight.desc(), ExposureRow.id)
    if limit is not None:
        stmt = stmt.limit(limit)
    return list((await session.execute(stmt)).scalars().all())


def snapshot_age_days(snapshot: ExposureSnapshot, *, now: datetime | None = None) -> int:
    moment = snapshot.created_at
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return ((now or datetime.now(UTC)) - moment).days


# --- response builders (API / dashboard) -------------------------------------


def _row_read(row: ExposureRow | ComputedRow) -> ExposureRowRead:
    return ExposureRowRead(
        dimension=row.dimension,
        bucket=row.bucket,
        label=row.label,
        weight=row.weight,
        market_value_base=row.market_value_base,
        currency=row.currency,
        source=row.source,
        status=row.status,
        instrument_id=row.instrument_id,
        instrument_listing_id=row.instrument_listing_id,
        fund_id=row.fund_id,
        price_date=row.price_date,
        price_source=row.price_source,
        price_status=row.price_status,
        fx_rate=row.fx_rate,
        fx_source=row.fx_source,
        valuation_method=row.valuation_method,
    )


def _coverage_from_snapshot(snapshot: ExposureSnapshot) -> ConstituentCoverage:
    return ConstituentCoverage(
        holdings_coverage_weight=snapshot.coverage_weight,
        identity_coverage_weight=snapshot.identity_coverage_weight,
        price_coverage_weight=snapshot.price_coverage_weight,
        fx_coverage_weight=snapshot.fx_coverage_weight,
        constituent_count=snapshot.constituent_count,
        resolved_constituent_count=snapshot.resolved_constituent_count,
        priced_constituent_count=snapshot.priced_constituent_count,
        stale_constituent_price_count=snapshot.stale_constituent_price_count,
        missing_constituent_price_count=snapshot.missing_constituent_price_count,
        constituent_fx_missing_count=snapshot.constituent_fx_missing_count,
    )


def _coverage_from_computed(computed: ComputedExposure) -> ConstituentCoverage:
    return ConstituentCoverage(
        holdings_coverage_weight=computed.coverage_weight,
        identity_coverage_weight=computed.identity_coverage_weight,
        price_coverage_weight=computed.price_coverage_weight,
        fx_coverage_weight=computed.fx_coverage_weight,
        constituent_count=computed.constituent_count,
        resolved_constituent_count=computed.resolved_constituent_count,
        priced_constituent_count=computed.priced_constituent_count,
        stale_constituent_price_count=computed.stale_constituent_price_count,
        missing_constituent_price_count=computed.missing_constituent_price_count,
        constituent_fx_missing_count=computed.constituent_fx_missing_count,
    )


# Synthetic constituent buckets (unresolved/unclassified aggregates) are excluded
# from "top constituents" so the GUI list shows only real resolved securities.
_SYNTHETIC_CONSTITUENT_BUCKETS = {"__unresolved__", "__unclassified__"}


def _top_constituents(rows: list[ExposureRowRead], n: int) -> list[ExposureRowRead]:
    real = [
        r
        for r in rows
        if r.dimension == "constituent" and r.bucket not in _SYNTHETIC_CONSTITUENT_BUCKETS
    ]
    return real[:n]


def _filter_sort_rows(
    rows: list[ExposureRowRead], dimension: str | None, limit: int | None
) -> list[ExposureRowRead]:
    if dimension is not None:
        rows = [r for r in rows if r.dimension == dimension]
        if limit is not None:
            rows = rows[:limit]
    return rows


async def build_response(
    session: AsyncSession,
    workspace_id: int,
    *,
    dimension: str | None = None,
    snapshot_id: int | None = None,
    limit: int | None = None,
) -> ExposureSnapshotResponse:
    """Read exposure for the API: a specific snapshot, the latest, or on-the-fly.

    Falls back to an on-the-fly computation (``cached=False``) when no snapshot
    exists yet, so the GUI still gets data but can flag that a recompute is due.
    """
    workspace = await workspaces_service.get_workspace(session, workspace_id)

    snapshot: ExposureSnapshot | None
    if snapshot_id is not None:
        snapshot = await get_snapshot(session, workspace_id, snapshot_id)
    else:
        snapshot = await get_latest_snapshot(session, workspace_id)

    if snapshot is not None:
        rows = [_row_read(r) for r in await get_rows(session, snapshot.id)]
        rows = _filter_sort_rows(rows, dimension, limit)
        return ExposureSnapshotResponse(
            workspace_id=workspace_id,
            snapshot_id=snapshot.id,
            as_of_date=snapshot.as_of_date,
            base_currency=snapshot.base_currency,
            source=snapshot.source,
            status=snapshot.status,
            total_market_value_base=snapshot.total_market_value_base,
            coverage_weight=snapshot.coverage_weight,
            unclassified_weight=snapshot.unclassified_weight,
            missing_holdings_count=snapshot.missing_holdings_count,
            missing_fx_count=snapshot.missing_fx_count,
            constituent_coverage=_coverage_from_snapshot(snapshot),
            created_at=snapshot.created_at,
            cached=True,
            dimensions=sorted({r.dimension for r in rows}),
            rows=rows,
        )

    # No cached snapshot — compute on the fly and flag it.
    computed = await compute_exposure(session, workspace_id)
    if computed is None:
        return ExposureSnapshotResponse(
            workspace_id=workspace_id,
            base_currency=workspace.base_currency,
            source=EXPOSURE_SOURCE,
            status="empty",
            cached=False,
        )
    rows = _filter_sort_rows([_row_read(r) for r in computed.rows], dimension, limit)
    return ExposureSnapshotResponse(
        workspace_id=workspace_id,
        snapshot_id=None,
        as_of_date=computed.as_of_date,
        base_currency=computed.base_currency,
        source=EXPOSURE_SOURCE,
        status="recompute_needed",
        total_market_value_base=computed.total_market_value_base,
        coverage_weight=computed.coverage_weight,
        unclassified_weight=computed.unclassified_weight,
        missing_holdings_count=computed.missing_holdings_count,
        missing_fx_count=computed.missing_fx_count,
        constituent_coverage=_coverage_from_computed(computed),
        created_at=None,
        cached=False,
        dimensions=sorted({r.dimension for r in computed.rows}),
        rows=rows,
    )


def _top(rows: list[ExposureRowRead], dimension: str, n: int) -> list[ExposureRowRead]:
    return [r for r in rows if r.dimension == dimension][:n]


async def build_dashboard_block(
    session: AsyncSession, workspace_id: int, *, top_n: int = 5
) -> ExposureDashboardBlock:
    """Compact exposure block for the dashboard from the latest snapshot.

    Falls back to an on-the-fly computation (status ``recompute_needed``) when no
    snapshot exists; ``missing`` when the workspace has no positions at all.
    """
    workspace = await workspaces_service.get_workspace(session, workspace_id)
    snapshot = await get_latest_snapshot(session, workspace_id)

    if snapshot is not None:
        rows = [_row_read(r) for r in await get_rows(session, snapshot.id)]
        age = snapshot_age_days(snapshot)
        status = "stale" if age > alert_rules.EXPOSURE_STALE_DAYS else "cached"
        return ExposureDashboardBlock(
            status=status,
            cached=True,
            snapshot_id=snapshot.id,
            as_of_date=snapshot.as_of_date,
            age_days=age,
            base_currency=snapshot.base_currency,
            total_market_value_base=snapshot.total_market_value_base,
            coverage_weight=snapshot.coverage_weight,
            unclassified_weight=snapshot.unclassified_weight,
            missing_holdings_count=snapshot.missing_holdings_count,
            missing_fx_count=snapshot.missing_fx_count,
            constituent_coverage=_coverage_from_snapshot(snapshot),
            top_sectors=_top(rows, "sector", top_n),
            top_countries=_top(rows, "country", top_n),
            top_currencies=_top(rows, "currency", top_n),
            top_holdings=_top(rows, "holding", top_n),
            top_constituents=_top_constituents(rows, top_n),
        )

    computed = await compute_exposure(session, workspace_id)
    if computed is None:
        return ExposureDashboardBlock(
            status="missing", cached=False, base_currency=workspace.base_currency
        )
    rows = [_row_read(r) for r in computed.rows]
    return ExposureDashboardBlock(
        status="recompute_needed",
        cached=False,
        snapshot_id=None,
        as_of_date=computed.as_of_date,
        age_days=None,
        base_currency=computed.base_currency,
        total_market_value_base=computed.total_market_value_base,
        coverage_weight=computed.coverage_weight,
        unclassified_weight=computed.unclassified_weight,
        missing_holdings_count=computed.missing_holdings_count,
        missing_fx_count=computed.missing_fx_count,
        constituent_coverage=_coverage_from_computed(computed),
        top_sectors=_top(rows, "sector", top_n),
        top_countries=_top(rows, "country", top_n),
        top_currencies=_top(rows, "currency", top_n),
        top_holdings=_top(rows, "holding", top_n),
        top_constituents=_top_constituents(rows, top_n),
    )


def summarize_snapshot(snapshot: ExposureSnapshot, row_count: int) -> ExposureSnapshotSummary:
    return ExposureSnapshotSummary(
        snapshot_id=snapshot.id,
        workspace_id=snapshot.workspace_id,
        as_of_date=snapshot.as_of_date,
        base_currency=snapshot.base_currency,
        source=snapshot.source,
        status=snapshot.status,
        input_hash=snapshot.input_hash,
        total_market_value_base=snapshot.total_market_value_base,
        coverage_weight=snapshot.coverage_weight,
        unclassified_weight=snapshot.unclassified_weight,
        missing_holdings_count=snapshot.missing_holdings_count,
        missing_fx_count=snapshot.missing_fx_count,
        constituent_coverage=_coverage_from_snapshot(snapshot),
        created_at=snapshot.created_at,
        row_count=row_count,
    )
