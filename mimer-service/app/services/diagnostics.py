"""Data-quality / diagnostics counts.

A first pass: simple counts derived from existing statuses, sources and job
states. Freshness and provenance counts can be scoped to a workspace's held
funds; job-queue health is shared infrastructure and stays global.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.db.models import (
    BrokerImport,
    BrokerImportRow,
    Distribution,
    DocumentSnapshot,
    ExposureSnapshot,
    Fund,
    FundListing,
    JobRun,
    PortfolioPosition,
    PortfolioPositionSnapshot,
    PortfolioTransaction,
    PortfolioValuationSnapshot,
    Price,
    ReferenceRate,
    ScheduledJob,
    SecurityIdentifier,
    SourceFetchLog,
    Workspace,
)
from app.schemas.diagnostics import Diagnostics, WorkspaceDiagnostics
from app.services import alert_rules, market_data_planner
from app.services import alerts as alerts_service
from app.services import exposure_drift as exposure_drift_service
from app.services import exposure_recompute as exposure_recompute_service
from app.services import freshness as freshness_service
from app.services import holding_performance as holding_performance_service
from app.services import holdings_ingestion as holdings_service
from app.services import instrument_onboarding as onboarding_service
from app.services import job_leases as job_leases_service
from app.services import portfolio_valuation as portfolio_valuation_service
from app.services import source_budget as source_budget_service
from app.services import workspaces as workspaces_service
from app.services.conversion import normalise_currency
from app.services.fx import MISSING, STALE, load_fx_index
from app.sources import issuer_source_config
from app.workers import scheduler as scheduler_worker

# Sources that mark data as placeholder / non-authoritative.
_MOCK_SOURCES = {"seed", "stub", "issuer_fixture"}
_MANUAL_SOURCES = {"manual"}
_DERIVED_SOURCES = {"derived", "estimated"}
# A job still "running" longer than this is treated as stuck (crashed worker).
_STUCK_JOB_SECONDS = 3600
# Window for "recent" external fetch failures.
_RECENT_FETCH_SECONDS = 86400
# Upper bound on the manual-correction provenance scan (keeps the count cheap).
_MAX_CORRECTION_SCAN = 2000


async def _scope_listing_ids(session: AsyncSession, workspace_id: int) -> list[int]:
    rows = (
        await session.execute(
            select(PortfolioPosition.fund_listing_id)
            .where(PortfolioPosition.workspace_id == workspace_id)
            .distinct()
        )
    ).scalars()
    return list(rows)


async def _fx_diagnostics(
    session: AsyncSession,
    diag: Diagnostics,
    *,
    base_currency: str,
    workspace_id: int | None,
) -> None:
    """Count FX coverage gaps for non-base-currency positions in scope.

    ``workspace_id=None`` scopes to every position (global view). A position's
    local currency is taken from its listing's quote/trading currency (GBX -> GBP);
    a path is sought to the base currency, classified by the rate's freshness.
    """
    base = base_currency.upper()
    stmt = select(FundListing.currency_unit, FundListing.trading_currency).join(
        PortfolioPosition, PortfolioPosition.fund_listing_id == FundListing.id
    )
    if workspace_id is not None:
        stmt = stmt.where(PortfolioPosition.workspace_id == workspace_id)
    rows = (await session.execute(stmt)).all()

    fx_index = await load_fx_index(session)
    missing_currencies: set[str] = set()
    stale_currencies: set[str] = set()
    for currency_unit, trading_currency in rows:
        local = normalise_currency(currency_unit or trading_currency, base)
        if local == base:
            continue
        result = fx_index.get_fx_rate(local, base)
        if result.status == MISSING:
            diag.unconverted_positions += 1
            missing_currencies.add(local)
        elif result.status == STALE:
            stale_currencies.add(local)
    diag.missing_fx_rates = len(missing_currencies)
    diag.stale_fx_rates = len(stale_currencies)

    # fx_ingestion runs that failed outright or had per-pair failures (global).
    diag.fx_conversion_failures = (
        await session.scalar(
            select(func.count())
            .select_from(JobRun)
            .where(
                JobRun.job_type == "fx_ingestion",
                or_(JobRun.status.in_(["failed", "partial_success"]), JobRun.records_failed > 0),
            )
        )
    ) or 0


# Document types that satisfy "the fund has key documents" (lower-cased).
_KEY_DOCUMENT_TYPES = {"factsheet", "kid", "kiid", "prospectus"}


async def _document_diagnostics(
    session: AsyncSession, diag: Diagnostics, *, fund_ids: list[int]
) -> None:
    """Document coverage + change activity for the scoped funds.

    * missing_documents — funds holding *none* of the key document types
    * stale_documents   — funds whose newest dated document is past the window
    * changed_documents — snapshots whose ingestion detected a content change
    * new_documents     — first-version (newly tracked) snapshots
    * failed_document_jobs — failed/partial document_snapshot_ingestion runs
    """
    if fund_ids:
        docs = list(
            (
                await session.execute(
                    select(DocumentSnapshot).where(DocumentSnapshot.fund_id.in_(fund_ids))
                )
            )
            .scalars()
            .all()
        )
        by_fund: dict[int, list[DocumentSnapshot]] = {fid: [] for fid in fund_ids}
        for doc in docs:
            by_fund.setdefault(doc.fund_id, []).append(doc)
            if doc.change_status == "changed":
                diag.changed_documents += 1
            elif doc.change_status == "new":
                diag.new_documents += 1

        for fund_id in fund_ids:
            fund_docs = by_fund.get(fund_id) or []
            types = {d.document_type.lower() for d in fund_docs}
            if types.isdisjoint(_KEY_DOCUMENT_TYPES):
                diag.missing_documents += 1
                continue
            dates = [d.document_date for d in fund_docs if d.document_date is not None]
            if dates and (
                freshness_service.freshness_state(max(dates), kind="document")
                == freshness_service.STALE
            ):
                diag.stale_documents += 1

    diag.failed_document_jobs = (
        await session.scalar(
            select(func.count())
            .select_from(JobRun)
            .where(
                JobRun.job_type == "document_snapshot_ingestion",
                JobRun.status.in_(["failed", "partial_success"]),
            )
        )
    ) or 0


async def _distribution_diagnostics(
    session: AsyncSession, diag: Diagnostics, *, funds: list[Fund]
) -> None:
    """Distribution coverage for the scoped funds.

    * distributions       — stored distribution rows for the scoped funds
    * latest_distribution_date — newest ex-date across the scoped funds
    * missing_distributions — distributing/unknown-policy funds with no distributions
    * stale_distributions — distributing funds whose newest ex-date is past the window
    * distribution_ingestion_failures — failed/partial distribution_ingestion runs (global)

    Collection coverage only — never dividend-forecast / yield-projection health. An
    accumulating fund pays nothing, so it is never counted as missing/stale.
    """
    fund_ids = [f.id for f in funds]
    if fund_ids:
        diag.distributions = (
            await session.scalar(
                select(func.count())
                .select_from(Distribution)
                .where(Distribution.fund_id.in_(fund_ids))
            )
        ) or 0
        diag.latest_distribution_date = await session.scalar(
            select(func.max(Distribution.ex_date)).where(Distribution.fund_id.in_(fund_ids))
        )
        latest_by_fund = dict(
            (
                await session.execute(
                    select(Distribution.fund_id, func.max(Distribution.ex_date))
                    .where(Distribution.fund_id.in_(fund_ids))
                    .group_by(Distribution.fund_id)
                )
            ).all()
        )
        for fund in funds:
            if (fund.distribution_policy or "") == "accumulating":
                continue
            latest = latest_by_fund.get(fund.id)
            if latest is None:
                diag.missing_distributions += 1
            elif (
                freshness_service.freshness_state(latest, kind="distribution")
                == freshness_service.STALE
            ):
                diag.stale_distributions += 1

    diag.distribution_ingestion_failures = (
        await session.scalar(
            select(func.count())
            .select_from(JobRun)
            .where(
                JobRun.job_type == "distribution_ingestion",
                JobRun.status.in_(["failed", "partial_success"]),
            )
        )
    ) or 0


async def _compute(
    session: AsyncSession,
    *,
    fund_ids: list[int] | None,
    listing_ids: list[int] | None,
    base_currency: str,
    position_workspace_id: int | None,
) -> Diagnostics:
    diag = Diagnostics()

    # --- Listings: price freshness + lifecycle -------------------------------
    listing_stmt = select(FundListing)
    if listing_ids is not None:
        listing_stmt = listing_stmt.where(FundListing.id.in_(listing_ids))
    elif fund_ids is not None:
        listing_stmt = listing_stmt.where(FundListing.fund_id.in_(fund_ids))
    listings = list((await session.execute(listing_stmt)).scalars().all())
    scoped_listing_ids = [ln.id for ln in listings]

    for listing in listings:
        state = freshness_service.freshness_state(listing.last_price_at, kind="price")
        setattr(diag, state, getattr(diag, state) + 1)
        if listing.status == "pending":
            diag.pending += 1

    # --- Funds: fact provenance + lifecycle ----------------------------------
    fund_stmt = select(Fund)
    if fund_ids is not None:
        fund_stmt = fund_stmt.where(Fund.id.in_(fund_ids))
    elif listing_ids is not None and scoped_listing_ids:
        fund_stmt = fund_stmt.where(
            Fund.id.in_(select(FundListing.fund_id).where(FundListing.id.in_(scoped_listing_ids)))
        )
    funds = list((await session.execute(fund_stmt)).scalars().all())
    for fund in funds:
        if fund.status == "pending":
            diag.pending += 1
        if fund.source in _MOCK_SOURCES:
            diag.mock_or_seed += 1
        elif fund.source in _MANUAL_SOURCES:
            diag.manual_overrides += 1
        elif fund.source in _DERIVED_SOURCES:
            diag.estimated_or_derived += 1

    # --- Holdings coverage: missing / stale latest snapshot per scoped fund ---
    scoped_fund_ids = [f.id for f in funds]
    snapshots = await holdings_service.latest_holdings_by_fund(session, scoped_fund_ids)
    for fund_id in scoped_fund_ids:
        snapshot = snapshots.get(fund_id) or []
        if not snapshot:
            diag.missing_holdings += 1
            continue
        as_of = max(h.as_of_date for h in snapshot)
        if freshness_service.freshness_state(as_of, kind="holdings") == freshness_service.STALE:
            diag.stale_holdings += 1

    # --- Document coverage: key docs present/fresh + change activity -----------
    await _document_diagnostics(session, diag, fund_ids=scoped_fund_ids)

    # --- Distribution coverage: missing/stale per distributing scoped fund -----
    await _distribution_diagnostics(session, diag, funds=funds)

    # --- Issuer source-config coverage (registry counts global; missing scoped) -
    # Informational only (the offline fixture default always works; no alerts).
    status_counts = issuer_source_config.status_counts()
    diag.issuer_source_configs = sum(status_counts.values())
    diag.verified_issuer_source_configs = status_counts.get(issuer_source_config.VERIFIED, 0)
    diag.candidate_issuer_source_configs = status_counts.get(issuer_source_config.CANDIDATE, 0)
    for fund in funds:
        if not issuer_source_config.has_source_config(
            fund.isin, issuer_source_config.DATA_TYPE_HOLDINGS, usable_only=True
        ):
            diag.missing_holdings_source_config += 1
        if (fund.distribution_policy or "") != "accumulating" and not (
            issuer_source_config.has_source_config(
                fund.isin, issuer_source_config.DATA_TYPE_DISTRIBUTIONS, usable_only=True
            )
        ):
            diag.missing_distribution_source_config += 1

    # --- Source conflicts: same listing+date asserted by multiple sources ----
    conflict_stmt = (
        select(Price.fund_listing_id, Price.price_date)
        .group_by(Price.fund_listing_id, Price.price_date)
        .having(func.count(func.distinct(Price.source)) > 1)
    )
    if scoped_listing_ids:
        conflict_stmt = conflict_stmt.where(Price.fund_listing_id.in_(scoped_listing_ids))
    elif listing_ids is not None or fund_ids is not None:
        conflict_stmt = conflict_stmt.where(Price.fund_listing_id.in_([]))
    diag.source_conflicts = len((await session.execute(conflict_stmt)).all())

    # --- Ambiguous instruments: identifiers resolved below high confidence ---
    ambiguous_stmt = (
        select(func.count())
        .select_from(SecurityIdentifier)
        .where(SecurityIdentifier.confidence != "high")
    )
    if fund_ids is not None:
        ambiguous_stmt = ambiguous_stmt.where(SecurityIdentifier.fund_id.in_(fund_ids))
    elif scoped_listing_ids:
        ambiguous_stmt = ambiguous_stmt.where(
            SecurityIdentifier.fund_id.in_(
                select(FundListing.fund_id).where(FundListing.id.in_(scoped_listing_ids))
            )
        )
    diag.ambiguous_instruments = (await session.scalar(ambiguous_stmt)) or 0

    # --- Job-queue health (shared infrastructure; always global) -------------
    diag.failed_jobs = (
        await session.scalar(
            select(func.count()).select_from(JobRun).where(JobRun.status == "failed")
        )
    ) or 0
    diag.queued_jobs = (
        await session.scalar(
            select(func.count()).select_from(JobRun).where(JobRun.status == "queued")
        )
    ) or 0
    diag.failed = diag.failed_jobs

    # --- FX coverage (non-base-currency positions in scope) ------------------
    await _fx_diagnostics(
        session, diag, base_currency=base_currency, workspace_id=position_workspace_id
    )

    return diag


async def _apply_alert_counts(
    session: AsyncSession, diag: Diagnostics, *, workspace_id: int | None
) -> None:
    """Merge alert counts in (workspace-scoped, or summed across workspaces)."""
    counts = await alerts_service.alert_counts(session, workspace_id)
    for field, value in counts.model_dump().items():
        setattr(diag, field, value)


async def _apply_exposure_diagnostics(
    session: AsyncSession, diag: Diagnostics, *, workspace_id: int | None
) -> None:
    """Exposure coverage diagnostics (one workspace, or summed across all)."""
    now = datetime.now(UTC)
    diag.exposure_recompute_failures = (
        await session.scalar(
            select(func.count())
            .select_from(JobRun)
            .where(JobRun.job_type == "exposure_recompute", JobRun.status == "failed")
        )
    ) or 0

    if workspace_id is not None:
        workspace_ids = [workspace_id]
    else:
        workspace_ids = list((await session.execute(select(Workspace.id))).scalars().all())
    positions_ws = set(
        (await session.execute(select(PortfolioPosition.workspace_id).distinct())).scalars()
    )

    for wid in workspace_ids:
        latest = await exposure_recompute_service.get_latest_snapshot(session, wid)
        if latest is None:
            if wid in positions_ws:
                diag.missing_exposure_snapshots += 1
            continue
        if exposure_recompute_service.snapshot_age_days(latest, now=now) > (
            alert_rules.EXPOSURE_STALE_DAYS
        ):
            diag.stale_exposure_snapshots += 1
        if (
            latest.status != "empty"
            and latest.coverage_weight is not None
            and latest.coverage_weight < alert_rules.EXPOSURE_MIN_COVERAGE
        ):
            diag.low_exposure_coverage += 1
        diag.missing_holdings_for_exposure += latest.missing_holdings_count or 0
        diag.missing_fx_for_exposure += latest.missing_fx_count or 0

        # Cheap onboarding "data-ready" proxy (reuses the already-loaded latest
        # snapshot; the workspace view refines this from the onboarding plan).
        if (
            wid in positions_ws
            and latest.status != "empty"
            and latest.coverage_weight is not None
            and latest.coverage_weight >= alert_rules.EXPOSURE_MIN_COVERAGE
            and exposure_recompute_service.snapshot_age_days(latest, now=now)
            <= alert_rules.EXPOSURE_STALE_DAYS
        ):
            diag.onboarding_ready_workspaces += 1

        # --- true constituent look-through coverage ---
        # Measured as a fraction *of the looked-through holdings* (so a fund whose
        # holdings simply aren't disclosed is not punished twice). Only flagged
        # once some resolution has happened (identity_coverage > 0) — the clean
        # pre-resolution state is surfaced by the market-data plan, not here.
        holdings_cov = latest.coverage_weight or Decimal("0")
        identity_cov = latest.identity_coverage_weight or Decimal("0")
        price_cov = latest.price_coverage_weight or Decimal("0")
        if latest.status != "empty" and identity_cov > 0:
            if holdings_cov > 0 and (
                identity_cov / holdings_cov < alert_rules.CONSTITUENT_MIN_IDENTITY_COVERAGE
            ):
                diag.low_constituent_identity_coverage += 1
            if price_cov / identity_cov < alert_rules.CONSTITUENT_MIN_PRICE_COVERAGE:
                diag.low_constituent_price_coverage += 1
        diag.constituent_valuation_fx_missing += latest.constituent_fx_missing_count or 0
        if workspace_id is not None:
            diag.unclassified_exposure_weight = latest.unclassified_weight
            diag.constituent_valuation_unclassified_weight = latest.unclassified_weight

        # --- exposure drift (latest vs previous snapshot) ---
        await _apply_drift_diagnostics(session, diag, wid, latest)


async def _apply_drift_diagnostics(
    session: AsyncSession,
    diag: Diagnostics,
    workspace_id: int,
    latest: ExposureSnapshot,
) -> None:
    """Large-drift + coverage-deterioration counts from latest vs previous snapshot.

    Stays quiet with only one snapshot (``no_prior_exposure_snapshot_for_drift``).
    Compares snapshots only — never infers trades (see AGENTS.md)."""
    previous = await exposure_drift_service.previous_snapshot(session, workspace_id, latest)
    if previous is None:
        diag.no_prior_exposure_snapshot_for_drift += 1
        diag.top_holding_performance_insufficient_history += 1
        return

    # --- top-holding price-context performance data quality ---
    # Conservative + data-quality oriented: never flags an ordinary price move,
    # only *why a contribution view is incomplete* (missing prices / FX context).
    perf = await holding_performance_service.compute_top_holding_performance(session, workspace_id)
    if perf.summary is not None:
        if perf.summary.missing_price_count > 0:
            diag.top_holding_performance_missing_prices += 1
        if perf.summary.fx_missing_count > 0:
            diag.top_holding_performance_fx_missing += 1

    threshold = alert_rules.EXPOSURE_DRIFT_WEIGHT_THRESHOLD
    deterioration = alert_rules.COVERAGE_DETERIORATION
    drift_dims = {
        "constituent": "large_constituent_exposure_drift",
        "sector": "large_sector_exposure_drift",
        "currency": "large_currency_exposure_drift",
    }
    for dimension, field in drift_dims.items():
        drift = await exposure_drift_service.compute_drift(
            session, workspace_id, dimension=dimension, with_price_context=False
        )
        summary = drift.summary
        if summary is None:
            continue
        if summary.total_abs_weight_delta >= threshold:
            setattr(diag, field, getattr(diag, field) + 1)
        if dimension == "constituent":
            if summary.price_coverage_delta is not None and (
                summary.price_coverage_delta <= -deterioration
            ):
                diag.price_coverage_deteriorated += 1
            if summary.fx_coverage_delta is not None and (
                summary.fx_coverage_delta <= -deterioration
            ):
                diag.fx_coverage_deteriorated += 1


async def _apply_operational_diagnostics(session: AsyncSession, diag: Diagnostics) -> None:
    """Scheduler + external-fetch health (shared infrastructure; always global)."""
    now = datetime.now(UTC)

    diag.due_scheduled_jobs = len(await scheduler_worker.due_jobs(session, now=now))

    diag.running_jobs = (
        await session.scalar(
            select(func.count()).select_from(JobRun).where(JobRun.status == "running")
        )
    ) or 0
    stuck_cutoff = now - timedelta(seconds=_STUCK_JOB_SECONDS)
    diag.stuck_jobs = (
        await session.scalar(
            select(func.count())
            .select_from(JobRun)
            .where(JobRun.status == "running", JobRun.started_at < stuck_cutoff)
        )
    ) or 0

    # Live scheduled-job lease health via the shared lease classifier, so these
    # agree with /scheduler/status and the /jobs/running read model (one
    # definition of running / stuck / expired / blocked).
    lease_summary = await job_leases_service.lease_summary_counts(session, now=now)
    diag.expired_job_leases = lease_summary.expired_lease_count
    diag.running_job_leases = lease_summary.running_count
    diag.stuck_job_leases = lease_summary.stuck_lease_count
    diag.blocked_scheduled_jobs_by_lease = lease_summary.blocked_by_lease_count

    # Failed constituent_identity_resolution runs (shared job-queue infrastructure).
    diag.constituent_identity_resolution_failures = (
        await session.scalar(
            select(func.count())
            .select_from(JobRun)
            .where(
                JobRun.job_type == "constituent_identity_resolution",
                JobRun.status == "failed",
            )
        )
    ) or 0

    # Failed constituent_eod_price_ingestion runs (shared job-queue infrastructure).
    diag.constituent_price_ingestion_failures = (
        await session.scalar(
            select(func.count())
            .select_from(JobRun)
            .where(
                JobRun.job_type == "constituent_eod_price_ingestion",
                JobRun.status == "failed",
            )
        )
    ) or 0

    # Failed unified instrument_eod_price_ingestion runs (shared job-queue
    # infrastructure; prices constituents + resolved imported direct holdings).
    diag.instrument_price_ingestion_failures = (
        await session.scalar(
            select(func.count())
            .select_from(JobRun)
            .where(
                JobRun.job_type == "instrument_eod_price_ingestion",
                JobRun.status == "failed",
            )
        )
    ) or 0

    recent_cutoff = now - timedelta(seconds=_RECENT_FETCH_SECONDS)
    diag.recent_failed_fetches = (
        await session.scalar(
            select(func.count())
            .select_from(SourceFetchLog)
            .where(SourceFetchLog.status == "failed", SourceFetchLog.started_at >= recent_cutoff)
        )
    ) or 0
    diag.rate_limited_sources = (
        await session.scalar(
            select(func.count(func.distinct(SourceFetchLog.source_name))).where(
                SourceFetchLog.status == "rate_limited", SourceFetchLog.started_at >= recent_cutoff
            )
        )
    ) or 0
    diag.sources_in_backoff = len(await source_budget_service.sources_in_backoff(session, now=now))

    # --- generic job-run observability rollup (bounded; feeds Data Operations) -
    diag.recent_partial_job_runs = (
        await session.scalar(
            select(func.count()).select_from(JobRun).where(JobRun.status == "partial_success")
        )
    ) or 0
    latest_failed = await session.scalar(
        select(JobRun)
        .where(JobRun.status.in_(["failed", "partial_success"]))
        .order_by(JobRun.id.desc())
        .limit(1)
    )
    if latest_failed is not None:
        diag.latest_failed_job_run_id = latest_failed.id
        diag.latest_failed_job_run_type = latest_failed.job_type


async def _apply_market_data_diagnostics(
    session: AsyncSession, diag: Diagnostics, *, workspace_id: int
) -> None:
    """Lightweight market-data plan rollup (workspace-scoped)."""
    plan = await market_data_planner.build_plan(session, workspace_id, include_constituents=True)
    diag.market_data_plan_items = plan.summary.total_items
    diag.unresolved_constituent_identities = plan.summary.unresolved_constituents
    diag.estimated_market_data_requests = sum(plan.summary.estimated_requests_by_source.values())
    diag.ambiguous_constituent_identities = plan.summary.ambiguous_constituents
    diag.constituents_ready_for_eod_prices = plan.summary.constituents_ready_for_eod_prices
    # Imported (broker CSV) directly-held instrument price backlog (from the plan).
    diag.missing_imported_instrument_prices = plan.summary.imported_ready_for_prices
    # Constituents skipped this resolution cycle for lack of source budget.
    diag.budget_blocked_constituent_resolution = sum(
        1
        for item in plan.items
        if item.blocked_by == "budget" and item.item_type == "resolve_constituent_identity"
    )

    # --- constituent EOD price coverage ---
    summary = plan.summary
    diag.missing_constituent_prices = summary.constituent_prices_missing
    diag.stale_constituent_prices = summary.constituent_prices_stale
    diag.budget_blocked_constituent_price_fetches = sum(
        1
        for item in plan.items
        if item.blocked_by == "budget" and item.item_type == "fetch_constituent_price"
    )
    priced = (
        summary.constituent_prices_fresh
        + summary.constituent_prices_missing
        + summary.constituent_prices_stale
    )
    if priced:
        diag.constituent_price_coverage = (
            Decimal(summary.constituent_prices_fresh) / Decimal(priced)
        ).quantize(Decimal("0.0001"))


async def _apply_onboarding_diagnostics(
    session: AsyncSession, diag: Diagnostics, *, workspace_id: int | None
) -> None:
    """Bounded onboarding rollup: last failed stage (global) + per-workspace plan.

    Reuses the market-data plan via ``build_onboarding_plan`` for one workspace;
    the global view only inspects the latest onboarding run + cheap counts (no
    per-workspace work). The failed stage comes from the run's *structured*
    payload (``failed_stage_from_run``), not the free-text message. Never does
    per-instrument computation."""
    last = await onboarding_service.latest_onboarding_run(session)
    if last is not None and last.status in ("failed", "partial_success"):
        diag.onboarding_last_failed_stage = onboarding_service.failed_stage_from_run(last)

    # Cheap run-history health counts (bounded aggregates over parent runs).
    diag.onboarding_recent_failures = (
        await session.scalar(
            select(func.count())
            .select_from(JobRun)
            .where(
                JobRun.job_type == onboarding_service.ONBOARDING_JOB,
                JobRun.status.in_(["failed", "partial_success"]),
            )
        )
    ) or 0
    # Scoped (workspace/fund) onboarding runs that predate the structured payload.
    diag.onboarding_legacy_runs_without_stage_metadata = (
        await session.scalar(
            select(func.count())
            .select_from(JobRun)
            .where(
                JobRun.job_type == onboarding_service.ONBOARDING_JOB,
                JobRun.payload_json.is_(None),
                or_(JobRun.workspace_id.is_not(None), JobRun.fund_id.is_not(None)),
            )
        )
    ) or 0

    if workspace_id is not None:
        plan = await onboarding_service.build_onboarding_plan(session, workspace_id=workspace_id)
        diag.onboarding_blocked_stages = sum(1 for st in plan.stages if st.status == "blocked")
        diag.onboarding_needed_stages = sum(1 for st in plan.stages if st.status == "needed")
        diag.onboarding_ready_workspaces = 1 if plan.status == "ready" else 0
        diag.onboarding_source_budget_blocked = (
            diag.budget_blocked_constituent_resolution
            + diag.budget_blocked_constituent_price_fetches
        )


async def _apply_broker_import_diagnostics(
    session: AsyncSession, diag: Diagnostics, *, workspace_id: int | None
) -> None:
    """Broker-import / transaction-ledger health (bounded counts).

    Workspace-scoped when ``workspace_id`` is given, else global. Pure counts —
    never re-parses a CSV or re-runs reconciliation."""

    def _scope(stmt, column):  # type: ignore[no-untyped-def]
        return stmt.where(column == workspace_id) if workspace_id is not None else stmt

    diag.broker_imports = (
        await session.scalar(
            _scope(
                select(func.count())
                .select_from(BrokerImport)
                .where(BrokerImport.status.in_(["committed", "partial"])),
                BrokerImport.workspace_id,
            )
        )
    ) or 0
    diag.broker_imports_with_errors = (
        await session.scalar(
            _scope(
                select(func.count()).select_from(BrokerImport).where(BrokerImport.error_count > 0),
                BrokerImport.workspace_id,
            )
        )
    ) or 0
    failed_rows_stmt = (
        select(func.count())
        .select_from(BrokerImportRow)
        .join(BrokerImport, BrokerImportRow.broker_import_id == BrokerImport.id)
        .where(BrokerImportRow.parse_status == "failed")
    )
    diag.broker_import_failed_rows = (
        await session.scalar(_scope(failed_rows_stmt, BrokerImport.workspace_id))
    ) or 0
    diag.portfolio_transactions = (
        await session.scalar(
            _scope(
                select(func.count()).select_from(PortfolioTransaction),
                PortfolioTransaction.workspace_id,
            )
        )
    ) or 0
    diag.unresolved_import_transactions = (
        await session.scalar(
            _scope(
                select(func.count())
                .select_from(PortfolioTransaction)
                .where(PortfolioTransaction.status == "unresolved_instrument"),
                PortfolioTransaction.workspace_id,
            )
        )
    ) or 0
    diag.ambiguous_import_transactions = (
        await session.scalar(
            _scope(
                select(func.count())
                .select_from(PortfolioTransaction)
                .where(PortfolioTransaction.status == "ambiguous_instrument"),
                PortfolioTransaction.workspace_id,
            )
        )
    ) or 0
    # Resolved imported transactions now linked to a priceable instrument listing.
    diag.imported_instruments_ready_for_prices = (
        await session.scalar(
            _scope(
                select(func.count(func.distinct(PortfolioTransaction.instrument_listing_id)))
                .select_from(PortfolioTransaction)
                .where(
                    PortfolioTransaction.status.in_(["resolved", "ready"]),
                    PortfolioTransaction.instrument_listing_id.is_not(None),
                ),
                PortfolioTransaction.workspace_id,
            )
        )
    ) or 0
    # Failed imported_instrument_resolution runs (shared job-queue infrastructure).
    diag.imported_instrument_resolution_failures = (
        await session.scalar(
            select(func.count())
            .select_from(JobRun)
            .where(
                JobRun.job_type == "imported_instrument_resolution",
                JobRun.status == "failed",
            )
        )
    ) or 0

    # --- manual correction state (surfaced, never hidden) ---------------------
    diag.manual_review_transactions = (
        await session.scalar(
            _scope(
                select(func.count())
                .select_from(PortfolioTransaction)
                .where(PortfolioTransaction.status == "manual_review"),
                PortfolioTransaction.workspace_id,
            )
        )
    ) or 0
    diag.ignored_import_transactions = (
        await session.scalar(
            _scope(
                select(func.count())
                .select_from(PortfolioTransaction)
                .where(PortfolioTransaction.status == "ignored"),
                PortfolioTransaction.workspace_id,
            )
        )
    ) or 0
    # Manually-linked rows: bounded scan of resolved transactions carrying
    # manual-correction provenance (portable across SQLite/Postgres — no JSON-path
    # SQL). Bounded by _MAX_CORRECTION_SCAN so the count stays cheap.
    payloads = (
        (
            await session.execute(
                _scope(
                    select(PortfolioTransaction.raw_payload_json)
                    .where(
                        PortfolioTransaction.status == "resolved",
                        PortfolioTransaction.raw_payload_json.is_not(None),
                    )
                    .limit(_MAX_CORRECTION_SCAN),
                    PortfolioTransaction.workspace_id,
                )
            )
        )
        .scalars()
        .all()
    )
    diag.manual_linked_transactions = sum(
        1
        for payload in payloads
        if isinstance(payload, dict)
        and isinstance(payload.get("manual_correction"), dict)
        and payload["manual_correction"].get("action") == "manual_link"
    )

    latest_import = await session.scalar(
        _scope(
            select(BrokerImport).order_by(BrokerImport.id.desc()).limit(1),
            BrokerImport.workspace_id,
        )
    )
    if latest_import is not None:
        diag.latest_broker_import_status = latest_import.status

    # Reconciliation coverage: workspaces with committed transactions but no /
    # a stale position snapshot. Bounded (workspace set is small).
    txn_ws = set(
        (
            await session.execute(
                _scope(
                    select(PortfolioTransaction.workspace_id).distinct(),
                    PortfolioTransaction.workspace_id,
                )
            )
        ).scalars()
    )
    for wid in txn_ws:
        snapshot = await session.scalar(
            select(PortfolioPositionSnapshot)
            .where(PortfolioPositionSnapshot.workspace_id == wid)
            .order_by(
                PortfolioPositionSnapshot.as_of_date.desc(),
                PortfolioPositionSnapshot.id.desc(),
            )
            .limit(1)
        )
        if snapshot is None:
            diag.missing_portfolio_positions += 1
        elif freshness_service.freshness_state(snapshot.created_at) == freshness_service.STALE:
            diag.stale_portfolio_positions += 1


async def _apply_portfolio_valuation_diagnostics(
    session: AsyncSession, diag: Diagnostics, *, workspace_id: int | None
) -> None:
    """Portfolio valuation/readiness coverage from the latest valuation snapshot.

    Workspace-scoped when ``workspace_id`` is given (one snapshot), else summed
    across every workspace's latest snapshot. Pure bounded reads — never recomputes
    valuation, never fetches, never computes PnL. ``valuation_failures`` counts
    failed ``portfolio_valuation_recompute`` runs (shared job-queue infrastructure)."""
    now = datetime.now(UTC)
    diag.portfolio_valuation_failures = (
        await session.scalar(
            select(func.count())
            .select_from(JobRun)
            .where(JobRun.job_type == "portfolio_valuation_recompute", JobRun.status == "failed")
        )
    ) or 0

    # History series length (snapshot count) in scope — workspace-scoped or global.
    hist_stmt = select(func.count()).select_from(PortfolioValuationSnapshot)
    if workspace_id is not None:
        hist_stmt = hist_stmt.where(PortfolioValuationSnapshot.workspace_id == workspace_id)
    diag.portfolio_valuation_history_points = (await session.scalar(hist_stmt)) or 0

    if workspace_id is not None:
        workspace_ids = [workspace_id]
    else:
        workspace_ids = list((await session.execute(select(Workspace.id))).scalars().all())

    latest_at: datetime | None = None
    for wid in workspace_ids:
        snapshot = await session.scalar(
            select(PortfolioValuationSnapshot)
            .where(PortfolioValuationSnapshot.workspace_id == wid)
            .order_by(
                PortfolioValuationSnapshot.as_of_date.desc(),
                PortfolioValuationSnapshot.id.desc(),
            )
            .limit(1)
        )
        if snapshot is None:
            continue
        diag.portfolio_positions += snapshot.positions_selected
        diag.portfolio_positions_valued += snapshot.positions_valued
        diag.portfolio_positions_missing_price += snapshot.missing_price_count
        diag.portfolio_positions_missing_fx += snapshot.missing_fx_count
        diag.portfolio_positions_unresolved += snapshot.unresolved_count
        diag.portfolio_positions_ambiguous += snapshot.ambiguous_count
        created = snapshot.created_at
        if created.tzinfo is None:
            created = created.replace(tzinfo=UTC)
        if (now - created).days > alert_rules.EXPOSURE_STALE_DAYS:
            diag.portfolio_valuation_snapshot_stale += 1
        if latest_at is None or created > latest_at:
            latest_at = created
        # Single-workspace view: surface the latest snapshot's coverage/readiness
        # (None for the global aggregate, like unclassified_exposure_weight).
        if workspace_id is not None:
            diag.portfolio_valuation_readiness_status = (
                portfolio_valuation_service.snapshot_readiness_status(snapshot)
            )
            diag.portfolio_valuation_latest_coverage_ratio = (
                portfolio_valuation_service.snapshot_coverage_ratio(snapshot)
            )
    diag.latest_portfolio_valuation_snapshot_at = latest_at


async def _apply_reference_rate_diagnostics(session: AsyncSession, diag: Diagnostics) -> None:
    """Official/reference-rate coverage (shared reference data; always global).

    Pure bounded counts — never builds a curve or evaluates curve health. Coverage
    is measured against the currencies the service intends to collect official
    rates for (``SUPPORTED_RATE_CURRENCIES``): a currency with no observation is
    ``missing``; one whose newest observation has aged past the reference-rate
    freshness window is ``stale``."""
    from app.sources.rates import SUPPORTED_RATE_CURRENCIES

    diag.reference_rates = (
        await session.scalar(select(func.count()).select_from(ReferenceRate))
    ) or 0
    diag.latest_reference_rate_date = await session.scalar(
        select(func.max(ReferenceRate.rate_date))
    )
    diag.rates_ingestion_failures = (
        await session.scalar(
            select(func.count())
            .select_from(JobRun)
            .where(JobRun.job_type == "rates_ingestion", JobRun.status == "failed")
        )
    ) or 0

    for currency in SUPPORTED_RATE_CURRENCIES:
        latest = await session.scalar(
            select(func.max(ReferenceRate.rate_date)).where(ReferenceRate.currency == currency)
        )
        if latest is None:
            diag.missing_reference_rates += 1
        elif (
            freshness_service.freshness_state(latest, kind="reference_rate")
            == freshness_service.STALE
        ):
            diag.stale_reference_rates += 1


# Active scheduled-job types that drive a configured ingestion source (so the readiness
# diagnostics can classify each as live vs fixture by its configured default source).
_SOURCE_DRIVEN_SCHEDULED_JOBS = {
    "price_ingestion",
    "fx_ingestion",
    "issuer_facts_ingestion",
    "distribution_ingestion",
    "issuer_holdings_ingestion",
    "document_snapshot_ingestion",
    "constituent_identity_resolution",
    "constituent_eod_price_ingestion",
    "instrument_eod_price_ingestion",
    "rates_ingestion",
    "imported_instrument_resolution",
}


async def _apply_source_readiness_diagnostics(session: AsyncSession, diag: Diagnostics) -> None:
    """Production data-source readiness counts (shared source infra; always global).

    The matrix-derived counts are pure + deterministic (no DB). The scheduled-job split is a
    bounded scan of active ``scheduled_jobs`` classified by their configured default source.
    ``live_source_failures`` is a bounded distinct count over the recent fetch log;
    ``stale_live_data_types`` is read off the (already-computed) stale coverage fields, so
    this must run AFTER the freshness/coverage passes."""
    from app.services import capabilities as capabilities_service
    from app.sources import source_readiness as readiness

    summary = readiness.summary()
    diag.verified_live_sources = summary.verified_live_count
    diag.candidate_live_sources = summary.candidate_count
    diag.planned_live_sources = summary.planned_count
    diag.scheduler_safe_sources = summary.scheduler_safe_count
    diag.missing_required_live_sources = len(summary.missing_required_live_sources)

    # Classify active, source-driven scheduled jobs by their configured default source: a
    # fixture default scheduled in production must be visible (never mistaken for live).
    rows = (
        (
            await session.execute(
                select(ScheduledJob.job_type).where(
                    ScheduledJob.is_active.is_(True),
                    ScheduledJob.schedule_kind != "manual",
                    ScheduledJob.job_type.in_(_SOURCE_DRIVEN_SCHEDULED_JOBS),
                )
            )
        )
        .scalars()
        .all()
    )
    for job_type in rows:
        source_name = capabilities_service.configured_source(job_type)
        row = readiness.get_row(source_name) if source_name else None
        if row is not None and row.status == readiness.FIXTURE:
            diag.fixture_scheduled_jobs += 1
        elif row is not None:
            diag.scheduled_live_jobs += 1
        elif source_name and source_name.endswith("_fixture"):
            # A fixture default with no readiness row (e.g. issuer_fixture) is still fixture.
            diag.fixture_scheduled_jobs += 1

    # Distinct live sources with a recent failed/rate-limited fetch (bounded).
    recent_cutoff = datetime.now(UTC) - timedelta(seconds=_RECENT_FETCH_SECONDS)
    diag.live_source_failures = (
        await session.scalar(
            select(func.count(func.distinct(SourceFetchLog.source_name))).where(
                SourceFetchLog.status.in_(["failed", "rate_limited"]),
                SourceFetchLog.started_at >= recent_cutoff,
            )
        )
    ) or 0

    # Stale live data types: read off the already-computed stale coverage counts (cheap).
    stale_by_data_type = {
        "prices": diag.stale,
        "fx_rates": diag.stale_fx_rates,
        "holdings": diag.stale_holdings,
        "distributions": diag.stale_distributions,
        "reference_rates": diag.stale_reference_rates,
    }
    diag.stale_live_data_types = sum(1 for count in stale_by_data_type.values() if count > 0)


def _apply_fund_coverage_diagnostics(diag: Diagnostics) -> None:
    """Target-fund (VUSA/ISF/JEPG) live-coverage counts (pure; no DB, shared/global).

    Read straight off the in-code fund coverage matrix (cheap + deterministic), so a
    fixture-fed data type is never mistaken for live coverage."""
    from app.sources import fund_source_coverage as fund_coverage

    s = fund_coverage.summary()
    diag.target_funds_total = s.target_funds_total
    diag.target_funds_with_live_price = s.target_funds_with_live_price
    diag.target_funds_with_live_holdings = s.target_funds_with_live_holdings
    diag.target_funds_with_live_distributions = s.target_funds_with_live_distributions
    diag.target_funds_with_live_facts = s.target_funds_with_live_facts
    diag.target_funds_with_live_documents = s.target_funds_with_live_documents
    diag.fund_sources_verified_live = s.fund_sources_verified_live
    diag.fund_sources_candidate = s.fund_sources_candidate
    diag.fund_sources_fixture_only = s.fund_sources_fixture_only
    diag.fund_source_blockers = s.fund_source_blockers


async def global_diagnostics(session: AsyncSession) -> Diagnostics:
    diag = await _compute(
        session,
        fund_ids=None,
        listing_ids=None,
        base_currency=get_settings().base_currency,
        position_workspace_id=None,
    )
    await _apply_alert_counts(session, diag, workspace_id=None)
    await _apply_exposure_diagnostics(session, diag, workspace_id=None)
    await _apply_operational_diagnostics(session, diag)
    await _apply_onboarding_diagnostics(session, diag, workspace_id=None)
    await _apply_broker_import_diagnostics(session, diag, workspace_id=None)
    await _apply_portfolio_valuation_diagnostics(session, diag, workspace_id=None)
    await _apply_reference_rate_diagnostics(session, diag)
    await _apply_source_readiness_diagnostics(session, diag)
    _apply_fund_coverage_diagnostics(diag)
    return diag


async def workspace_diagnostics(session: AsyncSession, workspace_id: int) -> WorkspaceDiagnostics:
    workspace = await workspaces_service.get_workspace(session, workspace_id)
    listing_ids = await _scope_listing_ids(session, workspace_id)
    base = await _compute(
        session,
        fund_ids=None,
        listing_ids=listing_ids,
        base_currency=workspace.base_currency,
        position_workspace_id=workspace_id,
    )
    diag = WorkspaceDiagnostics(workspace_id=workspace_id, **base.model_dump())
    await _apply_alert_counts(session, diag, workspace_id=workspace_id)
    await _apply_exposure_diagnostics(session, diag, workspace_id=workspace_id)
    await _apply_operational_diagnostics(session, diag)
    await _apply_market_data_diagnostics(session, diag, workspace_id=workspace_id)
    await _apply_onboarding_diagnostics(session, diag, workspace_id=workspace_id)
    await _apply_broker_import_diagnostics(session, diag, workspace_id=workspace_id)
    await _apply_portfolio_valuation_diagnostics(session, diag, workspace_id=workspace_id)
    await _apply_reference_rate_diagnostics(session, diag)
    await _apply_source_readiness_diagnostics(session, diag)
    _apply_fund_coverage_diagnostics(diag)
    return diag
