"""Reference-rate ingestion service.

The provider-agnostic half of official/reference-rate collection: it resolves a
``RatesSource`` adapter by name, fetches the (filtered) observations and upserts
them into ``reference_rates`` keyed on the
``(rate_date, currency, country_or_region, rate_family, rate_name, tenor, source)``
unique constraint (``uq_reference_rate``) so re-runs and backfills are idempotent.
Every row records its ``source`` for provenance, and distinct sources coexist for
the same series/date.

Compute boundary (see AGENTS.md): this module ONLY collects, normalises and
persists official observations. It never fits a curve, bootstraps, interpolates,
computes a forward rate, builds discount factors or prices a bond — those belong
in the Rust GUI / local pricer. One bad observation never fails the whole job; it
is isolated and counted.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ReferenceRate
from app.sources.rates import ReferenceRateRecord, get_rates_source

# Observation fields that may change between fetches for the same identity key.
# The unique-constraint columns are identity; everything else here is mutable.
_MUTABLE_FIELDS = (
    "rate_value",
    "unit",
    "tenor_months",
    "observed_at",
    "status",
    "source_url",
    "raw_payload_json",
)


@dataclass
class ReferenceRateIngestionResult:
    """Counters for one reference-rate ingestion run."""

    source: str = ""
    is_fixture: bool = True
    selected: int = 0
    inserted: int = 0
    updated: int = 0
    skipped: int = 0  # matched an existing row with no change (idempotent re-run)
    failed: int = 0
    start_date: date | None = None
    end_date: date | None = None

    def message(self) -> str:
        return (
            f"source={self.source} selected={self.selected} inserted={self.inserted} "
            f"updated={self.updated} skipped={self.skipped} failed={self.failed} "
            f"start={self.start_date} end={self.end_date}"
        )


def _record_fields(record: ReferenceRateRecord) -> dict[str, object]:
    return {
        "rate_value": record.rate_value,
        "unit": record.unit,
        "tenor_months": record.tenor_months,
        "observed_at": record.observed_at,
        "status": record.status,
        "source_url": record.source_url,
        "raw_payload_json": record.raw_payload,
    }


def _apply(existing: ReferenceRate, fields: dict[str, object]) -> bool:
    """Update mutable fields in place; return True if anything changed."""
    changed = False
    for field_name in _MUTABLE_FIELDS:
        if getattr(existing, field_name) != fields[field_name]:
            setattr(existing, field_name, fields[field_name])
            changed = True
    return changed


async def _find_existing(
    session: AsyncSession, record: ReferenceRateRecord
) -> ReferenceRate | None:
    """Look up the row matching the full identity key (NULL ``tenor`` aware)."""
    stmt = select(ReferenceRate).where(
        ReferenceRate.rate_date == record.rate_date,
        ReferenceRate.currency == record.currency,
        ReferenceRate.country_or_region == record.country_or_region,
        ReferenceRate.rate_family == record.rate_family,
        ReferenceRate.rate_name == record.rate_name,
        ReferenceRate.source == record.source,
    )
    # NULL tenor participates in the unique key via an explicit IS NULL match —
    # ``== None`` would generate the wrong SQL and break overnight/policy upserts.
    if record.tenor is None:
        stmt = stmt.where(ReferenceRate.tenor.is_(None))
    else:
        stmt = stmt.where(ReferenceRate.tenor == record.tenor)
    return await session.scalar(stmt)


async def ingest_reference_rates(
    session: AsyncSession,
    source: str = "rates_fixture",
    *,
    currency: str | None = None,
    country_or_region: str | None = None,
    rate_family: str | None = None,
    start_date: date | None = None,
    end_date: date | None = None,
    limit: int = 1000,
) -> ReferenceRateIngestionResult:
    """Fetch + idempotently upsert official/reference rates for the given filters.

    ``source`` names the adapter (offline ``rates_fixture`` by default). Filters
    (currency / country_or_region / rate_family / date range) narrow what the
    provider returns; ``limit`` bounds how many observations are processed. Returns
    insert/update/skip/failed counts; an exception on a single observation is
    isolated and counted as failed.
    """
    src = get_rates_source(source)
    result = ReferenceRateIngestionResult(
        source=src.name,
        is_fixture=src.name.endswith("_fixture"),
        start_date=start_date,
        end_date=end_date,
    )

    records = await src.fetch_rates(
        session=session,
        currency=currency,
        country_or_region=country_or_region,
        rate_family=rate_family,
        start_date=start_date,
        end_date=end_date,
    )
    if limit is not None and limit >= 0:
        records = records[:limit]
    result.selected = len(records)

    for record in records:
        try:
            if record.rate_value is None:
                raise ValueError(f"missing rate_value for {record.rate_name}")
            fields = _record_fields(record)
            existing = await _find_existing(session, record)
            if existing is not None:
                if _apply(existing, fields):
                    result.updated += 1
                else:
                    result.skipped += 1
            else:
                session.add(
                    ReferenceRate(
                        rate_date=record.rate_date,
                        currency=record.currency,
                        country_or_region=record.country_or_region,
                        rate_family=record.rate_family,
                        rate_name=record.rate_name,
                        tenor=record.tenor,
                        source=record.source,
                        **fields,
                    )
                )
                result.inserted += 1
        except Exception:  # noqa: BLE001 - isolate one bad observation; count + continue
            result.failed += 1

    await session.flush()
    return result
