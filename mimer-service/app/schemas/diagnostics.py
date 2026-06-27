"""Diagnostics / data-quality counts for the GUI.

A first-pass health snapshot computed from existing statuses, sources and job
states — not a validation engine. The same schema backs both the standalone
diagnostics endpoints and the dashboard's ``data_quality`` block.
"""

from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel

from app.schemas.common import DecimalStr


class Diagnostics(BaseModel):
    # Freshness of fact-bearing data (prices, fund facts) in scope.
    fresh: int = 0
    stale: int = 0
    missing: int = 0
    # Lifecycle / ingestion health.
    failed: int = 0
    pending: int = 0
    # Provenance signals.
    mock_or_seed: int = 0
    estimated_or_derived: int = 0
    manual_overrides: int = 0
    source_conflicts: int = 0
    ambiguous_instruments: int = 0
    # Holdings coverage (in scope): funds with no holdings snapshot at all, and
    # funds whose latest snapshot has aged past the holdings freshness window.
    missing_holdings: int = 0
    stale_holdings: int = 0
    # Distribution coverage (in scope): ``distributions`` is the stored row count
    # for scoped funds; ``missing`` / ``stale`` count distributing (or unknown-policy)
    # funds with no distributions / a distribution history aged past the window (an
    # accumulating fund pays nothing, so it is never flagged); ``latest_distribution_
    # date`` is the newest ex-date across scoped funds; ``distribution_ingestion_
    # failures`` counts failed/partial distribution_ingestion runs (global). Collection
    # coverage only — never dividend-forecast / yield-projection health.
    distributions: int = 0
    missing_distributions: int = 0
    stale_distributions: int = 0
    latest_distribution_date: date | None = None
    distribution_ingestion_failures: int = 0
    # FX coverage (in scope): non-base-currency positions whose local currency has
    # no usable rate to base / only a stale rate; positions that cannot be valued
    # in base for lack of FX; and failed/partial fx_ingestion runs (global).
    missing_fx_rates: int = 0
    stale_fx_rates: int = 0
    unconverted_positions: int = 0
    fx_conversion_failures: int = 0
    # Document coverage (in scope): held funds with none of the key document types
    # (factsheet/KID/prospectus); funds whose newest document has aged past the
    # document freshness window; snapshots whose ingestion detected a content
    # change vs the prior version; first-version (newly tracked) snapshots; and
    # failed/partial document_snapshot_ingestion runs (global).
    missing_documents: int = 0
    stale_documents: int = 0
    changed_documents: int = 0
    new_documents: int = 0
    failed_document_jobs: int = 0
    # Job queue health.
    failed_jobs: int = 0
    queued_jobs: int = 0
    # Alert rollup (workspace-scoped alerts; global view sums across workspaces).
    # ``active``/``unread`` count active alerts; the severity/category counts
    # cover *open* (active + read) alerts so the GUI badges stay meaningful.
    active_alerts: int = 0
    unread_alerts: int = 0
    critical_alerts: int = 0
    error_alerts: int = 0
    warning_alerts: int = 0
    document_alerts: int = 0
    price_alerts: int = 0
    fx_alerts: int = 0
    job_alerts: int = 0
    # Derived exposure coverage (workspace-scoped; global view aggregates).
    # ``missing_exposure_snapshots`` counts workspaces that hold positions but
    # have no exposure snapshot; ``stale`` counts snapshots older than the
    # freshness window; ``low_exposure_coverage`` counts snapshots whose
    # look-through coverage is below the minimum. ``unclassified_exposure_weight``
    # is the latest snapshot's unclassified fraction (None for the global view).
    missing_exposure_snapshots: int = 0
    stale_exposure_snapshots: int = 0
    exposure_recompute_failures: int = 0
    low_exposure_coverage: int = 0
    missing_holdings_for_exposure: int = 0
    missing_fx_for_exposure: int = 0
    unclassified_exposure_weight: DecimalStr | None = None
    # Operational readiness (shared scheduler/fetch infrastructure; global).
    # ``due_scheduled_jobs`` = active non-manual jobs past their next_run_at;
    # ``running_jobs`` = JobRuns still marked running; ``stuck_jobs`` = those
    # running well past a sane window; ``expired_job_leases`` = scheduled jobs
    # holding a lease whose ``lock_expires_at`` has passed (reclaimable);
    # ``recent_failed_fetches`` / ``rate_limited_sources`` / ``sources_in_backoff``
    # summarise external-source health from the fetch log + budgets.
    due_scheduled_jobs: int = 0
    running_jobs: int = 0
    stuck_jobs: int = 0
    expired_job_leases: int = 0
    recent_failed_fetches: int = 0
    rate_limited_sources: int = 0
    sources_in_backoff: int = 0
    # Live scheduled-job lease health (shared lease classifier — see
    # app/services/job_leases.py — so these agree with /scheduler/status and the
    # /jobs/running read model). ``running_job_leases`` are healthy active leases;
    # ``stuck_job_leases`` are active but unhealthy (worker watchdog/heartbeat);
    # ``blocked_scheduled_jobs_by_lease`` are jobs whose next_run_at has passed but
    # are held by an active lease (so the scheduler cannot claim them yet).
    # ``expired_job_leases`` (above) and ``due_scheduled_jobs`` (above) complete
    # the picture.
    running_job_leases: int = 0
    stuck_job_leases: int = 0
    blocked_scheduled_jobs_by_lease: int = 0
    # Job-run observability rollup (shared job-queue infrastructure; global). Feeds
    # the GUI Data Operations page alongside the bounded /jobs/timeline read model.
    # ``recent_partial_job_runs`` counts partial_success runs (``failed_jobs``
    # above counts outright failures); ``latest_failed_job_run_*`` name the most
    # recent failed/partial run so the GUI can deep-link straight to its drilldown.
    recent_partial_job_runs: int = 0
    latest_failed_job_run_id: int | None = None
    latest_failed_job_run_type: str | None = None
    # Market-data planning readiness (workspace-scoped; 0 for the global view).
    market_data_plan_items: int = 0
    unresolved_constituent_identities: int = 0
    estimated_market_data_requests: int = 0
    # Constituent identity-resolution health (workspace-scoped; global counts the
    # failures, which are shared job-queue infrastructure). ``ambiguous`` /
    # ``failures`` / ``budget_blocked`` flag constituents needing attention;
    # ``ready_for_eod_prices`` is the next ingestion phase's backlog.
    ambiguous_constituent_identities: int = 0
    constituent_identity_resolution_failures: int = 0
    budget_blocked_constituent_resolution: int = 0
    constituents_ready_for_eod_prices: int = 0
    # Constituent EOD price coverage (workspace-scoped; global counts the failures,
    # which are shared job-queue infrastructure). ``missing`` / ``stale`` flag
    # resolved listings needing a (re)fetch; ``coverage`` is the fraction of
    # resolved listings with a fresh price (None for the global view);
    # ``ingestion_failures`` counts failed constituent_eod_price_ingestion runs;
    # ``budget_blocked`` flags fetches skipped this cycle for lack of source budget.
    missing_constituent_prices: int = 0
    stale_constituent_prices: int = 0
    constituent_price_ingestion_failures: int = 0
    budget_blocked_constituent_price_fetches: int = 0
    constituent_price_coverage: DecimalStr | None = None
    # True constituent look-through valuation coverage (workspace-scoped; derived
    # from the latest exposure snapshot's weight-based coverage). ``low_*`` count
    # the workspace (0/1; summed across the global view) when a *meaningful* but
    # below-threshold coverage is observed — never the clean pre-resolution state.
    # ``fx_missing`` counts distinct priced constituents lacking an FX path;
    # ``unclassified_weight`` is the latest snapshot's not-looked-through fraction.
    low_constituent_identity_coverage: int = 0
    low_constituent_price_coverage: int = 0
    constituent_valuation_fx_missing: int = 0
    constituent_valuation_unclassified_weight: DecimalStr | None = None
    # Exposure drift (latest vs previous snapshot; workspace-scoped, global sums).
    # ``large_*_exposure_drift`` count workspaces whose looked-through weight moved
    # past the drift threshold for that dimension; ``*_coverage_deteriorated``
    # count workspaces whose constituent price/FX coverage dropped materially;
    # ``no_prior_exposure_snapshot_for_drift`` counts workspaces with positions and
    # exactly one snapshot (nothing to compare yet).
    large_constituent_exposure_drift: int = 0
    large_sector_exposure_drift: int = 0
    large_currency_exposure_drift: int = 0
    price_coverage_deteriorated: int = 0
    fx_coverage_deteriorated: int = 0
    no_prior_exposure_snapshot_for_drift: int = 0
    # Top-holding price-context performance data-quality (workspace-scoped; global
    # sums). Conservative + data-quality oriented — these flag *why a contribution
    # view is incomplete*, never that a constituent moved. ``*_missing_prices`` /
    # ``*_fx_missing`` count workspaces whose top-holding performance has missing
    # base/comparison prices / non-base constituents without FX context;
    # ``*_insufficient_history`` counts workspaces lacking a second snapshot.
    top_holding_performance_missing_prices: int = 0
    top_holding_performance_fx_missing: int = 0
    top_holding_performance_insufficient_history: int = 0
    # Instrument-onboarding / data-readiness rollup (bounded; reuses the
    # market-data plan + the latest onboarding job_run — never per-instrument
    # work). ``blocked``/``needed`` count the workspace's onboarding stages in
    # those states (0 for the global view); ``ready_workspaces`` counts
    # data-ready workspaces; ``source_budget_blocked`` reuses the budget-blocked
    # constituent counts; ``last_failed_stage`` names the most recent onboarding
    # run's failed stage (None if the last run was clean).
    onboarding_blocked_stages: int = 0
    onboarding_needed_stages: int = 0
    onboarding_ready_workspaces: int = 0
    onboarding_source_budget_blocked: int = 0
    onboarding_last_failed_stage: str | None = None
    # Onboarding run-history health (bounded counts over the parent
    # ``instrument_onboarding`` runs). ``recent_failures`` counts failed/partial
    # runs; ``legacy_runs_without_stage_metadata`` counts scoped runs predating
    # the structured payload (pre-0015) so the GUI knows which can't be expanded
    # into typed stages.
    onboarding_recent_failures: int = 0
    onboarding_legacy_runs_without_stage_metadata: int = 0
    # Broker CSV import / transaction-ledger health (workspace-scoped; the global
    # view counts globally / sums). ``broker_imports`` counts committed imports;
    # ``broker_import_failed_rows`` counts CSV rows that failed to parse (isolated
    # during import, never crashing it); ``broker_imports_with_errors`` counts
    # imports that had any failed row; ``unresolved_import_transactions`` counts
    # committed transactions left ``unresolved_instrument`` (stored with symbol/
    # ISIN, never a name-only guess); ``portfolio_transactions`` is the ledger
    # size; ``missing_portfolio_positions`` / ``stale_portfolio_positions`` flag
    # workspaces with committed transactions but no / a stale reconciliation
    # snapshot; ``latest_broker_import_status`` names the most recent import.
    broker_imports: int = 0
    broker_import_failed_rows: int = 0
    broker_imports_with_errors: int = 0
    unresolved_import_transactions: int = 0
    portfolio_transactions: int = 0
    missing_portfolio_positions: int = 0
    stale_portfolio_positions: int = 0
    latest_broker_import_status: str | None = None
    # Imported-instrument resolution bridge (workspace-scoped; global counts the
    # failures, which are shared job-queue infrastructure). ``ambiguous_import_
    # transactions`` resolved ambiguously and need manual review;
    # ``imported_instruments_ready_for_prices`` are resolved imported transactions
    # now linked to a priceable listing; ``missing_imported_instrument_prices`` is
    # the price-fetch backlog for those listings (workspace-scoped, from the plan);
    # ``imported_instrument_resolution_failures`` counts failed resolution runs.
    ambiguous_import_transactions: int = 0
    imported_instruments_ready_for_prices: int = 0
    missing_imported_instrument_prices: int = 0
    imported_instrument_resolution_failures: int = 0
    # Manual correction state (workspace-scoped; the global view sums/counts
    # globally). ``manual_review_transactions`` are rows a human parked for review
    # (still in the ledger, flagged); ``ignored_import_transactions`` are rows a
    # human excluded from the portfolio (dropped from reconciliation/valuation but
    # kept auditable); ``manual_linked_transactions`` are rows currently linked by an
    # explicit manual correction (existing-identity only — never created/guessed).
    # Surfaced (never hidden) so an operator can see the correction backlog/history.
    manual_review_transactions: int = 0
    ignored_import_transactions: int = 0
    manual_linked_transactions: int = 0
    # Failed unified instrument_eod_price_ingestion runs (shared job-queue
    # infrastructure; the worker that prices both constituents and resolved
    # imported direct holdings into ``instrument_prices``).
    instrument_price_ingestion_failures: int = 0
    # Official / reference-rate coverage (shared reference data; same counts in the
    # global + workspace views). ``reference_rates`` is the total stored observation
    # count; ``missing`` / ``stale`` count the supported currencies (EUR/GBP/USD)
    # with no / only stale official rates; ``latest_reference_rate_date`` is the
    # newest observation date across all series; ``rates_ingestion_failures`` counts
    # failed rates_ingestion runs. Collection coverage only — never curve health.
    reference_rates: int = 0
    missing_reference_rates: int = 0
    stale_reference_rates: int = 0
    latest_reference_rate_date: date | None = None
    rates_ingestion_failures: int = 0
    # Known issuer source-config coverage (live holdings/distribution download URLs).
    # ``issuer_source_configs`` is the total registered config count (global, in-code
    # registry); ``verified`` / ``candidate`` split it by status. ``missing_holdings_
    # source_config`` / ``missing_distribution_source_config`` count scoped funds with
    # NO usable live config for that data type — informational coverage only (the
    # offline fixture still works; this is NOT an error and raises no alerts).
    issuer_source_configs: int = 0
    verified_issuer_source_configs: int = 0
    candidate_issuer_source_configs: int = 0
    missing_holdings_source_config: int = 0
    missing_distribution_source_config: int = 0
    # Production data-source readiness rollup (the operational honesty layer — see
    # app/sources/source_readiness.py). The ``*_live_sources`` / ``scheduler_safe_sources``
    # counts come from the in-code readiness matrix (cheap, deterministic): how many sources
    # are live-verified / candidate / planned, and how many are safe to schedule.
    # ``scheduled_live_jobs`` / ``fixture_scheduled_jobs`` classify the *active* scheduled
    # jobs by whether their configured default source is live or an offline fixture (so a
    # fixture default scheduled in production is never mistaken for live readiness).
    # ``missing_required_live_sources`` counts required data types (rates/fx/prices/holdings)
    # with no verified-live source yet; ``live_source_failures`` counts distinct live sources
    # with a recent failed/rate-limited fetch; ``stale_live_data_types`` counts in-scope data
    # types whose coverage is stale. Informational readiness — never an alert.
    verified_live_sources: int = 0
    candidate_live_sources: int = 0
    planned_live_sources: int = 0
    scheduler_safe_sources: int = 0
    missing_required_live_sources: int = 0
    scheduled_live_jobs: int = 0
    fixture_scheduled_jobs: int = 0
    live_source_failures: int = 0
    stale_live_data_types: int = 0
    # Portfolio valuation/readiness coverage (workspace-scoped from the latest
    # valuation snapshot; the global view sums across workspaces). These are a
    # bounded read model over already-ingested prices/FX — NOT PnL. ``positions`` is
    # the snapshot's selected (non-cash) position count; ``valued`` /
    # ``missing_price`` / ``missing_fx`` / ``unresolved`` / ``ambiguous`` split it by
    # readiness; ``snapshot_stale`` flags a valuation snapshot aged past the freshness
    # window; ``latest_*_at`` is the newest snapshot's timestamp;
    # ``valuation_failures`` counts failed portfolio_valuation_recompute runs (global
    # job-queue infrastructure).
    portfolio_positions: int = 0
    portfolio_positions_valued: int = 0
    portfolio_positions_missing_price: int = 0
    portfolio_positions_missing_fx: int = 0
    portfolio_positions_unresolved: int = 0
    portfolio_positions_ambiguous: int = 0
    portfolio_valuation_snapshot_stale: int = 0
    latest_portfolio_valuation_snapshot_at: datetime | None = None
    portfolio_valuation_failures: int = 0
    # Valuation history/readiness read-model rollup. ``history_points`` counts the
    # snapshots in scope (the bounded series length available); ``latest_coverage_
    # ratio`` / ``readiness_status`` summarise the newest snapshot (workspace view
    # only — None for the global view, mirroring ``unclassified_exposure_weight``).
    # Coverage/readiness only — never returns/PnL/performance.
    portfolio_valuation_history_points: int = 0
    portfolio_valuation_latest_coverage_ratio: DecimalStr | None = None
    portfolio_valuation_readiness_status: str | None = None


class WorkspaceDiagnostics(Diagnostics):
    workspace_id: int
