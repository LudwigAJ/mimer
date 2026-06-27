"""Price ingestion service.

Fetches daily prices from a `PriceSource` adapter and upserts them into the
`prices` table, keyed on the (fund_listing_id, price_date, source) unique
constraint so re-runs and backfills are idempotent. Every row records its
`source` for provenance.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import FundListing, Price
from app.sources.base import PriceSource


@dataclass
class IngestCounts:
    inserted: int = 0
    updated: int = 0
    failed: int = 0


async def ingest_prices_for_listing(
    session: AsyncSession, listing: FundListing, source: PriceSource
) -> IngestCounts:
    counts = IngestCounts()
    fallback_currency = listing.currency_unit or listing.trading_currency or "UNK"

    points = await source.fetch(
        ticker=listing.ticker,
        exchange=listing.exchange,
        currency=fallback_currency,
    )

    for point in points:
        try:
            existing = await session.scalar(
                select(Price).where(
                    Price.fund_listing_id == listing.id,
                    Price.price_date == point.price_date,
                    Price.source == source.name,
                )
            )
            currency = point.currency or fallback_currency
            if existing is not None:
                existing.price = point.price
                existing.currency = currency
                counts.updated += 1
            else:
                session.add(
                    Price(
                        fund_listing_id=listing.id,
                        price_date=point.price_date,
                        price=point.price,
                        currency=currency,
                        source=source.name,
                    )
                )
                counts.inserted += 1
        except Exception:
            counts.failed += 1

    if points and (counts.inserted or counts.updated):
        listing.last_price_at = datetime.now(UTC)
        if listing.status == "pending":
            listing.status = "active"

    await session.flush()
    return counts
