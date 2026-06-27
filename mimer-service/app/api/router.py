"""Aggregate router mounted under /api/v1."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api.security import require_api_token
from app.api.v1 import (
    alerts,
    broker_imports,
    capabilities,
    dashboard,
    diagnostics,
    distributions,
    documents,
    exposure,
    funds,
    fx,
    fxrates,
    hierarchy,
    holdings,
    instrument_listings,
    instruments,
    jobs,
    market_data,
    onboarding,
    portfolio,
    portfolio_valuation,
    rates,
    scheduler,
    source_budgets,
    source_fetch_logs,
    sources,
    timeseries,
    workspaces,
)

# Optional shared Bearer-token auth applied to EVERY /api/v1 route (including the
# routers included below). A no-op when ``MIMER_API_TOKEN`` is unset; otherwise a
# missing/wrong token is 401. The health router is mounted separately in
# ``app.main`` and is intentionally NOT behind this dependency.
api_router = APIRouter(prefix="/api/v1", dependencies=[Depends(require_api_token)])

# Workspace / user.
api_router.include_router(workspaces.router)

# Workspace-scoped private data (canonical).
api_router.include_router(portfolio.workspace_router)
api_router.include_router(broker_imports.workspace_router)
api_router.include_router(portfolio_valuation.workspace_router)
api_router.include_router(exposure.workspace_router)
api_router.include_router(dashboard.workspace_router)
api_router.include_router(diagnostics.workspace_router)
api_router.include_router(hierarchy.workspace_router)
api_router.include_router(alerts.workspace_router)
api_router.include_router(timeseries.portfolio_router)

# Shared / reference data.
api_router.include_router(funds.router)
api_router.include_router(timeseries.fund_router)
api_router.include_router(timeseries.listing_router)
api_router.include_router(distributions.router)
api_router.include_router(holdings.router)
api_router.include_router(documents.router)
api_router.include_router(fxrates.router)
api_router.include_router(fx.router)
api_router.include_router(rates.router)

# Instrument resolution (ingestion entrypoint) + constituent prices/time-series.
api_router.include_router(instruments.router)
api_router.include_router(instrument_listings.router)

# Jobs / automation (+ workspace-scoped job-run observability).
api_router.include_router(jobs.router)
api_router.include_router(jobs.workspace_router)

# Scheduler / operational platform (job leasing, source budgets, fetch logs).
api_router.include_router(scheduler.router)
api_router.include_router(source_budgets.router)
api_router.include_router(source_fetch_logs.router)

# Market-data planning (workspace-scoped, read-only).
api_router.include_router(market_data.workspace_router)

# Instrument onboarding / data-readiness (workspace- + fund-scoped).
api_router.include_router(onboarding.workspace_router)
api_router.include_router(onboarding.fund_router)

# Data sources (priority registry + capability catalogue) and service discovery.
api_router.include_router(sources.router)
api_router.include_router(capabilities.router)

# Diagnostics / data-quality (global).
api_router.include_router(diagnostics.router)

# Legacy non-workspace aliases (deprecated).
api_router.include_router(portfolio.router)
api_router.include_router(exposure.router)
