"""Holdings ingestion service.

Fetches look-through holdings from a `HoldingsSource` adapter and upserts them
into ``fund_holdings``, keyed on the
(fund_id, as_of_date, source, holding_key) unique constraint so re-runs and
backfills are idempotent. Every row records its ``source`` for provenance.

This is the provider-agnostic half of the pipeline: it validates adapter output,
computes each holding's identity key, upserts canonical rows and counts
inserted/updated/failed. Provider-specific fetch/parse lives in
``app/sources/holdings.py``.

It also owns **snapshot selection** for the read side: a fund may carry holdings
from several sources/dates (seed placeholder, a fixture snapshot, a future
manual override). Reads should see *one* coherent snapshot, never a mix (which
would double-count exposure). `latest_holdings_by_fund` picks the highest
source-priority snapshot present, breaking ties by most recent ``as_of_date``.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.models import Fund, FundHolding
from app.sources.holdings import HoldingRecord, HoldingsSource

# Holding fields that may change between fetches for the same identity key.
# ``fund_id``/``as_of_date``/``source``/``holding_key`` are the identity.
_MUTABLE_FIELDS = (
    "security_name",
    "security_ticker",
    "security_isin",
    "security_sedol",
    "security_cusip",
    "security_figi",
    "country",
    "sector",
    "industry",
    "currency",
    "weight",
    "market_value",
    "shares",
    "status",
    "raw_payload_json",
)

# Read-side snapshot priority (lower = preferred). Mirrors the data_sources
# priority convention: a manual override outranks an issuer/fixture snapshot,
# which in turn outranks bare seed placeholder holdings. Live issuer downloads
# (iShares/JPM) and an official Vanguard export outrank the offline fixture.
_SNAPSHOT_PRIORITY: dict[str | None, int] = {
    "manual": 5,
    "vanguard_holdings_export": 8,
    "blackrock_ishares_holdings": 10,
    "jpmorgan_etf_holdings": 10,
    "vanguard_holdings": 10,
    "ishares": 10,
    "vanguard": 10,
    "jpmam": 10,
    "issuer": 10,
    "holdings_fixture": 20,
    "fmp": 30,
    "seed": 100,
    None: 100,
}
_DEFAULT_PRIORITY = 90  # an unknown automated source beats seed but not issuer


def snapshot_priority(source: str | None) -> int:
    return _SNAPSHOT_PRIORITY.get(source, _DEFAULT_PRIORITY)


@dataclass
class HoldingsCounts:
    inserted: int = 0
    updated: int = 0
    failed: int = 0  # rows that could not be normalised/upserted (bad rows)
    skipped: int = 0  # matched an existing row with no change (idempotent re-run)
    fetched: int = 0  # rows the source returned for this fund

    def add(self, other: HoldingsCounts) -> None:
        self.inserted += other.inserted
        self.updated += other.updated
        self.failed += other.failed
        self.skipped += other.skipped
        self.fetched += other.fetched


# --- ingestion (write side) --------------------------------------------------


def _record_fields(record: HoldingRecord) -> dict[str, object]:
    return {
        "security_name": record.holding_name,
        "security_ticker": record.holding_ticker,
        "security_isin": record.holding_isin,
        "security_sedol": record.holding_sedol,
        "security_cusip": record.holding_cusip,
        "security_figi": record.holding_figi,
        "country": record.country,
        "sector": record.sector,
        "industry": record.industry,
        "currency": record.currency,
        "weight": record.weight,
        "market_value": record.market_value,
        "shares": record.shares,
        "status": record.status,
        "raw_payload_json": record.raw_payload,
    }


def _apply(existing: FundHolding, fields: dict[str, object]) -> bool:
    """Update mutable fields in place; return True if anything changed."""
    changed = False
    for field in _MUTABLE_FIELDS:
        new_value = fields[field]
        if getattr(existing, field) != new_value:
            setattr(existing, field, new_value)
            changed = True
    return changed


async def ingest_holdings_for_fund(
    session: AsyncSession,
    fund: Fund,
    source: HoldingsSource,
    *,
    url: str | None = None,
) -> HoldingsCounts:
    """Fetch + idempotently upsert one fund's holdings from ``source``.

    Live issuer adapters need the ``session`` (budget/fetch-log/cache) and take an
    explicit ``url`` download override; the offline fixture ignores both. A row
    that cannot be normalised/upserted is isolated (counted as ``failed``/bad row),
    never failing the whole fund.
    """
    counts = HoldingsCounts()
    records = await source.fetch(isin=fund.isin, session=session, url=url)
    counts.fetched = len(records)

    for record in records:
        try:
            key = record.identity_key
            fields = _record_fields(record)
            existing = await session.scalar(
                select(FundHolding).where(
                    FundHolding.fund_id == fund.id,
                    FundHolding.as_of_date == record.as_of_date,
                    FundHolding.source == record.source,
                    FundHolding.holding_key == key,
                )
            )
            if existing is not None:
                if _apply(existing, fields):
                    counts.updated += 1
                else:
                    counts.skipped += 1
            else:
                session.add(
                    FundHolding(
                        fund_id=fund.id,
                        as_of_date=record.as_of_date,
                        source=record.source,
                        holding_key=key,
                        **fields,
                    )
                )
                counts.inserted += 1
        except Exception:
            counts.failed += 1

    await session.flush()
    return counts


# --- snapshot selection (read side) ------------------------------------------


def select_snapshot(rows: list[FundHolding]) -> list[FundHolding]:
    """Pick the single best (source, as_of_date) snapshot from a fund's rows.

    Best = lowest source priority number, then most recent ``as_of_date`` (then
    source name for determinism). Returns its rows sorted by weight desc.
    """
    if not rows:
        return []
    groups: dict[tuple[str, date], list[FundHolding]] = defaultdict(list)
    for holding in rows:
        groups[(holding.source, holding.as_of_date)].append(holding)
    best = min(
        groups,
        key=lambda k: (snapshot_priority(k[0]), -k[1].toordinal(), k[0]),
    )
    chosen = groups[best]
    chosen.sort(key=lambda h: h.weight, reverse=True)
    return chosen


async def latest_holdings_by_fund(
    session: AsyncSession, fund_ids: list[int]
) -> dict[int, list[FundHolding]]:
    """One coherent holdings snapshot per fund (see `select_snapshot`)."""
    if not fund_ids:
        return {}
    rows = (
        (
            await session.execute(
                select(FundHolding)
                .where(FundHolding.fund_id.in_(fund_ids))
                # Eager-load the resolved instrument so read-side ``HoldingRead`` /
                # constituent valuation never triggers an async lazy load.
                .options(selectinload(FundHolding.instrument))
            )
        )
        .scalars()
        .all()
    )
    by_fund: dict[int, list[FundHolding]] = defaultdict(list)
    for holding in rows:
        by_fund[holding.fund_id].append(holding)
    return {fund_id: select_snapshot(items) for fund_id, items in by_fund.items()}


async def latest_holdings_for_fund(
    session: AsyncSession,
    fund_id: int,
    *,
    as_of_date: date | None = None,
    source: str | None = None,
    limit: int | None = None,
) -> list[FundHolding]:
    """Latest snapshot for one fund, optionally pinned to a date/source.

    With no filters this returns the best snapshot per `select_snapshot`. An
    explicit ``source`` and/or ``as_of_date`` narrows to that exact snapshot
    (e.g. to inspect seed vs fixture provenance). Bounded by ``limit``.
    """
    stmt = (
        select(FundHolding)
        .where(FundHolding.fund_id == fund_id)
        .options(selectinload(FundHolding.instrument))
    )
    if source is not None:
        stmt = stmt.where(FundHolding.source == source)
    if as_of_date is not None:
        stmt = stmt.where(FundHolding.as_of_date == as_of_date)
    rows = list((await session.execute(stmt)).scalars().all())
    chosen = select_snapshot(rows)
    if limit is not None:
        chosen = chosen[:limit]
    return chosen
