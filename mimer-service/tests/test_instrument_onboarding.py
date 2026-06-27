"""Instrument onboarding orchestration — plan, execution, API, safety.

All offline (fixture source mode). The default seeded workspace (id 1, held VUSA/
ISF/JEPG with seed holdings that the fixtures resolve) exercises the full chain;
custom workspaces exercise the empty / blocked / ready edges. Readiness is
data-quality / coverage, never investment quality. No test makes a live call.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    Fund,
    FundHolding,
    FundListing,
    Instrument,
    InstrumentListing,
    InstrumentPrice,
    JobRun,
    PortfolioPosition,
    Price,
    Workspace,
)
from app.services import instrument_onboarding as ob
from app.sources.holdings import holding_identity_key
from app.workers.run import run_job

_TODAY = date.today()
_FIXTURE = "instrument_price_fixture"

_LIVE_SOURCES = {"openfigi", "stooq", "yfinance"}


# --- builders ----------------------------------------------------------------


async def _workspace(session: AsyncSession, name: str, base: str = "GBP") -> Workspace:
    ws = Workspace(name=name, base_currency=base)
    session.add(ws)
    await session.flush()
    return ws


async def _instrument(
    session: AsyncSession, name: str, *, currency: str, ticker: str
) -> Instrument:
    instr = Instrument(
        identity_key=f"name:{name.lower()}",
        instrument_type="equity",
        name=name,
        currency=currency,
        status="active",
        source="manual",
    )
    session.add(instr)
    await session.flush()
    listing = InstrumentListing(
        instrument_id=instr.id,
        listing_key=f"ticker:{ticker}",
        ticker=ticker,
        currency=currency,
        source="manual",
        status="active",
    )
    session.add(listing)
    await session.flush()
    instr._listing_id = listing.id  # type: ignore[attr-defined]
    return instr


async def _fund_with_holdings(
    session: AsyncSession,
    ws: Workspace,
    *,
    isin: str,
    units: str = "100",
    price: str = "10",
    currency: str = "GBP",
    holdings: list[tuple],
    fresh_price_for: list[Instrument] | None = None,
) -> Fund:
    """holdings tuples: (instrument|None, weight, name, identity_status|None)."""
    listing = FundListing(
        ticker=isin[-4:],
        trading_currency=currency,
        currency_unit=currency,
        status="active",
        prices=[Price(price_date=_TODAY, price=Decimal(price), currency=currency, source="stooq")],
    )
    fund = Fund(isin=isin, name=f"Fund {isin}", status="active", source="seed", listings=[listing])
    session.add(fund)
    await session.flush()
    session.add(
        PortfolioPosition(workspace_id=ws.id, fund_listing_id=listing.id, units=Decimal(units))
    )
    for tup in holdings:
        instr, weight, name = tup[0], tup[1], tup[2]
        identity_status = tup[3] if len(tup) > 3 else ("resolved" if instr else None)
        session.add(
            FundHolding(
                fund_id=fund.id,
                as_of_date=_TODAY,
                security_name=name,
                weight=Decimal(weight),
                source="holdings_fixture",
                holding_key=holding_identity_key(name=name),
                holding_instrument_id=instr.id if instr else None,
                identity_status=identity_status,
            )
        )
    for instr in fresh_price_for or []:
        session.add(
            InstrumentPrice(
                instrument_listing_id=instr._listing_id,  # type: ignore[attr-defined]
                price_date=_TODAY,
                close=Decimal("100"),
                currency=instr.currency,
                source=_FIXTURE,
                status="fixture",
            )
        )
        ln = await session.get(InstrumentListing, instr._listing_id)  # type: ignore[attr-defined]
        ln.last_price_at = datetime.now(UTC)
    await session.flush()
    return fund


def _stage(plan, name: str):
    return next(st for st in plan.stages if st.name == name)


def _stage_run(run, name: str):
    return next(sr for sr in run.stages if sr.name == name)


# --- plan: scope shapes ------------------------------------------------------


async def test_workspace_plan_seeded_after_holdings_before_identity(session: AsyncSession) -> None:
    # The seeded workspace ships with holdings but no resolved constituents.
    plan = await ob.build_onboarding_plan(session, workspace_id=1)
    assert plan.scope == "workspace"
    assert plan.source_mode == "fixture"
    assert plan.status == "needs_work"
    assert _stage(plan, "holdings").status == "complete"
    assert _stage(plan, "constituent_identity").status == "needed"
    assert _stage(plan, "constituent_prices").status == "blocked"
    # estimated requests are attributed to the resolved (fixture) source.
    assert plan.estimated_requests_by_source.get("constituent_identity_fixture", 0) > 0
    assert "constituent_identity_resolution" in plan.jobs_that_would_run
    assert plan.readiness.score is not None


async def test_workspace_plan_no_positions_is_empty_and_blocked(session: AsyncSession) -> None:
    ws = await _workspace(session, "Empty")
    await session.commit()
    plan = await ob.build_onboarding_plan(session, workspace_id=ws.id)
    assert plan.status == "empty"
    assert _stage(plan, "holdings").status == "blocked"
    assert "no_positions" in plan.blocking_issues
    assert plan.readiness.holdings_ready is False
    assert plan.readiness.exposure_ready is False
    assert plan.readiness.score <= Decimal("0.20")


async def test_fund_plan_missing_holdings(session: AsyncSession) -> None:
    ws = await _workspace(session, "FundScope")
    fund = await _fund_with_holdings(session, ws, isin="IE00FUND0001", holdings=[])
    # Remove the holdings rows so the fund has no snapshot at all.
    await session.execute(select(FundHolding))  # no-op, snapshot already empty
    await session.commit()
    plan = await ob.build_onboarding_plan(session, fund_id=fund.id)
    assert plan.scope == "fund"
    assert plan.fund_id == fund.id
    assert _stage(plan, "holdings").status == "needed"
    assert _stage(plan, "constituent_identity").status == "blocked"
    assert _stage(plan, "constituent_prices").status == "blocked"


async def test_plan_after_identity_before_prices(session: AsyncSession) -> None:
    # Resolve identity only, then the plan should move to needing prices.
    await run_job(
        session,
        "constituent_identity_resolution",
        workspace_id=1,
        source_name="constituent_identity_fixture",
    )
    await session.commit()
    plan = await ob.build_onboarding_plan(session, workspace_id=1)
    assert _stage(plan, "constituent_identity").status == "complete"
    assert _stage(plan, "constituent_prices").status == "needed"
    assert plan.readiness.identity_ready is True
    assert plan.readiness.constituent_prices_ready is False


async def test_plan_blockers_ambiguous_identity(session: AsyncSession) -> None:
    ws = await _workspace(session, "Ambig")
    await _fund_with_holdings(
        session,
        ws,
        isin="IE00AMBI0001",
        holdings=[(None, "0.5", "Mystery Co", "ambiguous")],
    )
    await session.commit()
    plan = await ob.build_onboarding_plan(session, fund_id=(await _only_fund(session, ws.id)))
    identity = _stage(plan, "constituent_identity")
    assert identity.status == "blocked"
    assert "ambiguous_identity" in identity.blockers
    assert plan.readiness.ambiguous_constituents == 1


async def test_ready_full_resolved_gbp_workspace(session: AsyncSession) -> None:
    ws = await _workspace(session, "Ready", base="GBP")
    apple = await _instrument(session, "Apple Co", currency="GBP", ticker="AAPL")
    await _fund_with_holdings(
        session,
        ws,
        isin="IE00READY001",
        currency="GBP",
        holdings=[(apple, "0.5", "Apple Co")],
        fresh_price_for=[apple],
    )
    await session.commit()
    run = await ob.execute_onboarding_plan(session, workspace_id=ws.id, source_mode="fixture")
    assert run.status == "success"
    r = run.readiness
    assert r.holdings_ready and r.identity_ready and r.constituent_prices_ready
    assert r.fx_ready and r.exposure_ready


# --- execution ---------------------------------------------------------------


async def test_execute_workspace_cascades_in_one_run(session: AsyncSession) -> None:
    run = await ob.execute_onboarding_plan(session, workspace_id=1, source_mode="fixture")
    assert run.status == "success"
    assert run.parent_job_run_id is not None
    # Identity -> prices -> exposure all ran in a single invocation.
    assert _stage_run(run, "constituent_identity").status == "success"
    assert _stage_run(run, "constituent_prices").status == "success"
    assert _stage_run(run, "exposure_recompute").status == "success"
    assert run.readiness.identity_ready is True
    assert run.readiness.constituent_prices_ready is True


async def test_execute_fund_scope(session: AsyncSession) -> None:
    # Fund 1 is held by workspace 1; onboarding the fund resolves + prices it.
    run = await ob.execute_onboarding_plan(session, fund_id=1, source_mode="fixture")
    assert run.scope == "fund"
    assert run.fund_id == 1
    assert run.status in ("success", "partial_success")
    # Exposure recompute targets the holding workspace.
    assert _stage_run(run, "exposure_recompute").status in ("success", "skipped")


async def test_plan_only_writes_nothing(session: AsyncSession) -> None:
    before_runs = len((await session.execute(select(JobRun))).scalars().all())
    out = await ob.execute_onboarding_plan(session, workspace_id=1, plan_only=True)
    assert out.status == "planned"
    assert out.parent_job_run_id is None
    after_runs = len((await session.execute(select(JobRun))).scalars().all())
    assert after_runs == before_runs  # no job_runs created


async def test_idempotent_rerun_skips_completed_stages(session: AsyncSession) -> None:
    await ob.execute_onboarding_plan(session, workspace_id=1, source_mode="fixture")
    run2 = await ob.execute_onboarding_plan(session, workspace_id=1, source_mode="fixture")
    assert _stage_run(run2, "holdings").status == "skipped"
    assert _stage_run(run2, "constituent_identity").status == "skipped"
    assert _stage_run(run2, "constituent_prices").status == "skipped"


async def test_limit_respected(session: AsyncSession) -> None:
    # A limit of 1 caps how many constituents identity resolution attempts.
    run = await ob.execute_onboarding_plan(session, workspace_id=1, source_mode="fixture", limit=1)
    identity = _stage_run(run, "constituent_identity")
    # At most one constituent resolved this pass (inserted counts instruments/links).
    assert identity.records_inserted <= 3  # 1 instrument + listing + identifier upserts


async def test_hard_blocker_unknown_fund_blocks_dependents(session: AsyncSession) -> None:
    # A fund the holdings fixture doesn't know -> holdings no-op -> identity/prices blocked.
    ws = await _workspace(session, "Unknown")
    await _fund_with_holdings(session, ws, isin="ZZ00UNKNOWN0", holdings=[])
    await session.commit()
    fund_id = await _only_fund(session, ws.id)
    run = await ob.execute_onboarding_plan(session, fund_id=fund_id, source_mode="fixture")
    assert _stage_run(run, "constituent_identity").status == "blocked"
    assert _stage_run(run, "constituent_prices").status == "blocked"


async def test_parent_run_message_and_child_correlation(session: AsyncSession) -> None:
    run = await ob.execute_onboarding_plan(session, workspace_id=1, source_mode="fixture")
    parent = await session.get(JobRun, run.parent_job_run_id)
    assert parent is not None
    assert parent.job_type == "instrument_onboarding"
    assert parent.source == "fixture"
    assert "constituent_identity=" in parent.message
    # Each child run id named in a stage exists in job_runs and is a real worker run.
    child_ids = [i for sr in run.stages for i in sr.child_run_ids]
    assert child_ids
    for cid in child_ids:
        child = await session.get(JobRun, cid)
        assert child is not None
        assert child.id != parent.id


async def test_skip_flags(session: AsyncSession) -> None:
    run = await ob.execute_onboarding_plan(
        session, workspace_id=1, source_mode="fixture", skip_exposure=True, skip_alerts=True
    )
    assert _stage_run(run, "exposure_recompute").status == "skipped"
    assert _stage_run(run, "alerts").status == "skipped"


# --- worker / scheduler compatibility ----------------------------------------


async def test_worker_no_scope_runs_all_workspaces(session: AsyncSession) -> None:
    run = await run_job(session, "instrument_onboarding", source_mode="fixture")
    assert run.job_type == "instrument_onboarding"
    assert run.status in ("success", "partial_success")
    # An umbrella run references per-workspace onboarding runs.
    assert "onboarding_runs=" in run.message


async def test_worker_plan_only_writes_no_job_run(session: AsyncSession) -> None:
    before = len((await session.execute(select(JobRun))).scalars().all())
    run = await run_job(session, "instrument_onboarding", workspace_id=1, plan_only=True)
    assert run.status == "planned"
    assert run.id is None  # transient, not persisted
    after = len((await session.execute(select(JobRun))).scalars().all())
    assert after == before


async def test_job_trigger_runs_onboarding(client: AsyncClient, session: AsyncSession) -> None:
    from app.db.models import ScheduledJob

    # The conftest seed does not include the onboarding scheduled job, so create a
    # manual one and trigger it via the jobs API (exercises run_job dispatch).
    sj = ScheduledJob(
        name="instrument_onboarding", job_type="instrument_onboarding", schedule_kind="manual"
    )
    session.add(sj)
    await session.commit()
    resp = await client.post(f"/api/v1/jobs/{sj.id}/run")
    assert resp.status_code == 201
    body = resp.json()
    assert body["job_type"] == "instrument_onboarding"
    assert body["status"] in ("success", "partial_success")


# --- API ----------------------------------------------------------------------


async def test_api_workspace_plan(client: AsyncClient) -> None:
    body = (await client.get("/api/v1/workspaces/1/onboarding/plan")).json()
    assert body["scope"] == "workspace"
    assert body["source_mode"] == "fixture"
    assert body["status"] in ("needs_work", "ready", "blocked")
    assert any(st["name"] == "constituent_identity" for st in body["stages"])
    assert "readiness" in body


async def test_api_workspace_run(client: AsyncClient) -> None:
    body = (await client.post("/api/v1/workspaces/1/onboarding/run")).json()
    assert body["status"] in ("success", "partial_success")
    assert body["parent_job_run_id"]
    assert body["source_mode"] == "fixture"
    names = {sr["name"] for sr in body["stages"]}
    assert {"holdings", "constituent_identity", "exposure_recompute"} <= names


async def test_api_workspace_run_plan_only(client: AsyncClient, session: AsyncSession) -> None:
    before = len((await session.execute(select(JobRun))).scalars().all())
    body = (await client.post("/api/v1/workspaces/1/onboarding/run?plan_only=true")).json()
    assert body["status"] == "planned"
    assert body["parent_job_run_id"] is None
    after = len((await session.execute(select(JobRun))).scalars().all())
    assert after == before


async def test_api_fund_plan_and_run(client: AsyncClient) -> None:
    plan = (await client.get("/api/v1/funds/1/onboarding/plan")).json()
    assert plan["scope"] == "fund"
    run = (await client.post("/api/v1/funds/1/onboarding/run")).json()
    assert run["scope"] == "fund"
    assert run["status"] in ("success", "partial_success")


async def test_api_status_endpoint(client: AsyncClient) -> None:
    body = (await client.get("/api/v1/workspaces/1/onboarding/status")).json()
    assert body["workspace_id"] == 1
    assert "readiness" in body
    assert body["status"] in ("needs_work", "ready", "blocked", "empty")


async def test_api_dashboard_includes_onboarding(client: AsyncClient) -> None:
    body = (await client.get("/api/v1/workspaces/1/dashboard")).json()
    assert "onboarding" in body
    assert "readiness" in body["onboarding"]
    assert body["onboarding"]["status"] in ("needs_work", "ready", "blocked", "empty")


async def test_api_diagnostics_onboarding_counts(client: AsyncClient) -> None:
    body = (await client.get("/api/v1/workspaces/1/diagnostics")).json()
    # Seeded workspace needs identity resolution -> at least one needed stage.
    assert body["onboarding_needed_stages"] >= 1
    assert "onboarding_blocked_stages" in body
    assert "onboarding_source_budget_blocked" in body


async def test_api_capabilities_lists_onboarding(client: AsyncClient) -> None:
    body = (await client.get("/api/v1/capabilities")).json()
    assert body["features"]["instrument_onboarding"] == "real"
    assert "instrument_onboarding" in body["workers"]["real"]


# --- safety -------------------------------------------------------------------


async def test_fixture_mode_makes_no_live_source_runs(session: AsyncSession) -> None:
    await ob.execute_onboarding_plan(session, workspace_id=1, source_mode="fixture")
    sources = set((await session.execute(select(JobRun.source))).scalars().all())
    assert not (sources & _LIVE_SOURCES)


async def test_default_source_mode_is_fixture(session: AsyncSession) -> None:
    plan = await ob.build_onboarding_plan(session, workspace_id=1)
    assert plan.source_mode == "fixture"
    for st in plan.stages:
        if st.source is not None:
            assert st.expected_offline is True


async def test_live_mode_warns_for_missing_adapters(session: AsyncSession) -> None:
    plan = await ob.build_onboarding_plan(session, workspace_id=1, source_mode="live")
    assert plan.source_mode == "live"
    # Holdings + FX have no enabled live adapter -> a warning each.
    assert any("holdings" in w for w in plan.warnings)
    assert any("fx" in w for w in plan.warnings)
    # Identity uses the live OpenFIGI source (not offline) when needed.
    identity = _stage(plan, "constituent_identity")
    if identity.status == "needed":
        assert identity.source == "openfigi"
        assert identity.expected_offline is False


# --- helpers -----------------------------------------------------------------


async def _only_fund(session: AsyncSession, workspace_id: int) -> int:
    row = await session.scalar(
        select(FundListing.fund_id)
        .join(PortfolioPosition, PortfolioPosition.fund_listing_id == FundListing.id)
        .where(PortfolioPosition.workspace_id == workspace_id)
        .limit(1)
    )
    assert row is not None
    return row
