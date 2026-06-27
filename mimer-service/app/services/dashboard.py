"""Workspace dashboard aggregation.

Composes existing read services into one bounded snapshot for the GUI's main
workstation view. Scope is the workspace's *held* funds/listings (the funds its
positions reference), so the payload stays relevant and small. Deeper history is
served by the fund-detail and time-series endpoints.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    Distribution,
    DocumentSnapshot,
    Fund,
    FundHolding,
    FundListing,
    FxRate,
    PortfolioPosition,
    Price,
)
from app.schemas.dashboard import (
    DashboardResponse,
    DashboardWorkspace,
    FreshnessSummary,
    ListingWithPrice,
    PortfolioSummaryBlock,
)
from app.schemas.distribution import DistributionRead
from app.schemas.document import DocumentRead
from app.schemas.fund import FundRead
from app.schemas.fxrate import FxRateRead
from app.schemas.holding import HoldingRead
from app.schemas.job import JobRunRead
from app.services import alerts as alerts_service
from app.services import diagnostics as diagnostics_service
from app.services import exposure as exposure_service
from app.services import exposure_drift as exposure_drift_service
from app.services import exposure_recompute as exposure_recompute_service
from app.services import freshness as freshness_service
from app.services import holding_performance as holding_performance_service
from app.services import holdings_ingestion as holdings_service
from app.services import instrument_onboarding as onboarding_service
from app.services import jobs as jobs_service
from app.services import portfolio as portfolio_service
from app.services import portfolio_valuation as portfolio_valuation_service
from app.services import workspaces as workspaces_service
from app.services.fx import FxIndex, load_fx_index

# Bounds — keep the dashboard payload small; deeper views fetch their own data.
_DISTRIBUTIONS_LIMIT = 12
_HOLDINGS_LIMIT = 30
_DOCUMENTS_LIMIT = 12
_ALERTS_LIMIT = 20
_JOB_RUNS_LIMIT = 20

_MOCK_SOURCES = {"seed", "stub", "issuer_fixture"}
_INCOME_Q = Decimal("0.01")


def _document_read(doc: DocumentSnapshot, fund_names: dict[int, str]) -> DocumentRead:
    read = DocumentRead.model_validate(doc)
    read.fund_name = fund_names.get(doc.fund_id)
    return read


def _distribution_read(
    dist: Distribution,
    fund_names: dict[int, str],
    fx_index: FxIndex,
    base_currency: str,
) -> DistributionRead:
    read = DistributionRead.model_validate(dist)
    read.fund_name = fund_names.get(dist.fund_id)
    # Convenience base-currency overlay. The original amount/currency is kept;
    # FX is taken as of the payment date (else ex-date), latest rate on/before.
    as_of = dist.payment_date or dist.ex_date
    conv = fx_index.convert_amount(dist.amount, dist.currency, base_currency, as_of_date=as_of)
    read.base_currency = base_currency
    if conv.converted_amount is not None:
        read.amount_base = conv.converted_amount.quantize(_INCOME_Q, rounding=ROUND_HALF_UP)
        read.fx_rate = conv.rate
    read.fx_source = conv.source
    read.fx_status = conv.status
    return read


async def _latest_price_by_listing(
    session: AsyncSession, listing_ids: list[int]
) -> dict[int, Price]:
    if not listing_ids:
        return {}
    rows = (
        (
            await session.execute(
                select(Price)
                .where(Price.fund_listing_id.in_(listing_ids))
                .order_by(Price.fund_listing_id, Price.price_date.asc())
            )
        )
        .scalars()
        .all()
    )
    latest: dict[int, Price] = {}
    for price in rows:  # ascending date => last wins
        latest[price.fund_listing_id] = price
    return latest


def _domain_freshness(values: list[date | datetime | None], kind: str) -> str:
    """Freshness of the newest record in a domain (``missing`` if none)."""
    present = [v for v in values if v is not None]
    if not present:
        return freshness_service.MISSING
    return freshness_service.freshness_state(max(present), kind=kind)


async def build_dashboard(session: AsyncSession, workspace_id: int) -> DashboardResponse:
    workspace = await workspaces_service.get_workspace(session, workspace_id)

    # Held listings/funds drive the scope of the snapshot.
    positions = (
        (
            await session.execute(
                select(PortfolioPosition).where(PortfolioPosition.workspace_id == workspace_id)
            )
        )
        .scalars()
        .all()
    )
    listing_ids = sorted({p.fund_listing_id for p in positions})

    listings: list[FundListing] = []
    if listing_ids:
        listings = list(
            (await session.execute(select(FundListing).where(FundListing.id.in_(listing_ids))))
            .scalars()
            .all()
        )
    fund_ids = sorted({ln.fund_id for ln in listings})

    funds: list[Fund] = []
    if fund_ids:
        funds = list(
            (await session.execute(select(Fund).where(Fund.id.in_(fund_ids)).order_by(Fund.name)))
            .scalars()
            .all()
        )

    latest_prices = await _latest_price_by_listing(session, listing_ids)

    # --- summary (reuse the canonical computation) ---------------------------
    summary = await portfolio_service.build_summary(session, workspace_id)
    if not positions:
        summary_status = "empty"
    elif latest_prices and all(p.source in _MOCK_SOURCES for p in latest_prices.values()):
        summary_status = "seed"
    else:
        summary_status = "active"

    # --- listings with latest price + freshness ------------------------------
    listings_with_price: list[ListingWithPrice] = []
    for listing in sorted(listings, key=lambda ln: ln.ticker):
        price = latest_prices.get(listing.id)
        item = ListingWithPrice.model_validate(listing)
        if price is not None:
            item.latest_price = price.price
            item.latest_price_date = price.price_date
            item.latest_price_currency = price.currency
            item.price_source = price.source
        item.freshness = freshness_service.freshness_state(listing.last_price_at, kind="price")
        listings_with_price.append(item)

    # --- bounded reference data for held funds -------------------------------
    distributions: list[Distribution] = []
    holdings: list[FundHolding] = []
    documents: list[DocumentSnapshot] = []
    if fund_ids:
        distributions = list(
            (
                await session.execute(
                    select(Distribution)
                    .where(Distribution.fund_id.in_(fund_ids))
                    .order_by(Distribution.ex_date.desc())
                    .limit(_DISTRIBUTIONS_LIMIT)
                )
            )
            .scalars()
            .all()
        )
        # One coherent snapshot per fund (never a seed+fixture mix), then take
        # the heaviest holdings across funds, bounded for the dashboard.
        snapshots = await holdings_service.latest_holdings_by_fund(session, fund_ids)
        holdings = sorted(
            (h for items in snapshots.values() for h in items),
            key=lambda h: h.weight,
            reverse=True,
        )[:_HOLDINGS_LIMIT]
        documents = list(
            (
                await session.execute(
                    select(DocumentSnapshot)
                    .where(DocumentSnapshot.fund_id.in_(fund_ids))
                    .order_by(DocumentSnapshot.created_at.desc())
                    .limit(_DOCUMENTS_LIMIT)
                )
            )
            .scalars()
            .all()
        )

    fx_rates = list(
        (
            await session.execute(
                select(FxRate).order_by(FxRate.rate_date.desc(), FxRate.id.desc()).limit(50)
            )
        )
        .scalars()
        .all()
    )

    fund_names = {f.id: f.name for f in funds}
    fx_index = await load_fx_index(session)

    exposures = await exposure_service.build_exposure(session, workspace_id)
    exposure_block = await exposure_recompute_service.build_dashboard_block(session, workspace_id)
    # Compact latest-vs-previous drift overlay (reuses the cached snapshots; small
    # limit). insufficient_history until a workspace has two snapshots.
    exposure_block.drift = await exposure_drift_service.build_dashboard_drift(session, workspace_id)
    # Compact top-holding price-context contribution (NOT PnL) — top movers only.
    exposure_block.top_holding_performance = (
        await holding_performance_service.build_dashboard_performance(session, workspace_id)
    )
    alerts = await alerts_service.recent_active_alerts(session, workspace_id, limit=_ALERTS_LIMIT)
    alert_summary = await alerts_service.alert_summary(session, workspace_id)
    scheduled_jobs = await jobs_service.list_jobs(session)
    job_runs = await jobs_service.list_runs(session, limit=_JOB_RUNS_LIMIT)
    data_quality = await diagnostics_service.workspace_diagnostics(session, workspace_id)
    onboarding = await onboarding_service.build_status(session, workspace_id)
    # Latest valuation/readiness snapshot context (read-only; never recomputed here).
    portfolio_valuation = await portfolio_valuation_service.build_dashboard_block(
        session, workspace_id, base_currency=workspace.base_currency
    )

    freshness = FreshnessSummary(
        prices=_domain_freshness([ln.last_price_at for ln in listings], "price"),
        distributions=_domain_freshness([d.ex_date for d in distributions], "distribution"),
        holdings=_domain_freshness([h.as_of_date for h in holdings], "holdings"),
        documents=_domain_freshness([d.document_date for d in documents], "document"),
        fx=_domain_freshness([fx.rate_date for fx in fx_rates], "fx"),
        fund_facts=_domain_freshness([f.last_refreshed_at for f in funds], "fund_facts"),
    )

    return DashboardResponse(
        workspace=DashboardWorkspace(
            id=workspace.id, name=workspace.name, base_currency=workspace.base_currency
        ),
        portfolio_summary=PortfolioSummaryBlock(
            base_currency=summary.base_currency,
            total_market_value=summary.total_market_value,
            daily_change=summary.daily_change,
            unrealised_gain_loss=summary.unrealised_gain_loss,
            trailing_12m_income=summary.trailing_12m_income,
            projected_annual_income=summary.projected_annual_income,
            status=summary_status,
            source="derived",
        ),
        positions=summary.positions,
        funds=[FundRead.model_validate(f) for f in funds],
        fund_listings=listings_with_price,
        distributions=[
            _distribution_read(d, fund_names, fx_index, workspace.base_currency)
            for d in distributions
        ],
        holdings=[HoldingRead.model_validate(h) for h in holdings],
        exposures=exposures,
        exposure=exposure_block,
        documents=[_document_read(d, fund_names) for d in documents],
        portfolio_valuation=portfolio_valuation,
        alerts=alerts,
        alert_summary=alert_summary,
        scheduled_jobs=[jobs_service.serialize_job(j) for j in scheduled_jobs],
        job_runs=[JobRunRead.model_validate(r) for r in job_runs],
        fx_rates=[FxRateRead.model_validate(fx) for fx in fx_rates],
        data_quality=data_quality,
        freshness=freshness,
        onboarding=onboarding,
    )
