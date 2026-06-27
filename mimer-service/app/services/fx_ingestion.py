"""FX ingestion service.

Fetches FX rates from an `FxSource` adapter and upserts them into ``fx_rates``,
keyed on the (rate_date, base_currency, quote_currency, source) unique constraint
(``uq_fx_rate``) so re-runs and backfills are idempotent. Every row records its
``source`` for provenance, and distinct sources coexist for the same pair/date.

This is the provider-agnostic half of the pipeline: it decides *which* currencies
are needed (explicit args, else inferred from the data the service actually
holds), calls the adapter, upserts canonical rows and counts inserted / updated /
failed. One bad currency pair never fails the whole job. Provider-specific
fetch/parse lives in ``app/sources/fx.py``; lookup/triangulation in
``app/services/fx.py``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    Distribution,
    FundHolding,
    FundListing,
    FxRate,
    PortfolioPosition,
    Price,
    Workspace,
)
from app.services.fx import normalise_pence
from app.sources.fx import FxRateRecord, FxSource

# Rate fields that may change between fetches for the same
# (rate_date, base, quote, source) key. The rest is identity.
_MUTABLE_FIELDS = ("rate", "status", "raw_payload_json")


@dataclass
class FxIngestCounts:
    inserted: int = 0
    updated: int = 0
    failed: int = 0


def _norm_currency(value: str | None) -> str | None:
    """Normalise a currency code (GBX -> GBP, upper-case), dropping blanks."""
    if not value or not value.strip():
        return None
    _amount, code = normalise_pence(None, value)
    return code or None


async def infer_currencies(session: AsyncSession) -> set[str]:
    """Currencies that appear anywhere we may need to value/convert.

    Looks at workspace base currencies, listing trading/quote currencies,
    position cost currencies, price currencies, distribution currencies and
    holding currencies — so ``fx_ingestion`` covers what the portfolio actually
    touches without being told.
    """
    found: set[str] = set()

    async def _collect(stmt) -> None:
        for value in (await session.execute(stmt)).scalars().all():
            code = _norm_currency(value)
            if code:
                found.add(code)

    await _collect(select(Workspace.base_currency).distinct())
    await _collect(select(FundListing.trading_currency).distinct())
    await _collect(select(FundListing.currency_unit).distinct())
    await _collect(select(PortfolioPosition.cost_currency).distinct())
    await _collect(select(Price.currency).distinct())
    await _collect(select(Distribution.currency).distinct())
    await _collect(select(FundHolding.currency).distinct())
    return found


def _record_fields(record: FxRateRecord) -> dict[str, object]:
    return {
        "rate": record.rate,
        "status": record.status,
        "raw_payload_json": record.raw_payload,
    }


def _apply(existing: FxRate, fields: dict[str, object]) -> bool:
    """Update mutable fields in place; return True if anything changed."""
    changed = False
    for field_name in _MUTABLE_FIELDS:
        if getattr(existing, field_name) != fields[field_name]:
            setattr(existing, field_name, fields[field_name])
            changed = True
    return changed


async def ingest_fx_rates(
    session: AsyncSession,
    source: FxSource,
    *,
    base_currency: str | None = None,
    quote_currencies: list[str] | None = None,
    start_date: date | None = None,
    end_date: date | None = None,
) -> FxIngestCounts:
    """Fetch + upsert FX rates for one base against a set of quote currencies.

    ``base_currency`` defaults to the configured reporting base; quotes default
    to every other currency inferred from the data. Returns insert/update/failed
    counts; an exception on a single record is isolated and counted as failed.
    """
    from app.core.config import get_settings

    base = (_norm_currency(base_currency) or get_settings().base_currency).upper()

    if quote_currencies:
        quotes = {q for q in (_norm_currency(c) for c in quote_currencies) if q}
    else:
        quotes = await infer_currencies(session)
    quotes.discard(base)

    counts = FxIngestCounts()
    if not quotes:
        return counts

    records = await source.fetch_rates(
        base_currency=base,
        quote_currencies=sorted(quotes),
        start_date=start_date,
        end_date=end_date,
    )

    for record in records:
        try:
            if record.rate is None:
                raise ValueError(f"missing rate for {record.base_currency}/{record.quote_currency}")
            fields = _record_fields(record)
            existing = await session.scalar(
                select(FxRate).where(
                    FxRate.rate_date == record.rate_date,
                    FxRate.base_currency == record.base_currency,
                    FxRate.quote_currency == record.quote_currency,
                    FxRate.source == record.source,
                )
            )
            if existing is not None:
                if _apply(existing, fields):
                    counts.updated += 1
            else:
                session.add(
                    FxRate(
                        rate_date=record.rate_date,
                        base_currency=record.base_currency,
                        quote_currency=record.quote_currency,
                        source=record.source,
                        **fields,
                    )
                )
                counts.inserted += 1
        except Exception:
            counts.failed += 1

    await session.flush()
    return counts
