"""Instrument EOD price ingestion + read shaping.

The provider-agnostic half of *unified* instrument price ingestion: it decides
*which* canonical ``instrument_listings`` to price — whether they came from
ETF/fund constituents, directly-held imported broker holdings, or a future
manually-linked instrument — calls an ``InstrumentPriceSource`` adapter, and
upserts the bars into ``instrument_prices`` idempotently. The source adapter
(``app/sources/instrument_prices.py``) owns the provider fetch and the
source-budget / fetch-log / request-cache guard for live providers; this module
owns selection, the idempotent upsert, freshness bookkeeping and run counters.

There is a single price path (``instrument_listings`` -> ``instrument_prices``):
imported direct holdings deliberately do NOT get a separate price table — once a
broker transaction resolves to a canonical listing it is priced through the same
selector/upsert as a constituent, so it becomes chartable and visible in Data
Operations like any other listing.

Safety (see AGENTS.md):
  * only *resolved* listings are priced — a constituent must be linked to an
    instrument, an imported transaction must have reached ``resolved``/``ready``
    with a listing; ambiguous / unresolved rows are never guessed;
  * listings are deduped (Apple priced once even if held via several funds *and*
    imported directly) so a live provider never loops per holding/transaction;
  * a missing / failed / budget-blocked / cached / unpriceable listing is
    isolated and counted, never failing the whole job;
  * a ``manual`` price row is never clobbered by an automated run.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    FundHolding,
    FundListing,
    InstrumentListing,
    InstrumentPrice,
    PortfolioPosition,
    PortfolioTransaction,
)
from app.services import freshness as freshness_service
from app.services import holdings_ingestion as holdings_service
from app.sources.instrument_prices import (
    FAILED,
    NO_DATA,
    OK,
    SKIPPED_BUDGET,
    SKIPPED_CACHED,
    InstrumentPriceRecord,
    InstrumentPriceRequest,
    InstrumentPriceSource,
    get_instrument_price_source,
)

# Imported broker-transaction statuses that carry a usable instrument link. A
# resolved/ready transaction has reached the canonical instrument universe via the
# imported-instrument resolution bridge; everything else is never priced.
_IMPORTED_PRICEABLE_STATUSES = ("resolved", "ready")

# Bar fields that may change between fetches for the same
# (instrument_listing_id, price_date, source) key. The rest is identity.
_MUTABLE_FIELDS = (
    "open",
    "high",
    "low",
    "close",
    "adjusted_close",
    "volume",
    "currency",
    "status",
    "raw_payload_json",
)


@dataclass
class PriceIngestCounts:
    inserted: int = 0
    updated: int = 0
    failed: int = 0
    skipped_budget: int = 0
    skipped_cached: int = 0
    no_data: int = 0
    listings: int = 0  # listings selected for this run

    def message(self) -> str:
        return (
            f"listings={self.listings} inserted={self.inserted} updated={self.updated} "
            f"failed={self.failed} no_data={self.no_data} "
            f"skipped_budget={self.skipped_budget} skipped_cached={self.skipped_cached}"
        )


# --- listing selection -------------------------------------------------------


async def _scoped_fund_ids(
    session: AsyncSession, *, fund_id: int | None, workspace_id: int | None
) -> list[int] | None:
    """Fund ids to scope price selection to (None == every fund with holdings)."""
    if fund_id is not None:
        return [fund_id]
    if workspace_id is None:
        return None
    rows = (
        await session.execute(
            select(FundListing.fund_id)
            .join(PortfolioPosition, PortfolioPosition.fund_listing_id == FundListing.id)
            .where(PortfolioPosition.workspace_id == workspace_id)
            .distinct()
        )
    ).scalars()
    return sorted(set(rows))


async def _imported_resolved_listing_ids(
    session: AsyncSession, *, workspace_id: int | None, broker_import_id: int | None
) -> set[int]:
    """Distinct instrument_listing_ids of resolved/ready imported broker txns.

    These are directly-held instruments that the imported-instrument resolution
    bridge linked to a canonical listing — so they price through the same path as
    a constituent. Unresolved / ambiguous transactions carry no listing and are
    never selected. ``workspace_id=None`` and ``broker_import_id=None`` means every
    workspace / import (bounded later by the selector's ``limit``)."""
    stmt = select(PortfolioTransaction.instrument_listing_id).where(
        PortfolioTransaction.status.in_(_IMPORTED_PRICEABLE_STATUSES),
        PortfolioTransaction.instrument_listing_id.is_not(None),
    )
    if workspace_id is not None:
        stmt = stmt.where(PortfolioTransaction.workspace_id == workspace_id)
    if broker_import_id is not None:
        stmt = stmt.where(PortfolioTransaction.broker_import_id == broker_import_id)
    return {lid for lid in (await session.execute(stmt.distinct())).scalars() if lid is not None}


async def _listings_by_id(session: AsyncSession, listing_ids: list[int]) -> list[InstrumentListing]:
    """Load listings for the given ids, ordered by id (deterministic)."""
    if not listing_ids:
        return []
    return list(
        (
            await session.execute(
                select(InstrumentListing)
                .where(InstrumentListing.id.in_(set(listing_ids)))
                .order_by(InstrumentListing.id)
            )
        )
        .scalars()
        .all()
    )


async def _primary_listings(
    session: AsyncSession, instrument_ids: list[int]
) -> dict[int, InstrumentListing]:
    """The primary (lowest-id) tradable listing per instrument."""
    if not instrument_ids:
        return {}
    rows = (
        (
            await session.execute(
                select(InstrumentListing)
                .where(InstrumentListing.instrument_id.in_(set(instrument_ids)))
                .order_by(InstrumentListing.id)
            )
        )
        .scalars()
        .all()
    )
    primary: dict[int, InstrumentListing] = {}
    for listing in rows:
        primary.setdefault(listing.instrument_id, listing)
    return primary


async def select_listings(
    session: AsyncSession,
    *,
    fund_id: int | None = None,
    workspace_id: int | None = None,
    instrument_id: int | None = None,
    instrument_listing_id: int | None = None,
    broker_import_id: int | None = None,
    transaction_id: int | None = None,
    limit: int | None = None,
    include_imported: bool = False,
) -> list[InstrumentListing]:
    """Canonical listings to price, deduped + prioritised.

    Explicit-target scopes win first: a direct ``instrument_listing_id`` /
    ``instrument_id`` targets one listing / instrument; a ``transaction_id`` targets
    the listing a resolved imported transaction links to; a ``broker_import_id``
    targets that import's resolved imported listings.

    Otherwise the scoped funds' *resolved* constituents drive the set (one primary
    listing per distinct instrument, heaviest weight first). With
    ``include_imported`` the workspace's resolved *imported* direct holdings are
    unioned in (deduped against the constituent set, so an instrument held both as
    an ETF constituent and directly is priced once). Bounded by ``limit``.
    """
    if instrument_listing_id is not None:
        listing = await session.get(InstrumentListing, instrument_listing_id)
        return [listing] if listing is not None else []

    if instrument_id is not None:
        primary = await _primary_listings(session, [instrument_id])
        listings = list(primary.values())
        return listings[:limit] if limit is not None else listings

    if transaction_id is not None:
        txn = await session.get(PortfolioTransaction, transaction_id)
        if txn is None or txn.instrument_listing_id is None:
            return []
        listing = await session.get(InstrumentListing, txn.instrument_listing_id)
        return [listing] if listing is not None else []

    if broker_import_id is not None:
        imported_ids = await _imported_resolved_listing_ids(
            session, workspace_id=workspace_id, broker_import_id=broker_import_id
        )
        listings = await _listings_by_id(session, sorted(imported_ids))
        return listings[:limit] if limit is not None else listings

    fund_ids = await _scoped_fund_ids(session, fund_id=fund_id, workspace_id=workspace_id)
    if fund_ids is None:
        fund_ids = list((await session.execute(select(FundHolding.fund_id).distinct())).scalars())
    snapshots = await holdings_service.latest_holdings_by_fund(session, fund_ids)

    # Heaviest weight a resolved constituent carries anywhere in scope.
    weight_by_instrument: dict[int, Decimal] = {}
    for holding in (h for items in snapshots.values() for h in items):
        iid = holding.holding_instrument_id
        if iid is None:
            continue
        weight = holding.weight or Decimal("0")
        if weight > weight_by_instrument.get(iid, Decimal("-1")):
            weight_by_instrument[iid] = weight

    primary = await _primary_listings(session, list(weight_by_instrument))
    ordered_ids = sorted(
        primary, key=lambda iid: (weight_by_instrument.get(iid, Decimal("0")), -iid), reverse=True
    )
    listings = [primary[iid] for iid in ordered_ids]

    # Union resolved imported direct holdings (constituents keep their weight
    # priority; imported-only listings follow, deduped by listing id).
    if include_imported and fund_id is None:
        imported_ids = await _imported_resolved_listing_ids(
            session, workspace_id=workspace_id, broker_import_id=None
        )
        existing = {ln.id for ln in listings}
        extra = await _listings_by_id(session, sorted(i for i in imported_ids if i not in existing))
        listings.extend(extra)

    return listings[:limit] if limit is not None else listings


def build_request(listing: InstrumentListing) -> InstrumentPriceRequest:
    return InstrumentPriceRequest(
        instrument_listing_id=listing.id,
        ticker=listing.ticker,
        mic=listing.mic,
        exchange=listing.exchange,
        currency=listing.currency,
    )


# --- idempotent upsert -------------------------------------------------------


def _record_fields(record: InstrumentPriceRecord) -> dict[str, object]:
    return {
        "open": record.open,
        "high": record.high,
        "low": record.low,
        "close": record.close,
        "adjusted_close": record.adjusted_close,
        "volume": record.volume,
        "currency": record.currency,
        "status": record.status,
        "raw_payload_json": record.raw_payload,
    }


def _apply(existing: InstrumentPrice, fields: dict[str, object]) -> bool:
    """Update mutable fields in place; return True if anything changed."""
    changed = False
    for field_name in _MUTABLE_FIELDS:
        if getattr(existing, field_name) != fields[field_name]:
            setattr(existing, field_name, fields[field_name])
            changed = True
    return changed


async def _upsert_records(
    session: AsyncSession,
    listing: InstrumentListing,
    records: list[InstrumentPriceRecord],
    counts: PriceIngestCounts,
    *,
    now: datetime,
) -> None:
    touched = False
    for record in records:
        try:
            if record.close is None:
                raise ValueError("missing close")
            fields = _record_fields(record)
            existing = await session.scalar(
                select(InstrumentPrice).where(
                    InstrumentPrice.instrument_listing_id == listing.id,
                    InstrumentPrice.price_date == record.price_date,
                    InstrumentPrice.source == record.source,
                )
            )
            if existing is not None:
                if existing.source == "manual":
                    continue  # never clobber a manual price
                if _apply(existing, fields):
                    counts.updated += 1
                    touched = True
            else:
                session.add(
                    InstrumentPrice(
                        instrument_listing_id=listing.id,
                        price_date=record.price_date,
                        source=record.source,
                        **fields,
                    )
                )
                counts.inserted += 1
                touched = True
        except Exception:  # noqa: BLE001 - isolate one bad bar; count and continue
            counts.failed += 1

    if touched:
        listing.last_price_at = now
        if listing.status == "pending":
            listing.status = "active"


async def ingest_prices(
    session: AsyncSession,
    source: InstrumentPriceSource,
    listings: list[InstrumentListing],
    *,
    start_date=None,
    end_date=None,
    batch_size: int = 1,
    ttl_seconds: int = 0,
    now: datetime | None = None,
) -> PriceIngestCounts:
    """Fetch + upsert EOD prices for the given resolved listings, idempotently."""
    now = now or datetime.now(UTC)
    counts = PriceIngestCounts(listings=len(listings))
    if not listings:
        return counts

    requests = [build_request(ln) for ln in listings]
    fetch = await source.fetch_eod_prices(
        session,
        requests,
        start_date=start_date,
        end_date=end_date,
        batch_size=batch_size,
        ttl_seconds=ttl_seconds,
    )

    records_by_listing: dict[int, list[InstrumentPriceRecord]] = {}
    for record in fetch.records:
        records_by_listing.setdefault(record.instrument_listing_id, []).append(record)

    for listing in listings:
        outcome = fetch.outcomes.get(listing.id, NO_DATA)
        if outcome == SKIPPED_BUDGET:
            counts.skipped_budget += 1
            continue
        if outcome == SKIPPED_CACHED:
            counts.skipped_cached += 1
            continue
        if outcome == FAILED:
            counts.failed += 1
            continue
        records = records_by_listing.get(listing.id, [])
        if outcome != OK or not records:
            counts.no_data += 1
            continue
        await _upsert_records(session, listing, records, counts, now=now)

    await session.flush()
    return counts


# --- generic (unified) instrument EOD price ingestion ------------------------


def _is_priceable(listing: InstrumentListing) -> bool:
    """A listing a provider can actually fetch (needs a ticker symbol).

    Both the offline fixture and the live Stooq/yfinance adapters key on the
    ticker; a listing with none is unpriceable (skipped + counted, never an
    error). A known-ticker listing the fixture does not recognise still returns
    ``no_data`` from the source layer — that is a fetch outcome, not unpriceable.
    """
    return bool(listing.ticker and listing.ticker.strip())


@dataclass
class ListingSelection:
    """The listings a generic ingestion run will price, plus why others dropped."""

    listings: list[InstrumentListing]
    skipped_fresh: int = 0
    skipped_unpriceable: int = 0


@dataclass
class InstrumentPriceIngestionResult:
    """Counters for one generic instrument-price ingestion run.

    Wraps the per-bar upsert counts (``inserted`` / ``updated`` / ``failed`` /
    ``no_data``) with the selection outcome (``selected`` / ``skipped_fresh`` /
    ``skipped_unpriceable``) and the per-listing live-provider outcomes
    (``rate_limited`` == budget-blocked, ``cached`` == request-cache hit). ``source``
    records the provider used and ``is_fixture`` whether it was the offline one.
    """

    source: str = ""
    is_fixture: bool = True
    selected: int = 0
    skipped_fresh: int = 0
    skipped_unpriceable: int = 0
    inserted: int = 0
    updated: int = 0
    failed: int = 0
    no_data: int = 0
    rate_limited: int = 0
    cached: int = 0

    @property
    def fetched(self) -> int:
        """Selected listings a fetch was attempted for (not budget/cache-skipped)."""
        return max(self.selected - self.rate_limited - self.cached, 0)

    def message(self) -> str:
        return (
            f"source={self.source} selected={self.selected} "
            f"skipped_fresh={self.skipped_fresh} skipped_unpriceable={self.skipped_unpriceable} "
            f"inserted={self.inserted} updated={self.updated} failed={self.failed} "
            f"no_data={self.no_data} rate_limited={self.rate_limited} cached={self.cached}"
        )


async def select_priceable_listings(
    session: AsyncSession,
    *,
    fund_id: int | None = None,
    workspace_id: int | None = None,
    instrument_id: int | None = None,
    instrument_listing_id: int | None = None,
    broker_import_id: int | None = None,
    transaction_id: int | None = None,
    limit: int | None = None,
    skip_fresh: bool = False,
) -> ListingSelection:
    """Select listings to price across every scope (constituent + imported).

    Builds the deduped candidate set via ``select_listings`` (unioning resolved
    imported direct holdings), drops unpriceable listings (no ticker) and — for the
    bulk scopes only — listings whose price is still fresh (``skip_fresh``), then
    bounds by ``limit``. Explicit single-target scopes (listing/instrument/
    transaction id) are always honoured (never freshness-skipped).
    """
    explicit_target = (
        instrument_listing_id is not None or instrument_id is not None or transaction_id is not None
    )
    candidates = await select_listings(
        session,
        fund_id=fund_id,
        workspace_id=workspace_id,
        instrument_id=instrument_id,
        instrument_listing_id=instrument_listing_id,
        broker_import_id=broker_import_id,
        transaction_id=transaction_id,
        limit=None,  # bound after filtering so a fresh listing never eats a slot
        include_imported=True,
    )

    selection = ListingSelection(listings=[])
    apply_freshness = skip_fresh and not explicit_target
    for listing in candidates:
        if not _is_priceable(listing):
            selection.skipped_unpriceable += 1
            continue
        if apply_freshness and (
            freshness_service.freshness_state(listing.last_price_at, kind="price")
            == freshness_service.FRESH
        ):
            selection.skipped_fresh += 1
            continue
        selection.listings.append(listing)
        if limit is not None and len(selection.listings) >= limit:
            break
    return selection


async def ingest_instrument_eod_prices(
    session: AsyncSession,
    *,
    workspace_id: int | None = None,
    fund_id: int | None = None,
    broker_import_id: int | None = None,
    instrument_id: int | None = None,
    instrument_listing_id: int | None = None,
    transaction_id: int | None = None,
    source: str | None = None,
    limit: int | None = 100,
    force: bool = False,
    now: datetime | None = None,
) -> InstrumentPriceIngestionResult:
    """Unified EOD price ingestion for any canonical ``instrument_listing``.

    Selects the priceable listings in scope (constituents + resolved imported
    direct holdings, deduped), then fetches via the configured provider (offline
    fixture by default; Stooq/yfinance behind ``guarded_fetch`` when named) and
    upserts bars idempotently into ``instrument_prices``. ``force`` re-prices fresh
    listings (the default skips them). One bounded pass — never an unbounded loop.
    """
    from app.core.config import get_settings
    from app.services import source_budget as source_budget_service

    src = get_instrument_price_source(source)
    result = InstrumentPriceIngestionResult(
        source=src.name, is_fixture=src.name.endswith("_fixture")
    )

    selection = await select_priceable_listings(
        session,
        fund_id=fund_id,
        workspace_id=workspace_id,
        instrument_id=instrument_id,
        instrument_listing_id=instrument_listing_id,
        broker_import_id=broker_import_id,
        transaction_id=transaction_id,
        limit=limit,
        skip_fresh=not force,
    )
    result.selected = len(selection.listings)
    result.skipped_fresh = selection.skipped_fresh
    result.skipped_unpriceable = selection.skipped_unpriceable
    if not selection.listings:
        return result

    # Batch size + cache TTL come from the source budget. Fixtures are offline and
    # deterministic, so they bypass the recent-success cache (TTL 0) — the
    # idempotent upsert is what guarantees no duplicate rows on a rerun. External
    # sources use the configured TTL.
    budget = await source_budget_service.get_budget(session, src.name)
    batch_size = budget.batch_size if budget and budget.batch_size else 1
    ttl_seconds = 0 if result.is_fixture else get_settings().request_cache_ttl_seconds

    counts = await ingest_prices(
        session, src, selection.listings, batch_size=batch_size, ttl_seconds=ttl_seconds, now=now
    )
    result.inserted = counts.inserted
    result.updated = counts.updated
    result.failed = counts.failed
    result.no_data = counts.no_data
    result.rate_limited = counts.skipped_budget
    result.cached = counts.skipped_cached
    return result


# --- read helpers (API / planner) --------------------------------------------


async def list_prices_for_listing(
    session: AsyncSession,
    instrument_listing_id: int,
    *,
    source: str | None = None,
    start_date=None,
    end_date=None,
    limit: int | None = None,
) -> list[InstrumentPrice]:
    """A listing's EOD bars, oldest first (one row per date+source)."""
    stmt = select(InstrumentPrice).where(
        InstrumentPrice.instrument_listing_id == instrument_listing_id
    )
    if source is not None:
        stmt = stmt.where(InstrumentPrice.source == source)
    if start_date is not None:
        stmt = stmt.where(InstrumentPrice.price_date >= start_date)
    if end_date is not None:
        stmt = stmt.where(InstrumentPrice.price_date <= end_date)
    stmt = stmt.order_by(InstrumentPrice.price_date.asc(), InstrumentPrice.id.asc())
    rows = list((await session.execute(stmt)).scalars().all())
    return rows[-limit:] if limit is not None else rows


async def latest_prices_for_listings(
    session: AsyncSession, listing_ids: list[int]
) -> dict[int, InstrumentPrice]:
    """The newest EOD bar per listing (last source wins on a tied date)."""
    if not listing_ids:
        return {}
    rows = (
        (
            await session.execute(
                select(InstrumentPrice)
                .where(InstrumentPrice.instrument_listing_id.in_(set(listing_ids)))
                .order_by(InstrumentPrice.price_date.asc(), InstrumentPrice.id.asc())
            )
        )
        .scalars()
        .all()
    )
    latest: dict[int, InstrumentPrice] = {}
    for row in rows:
        latest[row.instrument_listing_id] = row  # ordered asc => last wins
    return latest


async def latest_constituent_prices(
    session: AsyncSession, instrument_ids: list[int]
) -> dict[int, tuple[InstrumentListing, InstrumentPrice | None]]:
    """For each instrument, its primary listing + that listing's latest bar."""
    primary = await _primary_listings(session, instrument_ids)
    latest = await latest_prices_for_listings(session, [ln.id for ln in primary.values()])
    return {iid: (listing, latest.get(listing.id)) for iid, listing in primary.items()}


async def prices_asof_for_listings(
    session: AsyncSession, listing_ids: list[int], as_of_date
) -> dict[int, InstrumentPrice]:
    """The latest EOD bar on/before ``as_of_date`` per listing (SQL-friendly).

    Two bounded queries: a ``GROUP BY`` to find each listing's max ``price_date``
    on/before the cutoff, then the matching bars (SQLite + Postgres compatible —
    no ``DISTINCT ON``). On a tied date with several sources, ``manual`` wins, else
    the lowest id, deterministically. Used by the top-holding performance service
    so it never loads a listing's whole price history into Python.
    """
    if not listing_ids:
        return {}
    ids = set(listing_ids)
    max_dates = (
        select(
            InstrumentPrice.instrument_listing_id.label("lid"),
            func.max(InstrumentPrice.price_date).label("d"),
        )
        .where(
            InstrumentPrice.instrument_listing_id.in_(ids),
            InstrumentPrice.price_date <= as_of_date,
        )
        .group_by(InstrumentPrice.instrument_listing_id)
    ).subquery()
    rows = (
        (
            await session.execute(
                select(InstrumentPrice)
                .join(
                    max_dates,
                    (InstrumentPrice.instrument_listing_id == max_dates.c.lid)
                    & (InstrumentPrice.price_date == max_dates.c.d),
                )
                .order_by(InstrumentPrice.id.asc())
            )
        )
        .scalars()
        .all()
    )
    chosen: dict[int, InstrumentPrice] = {}
    for row in rows:
        existing = chosen.get(row.instrument_listing_id)
        if existing is None or (row.source == "manual" and existing.source != "manual"):
            chosen[row.instrument_listing_id] = row
    return chosen


async def prices_on_dates_for_listings(
    session: AsyncSession, listing_ids: list[int], dates: list
) -> dict[tuple[int, object], InstrumentPrice]:
    """Exact ``(instrument_listing_id, price_date)`` bars for a bounded set.

    One query over the listing set × the (small) date set — used by the
    top-holding performance service to read the precise closes a snapshot
    captured (each exposure row stores the ``price_date`` it priced at). On a tied
    date with several sources, ``manual`` wins, else the lowest id.
    """
    if not listing_ids or not dates:
        return {}
    rows = (
        (
            await session.execute(
                select(InstrumentPrice)
                .where(
                    InstrumentPrice.instrument_listing_id.in_(set(listing_ids)),
                    InstrumentPrice.price_date.in_(set(dates)),
                )
                .order_by(InstrumentPrice.id.asc())
            )
        )
        .scalars()
        .all()
    )
    chosen: dict[tuple[int, object], InstrumentPrice] = {}
    for row in rows:
        key = (row.instrument_listing_id, row.price_date)
        existing = chosen.get(key)
        if existing is None or (row.source == "manual" and existing.source != "manual"):
            chosen[key] = row
    return chosen
