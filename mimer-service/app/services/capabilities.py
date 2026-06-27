"""Service capability discovery — what is real vs fixture vs stub vs planned.

A single source of truth for the *implementation status* of each ingestion
worker / feature, consumed by:
  * the jobs endpoints (per-job `implementation_status` + `configured_source`);
  * `GET /api/v1/capabilities` (whole-service introspection for clients).

`MIGRATION_HEAD` is asserted equal to the real Alembic head by a test, so it
stays correct without importing Alembic at request time.
"""

from __future__ import annotations

from app.core.config import Settings, get_settings
from app.schemas.capability import (
    CapabilitiesEnvironment,
    CapabilitiesResponse,
    DataTypeStatus,
    SourceCapabilityRead,
)
from app.schemas.common import Meta
from app.schemas.source_readiness import (
    SourceReadinessMatrix,
    SourceReadinessRead,
    SourceReadinessSummary,
)
from app.sources import issuer_source_config, registry
from app.sources import source_readiness as readiness

# Issuer data types whose live adapters are explicit-only (need a known config URL
# or --url to run a live fetch).
_URL_DRIVEN_DATA_TYPES = (
    issuer_source_config.DATA_TYPE_HOLDINGS,
    issuer_source_config.DATA_TYPE_DISTRIBUTIONS,
)
# Best-to-worst config status ranking for the per-source summary.
_STATUS_RANK = {
    issuer_source_config.VERIFIED: 0,
    issuer_source_config.CANDIDATE: 1,
    issuer_source_config.PLANNED: 2,
    issuer_source_config.DISABLED: 3,
}

# Bump in lock-step with the latest Alembic revision (guarded by a test).
MIGRATION_HEAD = "0019"

SERVICE_NAME = "etf-data-service"

# Implementation status per job type / feature.
#   real    — provider-agnostic worker with a live-capable adapter.
#   fixture — real worker, but the shipped adapter is offline/fixture.
#   stub    — recognised job type that only records a success_stub run.
#   planned — named in the roadmap; no worker yet.
REAL = "real"
FIXTURE = "fixture"
STUB = "stub"
PLANNED = "planned"
# Real feature whose result is intentionally approximate (e.g. best-effort
# correlation). Honest signal to clients that the data is not exact.
PARTIAL = "partial"
# A capability deliberately NOT offered (a non-goal), distinct from ``planned``
# (which implies it is coming). Honest signal that it is never attempted by design.
UNSUPPORTED = "unsupported"

_WORKER_STATUS: dict[str, str] = {
    "price_ingestion": REAL,
    "issuer_facts_ingestion": FIXTURE,
    # Real provider-agnostic worker: collects issuer-published distributions into the
    # distributions table with source/provenance/freshness. Offline distribution_fixture
    # default; jpmorgan_distributions + vanguard_distributions are implemented live
    # (explicit-only, behind guarded_fetch + source budgets), vanguard_distributions_export
    # is an offline exported-file parser, blackrock_ishares_distributions stays planned.
    # Collection only — no dividend forecasting / yield projection / tax treatment / total
    # return / PnL (those live in the Rust local pricer). See
    # app/services/distributions_ingestion.py + app/sources/distributions.py.
    "distribution_ingestion": FIXTURE,
    # Real provider-agnostic worker: collects issuer-published ETF holdings into
    # fund_holdings with source/provenance/freshness. Offline holdings_fixture default;
    # blackrock_ishares_holdings + jpmorgan_etf_holdings are implemented live
    # (explicit-only, behind guarded_fetch + source budgets), vanguard_holdings_export
    # is an offline exported-file parser, vanguard_holdings stays planned. Collection
    # only — no look-through analytics/PnL (those live in the Rust local pricer).
    # See app/services/holdings_ingestion.py + app/sources/holdings.py.
    "issuer_holdings_ingestion": FIXTURE,
    "fx_ingestion": FIXTURE,
    "document_snapshot_ingestion": FIXTURE,
    # Real and database-only: derives alerts from existing signals (no provider).
    "alert_generation": REAL,
    # Real and database-only: derives cached look-through exposure (no provider).
    "exposure_recompute": REAL,
    # Real provider-agnostic worker: parses a broker CSV into the canonical
    # transaction ledger + a bounded position reconciliation. Offline by design
    # (no live resolver calls; instruments resolve against existing identity
    # only). See app/services/broker_imports.py + app/sources/broker_imports.py.
    "broker_csv_import": REAL,
    # Real provider-agnostic worker; OpenFIGI optional/configured, offline
    # fixture-backed tests. See app/services/constituent_identity.py.
    "constituent_identity_resolution": FIXTURE,
    # Real provider-agnostic worker; offline fixture default, Stooq/yfinance only
    # when explicitly requested (budget-guarded). See app/services/instrument_prices.py.
    "constituent_eod_price_ingestion": FIXTURE,
    # Unified instrument EOD price worker: prices any resolved instrument_listing
    # (ETF/fund constituents *and* resolved imported direct holdings) through the
    # one instrument_prices path. Same provider adapters/budgets as the constituent
    # worker — offline fixture default, Stooq/yfinance explicit (budget-guarded).
    # See app/services/instrument_prices.py (ingest_instrument_eod_prices).
    "instrument_eod_price_ingestion": FIXTURE,
    # Real provider-agnostic worker: resolves unresolved broker-import transactions
    # to canonical instruments (existing identity first, then the shared
    # constituent resolvers) and relinks them. Offline fixture default; OpenFIGI
    # only when explicitly requested (budget-guarded). Never name-only.
    # See app/services/imported_instrument_resolution.py.
    "imported_instrument_resolution": FIXTURE,
    # Real orchestration worker (database-only; no provider of its own). Coordinates
    # the existing ingestion/recompute workers into a data-readiness pipeline.
    # See app/services/instrument_onboarding.py.
    "instrument_onboarding": REAL,
    # Real provider-agnostic worker: collects + persists official/reference rate
    # *observations* (ECB/BoE policy rates, €STR/SONIA/SOFR/Fed Funds, US Treasury
    # par yields) into reference_rates. Offline rates_fixture default; us_treasury_rates
    # + ecb_rates are implemented live (explicit-only), boe_rates is planned.
    # Collection only — never builds curves/bootstraps/interpolates. See
    # app/services/rates_ingestion.py.
    "rates_ingestion": FIXTURE,
    # Real, database-only worker: recomputes the bounded portfolio valuation/
    # readiness snapshot from already-ingested prices/FX (never fetches, never
    # resolves identity, never computes PnL). See app/services/portfolio_valuation.py.
    "portfolio_valuation_recompute": REAL,
    # Future market-data expansion (planner already accounts for these). Curve
    # construction stays OUT of the backend — it belongs in the Rust local pricer.
    "rates_curve_ingestion": PLANNED,
    "bond_reference_ingestion": PLANNED,
}

# Operational platform features (not job types) surfaced in capabilities. These
# describe the scheduling / fetch-safety layer, all real in this iteration.
_OPERATIONAL_STATUS: dict[str, str] = {
    "scheduler": REAL,
    "job_leasing": REAL,
    "source_rate_budgets": REAL,
    "source_fetch_logs": REAL,
    "market_data_planner": REAL,
    # Derived read features over cached exposure snapshots (no worker/network).
    "true_constituent_lookthrough_valuation": REAL,
    "exposure_drift": REAL,
    "top_holding_performance": REAL,
    # Onboarding run history / stage observability — a bounded read model over
    # the parent instrument_onboarding runs' structured payload (no worker).
    "onboarding_run_history": REAL,
    "onboarding_stage_observability": REAL,
    # Generic job-run timeline / failure drilldown — a bounded read model over
    # all job_runs (timeline, detail, failures) for the GUI Data Operations page.
    "job_run_timeline": REAL,
    "job_run_detail": REAL,
    "job_failure_drilldown": REAL,
    # Source fetch logs are correlated to a run by source + time window (no exact
    # run↔fetch FK yet), so this is intentionally approximate.
    "source_fetch_log_correlation": PARTIAL,
    # Live running/leased scheduled-job read model (the timeline's live counterpart
    # for the GUI Data Operations page). Bounded, read-only — derives running /
    # stuck / expired / due / blocked from the scheduler's lease columns; there is
    # no unlock/kill/force endpoint. See app/services/job_leases.py.
    "running_job_timeline": REAL,
    "job_lease_observability": REAL,
    "stuck_lease_read_model": REAL,
    # Broker CSV import -> canonical transaction ledger -> bounded position
    # reconciliation. Preview is read-only; commit is idempotent (duplicate file
    # / duplicate transaction safe). ``portfolio_position_reconciliation`` is
    # PARTIAL: it reconciles quantities (buys − sells) + cash per currency only —
    # NOT market value / PnL / tax lots / total return (those belong in the Rust
    # GUI / local pricer). See app/services/broker_imports.py.
    "broker_import_preview": REAL,
    "portfolio_transaction_ledger": REAL,
    "portfolio_position_reconciliation": PARTIAL,
    # Imported-instrument resolution bridge: turns unresolved broker-import
    # transactions into the canonical instrument universe (existing identity first,
    # then the shared resolvers) and relinks them, then re-reconciles positions.
    # The planner read model that surfaces the imported resolve/price/FX backlog is
    # real and database-only. See app/services/imported_instrument_resolution.py +
    # app/services/market_data_planner.py.
    "imported_instrument_planner": REAL,
    # Manual correction workflows for unresolved/ambiguous/mis-linked imported
    # transactions: manual-link to existing identity, clear a link, ignore,
    # manual-review, plus a bounded candidate-context read and provenance. Real +
    # database-only — never resolves identity live, never calls OpenFIGI/a provider,
    # never creates an instrument, never name-only guesses a link. Ignored/
    # manual-review state is surfaced (never hidden) in diagnostics + the planner.
    # See app/services/transaction_corrections.py.
    "manual_transaction_corrections": REAL,
    "manual_imported_instrument_linking": REAL,
    # Name-only auto-linking is a deliberate NON-goal: a broker-supplied name is not
    # identity, so it is never auto-resolved/auto-linked (manual review only).
    "automatic_name_only_resolution": UNSUPPORTED,
    # Imported direct holdings are priced through the unified
    # instrument_eod_price_ingestion worker (same instrument_prices path as
    # constituents). Offline fixture default (TSLA/AAPL/... in the fixture
    # universe); Stooq/yfinance explicit + budget-guarded.
    "imported_instrument_prices": FIXTURE,
    # Portfolio valuation/readiness snapshot: a bounded, cacheable read model that
    # values the reconciled positions/cash from already-ingested prices/FX and
    # reports readiness blockers (missing price/FX, unresolved/ambiguous). PARTIAL
    # because coverage depends on how much price/FX/identity has been ingested — it
    # never invents a value. NOT PnL / tax lots / total return / performance
    # attribution (those live in the Rust GUI / local pricer; kept PLANNED below).
    # See app/services/portfolio_valuation.py.
    "portfolio_valuation": PARTIAL,
    "portfolio_valuation_snapshot": REAL,
    # Bounded, snapshot-backed read models over the persisted valuation snapshots:
    # an oldest-first coverage/readiness history series + a compact latest-context
    # summary (/portfolio/valuation/history + /summary), and the dashboard valuation
    # block. Real + database-only — they never recompute valuation, never fetch, and
    # never difference snapshots into a return/PnL (those stay PLANNED below). See
    # app/services/portfolio_valuation.py.
    "portfolio_valuation_history": REAL,
    "portfolio_valuation_dashboard": REAL,
    # Heavy analytics that stay OUT of the backend (Rust GUI / local pricer).
    "portfolio_pnl": PLANNED,
    "tax_lots": PLANNED,
    "total_return": PLANNED,
    "performance_attribution": PLANNED,
}

# Extra non-job features surfaced in the capabilities payload.
_FEATURE_STATUS: dict[str, str] = {
    "identity_resolution": REAL,
    **_WORKER_STATUS,
    **_OPERATIONAL_STATUS,
}

# Data types this service can populate today, and their status.
_DATA_TYPE_STATUS: dict[str, str] = {
    "identity": REAL,
    "prices": REAL,
    "fund_facts": FIXTURE,
    "distributions": FIXTURE,
    "holdings": FIXTURE,
    "fx_rates": FIXTURE,
    "documents": FIXTURE,
    "nav": PLANNED,
    "corporate_actions": PLANNED,
    # Broker CSV import persists canonical transactions (ledger), with a bounded
    # position reconciliation. Not PnL/tax-lots/total-return (see compute boundary).
    "transactions": REAL,
    "option_chain": PLANNED,
    "futures_contracts": PLANNED,
    # Official/reference rate observations (policy/overnight/par-yield), collected
    # + persisted only. Distinct from yield_curves: curves stay PLANNED (and are
    # built in the Rust local pricer, never the backend).
    "reference_rates": FIXTURE,
    "bond_reference": PLANNED,
    "bond_prices": PLANNED,
    "yield_curves": PLANNED,
    # Generic Stooq market series (curve/market context), classification-only this slice —
    # storage deferred. NOT tradable securities (see app/sources/stooq_market_series.py).
    "market_series": PLANNED,
    "sovereign_yield_benchmark_series": PLANNED,
    "sovereign_benchmark_price_series": PLANNED,
    "rates_futures_series": PLANNED,
}

SUPPORTED_ASSET_CLASSES = ["etf", "equity", "fx"]
PLANNED_ASSET_CLASSES = [
    "mutual_fund",
    "index",
    "bond",
    "future",
    "option",
    "cash",
    "commodity",
    "crypto",
]


def worker_status(job_type: str) -> str:
    """Implementation status for a job type (``planned`` if unknown)."""
    return _WORKER_STATUS.get(job_type, PLANNED)


def configured_source(job_type: str, settings: Settings | None = None) -> str | None:
    """The provider adapter a real/fixture worker would use (None otherwise)."""
    settings = settings or get_settings()
    mapping = {
        "price_ingestion": settings.price_source_default,
        "issuer_facts_ingestion": settings.issuer_facts_source_default,
        "distribution_ingestion": settings.distribution_source_default,
        "issuer_holdings_ingestion": settings.holdings_source_default,
        "fx_ingestion": settings.fx_source_default,
        "document_snapshot_ingestion": settings.document_source_default,
        "constituent_identity_resolution": settings.constituent_identity_source_default,
        "constituent_eod_price_ingestion": settings.constituent_price_source_default,
        "instrument_eod_price_ingestion": settings.constituent_price_source_default,
        "broker_csv_import": settings.broker_import_source_default,
        # Reuses the shared constituent resolver (offline fixture default).
        "imported_instrument_resolution": settings.constituent_identity_source_default,
        "rates_ingestion": settings.rates_source_default,
    }
    return mapping.get(job_type)


def _workers_by_status() -> dict[str, list[str]]:
    grouped: dict[str, list[str]] = {REAL: [], FIXTURE: [], STUB: [], PLANNED: []}
    for job_type, status in _WORKER_STATUS.items():
        grouped.setdefault(status, []).append(job_type)
    return grouped


def _source_config_metadata(capability: registry.SourceCapability) -> dict[str, object]:
    """Known issuer source-config awareness for one capability row (live URL-driven).

    ``requires_url`` is True for an *implemented live issuer* holdings/distribution
    adapter (explicit-only; needs a known config URL or --url). The rest summarise
    the per-fund ``issuer_source_config`` registry for this source."""
    is_url_driven_live = (
        capability.source_type == "issuer"
        and capability.supports_live
        and capability.adapter_status == "implemented"
        and any(dt in _URL_DRIVEN_DATA_TYPES for dt in capability.data_types)
    )
    configs = issuer_source_config.configs_for_source(capability.source_name)
    usable = [c for c in configs if c.is_usable]
    best_status: str | None = None
    if configs:
        best_status = min(configs, key=lambda c: _STATUS_RANK.get(c.source_status, 9)).source_status
    return {
        "requires_url": is_url_driven_live,
        "known_config_available": bool(usable),
        "config_status": best_status,
        "example_fund_identifiers": issuer_source_config.example_identifiers(
            capability.source_name
        ),
    }


def _readiness_row(row: readiness.SourceReadinessRow) -> SourceReadinessRead:
    return SourceReadinessRead(
        data_type=row.data_type,
        source_name=row.source_name,
        provider=row.provider,
        status=row.status,
        worker_name=row.worker_name,
        recommended_cadence=row.recommended_cadence,
        default_for_worker=readiness.default_for_worker(row),
        safe_for_scheduler=row.safe_for_scheduler,
        requires_secret=row.requires_secret,
        requires_url_config=row.requires_url_config,
        requires_running_gateway=row.requires_running_gateway,
        last_verified_at=row.last_verified_at,
        known_blockers=row.known_blockers,
        next_action=row.next_action,
        notes=row.notes,
        example_targets=list(row.example_targets),
    )


def build_source_readiness_summary() -> SourceReadinessSummary:
    """Compact readiness rollup (also embedded in the capabilities payload)."""
    s = readiness.summary()
    return SourceReadinessSummary(
        total_sources=s.total_sources,
        status_counts=s.status_counts,
        scheduler_safe_count=s.scheduler_safe_count,
        verified_live_count=s.verified_live_count,
        candidate_count=s.candidate_count,
        planned_count=s.planned_count,
        fixture_count=s.fixture_count,
        required_live_data_types=s.required_live_data_types,
        missing_required_live_sources=s.missing_required_live_sources,
        scheduler_safe_sources=s.scheduler_safe_sources,
    )


def build_source_readiness_matrix() -> SourceReadinessMatrix:
    """The full production data-source readiness matrix + its summary rollup."""
    rows = [_readiness_row(r) for r in readiness.list_rows()]
    return SourceReadinessMatrix(
        data=rows,
        meta=Meta(count=len(rows)),
        summary=build_source_readiness_summary(),
    )


def build_capabilities() -> CapabilitiesResponse:
    from app import __version__

    settings = get_settings()
    environment = CapabilitiesEnvironment(
        base_currency=settings.base_currency,
        resolver_default_provider=settings.resolver_default_provider,
        price_source_default=settings.price_source_default,
        distribution_source_default=settings.distribution_source_default,
        issuer_facts_source_default=settings.issuer_facts_source_default,
        holdings_source_default=settings.holdings_source_default,
        fx_source_default=settings.fx_source_default,
        document_source_default=settings.document_source_default,
        openfigi_api_key_configured=bool(settings.openfigi_api_key),
    )
    configured_sources = {
        "resolver": settings.resolver_default_provider,
        "price": settings.price_source_default,
        "distribution": settings.distribution_source_default,
        "issuer_facts": settings.issuer_facts_source_default,
        "holdings": settings.holdings_source_default,
        "fx": settings.fx_source_default,
        "documents": settings.document_source_default,
        "constituent_identity": settings.constituent_identity_source_default,
        "constituent_price": settings.constituent_price_source_default,
        "reference_rates": settings.rates_source_default,
    }
    sources = [
        SourceCapabilityRead(
            source_name=c.source_name,
            source_type=c.source_type,
            asset_classes=list(c.asset_classes),
            data_types=list(c.data_types),
            reliability_tier=c.reliability_tier,
            requires_api_key=c.requires_api_key,
            supports_history=c.supports_history,
            supports_intraday=c.supports_intraday,
            supports_live=c.supports_live,
            supports_identifiers=c.supports_identifiers,
            adapter_status=c.adapter_status,
            notes=c.notes,
            tags=list(c.tags),
            **_source_config_metadata(c),
        )
        for c in registry.list_capabilities()
    ]
    return CapabilitiesResponse(
        service=SERVICE_NAME,
        version=__version__,
        migration_head=MIGRATION_HEAD,
        environment=environment,
        features=dict(_FEATURE_STATUS),
        workers=_workers_by_status(),
        configured_sources=configured_sources,
        supported_asset_classes=list(SUPPORTED_ASSET_CLASSES),
        planned_asset_classes=list(PLANNED_ASSET_CLASSES),
        data_types=[
            DataTypeStatus(name=name, status=status) for name, status in _DATA_TYPE_STATUS.items()
        ],
        sources=sources,
        source_readiness=build_source_readiness_summary(),
    )
