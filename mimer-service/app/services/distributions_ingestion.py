"""Distribution ingestion service.

Fetches declared distributions from a `DistributionSource` adapter and upserts
them into the `distributions` table, keyed on the
(fund_id, ex_date, source) unique constraint so re-runs and backfills are
idempotent. Every row records its `source` for provenance.

This is the provider-agnostic half of the pipeline: it validates adapter output,
upserts canonical rows and counts inserted/updated/skipped/failed. Provider-specific
fetch/parse lives in ``app/sources/distributions.py``.

Compute boundary (see AGENTS.md): this module ONLY collects, normalises and
persists official distribution *observations*. It never forecasts dividends,
projects yield, computes tax treatment, total return or PnL — those belong in the
Rust GUI / local pricer. One bad row never fails the whole job; it is isolated and
counted.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Distribution, Fund
from app.sources.distributions import DistributionRecord, DistributionSource

# Distribution fields that may change between fetches for the same
# (fund, ex_date, source) key. ``ex_date``/``source`` are the identity key.
_MUTABLE_FIELDS = (
    "record_date",
    "payment_date",
    "distribution_date",
    "amount",
    "currency",
    "distribution_type",
    "frequency",
    "share_class",
    "status",
    "raw_payload_json",
)


@dataclass
class DistributionCounts:
    inserted: int = 0
    updated: int = 0
    failed: int = 0  # rows that could not be normalised/upserted (bad rows)
    skipped: int = 0  # matched an existing row with no change (idempotent re-run)
    fetched: int = 0  # rows the source returned for this fund

    def add(self, other: DistributionCounts) -> None:
        self.inserted += other.inserted
        self.updated += other.updated
        self.failed += other.failed
        self.skipped += other.skipped
        self.fetched += other.fetched


def _record_fields(record: DistributionRecord) -> dict[str, object]:
    return {
        "record_date": record.record_date,
        "payment_date": record.payment_date,
        "distribution_date": record.distribution_date,
        "amount": record.amount,
        "currency": record.currency,
        "distribution_type": record.distribution_type,
        "frequency": record.frequency,
        "share_class": record.share_class,
        "status": record.status,
        "raw_payload_json": record.raw_payload,
    }


def _apply(existing: Distribution, fields: dict[str, object]) -> bool:
    """Update mutable fields in place; return True if anything changed."""
    changed = False
    for field in _MUTABLE_FIELDS:
        if getattr(existing, field) != fields[field]:
            setattr(existing, field, fields[field])
            changed = True
    return changed


async def ingest_distributions_for_fund(
    session: AsyncSession,
    fund: Fund,
    source: DistributionSource,
    *,
    url: str | None = None,
) -> DistributionCounts:
    """Fetch + idempotently upsert one fund's distributions from ``source``.

    Live issuer adapters need the ``session`` (budget/fetch-log/cache) and take an
    explicit ``url`` download override; the offline fixture ignores both. A row that
    cannot be normalised/upserted (missing amount/currency/date, or a bad cell) is
    isolated (counted as ``failed``/bad row), never failing the whole fund.
    """
    counts = DistributionCounts()
    records = await source.fetch(isin=fund.isin, session=session, url=url)
    counts.fetched = len(records)

    for record in records:
        try:
            if record.amount is None or not record.currency:
                raise ValueError("distribution missing amount/currency")
            fields = _record_fields(record)
            existing = await session.scalar(
                select(Distribution).where(
                    Distribution.fund_id == fund.id,
                    Distribution.ex_date == record.ex_date,
                    Distribution.source == record.source,
                )
            )
            if existing is not None:
                if _apply(existing, fields):
                    counts.updated += 1
                else:
                    counts.skipped += 1
            else:
                session.add(
                    Distribution(
                        fund_id=fund.id,
                        ex_date=record.ex_date,
                        source=record.source,
                        **fields,
                    )
                )
                counts.inserted += 1
        except Exception:  # noqa: BLE001 - isolate one bad row; count + continue
            counts.failed += 1

    await session.flush()
    return counts
