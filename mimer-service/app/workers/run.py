"""Job worker — callable from the CLI, the API job trigger, and (later) cron.

    uv run python -m app.workers.run price_ingestion --fund-listing-id 1
    uv run python -m app.workers.run price_ingestion            # all listings
    uv run python -m app.workers.run price_ingestion --source yfinance
    uv run python -m app.workers.run issuer_facts_ingestion --fund-id 1
    uv run python -m app.workers.run issuer_facts_ingestion     # pending/stale funds
    uv run python -m app.workers.run distribution_ingestion --fund-id 1
    uv run python -m app.workers.run distribution_ingestion     # distributing funds
    uv run python -m app.workers.run distribution_ingestion --source distribution_fixture
    # live issuer distributions (explicit-only; uses a known source config URL when
    # present, else --url; the default stays the offline fixture):
    #   distribution_ingestion --fund-id 3 --source vanguard_distributions   # known config URL
    #   distribution_ingestion --fund-id 2 --source jpmorgan_distributions --url "<export>"
    #   distribution_ingestion --fund-id 3 --source vanguard_distributions --url "<product-data>"
    #   distribution_ingestion --fund-id 3 --source vanguard_distributions --verify-source
    #   distribution_ingestion --workspace-id 1 --source vanguard_distributions --limit 10
    #   distribution_ingestion --fund-id 1 --source vanguard_distributions_export  # offline
    uv run python -m app.workers.run issuer_holdings_ingestion --fund-id 1
    uv run python -m app.workers.run issuer_holdings_ingestion  # all eligible funds
    uv run python -m app.workers.run issuer_holdings_ingestion --fund-id 1 --source holdings_fixture
    # live issuer holdings (explicit-only; uses a known source config URL when present,
    # else --url; the default stays the offline fixture):
    #   issuer_holdings_ingestion --fund-id 1 --source blackrock_ishares_holdings  # known config
    #   issuer_holdings_ingestion --fund-id 1 --source blackrock_ishares_holdings --url "<csv>"
    #   issuer_holdings_ingestion --fund-id 2 --source jpmorgan_etf_holdings --url "<export>"
    #   issuer_holdings_ingestion --fund-id 1 --source blackrock_ishares_holdings --verify-source
    #   issuer_holdings_ingestion --workspace-id 1 --source blackrock_ishares_holdings --limit 10
    #   issuer_holdings_ingestion --fund-id 1 --source vanguard_holdings_export   # offline parser
    uv run python -m app.workers.run fx_ingestion               # infer currencies
    uv run python -m app.workers.run fx_ingestion --source fx_fixture
    uv run python -m app.workers.run fx_ingestion --base GBP --quote USD --quote EUR
    uv run python -m app.workers.run document_snapshot_ingestion --fund-id 1
    uv run python -m app.workers.run document_snapshot_ingestion  # all eligible funds
    uv run python -m app.workers.run document_snapshot_ingestion --source document_fixture
    uv run python -m app.workers.run alert_generation                 # all workspaces
    uv run python -m app.workers.run alert_generation --workspace-id 1
    uv run python -m app.workers.run exposure_recompute               # all workspaces
    uv run python -m app.workers.run exposure_recompute --workspace-id 1
    uv run python -m app.workers.run constituent_identity_resolution                 # all funds
    uv run python -m app.workers.run constituent_identity_resolution --fund-id 1
    uv run python -m app.workers.run constituent_identity_resolution --workspace-id 1
    uv run python -m app.workers.run constituent_identity_resolution --source openfigi --limit 50
    uv run python -m app.workers.run constituent_eod_price_ingestion                  # all resolved
    uv run python -m app.workers.run constituent_eod_price_ingestion --fund-id 1
    uv run python -m app.workers.run constituent_eod_price_ingestion --workspace-id 1
    uv run python -m app.workers.run constituent_eod_price_ingestion --instrument-id 1
    uv run python -m app.workers.run constituent_eod_price_ingestion --instrument-listing-id 1
    uv run python -m app.workers.run constituent_eod_price_ingestion --source stooq --limit 20
    uv run python -m app.workers.run instrument_eod_price_ingestion     # constituents + imported
    uv run python -m app.workers.run instrument_eod_price_ingestion --workspace-id 1
    uv run python -m app.workers.run instrument_eod_price_ingestion --fund-id 1
    uv run python -m app.workers.run instrument_eod_price_ingestion --broker-import-id 1
    uv run python -m app.workers.run instrument_eod_price_ingestion --instrument-listing-id 25
    uv run python -m app.workers.run instrument_eod_price_ingestion --source stooq --limit 25
    uv run python -m app.workers.run instrument_eod_price_ingestion --workspace-id 1 --force
    uv run python -m app.workers.run instrument_onboarding --workspace-id 1 --plan-only
    uv run python -m app.workers.run instrument_onboarding --workspace-id 1 --source-mode fixture
    uv run python -m app.workers.run instrument_onboarding --fund-id 1 --source-mode fixture
    uv run python -m app.workers.run instrument_onboarding --workspace-id 1 --skip-exposure
    uv run python -m app.workers.run instrument_onboarding --workspace-id 1 --source-mode live
    uv run python -m app.workers.run instrument_onboarding   # every workspace (umbrella run)
    uv run python -m app.workers.run imported_instrument_resolution --workspace-id 1
    uv run python -m app.workers.run imported_instrument_resolution --source openfigi --limit 25
    uv run python -m app.workers.run imported_instrument_resolution --broker-import-id 1
    uv run python -m app.workers.run imported_instrument_resolution --transaction-id 123
    uv run python -m app.workers.run rates_ingestion                       # offline fixture
    uv run python -m app.workers.run rates_ingestion --source rates_fixture
    uv run python -m app.workers.run rates_ingestion --currency EUR
    uv run python -m app.workers.run rates_ingestion --rate-family treasury_par_yield
    uv run python -m app.workers.run rates_ingestion --start-date 2026-01-01 --end-date 2026-06-24
    uv run python -m app.workers.run rates_ingestion --source us_treasury_rates --limit 50  # live
    uv run python -m app.workers.run rates_ingestion --source ecb_rates --limit 50          # live
    uv run python -m app.workers.run rates_ingestion --source ecb_rates --rate-family overnight_rate
    uv run python -m app.workers.run rates_ingestion --source boe_rates   # planned (clean fail)
    uv run python -m app.workers.run portfolio_valuation_recompute --workspace-id 1
    uv run python -m app.workers.run portfolio_valuation_recompute   # every workspace
    # portfolio_valuation_recompute scope flags (consume already-ingested prices/FX
    # only — no fetch, no resolver, no PnL):
    #   --as-of-date 2026-06-25  --base-currency GBP  --broker-account-id 1  --force

`price_ingestion`, `issuer_facts_ingestion`, `distribution_ingestion`,
`issuer_holdings_ingestion`, `fx_ingestion` and `document_snapshot_ingestion` are
real in this iteration (all but prices via offline fixture providers).
`alert_generation` and `exposure_recompute` are real and database-only: they
derive workspace alerts / cached look-through exposure from existing signals (no
external provider). `constituent_identity_resolution`,
`constituent_eod_price_ingestion` and `instrument_eod_price_ingestion` are real
provider-agnostic workers that default to offline fixtures (OpenFIGI / Stooq /
yfinance only when explicitly requested, always behind the source budget + fetch
log). `instrument_eod_price_ingestion` is the unified price worker: it prices any
resolved ``instrument_listing`` — ETF/fund constituents *and* directly-held
imported broker holdings — through the one ``instrument_prices`` path;
`constituent_eod_price_ingestion` remains the constituent-only entry point and
shares the same selector + idempotent upsert. `instrument_onboarding` is a real
*orchestration* worker (database-only of its own): it coordinates the workers
above into a data-readiness pipeline (plan + run; offline `fixture` mode by
default, explicit `--source-mode live`). Any remaining job types record a
`success_stub` JobRun so the pipeline shape is correct without doing real work.
There is intentionally no queue/broker yet.
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, date, datetime

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.db.models import Fund, FundListing, JobRun, PortfolioPosition, ScheduledJob
from app.db.session import get_engine, get_sessionmaker
from app.services import alert_generation as alerts_service
from app.services import constituent_identity as constituent_identity_service
from app.services import distributions_ingestion as distributions_service
from app.services import document_ingestion as documents_service
from app.services import exposure_recompute as exposure_service
from app.services import fx_ingestion as fx_service
from app.services import holdings_ingestion as holdings_service
from app.services import instrument_onboarding as onboarding_service
from app.services import instrument_prices as instrument_prices_service
from app.services import issuer_facts as issuer_facts_service
from app.services import issuer_source_verification as verification_service
from app.services import prices as prices_service
from app.services import source_budget as source_budget_service
from app.sources import (
    get_distribution_source,
    get_document_source,
    get_fx_source,
    get_holdings_source,
    get_issuer_facts_source,
    get_price_source,
    issuer_source_config,
)
from app.sources.constituents import get_constituent_resolver
from app.sources.instrument_prices import get_instrument_price_source

PRICE_JOB = "price_ingestion"
ISSUER_FACTS_JOB = "issuer_facts_ingestion"
DISTRIBUTION_JOB = "distribution_ingestion"
HOLDINGS_JOB = "issuer_holdings_ingestion"
FX_JOB = "fx_ingestion"
DOCUMENT_JOB = "document_snapshot_ingestion"
ALERT_JOB = "alert_generation"
EXPOSURE_JOB = "exposure_recompute"
CONSTITUENT_IDENTITY_JOB = "constituent_identity_resolution"
CONSTITUENT_PRICE_JOB = "constituent_eod_price_ingestion"
INSTRUMENT_PRICE_JOB = "instrument_eod_price_ingestion"
ONBOARDING_JOB = "instrument_onboarding"
BROKER_IMPORT_JOB = "broker_csv_import"
IMPORTED_RESOLUTION_JOB = "imported_instrument_resolution"
RATES_JOB = "rates_ingestion"
PORTFOLIO_VALUATION_JOB = "portfolio_valuation_recompute"


async def run_job(
    session: AsyncSession,
    job_type: str,
    *,
    fund_id: int | None = None,
    fund_listing_id: int | None = None,
    scheduled_job_id: int | None = None,
    source_name: str | None = None,
    base_currency: str | None = None,
    quote_currencies: list[str] | None = None,
    start_date: date | None = None,
    end_date: date | None = None,
    workspace_id: int | None = None,
    limit: int | None = None,
    instrument_id: int | None = None,
    instrument_listing_id: int | None = None,
    source_mode: str | None = None,
    plan_only: bool = False,
    skip_exposure: bool = False,
    skip_alerts: bool = False,
    csv_path: str | None = None,
    broker_import_id: int | None = None,
    broker_account_id: int | None = None,
    transaction_id: int | None = None,
    force: bool = False,
    currency: str | None = None,
    country_or_region: str | None = None,
    rate_family: str | None = None,
    as_of_date: date | None = None,
    url: str | None = None,
    verify_source: bool = False,
) -> JobRun:
    if job_type == PRICE_JOB:
        return await _run_price_ingestion(
            session,
            fund_listing_id=fund_listing_id,
            scheduled_job_id=scheduled_job_id,
            source_name=source_name,
        )
    if job_type == FX_JOB:
        return await _run_fx_ingestion(
            session,
            scheduled_job_id=scheduled_job_id,
            source_name=source_name,
            base_currency=base_currency,
            quote_currencies=quote_currencies,
            start_date=start_date,
            end_date=end_date,
        )
    if job_type == ISSUER_FACTS_JOB:
        return await _run_issuer_facts_ingestion(
            session,
            fund_id=fund_id,
            scheduled_job_id=scheduled_job_id,
            source_name=source_name,
        )
    if job_type == DISTRIBUTION_JOB:
        return await _run_distribution_ingestion(
            session,
            fund_id=fund_id,
            workspace_id=workspace_id,
            scheduled_job_id=scheduled_job_id,
            source_name=source_name,
            url=url,
            limit=limit,
            verify_source=verify_source,
        )
    if job_type == HOLDINGS_JOB:
        return await _run_holdings_ingestion(
            session,
            fund_id=fund_id,
            workspace_id=workspace_id,
            scheduled_job_id=scheduled_job_id,
            source_name=source_name,
            url=url,
            limit=limit,
            verify_source=verify_source,
        )
    if job_type == DOCUMENT_JOB:
        return await _run_document_ingestion(
            session,
            fund_id=fund_id,
            scheduled_job_id=scheduled_job_id,
            source_name=source_name,
        )
    if job_type == ALERT_JOB:
        return await _run_alert_generation(
            session,
            scheduled_job_id=scheduled_job_id,
            workspace_id=workspace_id,
        )
    if job_type == EXPOSURE_JOB:
        return await _run_exposure_recompute(
            session,
            scheduled_job_id=scheduled_job_id,
            workspace_id=workspace_id,
        )
    if job_type == CONSTITUENT_IDENTITY_JOB:
        return await _run_constituent_identity_resolution(
            session,
            fund_id=fund_id,
            scheduled_job_id=scheduled_job_id,
            source_name=source_name,
            workspace_id=workspace_id,
            limit=limit,
        )
    if job_type == CONSTITUENT_PRICE_JOB:
        return await _run_constituent_price_ingestion(
            session,
            fund_id=fund_id,
            scheduled_job_id=scheduled_job_id,
            source_name=source_name,
            workspace_id=workspace_id,
            instrument_id=instrument_id,
            instrument_listing_id=instrument_listing_id,
            limit=limit,
        )
    if job_type == INSTRUMENT_PRICE_JOB:
        return await _run_instrument_eod_price_ingestion(
            session,
            fund_id=fund_id,
            scheduled_job_id=scheduled_job_id,
            source_name=source_name,
            workspace_id=workspace_id,
            instrument_id=instrument_id,
            instrument_listing_id=instrument_listing_id,
            broker_import_id=broker_import_id,
            transaction_id=transaction_id,
            limit=limit,
            force=force,
        )
    if job_type == ONBOARDING_JOB:
        return await _run_instrument_onboarding(
            session,
            workspace_id=workspace_id,
            fund_id=fund_id,
            scheduled_job_id=scheduled_job_id,
            source_mode=source_mode,
            plan_only=plan_only,
            limit=limit,
            skip_exposure=skip_exposure,
            skip_alerts=skip_alerts,
        )
    if job_type == BROKER_IMPORT_JOB:
        return await _run_broker_csv_import(
            session,
            workspace_id=workspace_id,
            scheduled_job_id=scheduled_job_id,
            source_name=source_name,
            csv_path=csv_path,
        )
    if job_type == IMPORTED_RESOLUTION_JOB:
        return await _run_imported_instrument_resolution(
            session,
            workspace_id=workspace_id,
            scheduled_job_id=scheduled_job_id,
            source_name=source_name,
            broker_import_id=broker_import_id,
            broker_account_id=broker_account_id,
            transaction_id=transaction_id,
            limit=limit,
        )
    if job_type == RATES_JOB:
        return await _run_rates_ingestion(
            session,
            scheduled_job_id=scheduled_job_id,
            source_name=source_name,
            currency=currency,
            country_or_region=country_or_region,
            rate_family=rate_family,
            start_date=start_date,
            end_date=end_date,
            limit=limit,
        )
    if job_type == PORTFOLIO_VALUATION_JOB:
        return await _run_portfolio_valuation_recompute(
            session,
            workspace_id=workspace_id,
            scheduled_job_id=scheduled_job_id,
            as_of_date=as_of_date,
            base_currency=base_currency,
            broker_account_id=broker_account_id,
            limit=limit,
            force=force,
        )
    return await _run_stub(session, job_type, scheduled_job_id=scheduled_job_id)


async def _run_stub(
    session: AsyncSession, job_type: str, *, scheduled_job_id: int | None
) -> JobRun:
    now = datetime.now(UTC)
    run = JobRun(
        job_type=job_type,
        scheduled_job_id=scheduled_job_id,
        status="success_stub",
        started_at=now,
        finished_at=now,
        message=f"Stub run: {job_type} worker not implemented yet.",
    )
    session.add(run)
    await session.commit()
    await session.refresh(run)
    return run


async def _claim_or_create_run(
    session: AsyncSession,
    *,
    fund_listing_id: int | None,
    scheduled_job_id: int | None,
    source_name: str,
) -> JobRun:
    """Reuse a queued backfill run for this listing if present, else create one."""
    run: JobRun | None = None
    if fund_listing_id is not None:
        run = await session.scalar(
            select(JobRun)
            .where(
                JobRun.job_type == PRICE_JOB,
                JobRun.fund_listing_id == fund_listing_id,
                JobRun.status == "queued",
            )
            .order_by(JobRun.id.desc())
        )
    if run is None:
        run = JobRun(job_type=PRICE_JOB, fund_listing_id=fund_listing_id)
        session.add(run)
    run.status = "running"
    run.started_at = datetime.now(UTC)
    run.scheduled_job_id = scheduled_job_id
    run.source = source_name
    await session.flush()
    return run


async def _run_price_ingestion(
    session: AsyncSession,
    *,
    fund_listing_id: int | None,
    scheduled_job_id: int | None,
    source_name: str | None,
) -> JobRun:
    source = get_price_source(source_name)
    run = await _claim_or_create_run(
        session,
        fund_listing_id=fund_listing_id,
        scheduled_job_id=scheduled_job_id,
        source_name=source.name,
    )

    if fund_listing_id is not None:
        listing = await session.get(FundListing, fund_listing_id)
        if listing is None:
            run.status = "failed"
            run.message = f"Fund listing {fund_listing_id} not found"
            run.finished_at = datetime.now(UTC)
            await session.commit()
            await session.refresh(run)
            return run
        listings = [listing]
    else:
        listings = list((await session.execute(select(FundListing))).scalars().all())

    inserted = updated = failed = 0
    try:
        for listing in listings:
            counts = await prices_service.ingest_prices_for_listing(session, listing, source)
            inserted += counts.inserted
            updated += counts.updated
            failed += counts.failed
        run.records_inserted = inserted
        run.records_updated = updated
        run.records_failed = failed
        if failed and (inserted or updated):
            run.status = "partial_success"
        elif failed and not (inserted or updated):
            run.status = "failed"
        else:
            run.status = "success"
    except Exception as exc:  # noqa: BLE001 - record failure on the run
        run.status = "failed"
        run.message = str(exc)
        run.records_failed = failed

    run.finished_at = datetime.now(UTC)
    if scheduled_job_id is not None:
        scheduled = await session.get(ScheduledJob, scheduled_job_id)
        if scheduled is not None:
            scheduled.last_run_at = run.finished_at
    await session.commit()
    await session.refresh(run)
    return run


# --- fx ingestion ------------------------------------------------------------


async def _claim_or_create_fx_run(
    session: AsyncSession,
    *,
    scheduled_job_id: int | None,
    source_name: str,
) -> JobRun:
    """Reuse a queued (unscoped) fx_ingestion backfill run if present, else create."""
    run = await session.scalar(
        select(JobRun)
        .where(JobRun.job_type == FX_JOB, JobRun.status == "queued")
        .order_by(JobRun.id.desc())
    )
    if run is None:
        run = JobRun(job_type=FX_JOB)
        session.add(run)
    run.status = "running"
    run.started_at = datetime.now(UTC)
    run.scheduled_job_id = scheduled_job_id
    run.source = source_name
    await session.flush()
    return run


async def _run_fx_ingestion(
    session: AsyncSession,
    *,
    scheduled_job_id: int | None,
    source_name: str | None,
    base_currency: str | None,
    quote_currencies: list[str] | None,
    start_date: date | None,
    end_date: date | None,
) -> JobRun:
    source = get_fx_source(source_name)
    run = await _claim_or_create_fx_run(
        session, scheduled_job_id=scheduled_job_id, source_name=source.name
    )

    try:
        counts = await fx_service.ingest_fx_rates(
            session,
            source,
            base_currency=base_currency,
            quote_currencies=quote_currencies,
            start_date=start_date,
            end_date=end_date,
        )
        run.records_inserted = counts.inserted
        run.records_updated = counts.updated
        run.records_failed = counts.failed
        if counts.failed and (counts.inserted or counts.updated):
            run.status = "partial_success"
        elif counts.failed and not (counts.inserted or counts.updated):
            run.status = "failed"
        else:
            run.status = "success"
            if not (counts.inserted or counts.updated):
                run.message = "No new FX rates (already up to date or no currencies needed)."
    except Exception as exc:  # noqa: BLE001 - record failure on the run
        run.status = "failed"
        run.message = str(exc)

    run.finished_at = datetime.now(UTC)
    if scheduled_job_id is not None:
        scheduled = await session.get(ScheduledJob, scheduled_job_id)
        if scheduled is not None:
            scheduled.last_run_at = run.finished_at
    await session.commit()
    await session.refresh(run)
    return run


# --- issuer facts ingestion --------------------------------------------------


async def _claim_or_create_fund_run(
    session: AsyncSession,
    job_type: str,
    *,
    fund_id: int | None,
    scheduled_job_id: int | None,
    source_name: str | None,
) -> JobRun:
    """Reuse a queued fund-scoped backfill run if present, else create one."""
    run: JobRun | None = None
    if fund_id is not None:
        run = await session.scalar(
            select(JobRun)
            .where(
                JobRun.job_type == job_type,
                JobRun.fund_id == fund_id,
                JobRun.status == "queued",
            )
            .order_by(JobRun.id.desc())
        )
    if run is None:
        run = JobRun(job_type=job_type, fund_id=fund_id)
        session.add(run)
    run.status = "running"
    run.started_at = datetime.now(UTC)
    run.scheduled_job_id = scheduled_job_id
    run.source = source_name
    await session.flush()
    return run


async def _eligible_funds(session: AsyncSession) -> list[Fund]:
    """Funds worth (re)enriching: pending/stale, or still on seed/unknown facts."""
    stmt = select(Fund).where(
        or_(
            Fund.status.in_(["pending", "stale"]),
            Fund.source.is_(None),
            Fund.source == "seed",
        )
    )
    return list((await session.execute(stmt)).scalars().all())


async def _run_issuer_facts_ingestion(
    session: AsyncSession,
    *,
    fund_id: int | None,
    scheduled_job_id: int | None,
    source_name: str | None,
) -> JobRun:
    source = get_issuer_facts_source(source_name)
    run = await _claim_or_create_fund_run(
        session,
        ISSUER_FACTS_JOB,
        fund_id=fund_id,
        scheduled_job_id=scheduled_job_id,
        source_name=source.name,
    )

    if fund_id is not None:
        fund = await session.get(Fund, fund_id)
        if fund is None:
            run.status = "failed"
            run.message = f"Fund {fund_id} not found"
            run.finished_at = datetime.now(UTC)
            await session.commit()
            await session.refresh(run)
            return run
        funds = [fund]
    else:
        funds = await _eligible_funds(session)

    activated = changed = missed = 0
    try:
        for fund in funds:
            counts = await issuer_facts_service.ingest_issuer_facts_for_fund(session, fund, source)
            activated += counts.inserted
            changed += counts.updated
            missed += counts.failed
        run.records_inserted = activated
        run.records_updated = changed
        run.records_failed = missed
        processed = len(funds) - missed
        if not funds:
            run.status = "success"
            run.message = "No eligible funds to enrich."
        elif processed and missed:
            run.status = "partial_success"
        elif processed:
            run.status = "success"
        else:
            run.status = "failed"
            run.message = "No issuer facts found for any candidate fund."
    except Exception as exc:  # noqa: BLE001 - record failure on the run
        run.status = "failed"
        run.message = str(exc)
        run.records_failed = missed

    run.finished_at = datetime.now(UTC)
    if scheduled_job_id is not None:
        scheduled = await session.get(ScheduledJob, scheduled_job_id)
        if scheduled is not None:
            scheduled.last_run_at = run.finished_at
    await session.commit()
    await session.refresh(run)
    return run


# --- distribution ingestion --------------------------------------------------


async def _distribution_eligible_funds(session: AsyncSession) -> list[Fund]:
    """Funds worth ingesting distributions for: distributing or unknown policy.

    Accumulating funds do not pay distributions, so they are skipped in the bulk
    path (a fund with no provider match is simply a no-op either way).
    """
    stmt = select(Fund).where(
        or_(
            Fund.distribution_policy.is_(None),
            Fund.distribution_policy != "accumulating",
        )
    )
    return list((await session.execute(stmt)).scalars().all())


async def _run_source_verification(
    session: AsyncSession,
    run: JobRun,
    *,
    fund_id: int | None,
    source_name: str,
    data_type: str,
    url: str | None,
) -> JobRun:
    """Verify-only path for a known issuer source config (no ingestion).

    Runs exactly one guarded fetch + parse through the live adapter and folds the
    ``SourceVerificationReport`` into the job_run (status + message). Requires a
    single ``--fund-id`` (one file = one fund). Never upserts canonical rows; the
    only side effect is the fetch log ``guarded_fetch`` writes (budget-safe).
    """
    run.records_inserted = run.records_updated = run.records_failed = 0
    if fund_id is None:
        run.status = "failed"
        run.message = "--verify-source requires --fund-id (verify one fund/source at a time)."
        run.finished_at = datetime.now(UTC)
        await session.commit()
        await session.refresh(run)
        return run
    fund = await session.get(Fund, fund_id)
    if fund is None:
        run.status = "failed"
        run.message = f"Fund {fund_id} not found"
        run.finished_at = datetime.now(UTC)
        await session.commit()
        await session.refresh(run)
        return run

    report = await verification_service.verify_issuer_source_config(
        session,
        isin=fund.isin,
        source_name=source_name,
        data_type=data_type,
        url=url,
    )
    # A budget block / cache hit / missing-URL is a clean no-op (success); a real
    # fetch/parse failure (or an endpoint with no usable rows) is a verification failure.
    clean_noops = {
        verification_service.CACHE_HIT,
        verification_service.BUDGET_BLOCKED,
        verification_service.NO_URL,
    }
    if report.ok or report.fetch_outcome in clean_noops:
        run.status = "success"
    else:
        run.status = "failed"
    run.message = report.message()
    run.finished_at = datetime.now(UTC)
    await session.commit()
    await session.refresh(run)
    return run


async def _run_distribution_ingestion(
    session: AsyncSession,
    *,
    fund_id: int | None,
    workspace_id: int | None,
    scheduled_job_id: int | None,
    source_name: str | None,
    url: str | None,
    limit: int | None,
    verify_source: bool = False,
) -> JobRun:
    """Fetch + idempotently upsert fund distributions from a distribution source.

    Offline ``distribution_fixture`` by default; the live issuer adapters
    (``jpmorgan_distributions`` / ``vanguard_distributions``) fetch the
    issuer-published distribution file through ``guarded_fetch`` when named, and
    ``vanguard_distributions_export`` parses a manually exported file — all
    explicit-only. Scope is one fund (``--fund-id``), one workspace's held funds
    (``--workspace-id``) or every distributing/unknown-policy fund. ``--url``
    overrides the download URL (single-fund runs only); ``--limit`` bounds the fund
    count. Counts fold into the job_run as inserted/updated/failed (bad rows); the
    full breakdown (selected_funds/fetched/skipped/source/is_fixture) is in the
    message. Collection only — never forecasts dividends or projects yield.
    """
    source = get_distribution_source(source_name)
    run = await _claim_or_create_fund_run(
        session,
        DISTRIBUTION_JOB,
        fund_id=fund_id,
        scheduled_job_id=scheduled_job_id,
        source_name=source.name,
    )

    if verify_source:
        return await _run_source_verification(
            session,
            run,
            fund_id=fund_id,
            source_name=source.name,
            data_type=issuer_source_config.DATA_TYPE_DISTRIBUTIONS,
            url=url,
        )

    if fund_id is not None:
        fund = await session.get(Fund, fund_id)
        if fund is None:
            run.status = "failed"
            run.message = f"Fund {fund_id} not found"
            run.finished_at = datetime.now(UTC)
            await session.commit()
            await session.refresh(run)
            return run
        funds = [fund]
    elif workspace_id is not None:
        funds = await _workspace_held_funds(session, workspace_id)
    else:
        funds = await _distribution_eligible_funds(session)
    if limit is not None and limit >= 0:
        funds = funds[:limit]

    # A --url override only makes sense for a single fund (one file = one fund);
    # for multi-fund runs each fund resolves its own configured URL (none yet).
    effective_url = url if (url and len(funds) == 1) else None
    is_fixture = getattr(source, "is_fixture", source.name.endswith("_fixture"))

    totals = distributions_service.DistributionCounts()
    no_match = 0  # funds the provider had no distributions for (clean no-op)
    try:
        for fund in funds:
            counts = await distributions_service.ingest_distributions_for_fund(
                session, fund, source, url=effective_url
            )
            totals.add(counts)
            if counts.fetched == 0:
                no_match += 1
        run.records_inserted = totals.inserted
        run.records_updated = totals.updated
        run.records_failed = totals.failed
        detail = (
            f"source={source.name} is_fixture={is_fixture} selected_funds={len(funds)} "
            f"fetched={totals.fetched} inserted={totals.inserted} updated={totals.updated} "
            f"skipped={totals.skipped} bad_rows={totals.failed} no_provider_match={no_match}"
        )
        if not funds:
            run.status = "success"
            run.message = "No eligible funds for distribution ingestion."
        elif totals.failed and (totals.inserted or totals.updated):
            run.status = "partial_success"
            run.message = detail
        elif totals.failed and not (totals.inserted or totals.updated):
            run.status = "failed"
            run.message = detail
        else:
            run.status = "success"
            run.message = detail
    except Exception as exc:  # noqa: BLE001 - record failure on the run
        run.status = "failed"
        run.message = str(exc)
        run.records_failed = totals.failed

    run.finished_at = datetime.now(UTC)
    if scheduled_job_id is not None:
        scheduled = await session.get(ScheduledJob, scheduled_job_id)
        if scheduled is not None:
            scheduled.last_run_at = run.finished_at
    await session.commit()
    await session.refresh(run)
    return run


# --- holdings ingestion ------------------------------------------------------


async def _holdings_eligible_funds(session: AsyncSession) -> list[Fund]:
    """Funds worth (re)ingesting holdings for: everything except hard errors.

    Holdings apply to every fund. A fund the provider does not know simply
    yields no records (a clean no-op), so there is no need to pre-filter by
    provider coverage here.
    """
    stmt = select(Fund).where(or_(Fund.status.is_(None), Fund.status != "error"))
    return list((await session.execute(stmt)).scalars().all())


async def _workspace_held_funds(session: AsyncSession, workspace_id: int) -> list[Fund]:
    """Funds held (any position) in a workspace — the holdings ingestion scope."""
    stmt = (
        select(Fund)
        .join(FundListing, FundListing.fund_id == Fund.id)
        .join(PortfolioPosition, PortfolioPosition.fund_listing_id == FundListing.id)
        .where(PortfolioPosition.workspace_id == workspace_id)
        .distinct()
    )
    return list((await session.execute(stmt)).scalars().all())


async def _run_holdings_ingestion(
    session: AsyncSession,
    *,
    fund_id: int | None,
    workspace_id: int | None,
    scheduled_job_id: int | None,
    source_name: str | None,
    url: str | None,
    limit: int | None,
    verify_source: bool = False,
) -> JobRun:
    """Fetch + idempotently upsert fund holdings from a holdings source.

    Offline ``holdings_fixture`` by default; the live issuer adapters
    (``blackrock_ishares_holdings`` / ``jpmorgan_etf_holdings``) fetch the
    issuer-published holdings file through ``guarded_fetch`` when named, and
    ``vanguard_holdings_export`` parses a manually exported file — all explicit-only.
    Scope is one fund (``--fund-id``), one workspace's held funds
    (``--workspace-id``) or every eligible fund. ``--url`` overrides the download URL
    (single-fund runs only); ``--limit`` bounds the fund count. Counts fold into the
    job_run as inserted/updated/failed (bad rows); the full breakdown
    (selected_funds/fetched/skipped/source/is_fixture) is in the message.
    """
    source = get_holdings_source(source_name)
    run = await _claim_or_create_fund_run(
        session,
        HOLDINGS_JOB,
        fund_id=fund_id,
        scheduled_job_id=scheduled_job_id,
        source_name=source.name,
    )

    if verify_source:
        return await _run_source_verification(
            session,
            run,
            fund_id=fund_id,
            source_name=source.name,
            data_type=issuer_source_config.DATA_TYPE_HOLDINGS,
            url=url,
        )

    if fund_id is not None:
        fund = await session.get(Fund, fund_id)
        if fund is None:
            run.status = "failed"
            run.message = f"Fund {fund_id} not found"
            run.finished_at = datetime.now(UTC)
            await session.commit()
            await session.refresh(run)
            return run
        funds = [fund]
    elif workspace_id is not None:
        funds = await _workspace_held_funds(session, workspace_id)
    else:
        funds = await _holdings_eligible_funds(session)
    if limit is not None and limit >= 0:
        funds = funds[:limit]

    # A --url override only makes sense for a single fund (one file = one fund);
    # for multi-fund runs each fund resolves its own known URL.
    effective_url = url if (url and len(funds) == 1) else None
    is_fixture = getattr(source, "is_fixture", source.name.endswith("_fixture"))

    totals = holdings_service.HoldingsCounts()
    no_match = 0  # funds the provider had no holdings for (clean no-op)
    try:
        for fund in funds:
            counts = await holdings_service.ingest_holdings_for_fund(
                session, fund, source, url=effective_url
            )
            totals.add(counts)
            if counts.fetched == 0:
                no_match += 1
        run.records_inserted = totals.inserted
        run.records_updated = totals.updated
        run.records_failed = totals.failed
        detail = (
            f"source={source.name} is_fixture={is_fixture} selected_funds={len(funds)} "
            f"fetched={totals.fetched} inserted={totals.inserted} updated={totals.updated} "
            f"skipped={totals.skipped} bad_rows={totals.failed} no_provider_match={no_match}"
        )
        if not funds:
            run.status = "success"
            run.message = "No eligible funds for holdings ingestion."
        elif totals.failed and (totals.inserted or totals.updated):
            run.status = "partial_success"
            run.message = detail
        elif totals.failed and not (totals.inserted or totals.updated):
            run.status = "failed"
            run.message = detail
        else:
            run.status = "success"
            run.message = detail
    except Exception as exc:  # noqa: BLE001 - record failure on the run
        run.status = "failed"
        run.message = str(exc)
        run.records_failed = totals.failed

    run.finished_at = datetime.now(UTC)
    if scheduled_job_id is not None:
        scheduled = await session.get(ScheduledJob, scheduled_job_id)
        if scheduled is not None:
            scheduled.last_run_at = run.finished_at
    await session.commit()
    await session.refresh(run)
    return run


# --- document ingestion ------------------------------------------------------


async def _document_eligible_funds(session: AsyncSession) -> list[Fund]:
    """Funds worth (re)checking for documents: everything except hard errors.

    Documents apply to every fund. A fund the provider does not know simply
    yields no records (a clean no-op), so there is no need to pre-filter here.
    """
    stmt = select(Fund).where(or_(Fund.status.is_(None), Fund.status != "error"))
    return list((await session.execute(stmt)).scalars().all())


async def _run_document_ingestion(
    session: AsyncSession,
    *,
    fund_id: int | None,
    scheduled_job_id: int | None,
    source_name: str | None,
) -> JobRun:
    source = get_document_source(source_name)
    run = await _claim_or_create_fund_run(
        session,
        DOCUMENT_JOB,
        fund_id=fund_id,
        scheduled_job_id=scheduled_job_id,
        source_name=source.name,
    )

    if fund_id is not None:
        fund = await session.get(Fund, fund_id)
        if fund is None:
            run.status = "failed"
            run.message = f"Fund {fund_id} not found"
            run.finished_at = datetime.now(UTC)
            await session.commit()
            await session.refresh(run)
            return run
        funds = [fund]
    else:
        funds = await _document_eligible_funds(session)

    inserted = updated = failed = 0
    new = changed = unchanged = 0
    try:
        for fund in funds:
            counts = await documents_service.ingest_documents_for_fund(session, fund, source)
            inserted += counts.inserted
            updated += counts.updated
            failed += counts.failed
            new += counts.new
            changed += counts.changed
            unchanged += counts.unchanged
        run.records_inserted = inserted
        run.records_updated = updated
        run.records_failed = failed
        if not funds:
            run.status = "success"
            run.message = "No eligible funds for document ingestion."
        elif failed and (inserted or updated):
            run.status = "partial_success"
        elif failed and not (inserted or updated):
            run.status = "failed"
        else:
            run.status = "success"
        if funds and run.status in {"success", "partial_success"}:
            run.message = f"documents: new={new} changed={changed} unchanged={unchanged}"
    except Exception as exc:  # noqa: BLE001 - record failure on the run
        run.status = "failed"
        run.message = str(exc)
        run.records_failed = failed

    run.finished_at = datetime.now(UTC)
    if scheduled_job_id is not None:
        scheduled = await session.get(ScheduledJob, scheduled_job_id)
        if scheduled is not None:
            scheduled.last_run_at = run.finished_at
    await session.commit()
    await session.refresh(run)
    return run


# --- alert generation --------------------------------------------------------


async def _claim_or_create_alert_run(
    session: AsyncSession, *, scheduled_job_id: int | None
) -> JobRun:
    """Reuse a queued (unscoped) alert_generation run if present, else create."""
    run = await session.scalar(
        select(JobRun)
        .where(JobRun.job_type == ALERT_JOB, JobRun.status == "queued")
        .order_by(JobRun.id.desc())
    )
    if run is None:
        run = JobRun(job_type=ALERT_JOB)
        session.add(run)
    run.status = "running"
    run.started_at = datetime.now(UTC)
    run.scheduled_job_id = scheduled_job_id
    run.source = "alert_generation"
    await session.commit()  # persist the run before per-workspace work
    await session.refresh(run)
    return run


async def _run_alert_generation(
    session: AsyncSession,
    *,
    scheduled_job_id: int | None,
    workspace_id: int | None,
) -> JobRun:
    """Generate workspace alerts from existing DB signals.

    Runs one workspace if ``workspace_id`` is given, else every workspace. A
    failure in one workspace is recorded (``records_failed``) and does not abort
    the others. ``JobRun`` has no "resolved" column, so resolved/reactivated
    counts are folded into the run message.
    """
    run = await _claim_or_create_alert_run(session, scheduled_job_id=scheduled_job_id)

    if workspace_id is not None:
        workspace_ids = [workspace_id]
    else:
        workspace_ids = await alerts_service.active_workspace_ids(session)

    result = alerts_service.AlertGenerationResult()
    for wid in workspace_ids:
        try:
            ws_result = await alerts_service.generate_for_workspace(session, wid)
            await session.commit()
            result.add(ws_result)
        except Exception as exc:  # noqa: BLE001 - isolate one workspace's failure
            await session.rollback()
            result.failed += 1
            result.failures[wid] = str(exc)

    run.records_inserted = result.inserted
    run.records_updated = result.updated + result.reactivated
    run.records_failed = result.failed
    if result.failed and result.processed_workspaces:
        run.status = "partial_success"
    elif result.failed:
        run.status = "failed"
    else:
        run.status = "success"
    run.message = (
        f"workspaces={len(result.processed_workspaces)} inserted={result.inserted} "
        f"updated={result.updated} reactivated={result.reactivated} "
        f"resolved={result.resolved} failed={result.failed}"
    )
    if result.failures:
        run.message += f" failures={result.failures}"

    run.finished_at = datetime.now(UTC)
    if scheduled_job_id is not None:
        scheduled = await session.get(ScheduledJob, scheduled_job_id)
        if scheduled is not None:
            scheduled.last_run_at = run.finished_at
    await session.commit()
    await session.refresh(run)
    return run


# --- exposure recompute ------------------------------------------------------


async def _claim_or_create_exposure_run(
    session: AsyncSession, *, scheduled_job_id: int | None
) -> JobRun:
    """Reuse a queued (unscoped) exposure_recompute run if present, else create."""
    run = await session.scalar(
        select(JobRun)
        .where(JobRun.job_type == EXPOSURE_JOB, JobRun.status == "queued")
        .order_by(JobRun.id.desc())
    )
    if run is None:
        run = JobRun(job_type=EXPOSURE_JOB)
        session.add(run)
    run.status = "running"
    run.started_at = datetime.now(UTC)
    run.scheduled_job_id = scheduled_job_id
    run.source = EXPOSURE_JOB
    await session.commit()  # persist the run before per-workspace work
    await session.refresh(run)
    return run


async def _run_exposure_recompute(
    session: AsyncSession,
    *,
    scheduled_job_id: int | None,
    workspace_id: int | None,
) -> JobRun:
    """Recompute cached look-through exposure snapshots.

    Runs one workspace if ``workspace_id`` is given, else every workspace. A
    failure in one workspace is recorded (``records_failed``) and does not abort
    the others. ``JobRun`` has no "unchanged/skipped" columns, so those are
    folded into the run message.
    """
    run = await _claim_or_create_exposure_run(session, scheduled_job_id=scheduled_job_id)

    if workspace_id is not None:
        workspace_ids = [workspace_id]
    else:
        workspace_ids = await exposure_service.active_workspace_ids(session)

    result = exposure_service.ExposureRecomputeResult()
    for wid in workspace_ids:
        try:
            ws_result = await exposure_service.recompute_workspace(session, wid)
            await session.commit()
            result.add(ws_result)
        except Exception as exc:  # noqa: BLE001 - isolate one workspace's failure
            await session.rollback()
            result.failed += 1
            result.failures[wid] = str(exc)

    run.records_inserted = result.inserted
    run.records_updated = 0
    run.records_failed = result.failed
    if result.failed and result.processed_workspaces:
        run.status = "partial_success"
    elif result.failed:
        run.status = "failed"
    else:
        run.status = "success"
    run.message = (
        f"workspaces={len(result.processed_workspaces)} inserted={result.inserted} "
        f"unchanged={result.unchanged} skipped={result.skipped} failed={result.failed}"
    )
    if result.failures:
        run.message += f" failures={result.failures}"

    run.finished_at = datetime.now(UTC)
    if scheduled_job_id is not None:
        scheduled = await session.get(ScheduledJob, scheduled_job_id)
        if scheduled is not None:
            scheduled.last_run_at = run.finished_at
    await session.commit()
    await session.refresh(run)
    return run


# --- constituent identity resolution -----------------------------------------


async def _claim_or_create_constituent_run(
    session: AsyncSession,
    *,
    fund_id: int | None,
    scheduled_job_id: int | None,
    source_name: str,
) -> JobRun:
    """Reuse a queued fund-scoped run if present, else create a new run."""
    run: JobRun | None = None
    if fund_id is not None:
        run = await session.scalar(
            select(JobRun)
            .where(
                JobRun.job_type == CONSTITUENT_IDENTITY_JOB,
                JobRun.fund_id == fund_id,
                JobRun.status == "queued",
            )
            .order_by(JobRun.id.desc())
        )
    if run is None:
        run = JobRun(job_type=CONSTITUENT_IDENTITY_JOB, fund_id=fund_id)
        session.add(run)
    run.status = "running"
    run.started_at = datetime.now(UTC)
    run.scheduled_job_id = scheduled_job_id
    run.source = source_name
    await session.flush()
    return run


async def _run_constituent_identity_resolution(
    session: AsyncSession,
    *,
    fund_id: int | None,
    scheduled_job_id: int | None,
    source_name: str | None,
    workspace_id: int | None,
    limit: int | None,
) -> JobRun:
    """Resolve unresolved ETF/fund constituents to canonical instrument identity.

    Consumes unresolved holdings (deduped), resolves them via the configured
    resolver (offline fixture by default; OpenFIGI batches behind ``guarded_fetch``
    when ``--source openfigi``), and idempotently upserts instruments/listings/
    identifiers + links the holdings. Counts fold into the job_run as
    inserted=resolved, updated=ambiguous+not_found, failed=failed; the detailed
    breakdown (incl. skipped budget/cache/unsafe) goes in the message.
    """
    resolver = get_constituent_resolver(source_name)
    run = await _claim_or_create_constituent_run(
        session,
        fund_id=fund_id,
        scheduled_job_id=scheduled_job_id,
        source_name=resolver.name,
    )

    try:
        holdings = await constituent_identity_service.unresolved_holdings(
            session, fund_id=fund_id, workspace_id=workspace_id, limit=limit
        )
        requests, holding_ids_by_key, unsafe = constituent_identity_service.build_requests(
            holdings, resolver=resolver
        )
        # Batch size + cache TTL come from the source budget. Fixtures are offline
        # and deterministic, so they bypass the recent-success cache (TTL 0) — that
        # keeps the idempotent upsert (not a cache hit) the thing guaranteeing no
        # duplicate rows on a rerun. External sources use the configured TTL.
        budget = await source_budget_service.get_budget(session, resolver.name)
        batch_size = budget.batch_size if budget and budget.batch_size else 10
        ttl_seconds = (
            0 if resolver.name.endswith("_fixture") else (get_settings().request_cache_ttl_seconds)
        )
        result = await constituent_identity_service.resolve_and_persist(
            session,
            resolver,
            requests,
            holding_ids_by_key,
            batch_size=batch_size,
            ttl_seconds=ttl_seconds,
        )
        result.skipped_unsafe = len(unsafe)

        run.records_inserted = result.resolved
        run.records_updated = result.ambiguous + result.not_found
        run.records_failed = result.failed
        if not holdings:
            run.status = "success"
            run.message = "No unresolved constituents to resolve."
        elif result.failed and result.attempted > result.failed:
            run.status = "partial_success"
            run.message = result.message()
        elif result.failed and not (result.resolved or result.ambiguous or result.not_found):
            run.status = "failed"
            run.message = result.message()
        else:
            run.status = "success"
            run.message = result.message()
    except Exception as exc:  # noqa: BLE001 - record failure on the run
        run.status = "failed"
        run.message = str(exc)

    run.finished_at = datetime.now(UTC)
    if scheduled_job_id is not None:
        scheduled = await session.get(ScheduledJob, scheduled_job_id)
        if scheduled is not None:
            scheduled.last_run_at = run.finished_at
    await session.commit()
    await session.refresh(run)
    return run


# --- constituent EOD price ingestion -----------------------------------------


async def _claim_or_create_constituent_price_run(
    session: AsyncSession,
    *,
    fund_id: int | None,
    scheduled_job_id: int | None,
    source_name: str,
) -> JobRun:
    """Reuse a queued fund-scoped price run if present, else create a new run."""
    run: JobRun | None = None
    if fund_id is not None:
        run = await session.scalar(
            select(JobRun)
            .where(
                JobRun.job_type == CONSTITUENT_PRICE_JOB,
                JobRun.fund_id == fund_id,
                JobRun.status == "queued",
            )
            .order_by(JobRun.id.desc())
        )
    if run is None:
        run = JobRun(job_type=CONSTITUENT_PRICE_JOB, fund_id=fund_id)
        session.add(run)
    run.status = "running"
    run.started_at = datetime.now(UTC)
    run.scheduled_job_id = scheduled_job_id
    run.source = source_name
    await session.flush()
    return run


async def _run_constituent_price_ingestion(
    session: AsyncSession,
    *,
    fund_id: int | None,
    scheduled_job_id: int | None,
    source_name: str | None,
    workspace_id: int | None,
    instrument_id: int | None,
    instrument_listing_id: int | None,
    limit: int | None,
) -> JobRun:
    """Fetch + store EOD prices for resolved constituent ``instrument_listings``.

    Consumes the market-data planner's ``fetch_constituent_price`` backlog: it
    selects resolved constituents (deduped, top-weight first, bounded by
    ``--limit``), fetches via the configured provider (offline fixture by default;
    Stooq/yfinance behind ``guarded_fetch`` when ``--source`` names them) and
    upserts bars idempotently. Counts fold into the job_run as inserted/updated/
    failed; the detailed breakdown (no_data, skipped budget/cache) is in the
    message.
    """
    source = get_instrument_price_source(source_name)
    run = await _claim_or_create_constituent_price_run(
        session,
        fund_id=fund_id,
        scheduled_job_id=scheduled_job_id,
        source_name=source.name,
    )

    try:
        listings = await instrument_prices_service.select_listings(
            session,
            fund_id=fund_id,
            workspace_id=workspace_id,
            instrument_id=instrument_id,
            instrument_listing_id=instrument_listing_id,
            limit=limit,
        )
        # Batch size + cache TTL come from the source budget. Fixtures are offline
        # and deterministic, so they bypass the recent-success cache (TTL 0) — the
        # idempotent upsert is what guarantees no duplicate rows on a rerun.
        # External sources use the configured TTL.
        budget = await source_budget_service.get_budget(session, source.name)
        batch_size = budget.batch_size if budget and budget.batch_size else 1
        ttl_seconds = (
            0 if source.name.endswith("_fixture") else get_settings().request_cache_ttl_seconds
        )
        counts = await instrument_prices_service.ingest_prices(
            session,
            source,
            listings,
            batch_size=batch_size,
            ttl_seconds=ttl_seconds,
        )

        run.records_inserted = counts.inserted
        run.records_updated = counts.updated
        run.records_failed = counts.failed
        if not listings:
            run.status = "success"
            run.message = "No resolved constituent listings to price."
        elif counts.failed and (counts.inserted or counts.updated):
            run.status = "partial_success"
            run.message = counts.message()
        elif counts.failed and not (counts.inserted or counts.updated):
            run.status = "failed"
            run.message = counts.message()
        else:
            run.status = "success"
            run.message = counts.message()
    except Exception as exc:  # noqa: BLE001 - record failure on the run
        run.status = "failed"
        run.message = str(exc)

    run.finished_at = datetime.now(UTC)
    if scheduled_job_id is not None:
        scheduled = await session.get(ScheduledJob, scheduled_job_id)
        if scheduled is not None:
            scheduled.last_run_at = run.finished_at
    await session.commit()
    await session.refresh(run)
    return run


# --- unified instrument EOD price ingestion ----------------------------------


async def _claim_or_create_instrument_price_run(
    session: AsyncSession,
    *,
    fund_id: int | None,
    workspace_id: int | None,
    scheduled_job_id: int | None,
    source_name: str,
) -> JobRun:
    """Reuse a queued fund-scoped instrument-price run if present, else create."""
    run: JobRun | None = None
    if fund_id is not None:
        run = await session.scalar(
            select(JobRun)
            .where(
                JobRun.job_type == INSTRUMENT_PRICE_JOB,
                JobRun.fund_id == fund_id,
                JobRun.status == "queued",
            )
            .order_by(JobRun.id.desc())
        )
    if run is None:
        run = JobRun(job_type=INSTRUMENT_PRICE_JOB, fund_id=fund_id, workspace_id=workspace_id)
        session.add(run)
    run.status = "running"
    run.started_at = datetime.now(UTC)
    run.scheduled_job_id = scheduled_job_id
    run.source = source_name
    await session.flush()
    return run


async def _run_instrument_eod_price_ingestion(
    session: AsyncSession,
    *,
    fund_id: int | None,
    scheduled_job_id: int | None,
    source_name: str | None,
    workspace_id: int | None,
    instrument_id: int | None,
    instrument_listing_id: int | None,
    broker_import_id: int | None,
    transaction_id: int | None,
    limit: int | None,
    force: bool,
) -> JobRun:
    """Fetch + store EOD prices for ANY resolved ``instrument_listing``.

    The unified entry point: it prices resolved ETF/fund constituents *and*
    resolved directly-held imported broker holdings through the same selector and
    idempotent upsert (``app/services/instrument_prices.py``), so an imported TSLA
    becomes chartable exactly like an ETF constituent. Consumes the planner's
    ``fetch_constituent_price`` + ``fetch_imported_instrument_price`` backlog;
    offline fixture by default, Stooq/yfinance behind ``guarded_fetch`` when named.
    ``--force`` re-prices fresh listings (the default skips them). Counts fold into
    the job_run as inserted/updated/failed; the full breakdown is in the message.
    """
    source = get_instrument_price_source(source_name)
    run = await _claim_or_create_instrument_price_run(
        session,
        fund_id=fund_id,
        workspace_id=workspace_id,
        scheduled_job_id=scheduled_job_id,
        source_name=source.name,
    )

    try:
        result = await instrument_prices_service.ingest_instrument_eod_prices(
            session,
            workspace_id=workspace_id,
            fund_id=fund_id,
            broker_import_id=broker_import_id,
            instrument_id=instrument_id,
            instrument_listing_id=instrument_listing_id,
            transaction_id=transaction_id,
            source=source.name,
            limit=limit,
            force=force,
        )
        run.records_inserted = result.inserted
        run.records_updated = result.updated
        run.records_failed = result.failed
        if result.selected == 0:
            run.status = "success"
            run.message = "No priceable instrument listings to price. " + result.message()
        elif result.failed and (result.inserted or result.updated):
            run.status = "partial_success"
            run.message = result.message()
        elif result.failed and not (result.inserted or result.updated):
            run.status = "failed"
            run.message = result.message()
        else:
            run.status = "success"
            run.message = result.message()
    except Exception as exc:  # noqa: BLE001 - record failure on the run
        run.status = "failed"
        run.message = str(exc)

    run.finished_at = datetime.now(UTC)
    if scheduled_job_id is not None:
        scheduled = await session.get(ScheduledJob, scheduled_job_id)
        if scheduled is not None:
            scheduled.last_run_at = run.finished_at
    await session.commit()
    await session.refresh(run)
    return run


# --- instrument onboarding orchestration -------------------------------------


async def _run_instrument_onboarding(
    session: AsyncSession,
    *,
    workspace_id: int | None,
    fund_id: int | None,
    scheduled_job_id: int | None,
    source_mode: str | None,
    plan_only: bool,
    limit: int | None,
    skip_exposure: bool,
    skip_alerts: bool,
) -> JobRun:
    """Orchestrate the data-readiness pipeline for a workspace/fund (or all).

    Delegates to ``app.services.instrument_onboarding`` (which calls the existing
    worker dispatch per stage). ``--plan-only`` writes nothing and returns a
    transient run summary; an explicit scope runs one workspace/fund; no scope
    runs every workspace under one umbrella job_run.
    """
    mode = (
        source_mode
        if source_mode in onboarding_service.SOURCE_MODES
        else (onboarding_service.FIXTURE_MODE)
    )

    if plan_only:
        ws = workspace_id
        if ws is None and fund_id is None:
            from app.services import workspaces as workspaces_service

            ws = (await workspaces_service.get_default_workspace(session)).id
        plan = await onboarding_service.build_onboarding_plan(
            session, workspace_id=ws, fund_id=fund_id, source_mode=mode, limit=limit
        )
        now = datetime.now(UTC)
        # Transient run (NOT added to the session) so plan-only writes nothing.
        return JobRun(
            job_type=ONBOARDING_JOB,
            status="planned",
            source=mode,
            started_at=now,
            finished_at=now,
            message=(
                f"plan-only scope={plan.scope} status={plan.status} "
                f"next={plan.next_recommended_action}"
            ),
        )

    if workspace_id is not None or fund_id is not None:
        result = await onboarding_service.execute_onboarding_plan(
            session,
            workspace_id=workspace_id,
            fund_id=fund_id,
            source_mode=mode,
            limit=limit,
            skip_exposure=skip_exposure,
            skip_alerts=skip_alerts,
            scheduled_job_id=scheduled_job_id,
        )
        run = await session.get(JobRun, result.parent_job_run_id)
        assert run is not None
        return run

    # No scope -> onboard every workspace under one umbrella orchestration run.
    umbrella = JobRun(
        job_type=ONBOARDING_JOB,
        status="running",
        source=mode,
        started_at=datetime.now(UTC),
        scheduled_job_id=scheduled_job_id,
    )
    session.add(umbrella)
    await session.commit()
    await session.refresh(umbrella)

    workspace_ids = await exposure_service.active_workspace_ids(session)
    statuses: list[str] = []
    child_ids: list[int] = []
    ins = upd = fail = 0
    for wid in workspace_ids:
        result = await onboarding_service.execute_onboarding_plan(
            session,
            workspace_id=wid,
            source_mode=mode,
            limit=limit,
            skip_exposure=skip_exposure,
            skip_alerts=skip_alerts,
        )
        statuses.append(result.status)
        if result.parent_job_run_id is not None:
            child_ids.append(result.parent_job_run_id)
        for sr in result.stages:
            ins += sr.records_inserted
            upd += sr.records_updated
            fail += sr.records_failed

    if "failed" in statuses and any(s != "failed" for s in statuses):
        umbrella.status = "partial_success"
    elif "failed" in statuses:
        umbrella.status = "failed"
    elif "partial_success" in statuses:
        umbrella.status = "partial_success"
    else:
        umbrella.status = "success"
    umbrella.records_inserted = ins
    umbrella.records_updated = upd
    umbrella.records_failed = fail
    umbrella.message = f"mode={mode} workspaces={len(workspace_ids)} onboarding_runs={child_ids}"
    umbrella.finished_at = datetime.now(UTC)
    if scheduled_job_id is not None:
        scheduled = await session.get(ScheduledJob, scheduled_job_id)
        if scheduled is not None:
            scheduled.last_run_at = umbrella.finished_at
    await session.commit()
    await session.refresh(umbrella)
    return umbrella


# --- broker CSV import -------------------------------------------------------


async def _run_broker_csv_import(
    session: AsyncSession,
    *,
    workspace_id: int | None,
    scheduled_job_id: int | None,
    source_name: str | None,
    csv_path: str | None,
) -> JobRun:
    """Commit a broker CSV into the canonical ledger for one workspace.

    CSV content comes from ``--csv-path`` (a local file) or, when omitted, the
    bundled offline ``generic_csv_v1`` sample — so a run is fully offline and
    never makes a live call. Idempotent: re-running the same content is a
    duplicate no-op. ``--workspace-id`` is required.
    """
    from pathlib import Path

    from app.schemas.broker_import import BrokerImportRequest
    from app.services import broker_imports as broker_service
    from app.services import workspaces as workspaces_service
    from app.sources.broker_imports import SAMPLE_GENERIC_CSV_V1

    run = JobRun(
        job_type=BROKER_IMPORT_JOB,
        scheduled_job_id=scheduled_job_id,
        workspace_id=workspace_id,
        source="broker_csv",
        started_at=datetime.now(UTC),
        status="running",
    )
    session.add(run)
    await session.flush()

    try:
        ws_id = workspace_id
        if ws_id is None:
            ws_id = (await workspaces_service.get_default_workspace(session)).id
            run.workspace_id = ws_id
        if csv_path:
            csv_text = Path(csv_path).read_text(encoding="utf-8")
            filename = Path(csv_path).name
        else:
            csv_text = SAMPLE_GENERIC_CSV_V1
            filename = "sample_generic_csv_v1.csv"
        request = BrokerImportRequest(
            broker_name=source_name or "generic_csv_v1",
            source_filename=filename,
            csv_text=csv_text,
            account_label="Imported",
        )
        result = await broker_service.commit_import(session, ws_id, request=request)
        run.records_inserted = result.summary.transaction_count
        run.records_updated = 0
        run.records_failed = result.summary.error_count
        if result.duplicate:
            run.status = "success"
            run.message = f"duplicate import (import_id={result.import_id}); no changes"
        elif result.summary.error_count and result.summary.transaction_count:
            run.status = "partial_success"
            run.message = (
                f"import_id={result.import_id} transactions={result.summary.transaction_count} "
                f"errors={result.summary.error_count} unresolved={result.summary.unresolved_count}"
            )
        else:
            run.status = "success"
            run.message = (
                f"import_id={result.import_id} transactions={result.summary.transaction_count} "
                f"unresolved={result.summary.unresolved_count} "
                f"cash_movements={result.summary.cash_movement_count}"
            )
    except Exception as exc:  # noqa: BLE001 - record failure on the run
        run.status = "failed"
        run.message = str(exc)

    run.finished_at = datetime.now(UTC)
    if scheduled_job_id is not None:
        scheduled = await session.get(ScheduledJob, scheduled_job_id)
        if scheduled is not None:
            scheduled.last_run_at = run.finished_at
    await session.commit()
    await session.refresh(run)
    return run


# --- imported-instrument resolution bridge -----------------------------------


async def _run_imported_instrument_resolution(
    session: AsyncSession,
    *,
    workspace_id: int | None,
    scheduled_job_id: int | None,
    source_name: str | None,
    broker_import_id: int | None,
    broker_account_id: int | None,
    transaction_id: int | None,
    limit: int | None,
) -> JobRun:
    """Resolve unresolved imported transactions to canonical instruments + relink.

    Consumes ``status=unresolved_instrument`` broker-import transactions (scoped by
    workspace / broker import / account / transaction), resolves them through the
    configured resolver (offline ``constituent_identity_fixture`` by default;
    ``--source openfigi`` for the live, budget-guarded path), idempotently upserts
    the shared instrument graph, backfills the transaction links and re-reconciles
    the position snapshot. Counts fold into the job_run as inserted=linked,
    updated=ambiguous+not_found, failed=failed; the full breakdown is in the
    message. Defaults are fully offline (no surprise live call).
    """
    from app.services import imported_instrument_resolution as resolution_service

    run = JobRun(
        job_type=IMPORTED_RESOLUTION_JOB,
        scheduled_job_id=scheduled_job_id,
        workspace_id=workspace_id,
        source=source_name or get_settings().constituent_identity_source_default,
        started_at=datetime.now(UTC),
        status="running",
    )
    session.add(run)
    await session.flush()

    try:
        result = await resolution_service.resolve_imported_instruments(
            session,
            workspace_id=workspace_id,
            broker_import_id=broker_import_id,
            broker_account_id=broker_account_id,
            transaction_id=transaction_id,
            limit=limit,
            source=source_name,
        )
        run.records_inserted = result.linked
        run.records_updated = result.ambiguous + result.not_found
        run.records_failed = result.failed
        if result.transactions_selected == 0:
            run.status = "success"
            run.message = "No unresolved imported transactions to resolve."
        elif result.failed and result.attempted > result.failed:
            run.status = "partial_success"
            run.message = result.message()
        elif result.failed and not (result.linked or result.ambiguous or result.not_found):
            run.status = "failed"
            run.message = result.message()
        else:
            run.status = "success"
            run.message = result.message()
    except Exception as exc:  # noqa: BLE001 - record failure on the run
        run.status = "failed"
        run.message = str(exc)

    run.finished_at = datetime.now(UTC)
    if scheduled_job_id is not None:
        scheduled = await session.get(ScheduledJob, scheduled_job_id)
        if scheduled is not None:
            scheduled.last_run_at = run.finished_at
    await session.commit()
    await session.refresh(run)
    return run


# --- reference-rate ingestion ------------------------------------------------


async def _run_rates_ingestion(
    session: AsyncSession,
    *,
    scheduled_job_id: int | None,
    source_name: str | None,
    currency: str | None,
    country_or_region: str | None,
    rate_family: str | None,
    start_date: date | None,
    end_date: date | None,
    limit: int | None,
) -> JobRun:
    """Collect + persist official/reference rate observations.

    Resolves the configured adapter (offline ``rates_fixture`` by default; the live
    ``us_treasury_rates`` adapter fetches the official Treasury par-yield XML feed and
    ``ecb_rates`` the official ECB Data Portal SDMX API, both through ``guarded_fetch``
    when named; ``boe_rates`` is planned and fails cleanly), fetches the filtered
    observations and idempotently upserts them into ``reference_rates``. Counts fold
    into the job_run as inserted/updated/failed; the breakdown (selected/skipped/date
    range) is in the message.

    Collection only — never builds curves, bootstraps, interpolates or prices.
    """
    from app.services import rates_ingestion as rates_service

    src_name = source_name or get_settings().rates_source_default
    run = JobRun(
        job_type=RATES_JOB,
        scheduled_job_id=scheduled_job_id,
        source=src_name,
        started_at=datetime.now(UTC),
        status="running",
    )
    session.add(run)
    await session.flush()

    try:
        result = await rates_service.ingest_reference_rates(
            session,
            source=src_name,
            currency=currency,
            country_or_region=country_or_region,
            rate_family=rate_family,
            start_date=start_date,
            end_date=end_date,
            limit=limit if limit is not None else 1000,
        )
        run.records_inserted = result.inserted
        run.records_updated = result.updated
        run.records_failed = result.failed
        if result.selected == 0:
            run.status = "success"
            run.message = "No reference rates selected. " + result.message()
        elif result.failed and (result.inserted or result.updated or result.skipped):
            run.status = "partial_success"
            run.message = result.message()
        elif result.failed and not (result.inserted or result.updated or result.skipped):
            run.status = "failed"
            run.message = result.message()
        else:
            run.status = "success"
            run.message = result.message()
    except Exception as exc:  # noqa: BLE001 - record failure on the run
        run.status = "failed"
        run.message = str(exc)

    run.finished_at = datetime.now(UTC)
    if scheduled_job_id is not None:
        scheduled = await session.get(ScheduledJob, scheduled_job_id)
        if scheduled is not None:
            scheduled.last_run_at = run.finished_at
    await session.commit()
    await session.refresh(run)
    return run


# --- portfolio valuation/readiness recompute ---------------------------------


async def _run_portfolio_valuation_recompute(
    session: AsyncSession,
    *,
    workspace_id: int | None,
    scheduled_job_id: int | None,
    as_of_date: date | None,
    base_currency: str | None,
    broker_account_id: int | None,
    limit: int | None,
    force: bool,
) -> JobRun:
    """Recompute the bounded portfolio valuation/readiness snapshot for a workspace.

    Consumes already-ingested prices/FX only — it never fetches a price/FX source
    and never resolves identity (see AGENTS.md compute boundary). ``--workspace-id``
    is required; runs every workspace when omitted. ``--as-of-date`` /
    ``--base-currency`` / ``--broker-account-id`` narrow the valuation; ``--force``
    refreshes an unchanged snapshot's rows in place. Counts fold into the job_run as
    inserted=snapshots created, updated=snapshots updated; the breakdown is in the
    message. This is NOT PnL.
    """
    from app.services import portfolio_valuation as valuation_service

    run = JobRun(
        job_type=PORTFOLIO_VALUATION_JOB,
        scheduled_job_id=scheduled_job_id,
        workspace_id=workspace_id,
        source=valuation_service.SOURCE,
        started_at=datetime.now(UTC),
        status="running",
    )
    session.add(run)
    await session.flush()

    try:
        if workspace_id is not None:
            workspace_ids = [workspace_id]
        else:
            workspace_ids = await exposure_service.active_workspace_ids(session)

        created = updated = skipped = failed = 0
        last_message = ""
        for wid in workspace_ids:
            try:
                result = await valuation_service.recompute_portfolio_valuation_snapshot(
                    session,
                    wid,
                    as_of_date=as_of_date,
                    base_currency=base_currency,
                    broker_account_id=broker_account_id,
                    limit=limit if limit is not None else valuation_service.DEFAULT_LIMIT,
                    force=force,
                )
                await session.commit()
                created += 1 if result.snapshot_created else 0
                updated += 1 if result.snapshot_updated else 0
                skipped += 1 if result.snapshot_skipped else 0
                last_message = result.message()
            except Exception as exc:  # noqa: BLE001 - isolate one workspace's failure
                await session.rollback()
                failed += 1
                last_message = f"workspace {wid}: {exc}"

        run.records_inserted = created
        run.records_updated = updated
        run.records_failed = failed
        if failed and (created or updated or skipped):
            run.status = "partial_success"
        elif failed:
            run.status = "failed"
        else:
            run.status = "success"
        run.message = (
            f"workspaces={len(workspace_ids)} created={created} updated={updated} "
            f"skipped={skipped} failed={failed}" + (f" | {last_message}" if last_message else "")
        )
    except Exception as exc:  # noqa: BLE001 - record failure on the run
        run.status = "failed"
        run.message = str(exc)

    run.finished_at = datetime.now(UTC)
    if scheduled_job_id is not None:
        scheduled = await session.get(ScheduledJob, scheduled_job_id)
        if scheduled is not None:
            scheduled.last_run_at = run.finished_at
    await session.commit()
    await session.refresh(run)
    return run


async def _amain(
    job_type: str,
    fund_id: int | None,
    fund_listing_id: int | None,
    source_name: str | None,
    base_currency: str | None,
    quote_currencies: list[str] | None,
    workspace_id: int | None,
    limit: int | None,
    instrument_id: int | None,
    instrument_listing_id: int | None,
    source_mode: str | None,
    plan_only: bool,
    skip_exposure: bool,
    skip_alerts: bool,
    csv_path: str | None,
    broker_import_id: int | None,
    broker_account_id: int | None,
    transaction_id: int | None,
    force: bool,
    currency: str | None,
    country_or_region: str | None,
    rate_family: str | None,
    start_date: date | None,
    end_date: date | None,
    as_of_date: date | None,
    url: str | None,
    verify_source: bool,
) -> None:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        run = await run_job(
            session,
            job_type,
            fund_id=fund_id,
            fund_listing_id=fund_listing_id,
            source_name=source_name,
            base_currency=base_currency,
            quote_currencies=quote_currencies,
            workspace_id=workspace_id,
            limit=limit,
            instrument_id=instrument_id,
            instrument_listing_id=instrument_listing_id,
            source_mode=source_mode,
            plan_only=plan_only,
            skip_exposure=skip_exposure,
            skip_alerts=skip_alerts,
            csv_path=csv_path,
            broker_import_id=broker_import_id,
            broker_account_id=broker_account_id,
            transaction_id=transaction_id,
            force=force,
            currency=currency,
            country_or_region=country_or_region,
            rate_family=rate_family,
            start_date=start_date,
            end_date=end_date,
            as_of_date=as_of_date,
            url=url,
            verify_source=verify_source,
        )
        print(
            f"job_run={run.id} type={run.job_type} status={run.status} "
            f"inserted={run.records_inserted} updated={run.records_updated} "
            f"failed={run.records_failed}"
        )
        if run.message:
            print(f"  {run.message}")
    await get_engine().dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description="Run an ingestion/maintenance job.")
    parser.add_argument(
        "job_type",
        help="e.g. price_ingestion | issuer_facts_ingestion | fx_ingestion",
    )
    parser.add_argument("--fund-id", type=int, default=None)
    parser.add_argument("--fund-listing-id", type=int, default=None)
    parser.add_argument("--source", dest="source_name", default=None)
    # fx_ingestion only: base currency + repeatable --quote (else inferred).
    parser.add_argument("--base", dest="base_currency", default=None)
    parser.add_argument("--quote", dest="quote_currencies", action="append", default=None)
    # alert_generation / constituent_identity_resolution: scope to one workspace.
    parser.add_argument("--workspace-id", dest="workspace_id", type=int, default=None)
    # constituent_identity_resolution / constituent_eod_price_ingestion: cap how
    # many constituents/listings to attempt.
    parser.add_argument("--limit", dest="limit", type=int, default=None)
    # constituent_eod_price_ingestion only: target one instrument / listing.
    parser.add_argument("--instrument-id", dest="instrument_id", type=int, default=None)
    parser.add_argument(
        "--instrument-listing-id", dest="instrument_listing_id", type=int, default=None
    )
    # instrument_onboarding only: source-mode (fixture default / live explicit),
    # read-only planning, and per-stage skips.
    parser.add_argument(
        "--source-mode", dest="source_mode", choices=["fixture", "live"], default=None
    )
    parser.add_argument("--plan-only", dest="plan_only", action="store_true")
    parser.add_argument("--skip-exposure", dest="skip_exposure", action="store_true")
    parser.add_argument("--skip-alerts", dest="skip_alerts", action="store_true")
    # broker_csv_import only: a local CSV file (else the bundled offline sample).
    parser.add_argument("--csv-path", dest="csv_path", default=None)
    # imported_instrument_resolution / instrument_eod_price_ingestion: narrow the
    # transaction / import scope.
    parser.add_argument("--broker-import-id", dest="broker_import_id", type=int, default=None)
    parser.add_argument("--broker-account-id", dest="broker_account_id", type=int, default=None)
    parser.add_argument("--transaction-id", dest="transaction_id", type=int, default=None)
    # instrument_eod_price_ingestion only: re-price even fresh listings.
    parser.add_argument("--force", dest="force", action="store_true")
    # rates_ingestion only: narrow the collected reference-rate series + date range.
    parser.add_argument("--currency", dest="currency", default=None)
    parser.add_argument("--country-or-region", dest="country_or_region", default=None)
    parser.add_argument("--rate-family", dest="rate_family", default=None)
    # rates_ingestion / fx_ingestion: bound the fetched daily history (ISO dates).
    parser.add_argument("--start-date", dest="start_date", default=None)
    parser.add_argument("--end-date", dest="end_date", default=None)
    # portfolio_valuation_recompute only: value as of this date (default today) and
    # override the workspace base currency for the valuation.
    parser.add_argument("--as-of-date", dest="as_of_date", default=None)
    parser.add_argument("--base-currency", dest="base_currency", default=None)
    # issuer_holdings_ingestion / distribution_ingestion: explicit issuer download
    # URL override (single-fund runs), or a local exported-file path for the
    # *_export offline parsers.
    parser.add_argument("--url", dest="url", default=None)
    # issuer_holdings_ingestion / distribution_ingestion only: verify-only mode —
    # one guarded live fetch+parse of the known/--url source config, no ingestion.
    parser.add_argument("--verify-source", dest="verify_source", action="store_true")
    args = parser.parse_args()
    start_date = date.fromisoformat(args.start_date) if args.start_date else None
    end_date = date.fromisoformat(args.end_date) if args.end_date else None
    as_of_date = date.fromisoformat(args.as_of_date) if args.as_of_date else None
    asyncio.run(
        _amain(
            args.job_type,
            args.fund_id,
            args.fund_listing_id,
            args.source_name,
            args.base_currency,
            args.quote_currencies,
            args.workspace_id,
            args.limit,
            args.instrument_id,
            args.instrument_listing_id,
            args.source_mode,
            args.plan_only,
            args.skip_exposure,
            args.skip_alerts,
            args.csv_path,
            args.broker_import_id,
            args.broker_account_id,
            args.transaction_id,
            args.force,
            args.currency,
            args.country_or_region,
            args.rate_family,
            start_date,
            end_date,
            as_of_date,
            args.url,
            args.verify_source,
        )
    )


if __name__ == "__main__":
    main()
