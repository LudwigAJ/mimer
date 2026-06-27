"""Portfolio valuation/readiness snapshots — bounded, cacheable read model.

This sits one layer above the broker-import *position reconciliation*
(``app/services/broker_imports.py``: net quantity per instrument; cash per
currency) and joins it to the **latest already-ingested** fund/instrument price +
FX (at/before ``as_of_date``) to answer, for a workspace:

    What positions do I hold?
    Which have a latest price?  Which have the required FX?
    What is the latest market-value *context* per position, in base currency?
    Which rows are unresolved / unpriced / missing FX (and why)?

It is a bounded SQL-backed valuation/readiness context, **NOT**:

  * PnL / realised / unrealised gain
  * tax lots
  * total return / IRR
  * performance attribution
  * a local pricer

(those live in the Rust GUI / local pricer — see the AGENTS.md compute boundary).

Safety guarantees (do not regress):

  * **No live fetching.** It consumes already-ingested ``prices`` / ``instrument_prices``
    / ``fx_rates`` only — it never calls a price/FX source or an identity resolver.
  * **No invention.** A value that cannot be computed safely (no price, no FX,
    unresolved/ambiguous instrument) is reported as a *blocker*, never a guessed
    number.
  * **Idempotent + bounded.** ``input_hash`` keys on the reconciled positions/cash
    plus every price/FX used, so an unchanged input set re-values to the same hash
    and writes nothing; a new price/FX (or a (re)resolution) yields a new snapshot.
    The working set is bounded by ``limit``.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFoundError
from app.db.models import (
    FundListing,
    InstrumentListing,
    PortfolioTransaction,
    PortfolioValuationRow,
    PortfolioValuationSnapshot,
    Price,
)
from app.schemas.portfolio_valuation import (
    PortfolioValuationBrokerAccountSummary,
    PortfolioValuationCoverage,
    PortfolioValuationDashboardBlock,
    PortfolioValuationHistory,
    PortfolioValuationHistoryPoint,
    PortfolioValuationResponse,
    PortfolioValuationRowRead,
    PortfolioValuationSummary,
    PortfolioValuationSummaryResponse,
)
from app.services import broker_imports as broker_service
from app.services import freshness as freshness_service
from app.services import instrument_prices as instrument_prices_service
from app.services import workspaces as workspaces_service
from app.services.conversion import normalise_currency
from app.services.fx import MISSING, FxIndex, load_fx_index, normalise_pence

SOURCE = "portfolio_valuation"
DEFAULT_LIMIT = 1000
MAX_LIMIT = 5000
_MONEY_Q = Decimal("0.01")
_ZERO = Decimal("0")
_ONE = Decimal("1")

FRESH = freshness_service.FRESH
STALE = freshness_service.STALE

# Readiness rollup (GUI badge).
READY = "ready"
BLOCKED = "blocked"
STALE_READY = "stale"
CASH = "cash"

# Snapshot-level readiness rollup (history/summary/dashboard) — distinct from the
# per-row vocabulary above (which has no "partial"/"empty").
READINESS_READY = "ready"
READINESS_PARTIAL = "partial"
READINESS_BLOCKED = "blocked"
READINESS_STALE = "stale"
READINESS_EMPTY = "empty"

# Bounds for the read-only history/summary series (most-recent window).
HISTORY_DEFAULT_LIMIT = 250
HISTORY_MAX_LIMIT = 500
_COVERAGE_Q = Decimal("0.0001")


def _q(value: Decimal | None) -> Decimal | None:
    return None if value is None else value.quantize(_MONEY_Q, rounding=ROUND_HALF_UP)


def _dec(value: Decimal | None) -> str:
    """Canonical Decimal string for hashing (scale-stable; see exposure_recompute)."""
    if value is None:
        return ""
    return f"{value.normalize():f}"


def _sha(payload: Any) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str, separators=(",", ":")).encode()
    ).hexdigest()


# --- computed (pre-persist) value carriers -----------------------------------


@dataclass
class _ValRow:
    position_key: str
    position_type: str
    quantity: Decimal
    fund_id: int | None = None
    fund_listing_id: int | None = None
    instrument_id: int | None = None
    instrument_listing_id: int | None = None
    symbol: str | None = None
    isin: str | None = None
    name: str | None = None
    local_currency: str | None = None
    base_currency: str | None = None
    latest_price: Decimal | None = None
    latest_price_date: date | None = None
    latest_price_source: str | None = None
    latest_price_status: str | None = None
    fx_rate_to_base: Decimal | None = None
    fx_rate_date: date | None = None
    fx_rate_source: str | None = None
    fx_status: str | None = None
    market_value_local: Decimal | None = None
    market_value_base: Decimal | None = None
    valuation_status: str = "valued"
    readiness_status: str = READY
    source: str | None = None
    blocking_reasons: list[str] = field(default_factory=list)


@dataclass
class ComputedValuation:
    workspace_id: int
    as_of_date: date
    base_currency: str
    broker_account_id: int | None
    status: str
    input_hash: str
    rows: list[_ValRow]
    positions_selected: int = 0
    positions_valued: int = 0
    missing_price: int = 0
    missing_fx: int = 0
    unresolved: int = 0
    ambiguous: int = 0
    cash_rows: int = 0
    stale_price: int = 0
    stale_fx: int = 0
    total_market_value_base: Decimal | None = None


@dataclass
class PortfolioValuationResult:
    workspace_id: int
    base_currency: str
    as_of_date: date
    broker_account_id: int | None = None
    status: str = "empty"
    snapshot_id: int | None = None
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

    def message(self) -> str:
        action = (
            "created"
            if self.snapshot_created
            else "updated"
            if self.snapshot_updated
            else "skipped"
        )
        return (
            f"snapshot={action} as_of={self.as_of_date.isoformat()} base={self.base_currency} "
            f"selected={self.positions_selected} valued={self.positions_valued} "
            f"missing_price={self.missing_price} missing_fx={self.missing_fx} "
            f"unresolved={self.unresolved} ambiguous={self.ambiguous} "
            f"cash_rows={self.cash_rows} stale_price={self.stale_price} stale_fx={self.stale_fx}"
        )


# --- price lookups (latest at/before as_of; existing data only, no fetching) --


async def _fund_listing_prices_asof(
    session: AsyncSession, listing_ids: set[int], as_of: date
) -> dict[int, Price]:
    """Latest stored fund-listing ``Price`` on/before ``as_of`` per listing.

    Two bounded queries (max-date GROUP BY + the matching rows) — SQLite +
    Postgres compatible. On a tied date with several sources, ``manual`` wins, else
    the lowest id, deterministically."""
    if not listing_ids:
        return {}
    max_dates = (
        select(
            Price.fund_listing_id.label("lid"),
            func.max(Price.price_date).label("d"),
        )
        .where(Price.fund_listing_id.in_(listing_ids), Price.price_date <= as_of)
        .group_by(Price.fund_listing_id)
    ).subquery()
    rows = (
        (
            await session.execute(
                select(Price)
                .join(
                    max_dates,
                    (Price.fund_listing_id == max_dates.c.lid)
                    & (Price.price_date == max_dates.c.d),
                )
                .order_by(Price.id.asc())
            )
        )
        .scalars()
        .all()
    )
    chosen: dict[int, Price] = {}
    for row in rows:
        existing = chosen.get(row.fund_listing_id)
        if existing is None or (row.source == "manual" and existing.source != "manual"):
            chosen[row.fund_listing_id] = row
    return chosen


async def _primary_listing_for_instruments(
    session: AsyncSession, instrument_ids: set[int]
) -> dict[int, InstrumentListing]:
    """Lowest-id tradable listing per instrument (deterministic primary pick)."""
    if not instrument_ids:
        return {}
    rows = (
        (
            await session.execute(
                select(InstrumentListing)
                .where(InstrumentListing.instrument_id.in_(instrument_ids))
                .order_by(InstrumentListing.id.asc())
            )
        )
        .scalars()
        .all()
    )
    primary: dict[int, InstrumentListing] = {}
    for listing in rows:
        primary.setdefault(listing.instrument_id, listing)
    return primary


async def _single_listing_for_funds(
    session: AsyncSession, fund_ids: set[int]
) -> dict[int, FundListing]:
    """A fund's listing iff it has exactly one (never guess which of many a user holds)."""
    if not fund_ids:
        return {}
    rows = (
        (
            await session.execute(
                select(FundListing)
                .where(FundListing.fund_id.in_(fund_ids))
                .order_by(FundListing.id.asc())
            )
        )
        .scalars()
        .all()
    )
    by_fund: dict[int, list[FundListing]] = {}
    for listing in rows:
        by_fund.setdefault(listing.fund_id, []).append(listing)
    return {fid: lst[0] for fid, lst in by_fund.items() if len(lst) == 1}


@dataclass
class _PriceCtx:
    value: Decimal
    currency: str | None
    price_date: date
    source: str | None


def _price_ctx_from_fund(price: Price | None) -> _PriceCtx | None:
    if price is None:
        return None
    return _PriceCtx(price.price, price.currency, price.price_date, price.source)


def _price_ctx_from_instrument(price) -> _PriceCtx | None:  # type: ignore[no-untyped-def]
    if price is None:
        return None
    return _PriceCtx(price.close, price.currency, price.price_date, price.source)


# --- core computation --------------------------------------------------------


def _ambiguous_keys(txns: list[PortfolioTransaction]) -> set[str]:
    return {
        broker_service.transaction_position_key(t)
        for t in txns
        if t.status == "ambiguous_instrument"
    }


def _value_in_base(
    fx_index: FxIndex, mv_local: Decimal | None, local_ccy: str, base: str, as_of: date
) -> tuple[Decimal | None, date | None, str | None, str, Decimal | None]:
    """(fx_rate, fx_date, fx_source, fx_status, mv_base) for ``local_ccy`` -> base."""
    if local_ccy == base:
        return _ONE, None, None, "same_currency", mv_local
    res = fx_index.get_fx_rate(local_ccy, base, as_of_date=as_of)
    if res.status == MISSING or res.rate is None:
        return None, None, None, MISSING, None
    mv_base = (mv_local * res.rate) if mv_local is not None else None
    return res.rate, res.rate_date, res.source, res.status, mv_base


def _finalise_priced_row(row: _ValRow, *, price_state: str) -> None:
    """Set valuation/readiness/blocking from the resolved price + FX state."""
    fx_state = row.fx_status
    if fx_state == MISSING:
        row.valuation_status = "missing_fx"
        row.readiness_status = BLOCKED
        row.blocking_reasons = ["missing_fx"]
        return
    if price_state == STALE:
        row.valuation_status = "stale_price"
        row.readiness_status = STALE_READY
        row.blocking_reasons = ["stale_price"]
    elif fx_state == STALE:
        row.valuation_status = "stale_fx"
        row.readiness_status = STALE_READY
        row.blocking_reasons = ["stale_fx"]
    else:
        row.valuation_status = "valued"
        row.readiness_status = READY


async def compute_valuation(
    session: AsyncSession,
    workspace_id: int,
    *,
    as_of_date: date | None = None,
    base_currency: str | None = None,
    broker_account_id: int | None = None,
    limit: int = DEFAULT_LIMIT,
) -> ComputedValuation:
    """Compute (but do not persist) the valuation/readiness rows for a workspace.

    Read-only. Uses the existing reconciliation + the latest already-ingested
    prices/FX. Never fetches anything and never resolves identity."""
    workspace = await workspaces_service.get_workspace(session, workspace_id)
    base = (base_currency or workspace.base_currency).upper()
    as_of = as_of_date or date.today()
    limit = max(1, min(limit, MAX_LIMIT))

    txns = await broker_service.committed_transactions(
        session, workspace_id, broker_account_id=broker_account_id
    )
    rec = broker_service.reconcile_transactions(txns)
    ambiguous = _ambiguous_keys(txns)
    fx_index = await load_fx_index(session)

    positions = rec.positions[:limit]

    # Batch the price lookups by link kind so we never query per row.
    fund_listing_ids = {p.fund_listing_id for p in positions if p.fund_listing_id is not None}
    instrument_listing_ids = {
        p.instrument_listing_id for p in positions if p.instrument_listing_id is not None
    }
    instrument_only_ids = {
        p.instrument_id
        for p in positions
        if p.instrument_id is not None and p.instrument_listing_id is None
    }
    fund_only_ids = {
        p.fund_id for p in positions if p.fund_id is not None and p.fund_listing_id is None
    }

    primary_for_instrument = await _primary_listing_for_instruments(session, instrument_only_ids)
    single_for_fund = await _single_listing_for_funds(session, fund_only_ids)
    instrument_listing_ids |= {ln.id for ln in primary_for_instrument.values()}
    fund_listing_ids |= {ln.id for ln in single_for_fund.values()}

    fund_prices = await _fund_listing_prices_asof(session, fund_listing_ids, as_of)
    instrument_prices = await instrument_prices_service.prices_asof_for_listings(
        session, list(instrument_listing_ids), as_of
    )

    rows: list[_ValRow] = []
    total_mv = _ZERO
    has_value = False

    for agg in positions:
        row = _ValRow(
            position_key=agg.key,
            position_type="unresolved",
            quantity=agg.quantity,
            fund_id=agg.fund_id,
            fund_listing_id=agg.fund_listing_id,
            instrument_id=agg.instrument_id,
            instrument_listing_id=agg.instrument_listing_id,
            symbol=agg.symbol,
            isin=agg.isin,
            name=agg.name,
            base_currency=base,
            source=SOURCE,
        )
        linked = any(
            v is not None
            for v in (
                agg.fund_listing_id,
                agg.instrument_listing_id,
                agg.instrument_id,
                agg.fund_id,
            )
        )

        if not linked:
            if agg.key in ambiguous:
                row.position_type = "ambiguous"
                row.valuation_status = "ambiguous_instrument"
                row.blocking_reasons = ["ambiguous_instrument"]
            else:
                row.position_type = "unresolved"
                row.valuation_status = "unresolved_instrument"
                row.blocking_reasons = ["unresolved_instrument"]
            row.readiness_status = BLOCKED
            row.local_currency = normalise_currency(agg.currency, base)
            rows.append(row)
            continue

        if agg.quantity == _ZERO:
            row.position_type = _position_type(agg)
            row.valuation_status = "zero_quantity"
            row.readiness_status = READY
            row.local_currency = normalise_currency(agg.currency, base)
            row.market_value_local = _ZERO
            row.market_value_base = _ZERO
            rows.append(row)
            continue

        row.position_type = _position_type(agg)
        price_ctx = _resolve_price_ctx(
            agg,
            fund_prices=fund_prices,
            instrument_prices=instrument_prices,
            primary_for_instrument=primary_for_instrument,
            single_for_fund=single_for_fund,
        )
        if price_ctx is None:
            row.valuation_status = "missing_price"
            row.readiness_status = BLOCKED
            row.latest_price_status = MISSING
            row.blocking_reasons = ["missing_price"]
            row.local_currency = normalise_currency(agg.currency, base)
            rows.append(row)
            continue

        norm_price, local_ccy = normalise_pence(price_ctx.value, price_ctx.currency)
        local_ccy = local_ccy or normalise_currency(agg.currency, base) or base
        mv_local = (agg.quantity * norm_price) if norm_price is not None else None
        price_state = freshness_service.freshness_state(price_ctx.price_date, kind="price")

        row.latest_price = norm_price
        row.latest_price_date = price_ctx.price_date
        row.latest_price_source = price_ctx.source
        row.latest_price_status = price_state
        row.local_currency = local_ccy
        row.market_value_local = _q(mv_local)

        fx_rate, fx_date, fx_source, fx_state, mv_base = _value_in_base(
            fx_index, mv_local, local_ccy, base, as_of
        )
        row.fx_rate_to_base = fx_rate
        row.fx_rate_date = fx_date
        row.fx_rate_source = fx_source
        row.fx_status = fx_state
        row.market_value_base = _q(mv_base)
        _finalise_priced_row(row, price_state=price_state)
        if row.market_value_base is not None:
            total_mv += row.market_value_base
            has_value = True
        rows.append(row)

    # --- cash rows (signed net flow per currency) -----------------------------
    cash_count = 0
    for currency, bucket in sorted(rec.cash.items()):
        amount = Decimal(bucket[0])
        local_ccy = normalise_currency(currency, base) or (currency or base).upper()
        row = _ValRow(
            position_key=f"cash:{local_ccy}",
            position_type="cash",
            quantity=amount,
            local_currency=local_ccy,
            base_currency=base,
            market_value_local=_q(amount),
            readiness_status=CASH,
            source=SOURCE,
        )
        fx_rate, fx_date, fx_source, fx_state, mv_base = _value_in_base(
            fx_index, amount, local_ccy, base, as_of
        )
        row.fx_rate_to_base = fx_rate
        row.fx_rate_date = fx_date
        row.fx_rate_source = fx_source
        row.fx_status = fx_state
        row.market_value_base = _q(mv_base)
        if fx_state == MISSING:
            row.valuation_status = "missing_fx"
            row.readiness_status = BLOCKED
            row.blocking_reasons = ["missing_fx"]
        else:
            row.valuation_status = "cash_only"
            if row.market_value_base is not None:
                total_mv += row.market_value_base
                has_value = True
        rows.append(row)
        cash_count += 1

    # --- counts + status ------------------------------------------------------
    non_cash = [r for r in rows if r.position_type != "cash"]
    valued = sum(1 for r in rows if r.valuation_status in ("valued", "stale_price", "stale_fx"))
    missing_price = sum(1 for r in rows if r.valuation_status == "missing_price")
    missing_fx = sum(1 for r in rows if r.valuation_status == "missing_fx")
    unresolved = sum(1 for r in rows if r.valuation_status == "unresolved_instrument")
    ambiguous_n = sum(1 for r in rows if r.valuation_status == "ambiguous_instrument")
    stale_price = sum(1 for r in rows if r.valuation_status == "stale_price")
    stale_fx = sum(1 for r in rows if r.valuation_status == "stale_fx")

    if not rows:
        status = "empty"
    elif missing_price or missing_fx or unresolved or ambiguous_n or stale_price or stale_fx:
        status = "partial"
    else:
        status = "ok"

    # --- deterministic input hash --------------------------------------------
    row_inputs = sorted(
        (
            r.position_key,
            r.position_type,
            _dec(r.quantity),
            r.local_currency or "",
            _dec(r.latest_price),
            r.latest_price_date.isoformat() if r.latest_price_date else "",
            r.latest_price_source or "",
            _dec(r.fx_rate_to_base),
            r.fx_rate_date.isoformat() if r.fx_rate_date else "",
            r.fx_rate_source or "",
            r.valuation_status,
        )
        for r in rows
    )
    input_hash = _sha(
        {
            "base": base,
            "as_of": as_of.isoformat(),
            "broker_account_id": broker_account_id or 0,
            "rows": row_inputs,
        }
    )

    return ComputedValuation(
        workspace_id=workspace_id,
        as_of_date=as_of,
        base_currency=base,
        broker_account_id=broker_account_id,
        status=status,
        input_hash=input_hash,
        rows=rows,
        positions_selected=len(non_cash),
        positions_valued=valued,
        missing_price=missing_price,
        missing_fx=missing_fx,
        unresolved=unresolved,
        ambiguous=ambiguous_n,
        cash_rows=cash_count,
        stale_price=stale_price,
        stale_fx=stale_fx,
        total_market_value_base=_q(total_mv) if has_value else None,
    )


def _position_type(agg) -> str:  # type: ignore[no-untyped-def]
    if agg.fund_listing_id is not None:
        return "fund_listing"
    if agg.instrument_listing_id is not None:
        return "instrument_listing"
    if agg.instrument_id is not None:
        return "instrument"
    if agg.fund_id is not None:
        return "fund"
    return "unresolved"


def _resolve_price_ctx(
    agg,  # type: ignore[no-untyped-def]
    *,
    fund_prices: dict[int, Price],
    instrument_prices: dict[int, Any],
    primary_for_instrument: dict[int, InstrumentListing],
    single_for_fund: dict[int, FundListing],
) -> _PriceCtx | None:
    if agg.fund_listing_id is not None:
        return _price_ctx_from_fund(fund_prices.get(agg.fund_listing_id))
    if agg.instrument_listing_id is not None:
        return _price_ctx_from_instrument(instrument_prices.get(agg.instrument_listing_id))
    if agg.instrument_id is not None:
        listing = primary_for_instrument.get(agg.instrument_id)
        if listing is not None:
            return _price_ctx_from_instrument(instrument_prices.get(listing.id))
        return None
    if agg.fund_id is not None:
        listing = single_for_fund.get(agg.fund_id)
        if listing is not None:
            return _price_ctx_from_fund(fund_prices.get(listing.id))
        return None
    return None


# --- idempotent persistence --------------------------------------------------


async def get_latest_snapshot(
    session: AsyncSession, workspace_id: int, *, broker_account_id: int | None = None
) -> PortfolioValuationSnapshot | None:
    stmt = (
        select(PortfolioValuationSnapshot)
        .where(PortfolioValuationSnapshot.workspace_id == workspace_id)
        .order_by(
            PortfolioValuationSnapshot.as_of_date.desc(), PortfolioValuationSnapshot.id.desc()
        )
        .limit(1)
    )
    if broker_account_id is not None:
        stmt = stmt.where(PortfolioValuationSnapshot.broker_account_id == broker_account_id)
    return await session.scalar(stmt)


def _row_model(snapshot_id: int, r: _ValRow) -> PortfolioValuationRow:
    payload = {"blocking_reasons": r.blocking_reasons} if r.blocking_reasons else None
    return PortfolioValuationRow(
        snapshot_id=snapshot_id,
        position_key=r.position_key,
        position_type=r.position_type,
        fund_id=r.fund_id,
        fund_listing_id=r.fund_listing_id,
        instrument_id=r.instrument_id,
        instrument_listing_id=r.instrument_listing_id,
        symbol=r.symbol,
        isin=r.isin,
        name=r.name,
        quantity=r.quantity,
        local_currency=r.local_currency,
        base_currency=r.base_currency,
        latest_price=r.latest_price,
        latest_price_date=r.latest_price_date,
        latest_price_source=r.latest_price_source,
        latest_price_status=r.latest_price_status,
        fx_rate_to_base=r.fx_rate_to_base,
        fx_rate_date=r.fx_rate_date,
        fx_rate_source=r.fx_rate_source,
        fx_status=r.fx_status,
        market_value_local=_q(r.market_value_local),
        market_value_base=_q(r.market_value_base),
        valuation_status=r.valuation_status,
        readiness_status=r.readiness_status,
        source=r.source,
        status="ok",
        raw_payload_json=payload,
    )


def _add_rows(session: AsyncSession, snapshot_id: int, rows: list[_ValRow]) -> None:
    for r in rows:
        session.add(_row_model(snapshot_id, r))


def _result_from_computed(
    computed: ComputedValuation, snapshot: PortfolioValuationSnapshot
) -> PortfolioValuationResult:
    return PortfolioValuationResult(
        workspace_id=computed.workspace_id,
        base_currency=computed.base_currency,
        as_of_date=computed.as_of_date,
        broker_account_id=computed.broker_account_id,
        status=computed.status,
        snapshot_id=snapshot.id,
        positions_selected=computed.positions_selected,
        positions_valued=computed.positions_valued,
        missing_price=computed.missing_price,
        missing_fx=computed.missing_fx,
        unresolved=computed.unresolved,
        ambiguous=computed.ambiguous,
        cash_rows=computed.cash_rows,
        stale_price=computed.stale_price,
        stale_fx=computed.stale_fx,
    )


def _new_snapshot(computed: ComputedValuation) -> PortfolioValuationSnapshot:
    return PortfolioValuationSnapshot(
        workspace_id=computed.workspace_id,
        as_of_date=computed.as_of_date,
        base_currency=computed.base_currency,
        broker_account_id=computed.broker_account_id,
        source=SOURCE,
        status=computed.status,
        input_hash=computed.input_hash,
        positions_selected=computed.positions_selected,
        positions_valued=computed.positions_valued,
        missing_price_count=computed.missing_price,
        missing_fx_count=computed.missing_fx,
        unresolved_count=computed.unresolved,
        ambiguous_count=computed.ambiguous,
        stale_price_count=computed.stale_price,
        stale_fx_count=computed.stale_fx,
        cash_row_count=computed.cash_rows,
        total_market_value_base=computed.total_market_value_base,
    )


async def recompute_portfolio_valuation_snapshot(
    session: AsyncSession,
    workspace_id: int,
    *,
    as_of_date: date | None = None,
    base_currency: str | None = None,
    broker_account_id: int | None = None,
    limit: int = DEFAULT_LIMIT,
    force: bool = False,
) -> PortfolioValuationResult:
    """Compute + idempotently persist one workspace's valuation snapshot.

    Idempotent like ``exposure_recompute``: an unchanged input set (same
    ``input_hash``) writes nothing (``snapshot_skipped``); a changed set inserts a
    new snapshot (``snapshot_created``). ``force`` refreshes the existing snapshot's
    rows in place when the hash is unchanged (``snapshot_updated``). Does not
    commit — the caller (worker / API) owns the transaction boundary."""
    computed = await compute_valuation(
        session,
        workspace_id,
        as_of_date=as_of_date,
        base_currency=base_currency,
        broker_account_id=broker_account_id,
        limit=limit,
    )

    existing = await session.scalar(
        select(PortfolioValuationSnapshot).where(
            PortfolioValuationSnapshot.workspace_id == workspace_id,
            PortfolioValuationSnapshot.as_of_date == computed.as_of_date,
            PortfolioValuationSnapshot.input_hash == computed.input_hash,
        )
    )
    if existing is not None:
        result = _result_from_computed(computed, existing)
        if not force:
            result.snapshot_skipped = True
            return result
        # Forced recompute of an unchanged input set: refresh the rows in place.
        await session.execute(
            delete(PortfolioValuationRow).where(PortfolioValuationRow.snapshot_id == existing.id)
        )
        await session.flush()
        existing.status = computed.status
        existing.positions_selected = computed.positions_selected
        existing.positions_valued = computed.positions_valued
        existing.missing_price_count = computed.missing_price
        existing.missing_fx_count = computed.missing_fx
        existing.unresolved_count = computed.unresolved
        existing.ambiguous_count = computed.ambiguous
        existing.stale_price_count = computed.stale_price
        existing.stale_fx_count = computed.stale_fx
        existing.cash_row_count = computed.cash_rows
        existing.total_market_value_base = computed.total_market_value_base
        _add_rows(session, existing.id, computed.rows)
        await session.flush()
        result.snapshot_updated = True
        return result

    snapshot = _new_snapshot(computed)
    session.add(snapshot)
    await session.flush()
    _add_rows(session, snapshot.id, computed.rows)
    await session.flush()
    result = _result_from_computed(computed, snapshot)
    result.snapshot_created = True
    return result


# --- read helpers (API / dashboard) ------------------------------------------


def _summary_from_snapshot(snapshot: PortfolioValuationSnapshot) -> PortfolioValuationSummary:
    return PortfolioValuationSummary(
        positions_selected=snapshot.positions_selected,
        positions_valued=snapshot.positions_valued,
        missing_price=snapshot.missing_price_count,
        missing_fx=snapshot.missing_fx_count,
        unresolved=snapshot.unresolved_count,
        ambiguous=snapshot.ambiguous_count,
        cash_rows=snapshot.cash_row_count,
        stale_price=snapshot.stale_price_count,
        stale_fx=snapshot.stale_fx_count,
        total_market_value_base=snapshot.total_market_value_base,
    )


def _summary_from_computed(computed: ComputedValuation) -> PortfolioValuationSummary:
    return PortfolioValuationSummary(
        positions_selected=computed.positions_selected,
        positions_valued=computed.positions_valued,
        missing_price=computed.missing_price,
        missing_fx=computed.missing_fx,
        unresolved=computed.unresolved,
        ambiguous=computed.ambiguous,
        cash_rows=computed.cash_rows,
        stale_price=computed.stale_price,
        stale_fx=computed.stale_fx,
        total_market_value_base=computed.total_market_value_base,
    )


def _row_read_from_model(row: PortfolioValuationRow) -> PortfolioValuationRowRead:
    payload = row.raw_payload_json or {}
    reasons = payload.get("blocking_reasons", []) if isinstance(payload, dict) else []
    return PortfolioValuationRowRead(
        position_key=row.position_key,
        position_type=row.position_type,
        fund_id=row.fund_id,
        fund_listing_id=row.fund_listing_id,
        instrument_id=row.instrument_id,
        instrument_listing_id=row.instrument_listing_id,
        symbol=row.symbol,
        isin=row.isin,
        name=row.name,
        quantity=row.quantity,
        local_currency=row.local_currency,
        base_currency=row.base_currency,
        latest_price=row.latest_price,
        latest_price_date=row.latest_price_date,
        latest_price_source=row.latest_price_source,
        latest_price_status=row.latest_price_status,
        fx_rate_to_base=row.fx_rate_to_base,
        fx_rate_date=row.fx_rate_date,
        fx_rate_source=row.fx_rate_source,
        fx_status=row.fx_status,
        market_value_local=row.market_value_local,
        market_value_base=row.market_value_base,
        valuation_status=row.valuation_status,
        readiness_status=row.readiness_status,
        blocking_reasons=list(reasons),
        source=row.source,
    )


def _row_read_from_computed(r: _ValRow) -> PortfolioValuationRowRead:
    return PortfolioValuationRowRead(
        position_key=r.position_key,
        position_type=r.position_type,
        fund_id=r.fund_id,
        fund_listing_id=r.fund_listing_id,
        instrument_id=r.instrument_id,
        instrument_listing_id=r.instrument_listing_id,
        symbol=r.symbol,
        isin=r.isin,
        name=r.name,
        quantity=r.quantity,
        local_currency=r.local_currency,
        base_currency=r.base_currency,
        latest_price=r.latest_price,
        latest_price_date=r.latest_price_date,
        latest_price_source=r.latest_price_source,
        latest_price_status=r.latest_price_status,
        fx_rate_to_base=r.fx_rate_to_base,
        fx_rate_date=r.fx_rate_date,
        fx_rate_source=r.fx_rate_source,
        fx_status=r.fx_status,
        market_value_local=_q(r.market_value_local),
        market_value_base=_q(r.market_value_base),
        valuation_status=r.valuation_status,
        readiness_status=r.readiness_status,
        blocking_reasons=list(r.blocking_reasons),
        source=r.source,
    )


async def get_snapshot_rows(
    session: AsyncSession,
    snapshot_id: int,
    *,
    valuation_status: str | None = None,
    limit: int | None = None,
) -> list[PortfolioValuationRow]:
    stmt = select(PortfolioValuationRow).where(PortfolioValuationRow.snapshot_id == snapshot_id)
    if valuation_status is not None:
        stmt = stmt.where(PortfolioValuationRow.valuation_status == valuation_status)
    stmt = stmt.order_by(PortfolioValuationRow.id.asc())
    if limit is not None:
        stmt = stmt.limit(limit)
    return list((await session.execute(stmt)).scalars().all())


async def build_latest_response(
    session: AsyncSession,
    workspace_id: int,
    *,
    valuation_status: str | None = None,
    limit: int = 500,
    compute_if_missing: bool = True,
) -> PortfolioValuationResponse:
    """Latest persisted valuation snapshot (or an on-the-fly compute when none).

    ``compute_if_missing`` mirrors the exposure read: when no snapshot exists yet
    the GUI still gets data (``cached=false``, ``status=recompute_needed``)."""
    workspace = await workspaces_service.get_workspace(session, workspace_id)
    snapshot = await get_latest_snapshot(session, workspace_id)
    limit = max(1, min(limit, MAX_LIMIT))

    if snapshot is not None:
        rows = await get_snapshot_rows(
            session, snapshot.id, valuation_status=valuation_status, limit=limit
        )
        return PortfolioValuationResponse(
            workspace_id=workspace_id,
            snapshot_id=snapshot.id,
            as_of_date=snapshot.as_of_date,
            base_currency=snapshot.base_currency,
            broker_account_id=snapshot.broker_account_id,
            source=snapshot.source,
            status=snapshot.status,
            cached=True,
            created_at=snapshot.created_at,
            summary=_summary_from_snapshot(snapshot),
            rows=[_row_read_from_model(r) for r in rows],
        )

    if not compute_if_missing:
        return PortfolioValuationResponse(
            workspace_id=workspace_id,
            base_currency=workspace.base_currency,
            status="empty",
            cached=False,
        )

    computed = await compute_valuation(session, workspace_id)
    rows = [
        r
        for r in computed.rows
        if valuation_status is None or r.valuation_status == valuation_status
    ][:limit]
    return PortfolioValuationResponse(
        workspace_id=workspace_id,
        snapshot_id=None,
        as_of_date=computed.as_of_date,
        base_currency=computed.base_currency,
        source=SOURCE,
        status="recompute_needed" if computed.rows else "empty",
        cached=False,
        created_at=None,
        summary=_summary_from_computed(computed),
        rows=[_row_read_from_computed(r) for r in rows],
    )


async def build_coverage(session: AsyncSession, workspace_id: int) -> PortfolioValuationCoverage:
    """Coverage-only view (no rows) for a compact dashboard/diagnostics widget."""
    await workspaces_service.get_workspace(session, workspace_id)
    snapshot = await get_latest_snapshot(session, workspace_id)
    if snapshot is not None:
        return PortfolioValuationCoverage(
            workspace_id=workspace_id,
            snapshot_id=snapshot.id,
            as_of_date=snapshot.as_of_date,
            base_currency=snapshot.base_currency,
            status=snapshot.status,
            cached=True,
            created_at=snapshot.created_at,
            summary=_summary_from_snapshot(snapshot),
        )
    computed = await compute_valuation(session, workspace_id)
    return PortfolioValuationCoverage(
        workspace_id=workspace_id,
        snapshot_id=None,
        as_of_date=computed.as_of_date,
        base_currency=computed.base_currency,
        status="recompute_needed" if computed.rows else "empty",
        cached=False,
        summary=_summary_from_computed(computed),
    )


async def get_snapshot(
    session: AsyncSession, workspace_id: int, snapshot_id: int
) -> PortfolioValuationSnapshot:
    snapshot = await session.scalar(
        select(PortfolioValuationSnapshot).where(
            PortfolioValuationSnapshot.id == snapshot_id,
            PortfolioValuationSnapshot.workspace_id == workspace_id,
        )
    )
    if snapshot is None:
        raise NotFoundError(
            "Portfolio valuation snapshot not found", code="valuation_snapshot_not_found"
        )
    return snapshot


# --- history / summary / dashboard read models (snapshots only; no recompute) -
#
# These are bounded, snapshot-backed read models over the snapshots the recompute
# worker already persisted. They never recompute valuation, never fetch a price/FX
# source, never resolve identity, and never difference two snapshots into a return /
# PnL / performance number (those live in the Rust GUI / local pricer; see the
# AGENTS.md compute boundary).


def snapshot_coverage_ratio(snapshot: PortfolioValuationSnapshot) -> Decimal | None:
    """``positions_valued / positions_selected`` (None when nothing is selected).

    A simple coverage fraction in [0, 1] — NOT a return/performance figure."""
    if snapshot.positions_selected <= 0:
        return None
    ratio = Decimal(snapshot.positions_valued) / Decimal(snapshot.positions_selected)
    if ratio > _ONE:
        ratio = _ONE
    return ratio.quantize(_COVERAGE_Q, rounding=ROUND_HALF_UP)


def snapshot_readiness_status(snapshot: PortfolioValuationSnapshot) -> str:
    """Roll a snapshot's blocker/stale counts up to a single readiness badge.

    Simple + explainable (see READINESS_STATUSES): hard blockers are missing
    price/FX and unresolved/ambiguous instruments; staleness is a soft blocker."""
    blockers = (
        snapshot.missing_price_count
        + snapshot.missing_fx_count
        + snapshot.unresolved_count
        + snapshot.ambiguous_count
    )
    stale = snapshot.stale_price_count + snapshot.stale_fx_count
    if snapshot.positions_selected <= 0 and snapshot.cash_row_count <= 0:
        return READINESS_EMPTY
    if snapshot.positions_valued <= 0 and blockers > 0:
        return READINESS_BLOCKED
    if blockers > 0:
        return READINESS_PARTIAL
    if stale > 0:
        return READINESS_STALE
    return READINESS_READY


def snapshot_blocking_reasons(snapshot: PortfolioValuationSnapshot) -> list[str]:
    """Human-readable blocker codes present in a snapshot (empty when ready)."""
    reasons: list[str] = []
    if snapshot.missing_price_count:
        reasons.append("missing_price")
    if snapshot.missing_fx_count:
        reasons.append("missing_fx")
    if snapshot.unresolved_count:
        reasons.append("unresolved_instrument")
    if snapshot.ambiguous_count:
        reasons.append("ambiguous_instrument")
    if snapshot.stale_price_count:
        reasons.append("stale_price")
    if snapshot.stale_fx_count:
        reasons.append("stale_fx")
    return reasons


def _recommended_action(
    snapshot: PortfolioValuationSnapshot, *, needs_recompute: bool
) -> str | None:
    """Single next action for the dashboard (most-blocking first)."""
    if needs_recompute:
        return "run portfolio_valuation_recompute"
    if snapshot.unresolved_count or snapshot.ambiguous_count:
        return "resolve imported instruments"
    if snapshot.missing_price_count:
        return "fetch missing prices"
    if snapshot.missing_fx_count:
        return "fetch missing FX"
    if snapshot.stale_price_count:
        return "fetch fresh prices"
    if snapshot.stale_fx_count:
        return "fetch fresh FX"
    return None


def _history_point(snapshot: PortfolioValuationSnapshot) -> PortfolioValuationHistoryPoint:
    return PortfolioValuationHistoryPoint(
        snapshot_id=snapshot.id,
        as_of_date=snapshot.as_of_date,
        created_at=snapshot.created_at,
        base_currency=snapshot.base_currency,
        broker_account_id=snapshot.broker_account_id,
        positions_selected=snapshot.positions_selected,
        positions_valued=snapshot.positions_valued,
        missing_price_count=snapshot.missing_price_count,
        missing_fx_count=snapshot.missing_fx_count,
        unresolved_count=snapshot.unresolved_count,
        ambiguous_count=snapshot.ambiguous_count,
        stale_price_count=snapshot.stale_price_count,
        stale_fx_count=snapshot.stale_fx_count,
        cash_row_count=snapshot.cash_row_count,
        total_market_value_base=snapshot.total_market_value_base,
        valuation_coverage_ratio=snapshot_coverage_ratio(snapshot),
        readiness_status=snapshot_readiness_status(snapshot),
    )


def _scoped_snapshot_query(
    workspace_id: int,
    *,
    broker_account_id: int | None,
    base_currency: str | None,
    start_date: date | None = None,
    end_date: date | None = None,
):  # type: ignore[no-untyped-def]
    stmt = select(PortfolioValuationSnapshot).where(
        PortfolioValuationSnapshot.workspace_id == workspace_id
    )
    if broker_account_id is not None:
        stmt = stmt.where(PortfolioValuationSnapshot.broker_account_id == broker_account_id)
    if base_currency is not None:
        stmt = stmt.where(PortfolioValuationSnapshot.base_currency == base_currency)
    if start_date is not None:
        stmt = stmt.where(PortfolioValuationSnapshot.as_of_date >= start_date)
    if end_date is not None:
        stmt = stmt.where(PortfolioValuationSnapshot.as_of_date <= end_date)
    return stmt


async def get_portfolio_valuation_history(
    session: AsyncSession,
    workspace_id: int,
    *,
    broker_account_id: int | None = None,
    start_date: date | None = None,
    end_date: date | None = None,
    base_currency: str | None = None,
    limit: int = HISTORY_DEFAULT_LIMIT,
) -> PortfolioValuationHistory:
    """Bounded, oldest-first series of already-persisted valuation snapshots.

    Read-only over ``portfolio_valuation_snapshots`` — never recomputes, never
    fetches, never differences points into a return/PnL. ``limit`` bounds the
    *most-recent* window (we take the newest ``limit`` then present oldest-first)."""
    workspace = await workspaces_service.get_workspace(session, workspace_id)
    limit = max(1, min(limit, HISTORY_MAX_LIMIT))
    base = base_currency.upper() if base_currency else None

    stmt = (
        _scoped_snapshot_query(
            workspace_id,
            broker_account_id=broker_account_id,
            base_currency=base,
            start_date=start_date,
            end_date=end_date,
        )
        .order_by(
            PortfolioValuationSnapshot.as_of_date.desc(), PortfolioValuationSnapshot.id.desc()
        )
        .limit(limit)
    )
    snapshots = list((await session.execute(stmt)).scalars().all())
    snapshots.reverse()  # newest-first window -> oldest-first for charting
    return PortfolioValuationHistory(
        workspace_id=workspace_id,
        base_currency=base or workspace.base_currency,
        broker_account_id=broker_account_id,
        count=len(snapshots),
        points=[_history_point(s) for s in snapshots],
    )


async def _broker_account_summaries(
    session: AsyncSession, workspace_id: int, *, base_currency: str | None
) -> list[PortfolioValuationBrokerAccountSummary]:
    """Latest coverage per broker account that has account-scoped snapshots.

    Bounded: distinct non-NULL ``broker_account_id`` (a small set) then one latest
    snapshot each. Empty when every snapshot is workspace-level (account NULL)."""
    stmt = select(PortfolioValuationSnapshot.broker_account_id).where(
        PortfolioValuationSnapshot.workspace_id == workspace_id,
        PortfolioValuationSnapshot.broker_account_id.is_not(None),
    )
    if base_currency is not None:
        stmt = stmt.where(PortfolioValuationSnapshot.base_currency == base_currency)
    account_ids = sorted(
        {a for a in (await session.execute(stmt.distinct())).scalars().all() if a is not None}
    )
    summaries: list[PortfolioValuationBrokerAccountSummary] = []
    for account_id in account_ids:
        latest = await session.scalar(
            _scoped_snapshot_query(
                workspace_id, broker_account_id=account_id, base_currency=base_currency
            )
            .order_by(
                PortfolioValuationSnapshot.as_of_date.desc(),
                PortfolioValuationSnapshot.id.desc(),
            )
            .limit(1)
        )
        if latest is None:
            continue
        summaries.append(
            PortfolioValuationBrokerAccountSummary(
                broker_account_id=account_id,
                latest_snapshot_id=latest.id,
                as_of_date=latest.as_of_date,
                total_market_value_base=latest.total_market_value_base,
                positions_selected=latest.positions_selected,
                positions_valued=latest.positions_valued,
                valuation_coverage_ratio=snapshot_coverage_ratio(latest),
                readiness_status=snapshot_readiness_status(latest),
            )
        )
    return summaries


async def build_summary(
    session: AsyncSession,
    workspace_id: int,
    *,
    broker_account_id: int | None = None,
    base_currency: str | None = None,
) -> PortfolioValuationSummaryResponse:
    """Compact latest-context + readiness roll-up over the workspace's snapshots.

    Cheap SQL over the snapshots table (no rows). Never recomputes / fetches / diffs
    points. ``status=empty`` when no snapshot exists in scope yet."""
    workspace = await workspaces_service.get_workspace(session, workspace_id)
    base = base_currency.upper() if base_currency else None

    history_points = (
        await session.scalar(
            select(func.count()).select_from(
                _scoped_snapshot_query(
                    workspace_id, broker_account_id=broker_account_id, base_currency=base
                ).subquery()
            )
        )
    ) or 0

    latest = await session.scalar(
        _scoped_snapshot_query(
            workspace_id, broker_account_id=broker_account_id, base_currency=base
        )
        .order_by(
            PortfolioValuationSnapshot.as_of_date.desc(), PortfolioValuationSnapshot.id.desc()
        )
        .limit(1)
    )
    if latest is None:
        return PortfolioValuationSummaryResponse(
            workspace_id=workspace_id,
            status="empty",
            base_currency=base or workspace.base_currency,
            broker_account_id=broker_account_id,
            readiness_status=READINESS_EMPTY,
            history_points=history_points,
        )
    return PortfolioValuationSummaryResponse(
        workspace_id=workspace_id,
        status="present",
        latest_snapshot_id=latest.id,
        latest_as_of_date=latest.as_of_date,
        latest_created_at=latest.created_at,
        base_currency=latest.base_currency,
        broker_account_id=latest.broker_account_id,
        total_market_value_base=latest.total_market_value_base,
        positions_selected=latest.positions_selected,
        positions_valued=latest.positions_valued,
        valuation_coverage_ratio=snapshot_coverage_ratio(latest),
        missing_price_count=latest.missing_price_count,
        missing_fx_count=latest.missing_fx_count,
        unresolved_count=latest.unresolved_count,
        ambiguous_count=latest.ambiguous_count,
        stale_price_count=latest.stale_price_count,
        stale_fx_count=latest.stale_fx_count,
        cash_row_count=latest.cash_row_count,
        readiness_status=snapshot_readiness_status(latest),
        blocking_reasons=snapshot_blocking_reasons(latest),
        history_points=history_points,
        broker_accounts=await _broker_account_summaries(session, workspace_id, base_currency=base),
    )


async def build_dashboard_block(
    session: AsyncSession, workspace_id: int, *, base_currency: str
) -> PortfolioValuationDashboardBlock:
    """Compact dashboard valuation/readiness block from the latest snapshot only.

    Never recomputes valuation, never fetches, never diffs snapshots. ``needs_
    recompute`` is True when there is a transaction ledger but no snapshot yet, or
    the latest snapshot has aged past the freshness window."""
    snapshot = await get_latest_snapshot(session, workspace_id)
    if snapshot is None:
        from app.services.broker_imports import LEDGER_STATUSES

        ledger_count = (
            await session.scalar(
                select(func.count())
                .select_from(PortfolioTransaction)
                .where(
                    PortfolioTransaction.workspace_id == workspace_id,
                    PortfolioTransaction.status.in_(LEDGER_STATUSES),
                )
            )
        ) or 0
        needs = ledger_count > 0
        return PortfolioValuationDashboardBlock(
            status="missing",
            base_currency=base_currency,
            readiness_status=READINESS_EMPTY,
            needs_recompute=needs,
            recommended_action="run portfolio_valuation_recompute" if needs else None,
        )

    needs_recompute = freshness_service.freshness_state(snapshot.created_at) == STALE
    return PortfolioValuationDashboardBlock(
        status="present",
        latest_snapshot_id=snapshot.id,
        latest_as_of_date=snapshot.as_of_date,
        base_currency=snapshot.base_currency,
        total_market_value_base=snapshot.total_market_value_base,
        positions_selected=snapshot.positions_selected,
        positions_valued=snapshot.positions_valued,
        valuation_coverage_ratio=snapshot_coverage_ratio(snapshot),
        readiness_status=snapshot_readiness_status(snapshot),
        missing_price_count=snapshot.missing_price_count,
        missing_fx_count=snapshot.missing_fx_count,
        unresolved_count=snapshot.unresolved_count,
        ambiguous_count=snapshot.ambiguous_count,
        stale_price_count=snapshot.stale_price_count,
        stale_fx_count=snapshot.stale_fx_count,
        needs_recompute=needs_recompute,
        recommended_action=_recommended_action(snapshot, needs_recompute=needs_recompute),
    )
