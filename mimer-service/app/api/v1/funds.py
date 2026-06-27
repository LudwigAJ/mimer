"""Fund endpoints."""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Query

from app.api.deps import SessionDep
from app.schemas.common import ListResponse
from app.schemas.constituent import FundConstituentsResponse
from app.schemas.detail import FundDetailResponse
from app.schemas.distribution import DistributionRead
from app.schemas.document import DocumentRead
from app.schemas.fund import FundListingRead, FundRead
from app.schemas.holding import FundHoldingsResponse
from app.services import constituents as constituents_service
from app.services import detail as detail_service
from app.services import documents as documents_service
from app.services import funds as funds_service
from app.services import holdings as holdings_service

router = APIRouter(prefix="/funds", tags=["funds"])


@router.get("", response_model=ListResponse[FundRead])
async def list_funds(session: SessionDep) -> ListResponse[FundRead]:
    funds = await funds_service.list_funds(session)
    return ListResponse.of([FundRead.model_validate(f) for f in funds])


@router.get("/{fund_id}", response_model=FundRead)
async def get_fund(fund_id: int, session: SessionDep) -> FundRead:
    fund = await funds_service.get_fund(session, fund_id)
    return FundRead.model_validate(fund)


@router.get("/{fund_id}/detail", response_model=FundDetailResponse)
async def get_fund_detail(
    fund_id: int,
    session: SessionDep,
    include_prices: bool = Query(default=True),
    include_holdings: bool = Query(default=True),
    history_days: int = Query(default=365, ge=1, le=3650),
) -> FundDetailResponse:
    """Hydrate the Fund Detail page (facts, listings, prices, distributions,
    holdings, documents, jobs, identifiers) in one bounded call."""
    return await detail_service.build_fund_detail(
        session,
        fund_id,
        include_prices=include_prices,
        include_holdings=include_holdings,
        history_days=history_days,
    )


@router.get("/{fund_id}/listings", response_model=ListResponse[FundListingRead])
async def list_listings(fund_id: int, session: SessionDep) -> ListResponse[FundListingRead]:
    items = await funds_service.list_listings(session, fund_id)
    return ListResponse.of([FundListingRead.model_validate(i) for i in items])


@router.get("/{fund_id}/distributions", response_model=ListResponse[DistributionRead])
async def list_distributions(fund_id: int, session: SessionDep) -> ListResponse[DistributionRead]:
    items = await funds_service.list_fund_distributions(session, fund_id)
    return ListResponse.of([DistributionRead.model_validate(i) for i in items])


@router.get("/{fund_id}/holdings", response_model=FundHoldingsResponse)
async def list_holdings(
    fund_id: int,
    session: SessionDep,
    as_of_date: date | None = None,
    source: str | None = None,
    limit: int = Query(default=100, ge=1, le=1000),
    include_identity: bool = Query(default=False),
) -> FundHoldingsResponse:
    """Latest holdings snapshot for a fund (single source + as-of date), with
    its provenance. Pin a specific snapshot via ``source`` / ``as_of_date``.

    ``include_identity=true`` hydrates each holding with its resolved canonical
    instrument (constituent identity resolution). ``identity_status`` /
    ``holding_instrument_id`` are always present; the ``instrument`` summary only
    when requested."""
    if include_identity:
        return await constituents_service.build_fund_holdings_with_identity(
            session,
            fund_id,
            as_of_date=as_of_date,
            source=source,
            limit=limit,
            include_identity=True,
        )
    return await holdings_service.build_fund_holdings(
        session, fund_id, as_of_date=as_of_date, source=source, limit=limit
    )


@router.get("/{fund_id}/constituents", response_model=FundConstituentsResponse)
async def list_constituents(
    fund_id: int,
    session: SessionDep,
    status: str | None = Query(
        default=None,
        description="Filter by identity state: resolved | unresolved | ambiguous "
        "| not_found | failed.",
    ),
    include_prices: bool = Query(default=False),
) -> FundConstituentsResponse:
    """A fund's constituents with constituent identity-resolution state.

    Shows, per constituent, its resolution state, the resolved canonical
    instrument (when linked), and the next action — plus a rollup. ``status``
    filters the returned list (the rollup is always over the whole snapshot).

    ``include_prices=true`` attaches each resolved constituent's latest EOD price
    (instrument's primary listing): close, date, currency, source, status and
    derived freshness — what the GUI needs for the holdings/constituents table."""
    return await constituents_service.build_fund_constituents(
        session, fund_id, status=status, include_prices=include_prices
    )


@router.get("/{fund_id}/documents", response_model=ListResponse[DocumentRead])
async def list_documents(
    fund_id: int,
    session: SessionDep,
    document_type: str | None = None,
    latest_only: bool = Query(default=False),
    limit: int = Query(default=200, ge=1, le=1000),
) -> ListResponse[DocumentRead]:
    """Document snapshots for a fund (full history, newest first).

    Filter by ``document_type``; ``latest_only=true`` collapses to the newest
    snapshot per document type. Each item carries change-detection provenance
    (``change_status``/``previous_content_hash``)."""
    fund = await funds_service.get_fund(session, fund_id)
    items = await documents_service.list_fund_documents(
        session,
        fund_id,
        document_type=document_type,
        latest_only=latest_only,
        limit=limit,
    )
    reads = []
    for doc in items:
        read = DocumentRead.model_validate(doc)
        read.fund_name = fund.name
        reads.append(read)
    return ListResponse.of(reads)
