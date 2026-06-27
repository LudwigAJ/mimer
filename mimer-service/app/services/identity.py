"""Identity orchestration: resolve an identifier, then create/reuse the fund and
listing, record provenance, and queue backfill jobs.

Auditable rules:
* Auto-create only on a *single, high-confidence* candidate that carries an ISIN.
  Anything else (zero, many, low confidence, or no ISIN) returns candidates and
  creates nothing — we never guess which fund an ambiguous ticker means.
* Funds dedupe on ISIN; listings dedupe on (fund, ticker, exchange). A fund can
  therefore accumulate multiple listings across exchanges/currencies.
* Every resolved identifier is written to `security_identifiers` with its source,
  confidence and raw payload.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFoundError
from app.db.models import Fund, FundListing, JobRun, SecurityIdentifier
from app.schemas.instrument import (
    InstrumentAmbiguousResponse,
    InstrumentCandidate,
    InstrumentCreated,
    InstrumentRequest,
    InstrumentResolveResponse,
    ResolvedInstrument,
)
from app.services import resolver

# Backfill jobs queued after a successful resolution. Only price_ingestion has a
# real worker in this iteration; the rest are queued for a future worker.
BACKFILL_JOB_TYPES = [
    "price_ingestion",
    "distribution_ingestion",
    "issuer_facts_ingestion",
    "issuer_holdings_ingestion",
    "document_snapshot_ingestion",
]
_LISTING_SCOPED_JOBS = {"price_ingestion"}


async def _upsert_identifier(
    session: AsyncSession,
    *,
    scheme: str,
    value: str,
    candidate: InstrumentCandidate,
    fund_id: int | None,
    fund_listing_id: int | None,
    exchange: str | None,
    currency: str | None,
) -> None:
    existing = await session.scalar(
        select(SecurityIdentifier).where(
            SecurityIdentifier.scheme == scheme,
            SecurityIdentifier.value == value,
            SecurityIdentifier.source == candidate.source,
            SecurityIdentifier.exchange == exchange,
            SecurityIdentifier.currency == currency,
        )
    )
    raw = candidate.model_dump()
    if existing is None:
        session.add(
            SecurityIdentifier(
                scheme=scheme,
                value=value,
                fund_id=fund_id,
                fund_listing_id=fund_listing_id,
                exchange=exchange,
                currency=currency,
                source=candidate.source,
                confidence=candidate.confidence,
                raw_payload_json=raw,
            )
        )
    else:
        existing.fund_id = fund_id
        existing.fund_listing_id = fund_listing_id
        existing.confidence = candidate.confidence
        existing.raw_payload_json = raw


async def _record_identifiers(
    session: AsyncSession,
    candidate: InstrumentCandidate,
    req: InstrumentRequest,
    fund: Fund,
    listing: FundListing | None,
) -> None:
    if candidate.isin:
        await _upsert_identifier(
            session,
            scheme="isin",
            value=candidate.isin,
            candidate=candidate,
            fund_id=fund.id,
            fund_listing_id=None,
            exchange=None,
            currency=None,
        )
    if listing is not None and candidate.figi:
        await _upsert_identifier(
            session,
            scheme="figi",
            value=candidate.figi,
            candidate=candidate,
            fund_id=fund.id,
            fund_listing_id=listing.id,
            exchange=candidate.exchange,
            currency=candidate.trading_currency,
        )
    if listing is not None and candidate.ticker:
        await _upsert_identifier(
            session,
            scheme="ticker",
            value=candidate.ticker,
            candidate=candidate,
            fund_id=fund.id,
            fund_listing_id=listing.id,
            exchange=candidate.exchange,
            currency=candidate.trading_currency,
        )
    # Preserve the originally submitted scheme even if the candidate omitted it
    # (e.g. SEDOL/CUSIP, which the crosswalk still wants to record).
    if req.symbol_type in ("sedol", "cusip"):
        await _upsert_identifier(
            session,
            scheme=req.symbol_type,
            value=req.symbol,
            candidate=candidate,
            fund_id=fund.id,
            fund_listing_id=listing.id if listing else None,
            exchange=None,
            currency=None,
        )


async def create_from_symbol(
    session: AsyncSession, req: InstrumentRequest
) -> InstrumentResolveResponse | InstrumentAmbiguousResponse:
    candidates = await resolver.resolve_identifier(req, session=session)
    if not candidates:
        raise NotFoundError(
            "No instrument matched the submitted identifier", code="instrument_not_found"
        )

    confident = [c for c in candidates if c.isin and c.confidence == "high"]
    if not (len(candidates) == 1 and len(confident) == 1):
        return InstrumentAmbiguousResponse(
            message=(
                "Could not confidently identify a single instrument. "
                "Submit an ISIN/FIGI, or add exchange + currency hints."
            ),
            candidates=candidates,
        )

    chosen = confident[0]
    now = datetime.now(UTC)

    # Fund: dedupe on ISIN.
    fund = await session.scalar(select(Fund).where(Fund.isin == chosen.isin))
    created_fund = False
    if fund is None:
        fund = Fund(isin=chosen.isin, name=chosen.name or chosen.isin, status="pending")
        session.add(fund)
        await session.flush()
        created_fund = True

    # Listing: dedupe on (fund, ticker, exchange). Needs a ticker to create one.
    listing: FundListing | None = None
    created_listing = False
    if chosen.ticker:
        listing = await session.scalar(
            select(FundListing).where(
                FundListing.fund_id == fund.id,
                FundListing.ticker == chosen.ticker,
                FundListing.exchange == chosen.exchange,
            )
        )
        if listing is None:
            listing = FundListing(
                fund_id=fund.id,
                ticker=chosen.ticker,
                exchange=chosen.exchange,
                trading_currency=chosen.trading_currency,
                currency_unit=chosen.trading_currency,
                figi=chosen.figi,
                status="pending",
                last_resolved_at=now,
            )
            session.add(listing)
            await session.flush()
            created_listing = True
        else:
            listing.last_resolved_at = now

    await _record_identifiers(session, chosen, req, fund, listing)

    # Queue backfill jobs.
    job_run_ids: list[int] = []
    for job_type in BACKFILL_JOB_TYPES:
        run = JobRun(
            job_type=job_type,
            status="queued",
            fund_id=fund.id,
            fund_listing_id=listing.id if (job_type in _LISTING_SCOPED_JOBS and listing) else None,
        )
        session.add(run)
        await session.flush()
        job_run_ids.append(run.id)

    await session.commit()

    return InstrumentResolveResponse(
        status="pending",
        fund_id=fund.id,
        fund_listing_id=listing.id if listing else None,
        resolved=ResolvedInstrument(
            isin=chosen.isin,
            figi=chosen.figi,
            ticker=chosen.ticker,
            exchange=chosen.exchange,
            trading_currency=chosen.trading_currency,
            name=chosen.name,
        ),
        created=InstrumentCreated(fund=created_fund, listing=created_listing),
        job_run_ids=job_run_ids,
    )
