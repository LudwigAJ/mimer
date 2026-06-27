"""Reference-rate read shaping (bounded SQL-backed read models).

Read-only helpers over ``reference_rates`` for the GUI / local pricer to consume:
a filtered list, the latest observation per series, and a single series'
time-series. All bounded and SQL-backed — no curve building, interpolation or
forward-rate computation happens here (see AGENTS.md compute boundary). The
backend hands the local pricer the official observations; the pricer builds
curves.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ReferenceRate

# Bound on the latest-per-series scan: reference data is small, but cap the
# underlying query so an unfiltered "latest" can never load an unbounded set.
_LATEST_SCAN_CAP = 5000


def _apply_filters(
    stmt,  # type: ignore[no-untyped-def]
    *,
    currency: str | None = None,
    country_or_region: str | None = None,
    rate_family: str | None = None,
    rate_name: str | None = None,
    tenor: str | None = None,
    source: str | None = None,
    rate_date: date | None = None,
    start_date: date | None = None,
    end_date: date | None = None,
):
    if currency is not None:
        stmt = stmt.where(ReferenceRate.currency == currency.upper())
    if country_or_region is not None:
        stmt = stmt.where(ReferenceRate.country_or_region == country_or_region.lower())
    if rate_family is not None:
        stmt = stmt.where(ReferenceRate.rate_family == rate_family.lower())
    if rate_name is not None:
        stmt = stmt.where(ReferenceRate.rate_name == rate_name.upper())
    if tenor is not None:
        stmt = stmt.where(ReferenceRate.tenor == tenor.upper())
    if source is not None:
        stmt = stmt.where(ReferenceRate.source == source)
    if rate_date is not None:
        stmt = stmt.where(ReferenceRate.rate_date == rate_date)
    if start_date is not None:
        stmt = stmt.where(ReferenceRate.rate_date >= start_date)
    if end_date is not None:
        stmt = stmt.where(ReferenceRate.rate_date <= end_date)
    return stmt


async def list_reference_rates(
    session: AsyncSession,
    *,
    currency: str | None = None,
    country_or_region: str | None = None,
    rate_family: str | None = None,
    rate_name: str | None = None,
    tenor: str | None = None,
    source: str | None = None,
    rate_date: date | None = None,
    start_date: date | None = None,
    end_date: date | None = None,
    limit: int = 200,
) -> list[ReferenceRate]:
    """Filtered reference-rate observations, newest first (bounded by ``limit``)."""
    stmt = _apply_filters(
        select(ReferenceRate),
        currency=currency,
        country_or_region=country_or_region,
        rate_family=rate_family,
        rate_name=rate_name,
        tenor=tenor,
        source=source,
        rate_date=rate_date,
        start_date=start_date,
        end_date=end_date,
    )
    stmt = stmt.order_by(
        ReferenceRate.rate_date.desc(),
        ReferenceRate.rate_name.asc(),
        ReferenceRate.tenor_months.asc().nulls_first(),
        ReferenceRate.id.desc(),
    ).limit(limit)
    return list((await session.execute(stmt)).scalars().all())


def _series_key(row: ReferenceRate) -> tuple:
    return (row.rate_name, row.tenor, row.currency, row.country_or_region, row.source)


async def latest_reference_rates(
    session: AsyncSession,
    *,
    currency: str | None = None,
    country_or_region: str | None = None,
    rate_family: str | None = None,
    rate_name: str | None = None,
    source: str | None = None,
    limit: int = 500,
) -> list[ReferenceRate]:
    """The newest observation per distinct series, matching the filters.

    A series is ``(rate_name, tenor, currency, country_or_region, source)``. The
    underlying scan is bounded and NULL-``tenor``-safe (deduped in Python, so a
    policy rate's NULL tenor never breaks a SQL self-join). Sorted for stable GUI
    display: currency, family, name, then tenor.
    """
    stmt = _apply_filters(
        select(ReferenceRate),
        currency=currency,
        country_or_region=country_or_region,
        rate_family=rate_family,
        rate_name=rate_name,
        source=source,
    )
    stmt = stmt.order_by(ReferenceRate.rate_date.desc(), ReferenceRate.id.desc()).limit(
        _LATEST_SCAN_CAP
    )
    rows = (await session.execute(stmt)).scalars().all()
    latest: dict[tuple, ReferenceRate] = {}
    for row in rows:  # rows are newest-first, so the first per series wins
        latest.setdefault(_series_key(row), row)
    result = sorted(
        latest.values(),
        key=lambda r: (r.currency, r.rate_family, r.rate_name, r.tenor_months or 0),
    )
    return result[:limit]


@dataclass
class ReferenceRatePoint:
    rate_date: date
    rate_value: object
    unit: str
    source: str
    status: str | None
    tenor: str | None


@dataclass
class ReferenceRateSeries:
    rate_name: str | None
    currency: str | None
    country_or_region: str | None
    tenor: str | None
    source: str | None
    unit: str | None
    points: list[ReferenceRatePoint]


async def reference_rate_time_series(
    session: AsyncSession,
    *,
    rate_name: str,
    currency: str | None = None,
    country_or_region: str | None = None,
    tenor: str | None = None,
    source: str | None = None,
    start_date: date | None = None,
    end_date: date | None = None,
    limit: int = 730,
) -> ReferenceRateSeries:
    """One series' observations, oldest first (GUI-friendly chart shape).

    Specify ``rate_name`` (plus ``tenor`` for a par-yield series like
    US_TREASURY_PAR_YIELD) to pin a single series. No interpolation / gap-filling:
    only the dates the source actually published are returned.
    """
    stmt = _apply_filters(
        select(ReferenceRate),
        currency=currency,
        country_or_region=country_or_region,
        rate_name=rate_name,
        tenor=tenor,
        source=source,
        start_date=start_date,
        end_date=end_date,
    )
    stmt = stmt.order_by(
        ReferenceRate.rate_date.asc(),
        ReferenceRate.tenor_months.asc().nulls_first(),
        ReferenceRate.id.asc(),
    )
    rows = list((await session.execute(stmt)).scalars().all())
    rows = rows[-limit:] if limit is not None else rows
    points = [
        ReferenceRatePoint(
            rate_date=r.rate_date,
            rate_value=r.rate_value,
            unit=r.unit,
            source=r.source,
            status=r.status,
            tenor=r.tenor,
        )
        for r in rows
    ]
    head = rows[-1] if rows else None
    return ReferenceRateSeries(
        rate_name=rate_name.upper() if rate_name else None,
        currency=(currency.upper() if currency else (head.currency if head else None)),
        country_or_region=(
            country_or_region.lower()
            if country_or_region
            else (head.country_or_region if head else None)
        ),
        tenor=(tenor.upper() if tenor else (head.tenor if head else None)),
        source=(source if source else (head.source if head else None)),
        unit=(head.unit if head else None),
        points=points,
    )


async def count_reference_rates(session: AsyncSession) -> int:
    return (await session.scalar(select(func.count()).select_from(ReferenceRate))) or 0


async def latest_reference_rate_date(
    session: AsyncSession, *, currency: str | None = None
) -> date | None:
    stmt = select(func.max(ReferenceRate.rate_date))
    if currency is not None:
        stmt = stmt.where(ReferenceRate.currency == currency.upper())
    return await session.scalar(stmt)
