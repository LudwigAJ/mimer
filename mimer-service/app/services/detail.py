"""Fund detail aggregation — one bounded payload for the Fund Detail page."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    Distribution,
    DocumentSnapshot,
    FundHolding,
    FundListing,
    JobRun,
    Price,
    SecurityIdentifier,
)
from app.schemas.dashboard import ListingWithPrice
from app.schemas.detail import (
    FundDetailResponse,
    FundFreshness,
    ListingDetail,
    PriceHistorySummary,
    PricePointRead,
)
from app.schemas.distribution import DistributionRead
from app.schemas.document import DocumentRead
from app.schemas.fund import FundRead
from app.schemas.holding import HoldingRead
from app.schemas.identifier import SecurityIdentifierRead
from app.schemas.job import JobRunRead
from app.services import freshness as freshness_service
from app.services import funds as funds_service
from app.services import holdings_ingestion as holdings_service

# Bounds — detail is richer than the dashboard but still not unlimited history.
_PRICE_POINTS_CAP = 400
_DISTRIBUTIONS_LIMIT = 24
_HOLDINGS_LIMIT = 100
_JOB_RUNS_LIMIT = 20
_PCT_Q = Decimal("0.0001")


def _price_summary(points: list[Price]) -> PriceHistorySummary:
    if not points:
        return PriceHistorySummary(points=0)
    first, last = points[0], points[-1]
    change_pct = None
    if first.price and first.price != 0:
        change_pct = ((last.price - first.price) / first.price).quantize(
            _PCT_Q, rounding=ROUND_HALF_UP
        )
    return PriceHistorySummary(
        points=len(points),
        start_date=first.price_date,
        end_date=last.price_date,
        first=first.price,
        last=last.price,
        change_pct=change_pct,
    )


def _domain_freshness(values: list[date | datetime | None], kind: str) -> str:
    present = [v for v in values if v is not None]
    if not present:
        return freshness_service.MISSING
    return freshness_service.freshness_state(max(present), kind=kind)


async def build_fund_detail(
    session: AsyncSession,
    fund_id: int,
    *,
    include_prices: bool = True,
    include_holdings: bool = True,
    history_days: int = 365,
) -> FundDetailResponse:
    fund = await funds_service.get_fund(session, fund_id)

    listings = list(
        (
            await session.execute(
                select(FundListing)
                .where(FundListing.fund_id == fund_id)
                .order_by(FundListing.ticker)
            )
        )
        .scalars()
        .all()
    )
    listing_ids = [ln.id for ln in listings]
    cutoff = date.today() - timedelta(days=history_days)

    # Prices per listing within the history window (ascending).
    prices_by_listing: dict[int, list[Price]] = {ln.id: [] for ln in listings}
    if listing_ids:
        rows = (
            (
                await session.execute(
                    select(Price)
                    .where(Price.fund_listing_id.in_(listing_ids), Price.price_date >= cutoff)
                    .order_by(Price.fund_listing_id, Price.price_date.asc(), Price.id.asc())
                )
            )
            .scalars()
            .all()
        )
        for price in rows:
            prices_by_listing[price.fund_listing_id].append(price)

    listing_details: list[ListingDetail] = []
    for listing in listings:
        series = prices_by_listing.get(listing.id, [])
        detail = ListingDetail(
            **ListingWithPrice.model_validate(listing).model_dump(),
            price_summary=_price_summary(series),
        )
        if series:
            latest = series[-1]
            detail.latest_price = latest.price
            detail.latest_price_date = latest.price_date
            detail.latest_price_currency = latest.currency
            detail.price_source = latest.source
        detail.freshness = freshness_service.freshness_state(listing.last_price_at, kind="price")
        if include_prices:
            capped = series[-_PRICE_POINTS_CAP:]
            detail.prices = [
                PricePointRead(
                    date=p.price_date, value=p.price, currency=p.currency, source=p.source
                )
                for p in capped
            ]
        listing_details.append(detail)

    distributions = list(
        (
            await session.execute(
                select(Distribution)
                .where(Distribution.fund_id == fund_id)
                .order_by(Distribution.ex_date.desc())
                .limit(_DISTRIBUTIONS_LIMIT)
            )
        )
        .scalars()
        .all()
    )

    holdings: list[FundHolding] = []
    if include_holdings:
        holdings = await holdings_service.latest_holdings_for_fund(
            session, fund_id, limit=_HOLDINGS_LIMIT
        )

    documents = list(
        (
            await session.execute(
                select(DocumentSnapshot)
                .where(DocumentSnapshot.fund_id == fund_id)
                .order_by(DocumentSnapshot.created_at.desc())
            )
        )
        .scalars()
        .all()
    )

    # Job runs targeting this fund or any of its listings.
    job_filter = JobRun.fund_id == fund_id
    if listing_ids:
        job_filter = or_(job_filter, JobRun.fund_listing_id.in_(listing_ids))
    job_runs = list(
        (
            await session.execute(
                select(JobRun).where(job_filter).order_by(JobRun.id.desc()).limit(_JOB_RUNS_LIMIT)
            )
        )
        .scalars()
        .all()
    )

    id_filter = SecurityIdentifier.fund_id == fund_id
    if listing_ids:
        id_filter = or_(id_filter, SecurityIdentifier.fund_listing_id.in_(listing_ids))
    identifiers = list(
        (
            await session.execute(
                select(SecurityIdentifier)
                .where(id_filter)
                .order_by(SecurityIdentifier.scheme, SecurityIdentifier.value)
            )
        )
        .scalars()
        .all()
    )

    freshness = FundFreshness(
        prices=_domain_freshness([ln.last_price_at for ln in listings], "price"),
        distributions=_domain_freshness([d.ex_date for d in distributions], "distribution"),
        holdings=_domain_freshness([h.as_of_date for h in holdings], "holdings"),
        documents=_domain_freshness([d.document_date for d in documents], "document"),
        fund_facts=_domain_freshness([fund.last_refreshed_at], "fund_facts"),
    )

    distribution_reads: list[DistributionRead] = []
    for dist in distributions:
        read = DistributionRead.model_validate(dist)
        read.fund_name = fund.name
        distribution_reads.append(read)

    document_reads: list[DocumentRead] = []
    for doc in documents:
        read = DocumentRead.model_validate(doc)
        read.fund_name = fund.name
        document_reads.append(read)

    return FundDetailResponse(
        fund=FundRead.model_validate(fund),
        listings=listing_details,
        distributions=distribution_reads,
        holdings=[HoldingRead.model_validate(h) for h in holdings],
        documents=document_reads,
        job_runs=[JobRunRead.model_validate(r) for r in job_runs],
        identifiers=[SecurityIdentifierRead.model_validate(i) for i in identifiers],
        freshness=freshness,
    )
