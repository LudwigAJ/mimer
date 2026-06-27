"""Instrument onboarding orchestration — plan + execute data-readiness.

This is an **orchestration / data-readiness** layer, deliberately NOT a new
analytics engine. It takes a workspace or fund from "not ready" to "data-ready
enough for charts / exposure / performance" by coordinating the *existing*
ingestion + recompute workers, in dependency order:

    holdings -> constituent_identity -> constituent_prices -> fx
             -> exposure_recompute -> alerts

Two modes:

* ``build_onboarding_plan`` — read-only. Computes, from current DB state and the
  market-data planner, which stages are ready/needed/blocked, the estimated
  request cost per source, the jobs that would run, and a readiness/coverage
  summary. It performs **no writes** and **no network I/O**.
* ``execute_onboarding_plan`` — runs only the *needed* stages by calling the
  existing ``app.workers.run.run_job`` dispatch (never re-implementing a worker),
  records a parent ``instrument_onboarding`` job_run that references the child
  runs, applies a simple failure policy (a hard blocker stops dependent stages;
  a non-critical failure records ``partial_success`` and continues), and returns
  the per-stage outcome.

Source-mode policy (safe by default):

* ``fixture`` (default) — every stage uses its offline fixture source. Fully
  offline; the safe default for tests / local demos / the seeded scheduler.
* ``live`` — stages with a live-capable adapter use it (identity -> OpenFIGI,
  constituent prices -> Stooq), still budgeted/cached/logged via ``guarded_fetch``
  in the underlying worker. Stages with no live adapter (holdings, FX) fall back
  to their offline fixture and a warning is emitted. Live must be explicit.

Readiness is **data-quality / coverage**, never investment quality. The compute
boundary (see AGENTS.md) is respected: this module only reads bounded DB state +
the planner and dispatches existing workers — no per-instrument Python loops, no
dataframe analytics, no live source calls of its own.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.db.models import (
    Fund,
    FundListing,
    JobRun,
    PortfolioPosition,
    ScheduledJob,
)
from app.schemas.onboarding import (
    ONBOARDING_PAYLOAD_KIND,
    ONBOARDING_PAYLOAD_VERSION,
    PLAN_BLOCKED,
    PLAN_EMPTY,
    PLAN_NEEDS_WORK,
    PLAN_READY,
    REASON_ALREADY_READY,
    REASON_BLOCKED,
    REASON_MISSING_HOLDINGS,
    REASON_NO_POSITIONS,
    REASON_NOT_NEEDED,
    REASON_SKIPPED_BY_FLAG,
    REASON_UNRESOLVED_IDENTITY,
    REASON_WORKER_FAILED,
    RUN_BLOCKED,
    RUN_FAILED,
    RUN_SKIPPED,
    STAGE_ALERTS,
    STAGE_EXPOSURE,
    STAGE_FX,
    STAGE_HOLDINGS,
    STAGE_IDENTITY,
    STAGE_PRICES,
    STATUS_BLOCKED,
    STATUS_COMPLETE,
    STATUS_NEEDED,
    STATUS_READY,
    STATUS_SKIPPED,
    OnboardingPlanResponse,
    OnboardingReadiness,
    OnboardingRunResponse,
    OnboardingStage,
    OnboardingStageRun,
    OnboardingStatus,
)
from app.services import alert_rules, market_data_planner
from app.services import exposure_recompute as exposure_service
from app.services import holdings_ingestion as holdings_service
from app.services import instrument_prices as instrument_prices_service
from app.services import workspaces as workspaces_service
from app.services.conversion import normalise_currency
from app.services.freshness import FRESH, freshness_state
from app.services.fx import MISSING, load_fx_index

# Worker job-type names (kept as local constants to avoid an import cycle with
# ``app.workers.run``, which imports this module's package).
HOLDINGS_JOB = "issuer_holdings_ingestion"
IDENTITY_JOB = "constituent_identity_resolution"
PRICE_JOB = "constituent_eod_price_ingestion"
FX_JOB = "fx_ingestion"
EXPOSURE_JOB = "exposure_recompute"
ALERT_JOB = "alert_generation"
ONBOARDING_JOB = "instrument_onboarding"

# Source modes.
FIXTURE_MODE = "fixture"
LIVE_MODE = "live"
SOURCE_MODES = (FIXTURE_MODE, LIVE_MODE)

_SCORE_Q = Decimal("0.01")
# Child run statuses that count as "did some work successfully".
_OK_STATUSES = {"success", "partial_success", "success_stub", "planned"}


# --- source-mode policy ------------------------------------------------------


def _resolve_source_mode(source_mode: str | None) -> str:
    return source_mode if source_mode in SOURCE_MODES else FIXTURE_MODE


def _stage_source(
    stage: str, source_mode: str, settings: Settings
) -> tuple[str | None, bool, bool]:
    """Resolve ``(source_name, expected_offline, live_available)`` for a stage.

    DB-only stages (exposure / alerts) return ``(None, True, True)``. In live
    mode a stage with no live adapter (holdings / FX) falls back to its offline
    fixture and reports ``live_available=False`` so the plan can warn.
    """
    if stage in (STAGE_EXPOSURE, STAGE_ALERTS):
        return None, True, True

    if source_mode == LIVE_MODE:
        if stage == STAGE_IDENTITY:
            return "openfigi", False, True
        if stage == STAGE_PRICES:
            return settings.price_source_default, False, True
        # holdings / fx: no enabled live adapter -> offline fixture fallback.
        fallback = {
            STAGE_HOLDINGS: settings.holdings_source_default,
            STAGE_FX: settings.fx_source_default,
        }
        return fallback[stage], True, False

    fixture = {
        STAGE_HOLDINGS: settings.holdings_source_default,
        STAGE_IDENTITY: settings.constituent_identity_source_default,
        STAGE_PRICES: settings.constituent_price_source_default,
        STAGE_FX: settings.fx_source_default,
    }
    return fixture[stage], True, True


# --- small DB helpers --------------------------------------------------------


async def _held_fund_ids(session: AsyncSession, workspace_id: int) -> list[int]:
    rows = (
        await session.execute(
            select(FundListing.fund_id)
            .join(PortfolioPosition, PortfolioPosition.fund_listing_id == FundListing.id)
            .where(PortfolioPosition.workspace_id == workspace_id)
            .distinct()
        )
    ).scalars()
    return sorted(set(rows))


async def _holding_workspace_ids(session: AsyncSession, fund_id: int) -> list[int]:
    rows = (
        await session.execute(
            select(PortfolioPosition.workspace_id)
            .join(FundListing, PortfolioPosition.fund_listing_id == FundListing.id)
            .where(FundListing.fund_id == fund_id)
            .distinct()
        )
    ).scalars()
    return sorted(set(rows))


def _score(readiness: OnboardingReadiness) -> Decimal:
    flags = [
        readiness.holdings_ready,
        readiness.identity_ready,
        readiness.constituent_prices_ready,
        readiness.fx_ready,
        readiness.exposure_ready,
        readiness.top_holding_performance_ready,
    ]
    return (Decimal(sum(1 for f in flags if f)) / Decimal(len(flags))).quantize(
        _SCORE_Q, rounding=ROUND_HALF_UP
    )


# --- stage assembly ----------------------------------------------------------


def _stage(
    name: str,
    job_type: str | None,
    status: str,
    reason: str,
    *,
    source_mode: str,
    settings: Settings,
    blockers: list[str] | None = None,
    estimated_requests: int = 0,
    detail: dict[str, int] | None = None,
) -> OnboardingStage:
    source, offline, _ = _stage_source(name, source_mode, settings)
    return OnboardingStage(
        name=name,
        job_type=job_type,
        status=status,
        reason=reason,
        blockers=blockers or [],
        source=source,
        expected_offline=offline,
        estimated_requests=estimated_requests,
        detail=detail or {},
    )


_ACTION_BY_STAGE = {
    STAGE_HOLDINGS: "Ingest look-through holdings",
    STAGE_IDENTITY: "Resolve constituent identities",
    STAGE_PRICES: "Fetch constituent EOD prices",
    STAGE_FX: "Ingest FX rates",
    STAGE_EXPOSURE: "Recompute exposure snapshot",
    STAGE_ALERTS: "Refresh alerts / diagnostics",
}
_BLOCK_ACTION = {
    "no_positions": "Add positions to this workspace before onboarding",
    "no_holdings": "Holdings disclosure is required before look-through",
    "ambiguous_identity": "Resolve ambiguous constituent identities manually",
    "missing_holdings": "Run holdings ingestion first",
    "not_held": "Fund is not held by any workspace",
}


# Plan blocker codes -> structured run-stage reason codes (so the run history is
# typed and not parsed from the human-readable message).
_BLOCKER_TO_REASON = {
    "no_positions": REASON_NO_POSITIONS,
    "no_holdings": REASON_MISSING_HOLDINGS,
    "missing_holdings": REASON_MISSING_HOLDINGS,
    "ambiguous_identity": REASON_UNRESOLVED_IDENTITY,
    "not_held": REASON_NOT_NEEDED,
}


def _blocked_reason(stage: OnboardingStage) -> str:
    for code in stage.blockers:
        mapped = _BLOCKER_TO_REASON.get(code)
        if mapped:
            return mapped
    return REASON_BLOCKED


def _noop_reason(stage: OnboardingStage) -> str:
    """Structured reason for a stage skipped because it is already satisfied."""
    return REASON_ALREADY_READY if stage.status == STATUS_COMPLETE else REASON_NOT_NEEDED


def _next_action(stages: list[OnboardingStage]) -> str:
    needed = [st for st in stages if st.status == STATUS_NEEDED]
    if needed:
        st = needed[0]
        return _ACTION_BY_STAGE.get(st.name, f"Run {st.job_type}")
    blocked = [st for st in stages if st.status == STATUS_BLOCKED]
    if blocked:
        st = blocked[0]
        for code in st.blockers:
            if code in _BLOCK_ACTION:
                return _BLOCK_ACTION[code]
        return st.reason
    return "Up to date — no onboarding action needed."


def _assemble_plan(
    *,
    scope: str,
    workspace_id: int | None,
    fund_id: int | None,
    base_currency: str | None,
    source_mode: str,
    has_data: bool,
    stages: list[OnboardingStage],
    readiness: OnboardingReadiness,
    warnings: list[str],
) -> OnboardingPlanResponse:
    needed = [st for st in stages if st.status == STATUS_NEEDED]
    blocked = [st for st in stages if st.status == STATUS_BLOCKED]

    if not has_data:
        status = PLAN_EMPTY
    elif needed:
        status = PLAN_NEEDS_WORK
    elif blocked:
        status = PLAN_BLOCKED
    else:
        status = PLAN_READY

    blocking_issues = sorted({b for st in stages for b in st.blockers})
    jobs_that_would_run: list[str] = []
    for st in needed:
        if st.job_type and st.job_type not in jobs_that_would_run:
            jobs_that_would_run.append(st.job_type)

    by_source: dict[str, int] = {}
    for st in needed:
        if st.source and st.estimated_requests:
            by_source[st.source] = by_source.get(st.source, 0) + st.estimated_requests

    readiness.score = _score(readiness)

    return OnboardingPlanResponse(
        scope=scope,
        workspace_id=workspace_id,
        fund_id=fund_id,
        base_currency=base_currency,
        source_mode=source_mode,
        status=status,
        stages=stages,
        readiness=readiness,
        blocking_issues=blocking_issues,
        warnings=warnings,
        estimated_requests_by_source=by_source,
        jobs_that_would_run=jobs_that_would_run,
        next_recommended_action=_next_action(stages),
    )


# --- workspace-scope plan ----------------------------------------------------


async def _build_workspace_plan(
    session: AsyncSession, workspace_id: int, *, source_mode: str, settings: Settings
) -> OnboardingPlanResponse:
    await workspaces_service.get_workspace(session, workspace_id)  # 404s unknown workspace
    plan = await market_data_planner.build_plan(session, workspace_id, include_constituents=True)
    s = plan.summary
    base = plan.base_currency

    held_fund_ids = await _held_fund_ids(session, workspace_id)
    has_positions = bool(held_fund_ids)

    snapshots = await exposure_service.list_snapshots(session, workspace_id, limit=50)
    latest = snapshots[0][0] if snapshots else None
    snap_count = len(snapshots)
    snap_stale = latest is not None and exposure_service.snapshot_age_days(latest) > (
        alert_rules.EXPOSURE_STALE_DAYS
    )

    refresh_holdings = sum(1 for i in plan.items if i.item_type == "refresh_holdings")

    warnings: list[str] = []
    for stage in (STAGE_HOLDINGS, STAGE_IDENTITY, STAGE_PRICES, STAGE_FX):
        _, _, live_ok = _stage_source(stage, source_mode, settings)
        if source_mode == LIVE_MODE and not live_ok:
            warnings.append(
                f"live source mode requested but '{stage}' has no enabled live adapter; "
                "using the offline fixture"
            )

    stages: list[OnboardingStage] = []

    # 1) holdings ------------------------------------------------------------
    if not has_positions:
        stages.append(
            _stage(
                STAGE_HOLDINGS,
                HOLDINGS_JOB,
                STATUS_BLOCKED,
                "workspace holds no positions",
                source_mode=source_mode,
                settings=settings,
                blockers=["no_positions"],
            )
        )
        holdings_ready = False
    elif refresh_holdings:
        stages.append(
            _stage(
                STAGE_HOLDINGS,
                HOLDINGS_JOB,
                STATUS_NEEDED,
                f"{refresh_holdings} held fund(s) missing or stale holdings",
                source_mode=source_mode,
                settings=settings,
                estimated_requests=refresh_holdings,
                detail={
                    "held_funds": len(held_fund_ids),
                    "funds_needing_holdings": refresh_holdings,
                },
            )
        )
        holdings_ready = False
    else:
        stages.append(
            _stage(
                STAGE_HOLDINGS,
                HOLDINGS_JOB,
                STATUS_COMPLETE,
                "all held funds have a fresh holdings snapshot",
                source_mode=source_mode,
                settings=settings,
                detail={"held_funds": len(held_fund_ids)},
            )
        )
        holdings_ready = True

    # 2) constituent identity ------------------------------------------------
    identity_detail = {
        "unresolved": s.unresolved_constituents,
        "ambiguous": s.ambiguous_constituents,
        "resolved": s.resolved_constituents,
    }
    if not has_positions:
        stages.append(
            _stage(
                STAGE_IDENTITY,
                IDENTITY_JOB,
                STATUS_BLOCKED,
                "no positions to resolve constituents for",
                source_mode=source_mode,
                settings=settings,
                blockers=["no_positions"],
            )
        )
        identity_ready = False
    elif s.unresolved_constituents:
        stages.append(
            _stage(
                STAGE_IDENTITY,
                IDENTITY_JOB,
                STATUS_NEEDED,
                f"{s.unresolved_constituents} constituent(s) need identity resolution",
                source_mode=source_mode,
                settings=settings,
                estimated_requests=s.estimated_openfigi_requests,
                detail=identity_detail,
            )
        )
        identity_ready = False
    elif s.ambiguous_constituents and not s.resolved_constituents:
        stages.append(
            _stage(
                STAGE_IDENTITY,
                IDENTITY_JOB,
                STATUS_BLOCKED,
                f"{s.ambiguous_constituents} constituent(s) ambiguous; need manual disambiguation",
                source_mode=source_mode,
                settings=settings,
                blockers=["ambiguous_identity"],
                detail=identity_detail,
            )
        )
        identity_ready = False
    elif s.constituent_count == 0:
        # No constituents to resolve (e.g. holdings not yet disclosed).
        blocked = not holdings_ready
        stages.append(
            _stage(
                STAGE_IDENTITY,
                IDENTITY_JOB,
                STATUS_BLOCKED if blocked else STATUS_READY,
                "holdings disclosure required before constituent identity"
                if blocked
                else "no constituents to resolve",
                source_mode=source_mode,
                settings=settings,
                blockers=["missing_holdings"] if blocked else [],
            )
        )
        identity_ready = not blocked
    else:
        stages.append(
            _stage(
                STAGE_IDENTITY,
                IDENTITY_JOB,
                STATUS_COMPLETE,
                f"{s.resolved_constituents} constituent(s) resolved to identity",
                source_mode=source_mode,
                settings=settings,
                detail=identity_detail,
            )
        )
        identity_ready = True

    # 3) constituent prices --------------------------------------------------
    price_backlog = s.constituents_ready_for_eod_prices
    price_detail = {
        "missing": s.constituent_prices_missing,
        "stale": s.constituent_prices_stale,
        "fresh": s.constituent_prices_fresh,
    }
    if not has_positions:
        stages.append(
            _stage(
                STAGE_PRICES,
                PRICE_JOB,
                STATUS_BLOCKED,
                "no positions to price constituents for",
                source_mode=source_mode,
                settings=settings,
                blockers=["no_positions"],
            )
        )
        prices_ready = False
    elif s.resolved_constituents == 0:
        blocked = (
            not holdings_ready or s.unresolved_constituents > 0 or s.ambiguous_constituents > 0
        )
        stages.append(
            _stage(
                STAGE_PRICES,
                PRICE_JOB,
                STATUS_BLOCKED if blocked else STATUS_READY,
                "no resolved constituents to price (resolve identity first)"
                if blocked
                else "no resolved constituents to price",
                source_mode=source_mode,
                settings=settings,
                blockers=["missing_holdings"] if blocked else [],
            )
        )
        prices_ready = not blocked
    elif price_backlog:
        stages.append(
            _stage(
                STAGE_PRICES,
                PRICE_JOB,
                STATUS_NEEDED,
                f"{price_backlog} resolved constituent(s) need an EOD price",
                source_mode=source_mode,
                settings=settings,
                estimated_requests=s.estimated_price_requests,
                detail=price_detail,
            )
        )
        prices_ready = False
    else:
        stages.append(
            _stage(
                STAGE_PRICES,
                PRICE_JOB,
                STATUS_COMPLETE,
                "all resolved constituents have a fresh EOD price",
                source_mode=source_mode,
                settings=settings,
                detail=price_detail,
            )
        )
        prices_ready = True

    # 4) fx ------------------------------------------------------------------
    fx_needed = s.missing_fx + s.blocked_by_missing_fx
    if not has_positions:
        stages.append(
            _stage(
                STAGE_FX,
                FX_JOB,
                STATUS_READY,
                "no positions requiring FX",
                source_mode=source_mode,
                settings=settings,
            )
        )
        fx_ready = True
    elif fx_needed:
        stages.append(
            _stage(
                STAGE_FX,
                FX_JOB,
                STATUS_NEEDED,
                f"{fx_needed} currency path(s) to {base} missing",
                source_mode=source_mode,
                settings=settings,
                estimated_requests=s.missing_fx,
                detail={
                    "missing_fx": s.missing_fx,
                    "constituent_fx_blocked": s.blocked_by_missing_fx,
                },
            )
        )
        fx_ready = False
    else:
        stages.append(
            _stage(
                STAGE_FX,
                FX_JOB,
                STATUS_COMPLETE,
                f"all held / constituent currencies convert to {base}",
                source_mode=source_mode,
                settings=settings,
            )
        )
        fx_ready = True

    # 5) exposure recompute --------------------------------------------------
    upstream_needed = any(st.status == STATUS_NEEDED for st in stages)
    if not has_positions:
        stages.append(
            _stage(
                STAGE_EXPOSURE,
                EXPOSURE_JOB,
                STATUS_BLOCKED,
                "no positions to compute exposure for",
                source_mode=source_mode,
                settings=settings,
                blockers=["no_positions"],
            )
        )
        exposure_ready = False
    elif latest is None or snap_stale or upstream_needed:
        reason = (
            "no exposure snapshot yet"
            if latest is None
            else ("exposure snapshot is stale" if snap_stale else "upstream data changed")
        )
        stages.append(
            _stage(
                STAGE_EXPOSURE,
                EXPOSURE_JOB,
                STATUS_NEEDED,
                reason,
                source_mode=source_mode,
                settings=settings,
                detail={"snapshots": snap_count},
            )
        )
        exposure_ready = latest is not None and not snap_stale
    else:
        stages.append(
            _stage(
                STAGE_EXPOSURE,
                EXPOSURE_JOB,
                STATUS_COMPLETE,
                "exposure snapshot is fresh",
                source_mode=source_mode,
                settings=settings,
                detail={"snapshots": snap_count},
            )
        )
        exposure_ready = True

    # 6) alerts / diagnostics ------------------------------------------------
    alerts_needed = has_positions and any(st.status == STATUS_NEEDED for st in stages)
    stages.append(
        _stage(
            STAGE_ALERTS,
            ALERT_JOB,
            STATUS_NEEDED if alerts_needed else STATUS_COMPLETE,
            "refresh alerts after data changes" if alerts_needed else "alerts up to date",
            source_mode=source_mode,
            settings=settings,
        )
    )

    readiness = OnboardingReadiness(
        holdings_ready=holdings_ready,
        identity_ready=identity_ready,
        constituent_prices_ready=prices_ready,
        fx_ready=fx_ready,
        exposure_ready=exposure_ready,
        top_holding_performance_ready=snap_count >= 2,
        holdings_coverage_weight=latest.coverage_weight if latest else None,
        identity_coverage_weight=latest.identity_coverage_weight if latest else None,
        price_coverage_weight=latest.price_coverage_weight if latest else None,
        fx_coverage_weight=latest.fx_coverage_weight if latest else None,
        exposure_snapshot_count=snap_count,
        latest_exposure_snapshot_at=latest.created_at if latest else None,
        missing_top_constituent_prices=s.constituent_prices_missing,
        ambiguous_constituents=s.ambiguous_constituents,
    )

    return _assemble_plan(
        scope="workspace",
        workspace_id=workspace_id,
        fund_id=None,
        base_currency=base,
        source_mode=source_mode,
        has_data=has_positions,
        stages=stages,
        readiness=readiness,
        warnings=warnings,
    )


# --- fund-scope plan ---------------------------------------------------------


async def _build_fund_plan(
    session: AsyncSession, fund_id: int, *, source_mode: str, settings: Settings
) -> OnboardingPlanResponse:
    fund = await session.get(Fund, fund_id)
    if fund is None:
        from app.core.errors import NotFoundError

        raise NotFoundError("Fund not found", code="fund_not_found")
    base = (fund.base_currency or settings.base_currency).upper()

    snapshots = await holdings_service.latest_holdings_by_fund(session, [fund_id])
    snapshot = snapshots.get(fund_id) or []
    has_holdings = bool(snapshot)
    as_of = max((h.as_of_date for h in snapshot), default=None)
    holdings_fresh = as_of is not None and freshness_state(as_of, kind="holdings") == FRESH

    resolved = [h for h in snapshot if h.holding_instrument_id is not None]
    unresolved = [
        h
        for h in snapshot
        if h.holding_instrument_id is None and (h.identity_status in (None, "failed"))
    ]
    ambiguous = [h for h in snapshot if h.identity_status == "ambiguous"]

    warnings: list[str] = []
    for stage in (STAGE_HOLDINGS, STAGE_IDENTITY, STAGE_PRICES, STAGE_FX):
        _, _, live_ok = _stage_source(stage, source_mode, settings)
        if source_mode == LIVE_MODE and not live_ok:
            warnings.append(
                f"live source mode requested but '{stage}' has no enabled live adapter; "
                "using the offline fixture"
            )

    stages: list[OnboardingStage] = []

    # 1) holdings ------------------------------------------------------------
    if not has_holdings or not holdings_fresh:
        stages.append(
            _stage(
                STAGE_HOLDINGS,
                HOLDINGS_JOB,
                STATUS_NEEDED,
                "fund has no holdings snapshot"
                if not has_holdings
                else "holdings snapshot is stale",
                source_mode=source_mode,
                settings=settings,
                estimated_requests=1,
                detail={"holdings": len(snapshot)},
            )
        )
        holdings_ready = False
    else:
        stages.append(
            _stage(
                STAGE_HOLDINGS,
                HOLDINGS_JOB,
                STATUS_COMPLETE,
                "fresh holdings snapshot present",
                source_mode=source_mode,
                settings=settings,
                detail={"holdings": len(snapshot)},
            )
        )
        holdings_ready = True

    identity_detail = {
        "unresolved": len(unresolved),
        "ambiguous": len(ambiguous),
        "resolved": len(resolved),
    }
    # 2) constituent identity ------------------------------------------------
    if not has_holdings:
        stages.append(
            _stage(
                STAGE_IDENTITY,
                IDENTITY_JOB,
                STATUS_BLOCKED,
                "holdings disclosure required before constituent identity",
                source_mode=source_mode,
                settings=settings,
                blockers=["missing_holdings"],
            )
        )
        identity_ready = False
    elif unresolved:
        stages.append(
            _stage(
                STAGE_IDENTITY,
                IDENTITY_JOB,
                STATUS_NEEDED,
                f"{len(unresolved)} constituent(s) need identity resolution",
                source_mode=source_mode,
                settings=settings,
                estimated_requests=len(unresolved),
                detail=identity_detail,
            )
        )
        identity_ready = False
    elif ambiguous and not resolved:
        stages.append(
            _stage(
                STAGE_IDENTITY,
                IDENTITY_JOB,
                STATUS_BLOCKED,
                f"{len(ambiguous)} constituent(s) ambiguous; need manual disambiguation",
                source_mode=source_mode,
                settings=settings,
                blockers=["ambiguous_identity"],
                detail=identity_detail,
            )
        )
        identity_ready = False
    else:
        stages.append(
            _stage(
                STAGE_IDENTITY,
                IDENTITY_JOB,
                STATUS_COMPLETE,
                f"{len(resolved)} constituent(s) resolved to identity",
                source_mode=source_mode,
                settings=settings,
                detail=identity_detail,
            )
        )
        identity_ready = True

    # 3) constituent prices --------------------------------------------------
    listings = await instrument_prices_service.select_listings(session, fund_id=fund_id)
    fresh_listings = [
        ln for ln in listings if freshness_state(ln.last_price_at, kind="price") == FRESH
    ]
    price_backlog = len(listings) - len(fresh_listings)
    if not resolved:
        blocked = (not has_holdings) or bool(unresolved or ambiguous)
        stages.append(
            _stage(
                STAGE_PRICES,
                PRICE_JOB,
                STATUS_BLOCKED if blocked else STATUS_READY,
                "no resolved constituents to price (resolve identity first)"
                if blocked
                else "no resolved constituents to price",
                source_mode=source_mode,
                settings=settings,
                blockers=["missing_holdings"] if blocked else [],
            )
        )
        prices_ready = not blocked
    elif price_backlog:
        stages.append(
            _stage(
                STAGE_PRICES,
                PRICE_JOB,
                STATUS_NEEDED,
                f"{price_backlog} resolved constituent listing(s) need an EOD price",
                source_mode=source_mode,
                settings=settings,
                estimated_requests=price_backlog,
                detail={"missing_or_stale": price_backlog, "fresh": len(fresh_listings)},
            )
        )
        prices_ready = False
    else:
        stages.append(
            _stage(
                STAGE_PRICES,
                PRICE_JOB,
                STATUS_COMPLETE,
                "all resolved constituent listings have a fresh EOD price",
                source_mode=source_mode,
                settings=settings,
                detail={"fresh": len(fresh_listings)},
            )
        )
        prices_ready = True

    # 4) fx ------------------------------------------------------------------
    fx_index = await load_fx_index(session)
    currencies: set[str] = set()
    for ln in await _fund_listings(session, fund_id):
        local = normalise_currency(ln.currency_unit or ln.trading_currency, base)
        if local and local != base:
            currencies.add(local)
    for ln in listings:
        local = normalise_currency(ln.currency, base)
        if local and local != base:
            currencies.add(local)
    missing_fx = sum(1 for ccy in currencies if fx_index.get_fx_rate(ccy, base).status == MISSING)
    if missing_fx:
        stages.append(
            _stage(
                STAGE_FX,
                FX_JOB,
                STATUS_NEEDED,
                f"{missing_fx} currency path(s) to {base} missing",
                source_mode=source_mode,
                settings=settings,
                estimated_requests=missing_fx,
                detail={"missing_fx": missing_fx},
            )
        )
        fx_ready = False
    else:
        stages.append(
            _stage(
                STAGE_FX,
                FX_JOB,
                STATUS_COMPLETE,
                f"all fund / constituent currencies convert to {base}",
                source_mode=source_mode,
                settings=settings,
            )
        )
        fx_ready = True

    # 5) exposure recompute + 6) alerts (per workspace holding the fund) -----
    holding_ws = await _holding_workspace_ids(session, fund_id)
    total_snaps = 0
    latest_at: datetime | None = None
    needs_exposure = False
    for wid in holding_ws:
        ws_snaps = await exposure_service.list_snapshots(session, wid, limit=50)
        total_snaps += len(ws_snaps)
        if ws_snaps:
            created = ws_snaps[0][0].created_at
            latest_at = max(latest_at, created) if latest_at else created
            if exposure_service.snapshot_age_days(ws_snaps[0][0]) > alert_rules.EXPOSURE_STALE_DAYS:
                needs_exposure = True
        else:
            needs_exposure = True
    upstream_needed = any(st.status == STATUS_NEEDED for st in stages)

    if not holding_ws:
        stages.append(
            _stage(
                STAGE_EXPOSURE,
                EXPOSURE_JOB,
                STATUS_SKIPPED,
                "fund is not held by any workspace",
                source_mode=source_mode,
                settings=settings,
                blockers=["not_held"],
            )
        )
        exposure_ready = False
    elif needs_exposure or upstream_needed:
        stages.append(
            _stage(
                STAGE_EXPOSURE,
                EXPOSURE_JOB,
                STATUS_NEEDED,
                f"recompute exposure for {len(holding_ws)} holding workspace(s)",
                source_mode=source_mode,
                settings=settings,
                detail={"workspaces": len(holding_ws), "snapshots": total_snaps},
            )
        )
        exposure_ready = not needs_exposure
    else:
        stages.append(
            _stage(
                STAGE_EXPOSURE,
                EXPOSURE_JOB,
                STATUS_COMPLETE,
                "exposure snapshots fresh for all holding workspaces",
                source_mode=source_mode,
                settings=settings,
                detail={"workspaces": len(holding_ws), "snapshots": total_snaps},
            )
        )
        exposure_ready = True

    if not holding_ws:
        stages.append(
            _stage(
                STAGE_ALERTS,
                ALERT_JOB,
                STATUS_SKIPPED,
                "fund is not held by any workspace",
                source_mode=source_mode,
                settings=settings,
            )
        )
    else:
        alerts_needed = any(st.status == STATUS_NEEDED for st in stages)
        stages.append(
            _stage(
                STAGE_ALERTS,
                ALERT_JOB,
                STATUS_NEEDED if alerts_needed else STATUS_COMPLETE,
                "refresh alerts after data changes" if alerts_needed else "alerts up to date",
                source_mode=source_mode,
                settings=settings,
            )
        )

    readiness = OnboardingReadiness(
        holdings_ready=holdings_ready,
        identity_ready=identity_ready,
        constituent_prices_ready=prices_ready,
        fx_ready=fx_ready,
        exposure_ready=exposure_ready,
        top_holding_performance_ready=total_snaps >= 2,
        exposure_snapshot_count=total_snaps,
        latest_exposure_snapshot_at=latest_at,
        missing_top_constituent_prices=price_backlog,
        ambiguous_constituents=len(ambiguous),
    )

    return _assemble_plan(
        scope="fund",
        workspace_id=None,
        fund_id=fund_id,
        base_currency=base,
        source_mode=source_mode,
        has_data=True,
        stages=stages,
        readiness=readiness,
        warnings=warnings,
    )


async def _fund_listings(session: AsyncSession, fund_id: int) -> list[FundListing]:
    return list(
        (await session.execute(select(FundListing).where(FundListing.fund_id == fund_id)))
        .scalars()
        .all()
    )


# --- public plan entrypoint --------------------------------------------------


async def build_onboarding_plan(
    session: AsyncSession,
    *,
    workspace_id: int | None = None,
    fund_id: int | None = None,
    source_mode: str | None = None,
    limit: int | None = None,
) -> OnboardingPlanResponse:
    """Read-only onboarding plan for a workspace or fund scope (no writes)."""
    mode = _resolve_source_mode(source_mode)
    settings = get_settings()
    if fund_id is not None:
        return await _build_fund_plan(session, fund_id, source_mode=mode, settings=settings)
    if workspace_id is None:
        workspace = await workspaces_service.get_default_workspace(session)
        workspace_id = workspace.id
    return await _build_workspace_plan(session, workspace_id, source_mode=mode, settings=settings)


# --- execution ---------------------------------------------------------------


@dataclass
class _StageRun:
    name: str
    job_type: str | None
    status: str
    reason: str | None = None
    child_run_ids: list[int] = field(default_factory=list)
    inserted: int = 0
    updated: int = 0
    failed: int = 0
    message: str | None = None


def _agg_status(child_statuses: list[str]) -> str:
    if not child_statuses:
        return STATUS_SKIPPED
    failed = [c for c in child_statuses if c == "failed"]
    ok = [c for c in child_statuses if c in _OK_STATUSES]
    if failed and ok:
        return "partial_success"
    if failed:
        return "failed"
    if any(c == "partial_success" for c in child_statuses):
        return "partial_success"
    return "success"


async def _run_child_jobs(session: AsyncSession, invocations: list[dict]) -> _StageRun:
    """Run a list of ``run_job`` invocations and aggregate them into a stage run."""
    from app.workers.run import run_job  # lazy import to avoid a module cycle

    statuses: list[str] = []
    ids: list[int] = []
    ins = upd = fail = 0
    for kwargs in invocations:
        run = await run_job(session, **kwargs)
        statuses.append(run.status)
        if run.id is not None:
            ids.append(run.id)
        ins += run.records_inserted or 0
        upd += run.records_updated or 0
        fail += run.records_failed or 0
    return _StageRun(
        name="",
        job_type=None,
        status=_agg_status(statuses),
        child_run_ids=ids,
        inserted=ins,
        updated=upd,
        failed=fail,
    )


async def _execute_stage(
    session: AsyncSession,
    stage: OnboardingStage,
    *,
    workspace_id: int | None,
    fund_id: int | None,
    held_fund_ids: list[int],
    holding_ws: list[int],
    source_mode: str,
    settings: Settings,
    limit: int | None,
) -> _StageRun:
    source, _, _ = _stage_source(stage.name, source_mode, settings)
    invocations: list[dict] = []

    if stage.name == STAGE_HOLDINGS:
        targets = [fund_id] if fund_id is not None else held_fund_ids
        invocations = [
            {"job_type": HOLDINGS_JOB, "fund_id": fid, "source_name": source} for fid in targets
        ]
    elif stage.name == STAGE_IDENTITY:
        invocations = [
            {
                "job_type": IDENTITY_JOB,
                "fund_id": fund_id,
                "workspace_id": workspace_id,
                "source_name": source,
                "limit": limit,
            }
        ]
    elif stage.name == STAGE_PRICES:
        invocations = [
            {
                "job_type": PRICE_JOB,
                "fund_id": fund_id,
                "workspace_id": workspace_id,
                "source_name": source,
                "limit": limit,
            }
        ]
    elif stage.name == STAGE_FX:
        invocations = [{"job_type": FX_JOB, "source_name": source}]
    elif stage.name == STAGE_EXPOSURE:
        targets = [workspace_id] if workspace_id is not None else holding_ws
        invocations = [{"job_type": EXPOSURE_JOB, "workspace_id": wid} for wid in targets]
    elif stage.name == STAGE_ALERTS:
        targets = [workspace_id] if workspace_id is not None else holding_ws
        invocations = [{"job_type": ALERT_JOB, "workspace_id": wid} for wid in targets]

    stage_run = await _run_child_jobs(session, invocations)
    stage_run.name = stage.name
    stage_run.job_type = stage.job_type
    return stage_run


async def execute_onboarding_plan(
    session: AsyncSession,
    *,
    workspace_id: int | None = None,
    fund_id: int | None = None,
    source_mode: str | None = None,
    plan_only: bool = False,
    limit: int | None = None,
    skip_exposure: bool = False,
    skip_alerts: bool = False,
    scheduled_job_id: int | None = None,
) -> OnboardingRunResponse:
    """Run only the *needed* stages for a single scope (workspace or fund).

    Calls the existing worker dispatch (never re-implements a worker), records a
    parent ``instrument_onboarding`` job_run referencing the child runs, applies
    the failure policy, and returns the per-stage outcome + post-run readiness.
    With ``plan_only`` it writes nothing and returns the plan as a run response.
    """
    mode = _resolve_source_mode(source_mode)
    settings = get_settings()
    plan = await build_onboarding_plan(
        session, workspace_id=workspace_id, fund_id=fund_id, source_mode=mode, limit=limit
    )
    # Resolve the concrete workspace id the plan chose (default-workspace case).
    resolved_ws = plan.workspace_id
    resolved_fund = plan.fund_id

    if plan_only:
        return OnboardingRunResponse(
            scope=plan.scope,
            workspace_id=resolved_ws,
            fund_id=resolved_fund,
            source_mode=mode,
            plan_only=True,
            parent_job_run_id=None,
            status="planned",
            stages=[
                OnboardingStageRun(
                    name=st.name, job_type=st.job_type, status=st.status, reason=st.reason
                )
                for st in plan.stages
            ],
            readiness=plan.readiness,
            message=f"plan-only: {plan.status}",
            next_recommended_action=plan.next_recommended_action,
        )

    held_fund_ids = await _held_fund_ids(session, resolved_ws) if resolved_ws is not None else []
    holding_ws = (
        await _holding_workspace_ids(session, resolved_fund) if resolved_fund is not None else []
    )

    # Parent orchestration run (persist before child work so children correlate).
    now = datetime.now(UTC)
    parent = JobRun(
        job_type=ONBOARDING_JOB,
        status="running",
        started_at=now,
        source=mode,
        fund_id=resolved_fund,
        scheduled_job_id=scheduled_job_id,
    )
    session.add(parent)
    await session.commit()
    await session.refresh(parent)

    stage_runs: list[OnboardingStageRun] = []
    # Typed stage rows persisted on the parent run's ``payload_json`` — the
    # GUI-facing source of truth (status / reason / timings / child run ids /
    # counts), so the run history is queryable without parsing ``message``.
    stage_meta: list[dict] = []
    executed_failed = executed_ok = False
    total_ins = total_upd = total_fail = 0
    # A single run cascades: after each *data-producing* stage we re-derive the
    # plan so downstream stages (e.g. prices after identity) see the fresh state.
    data_producing = {STAGE_HOLDINGS, STAGE_IDENTITY, STAGE_PRICES, STAGE_FX}
    data_changed = False
    stages_by_name = {st.name: st for st in plan.stages}

    def _record(
        stage: OnboardingStage,
        *,
        status: str,
        reason: str | None,
        message: str | None = None,
        started_at: datetime | None = None,
        finished_at: datetime | None = None,
        child_run_ids: list[int] | None = None,
        inserted: int = 0,
        updated: int = 0,
        failed: int = 0,
    ) -> None:
        """Record one stage outcome into both the response + the typed payload."""
        started = started_at or datetime.now(UTC)
        finished = finished_at or started
        duration_ms = max(0, int((finished - started).total_seconds() * 1000))
        source, offline, _ = _stage_source(stage.name, mode, settings)
        ids = list(child_run_ids or [])
        stage_runs.append(
            OnboardingStageRun(
                name=stage.name,
                job_type=stage.job_type,
                status=status,
                reason=reason,
                child_run_ids=ids,
                records_inserted=inserted,
                records_updated=updated,
                records_failed=failed,
                message=message,
            )
        )
        stage_meta.append(
            {
                "stage": stage.name,
                "label": _ACTION_BY_STAGE.get(stage.name),
                "status": status,
                "reason": reason,
                "source": source,
                "source_mode": mode,
                "expected_offline": offline,
                "started_at": started.isoformat(),
                "finished_at": finished.isoformat(),
                "duration_ms": duration_ms,
                "child_run_ids": ids,
                "records_inserted": inserted,
                "records_updated": updated,
                "records_failed": failed,
                "blockers": list(stage.blockers),
                "message": message,
            }
        )

    for name in (
        STAGE_HOLDINGS,
        STAGE_IDENTITY,
        STAGE_PRICES,
        STAGE_FX,
        STAGE_EXPOSURE,
        STAGE_ALERTS,
    ):
        stage = stages_by_name[name]

        if (name == STAGE_EXPOSURE and skip_exposure) or (name == STAGE_ALERTS and skip_alerts):
            _record(
                stage, status=RUN_SKIPPED, reason=REASON_SKIPPED_BY_FLAG, message="skipped by flag"
            )
            continue

        if name in (STAGE_EXPOSURE, STAGE_ALERTS):
            # Terminal/derived stages: run when upstream changed data this run or
            # the plan independently flags them needed; never block on them.
            if stage.status == STATUS_SKIPPED or not (
                data_changed or stage.status == STATUS_NEEDED
            ):
                _record(stage, status=RUN_SKIPPED, reason=_noop_reason(stage), message=stage.reason)
                continue
        else:
            if stage.status in (STATUS_COMPLETE, STATUS_READY, STATUS_SKIPPED):
                _record(stage, status=RUN_SKIPPED, reason=_noop_reason(stage), message=stage.reason)
                continue
            if stage.status == STATUS_BLOCKED:
                _record(
                    stage, status=RUN_BLOCKED, reason=_blocked_reason(stage), message=stage.reason
                )
                continue

        # Execute via the existing worker dispatch (timed for the run history).
        started = datetime.now(UTC)
        sr = await _execute_stage(
            session,
            stage,
            workspace_id=resolved_ws,
            fund_id=resolved_fund,
            held_fund_ids=held_fund_ids,
            holding_ws=holding_ws,
            source_mode=mode,
            settings=settings,
            limit=limit,
        )
        finished = datetime.now(UTC)
        _record(
            stage,
            status=sr.status,
            reason=REASON_WORKER_FAILED if sr.status == "failed" else None,
            started_at=started,
            finished_at=finished,
            child_run_ids=sr.child_run_ids,
            inserted=sr.inserted,
            updated=sr.updated,
            failed=sr.failed,
        )
        total_ins += sr.inserted
        total_upd += sr.updated
        total_fail += sr.failed
        if sr.status == "failed":
            executed_failed = True
        else:
            executed_ok = True
        if name in data_producing and sr.status in _OK_STATUSES:
            data_changed = True
        if name == STAGE_EXPOSURE and sr.status in _OK_STATUSES:
            data_changed = True  # a refreshed snapshot warrants an alerts pass
        # Re-derive the plan after a data-producing stage so the next stages see
        # the data it just produced (the failure policy falls out of this: if
        # holdings failed and nothing landed, identity/prices read as blocked).
        if name in data_producing:
            plan = await build_onboarding_plan(
                session,
                workspace_id=resolved_ws,
                fund_id=resolved_fund,
                source_mode=mode,
                limit=limit,
            )
            stages_by_name = {st.name: st for st in plan.stages}

    if executed_failed and executed_ok:
        parent_status = "partial_success"
    elif executed_failed:
        parent_status = "failed"
    else:
        parent_status = "success"

    # Post-run readiness + blockers reflect the data the stages just produced.
    after = await build_onboarding_plan(
        session, workspace_id=resolved_ws, fund_id=resolved_fund, source_mode=mode, limit=limit
    )

    parts = []
    for sr in stage_runs:
        ids = ",".join(str(i) for i in sr.child_run_ids) if sr.child_run_ids else "-"
        parts.append(f"{sr.name}={sr.status}(runs={ids})")
    message = f"mode={mode} " + " ".join(parts)
    if after.blocking_issues:
        message += f" blockers={after.blocking_issues}"

    finished_at = datetime.now(UTC)
    parent.status = parent_status
    parent.workspace_id = resolved_ws
    parent.records_inserted = total_ins
    parent.records_updated = total_upd
    parent.records_failed = total_fail
    parent.message = message
    parent.finished_at = finished_at
    parent.payload_json = {
        "kind": ONBOARDING_PAYLOAD_KIND,
        "schema_version": ONBOARDING_PAYLOAD_VERSION,
        "scope": {
            "type": plan.scope,
            "id": resolved_ws if resolved_ws is not None else resolved_fund,
        },
        "source_mode": mode,
        "plan_only": False,
        "skip_exposure": skip_exposure,
        "skip_alerts": skip_alerts,
        "status": parent_status,
        "started_at": now.isoformat(),
        "finished_at": finished_at.isoformat(),
        "duration_ms": max(0, int((finished_at - now).total_seconds() * 1000)),
        "next_recommended_action": after.next_recommended_action,
        "blocking_issues": after.blocking_issues,
        "stages": stage_meta,
    }
    if scheduled_job_id is not None:
        scheduled = await session.get(ScheduledJob, scheduled_job_id)
        if scheduled is not None:
            scheduled.last_run_at = parent.finished_at
    await session.commit()
    await session.refresh(parent)

    return OnboardingRunResponse(
        scope=plan.scope,
        workspace_id=resolved_ws,
        fund_id=resolved_fund,
        source_mode=mode,
        plan_only=False,
        parent_job_run_id=parent.id,
        status=parent_status,
        stages=stage_runs,
        readiness=after.readiness,
        message=message,
        next_recommended_action=after.next_recommended_action,
    )


# --- status (dashboard / status endpoint) ------------------------------------


async def latest_onboarding_run(
    session: AsyncSession, *, workspace_id: int | None = None
) -> JobRun | None:
    """Most recent ``instrument_onboarding`` run (optionally workspace-scoped).

    With ``workspace_id`` it returns the latest *workspace-scoped* parent run for
    that workspace; without it, the latest run of any scope (used by the global
    diagnostics rollup). Bounded by the ``(job_type, id)`` index + ``LIMIT 1``.
    """
    stmt = select(JobRun).where(JobRun.job_type == ONBOARDING_JOB)
    if workspace_id is not None:
        stmt = stmt.where(JobRun.workspace_id == workspace_id)
    return await session.scalar(stmt.order_by(JobRun.id.desc()).limit(1))


def failed_stage_from_run(run: JobRun | None) -> str | None:
    """First failed stage of a run, from its structured payload (typed).

    Falls back to a tiny parse of the legacy free-text ``message`` only when the
    run predates structured payloads (``payload_json is None``)."""
    if run is None:
        return None
    payload = run.payload_json
    if isinstance(payload, dict):
        for st in payload.get("stages") or []:
            if isinstance(st, dict) and st.get("status") == RUN_FAILED:
                return st.get("stage")
        return None
    # Legacy fallback: tokens look like ``constituent_identity=failed(runs=...)``.
    for token in (run.message or "").split():
        if "=failed" in token:
            return token.split("=", 1)[0]
    return None


def run_duration_ms(run: JobRun | None) -> int | None:
    """Total run duration from the payload, else derived from start/finish."""
    if run is None:
        return None
    payload = run.payload_json
    if isinstance(payload, dict) and payload.get("duration_ms") is not None:
        return payload["duration_ms"]
    if run.started_at and run.finished_at:
        return max(0, int((run.finished_at - run.started_at).total_seconds() * 1000))
    return None


async def build_status(session: AsyncSession, workspace_id: int) -> OnboardingStatus:
    """Compact onboarding status for the dashboard / status endpoint."""
    plan = await build_onboarding_plan(session, workspace_id=workspace_id)
    last = await latest_onboarding_run(session, workspace_id=workspace_id)
    attention = [st.name for st in plan.stages if st.status in (STATUS_NEEDED, STATUS_BLOCKED)]
    return OnboardingStatus(
        scope="workspace",
        workspace_id=workspace_id,
        status=plan.status,
        readiness=plan.readiness,
        last_run_id=last.id if last else None,
        last_run_status=last.status if last else None,
        last_run_at=last.finished_at if last else None,
        last_run_duration_ms=run_duration_ms(last),
        last_run_failed_stage=failed_stage_from_run(last),
        stages_needing_attention=attention,
        next_recommended_action=plan.next_recommended_action,
    )
