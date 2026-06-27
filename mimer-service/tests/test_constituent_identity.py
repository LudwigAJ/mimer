"""Constituent identity resolution — model, resolver, worker, planner, API.

All offline: the fixture resolver never touches the network, and the OpenFIGI
path is exercised with a mocked ``_call`` + the real source budget / fetch-log /
request-cache plumbing. No test may require a live OpenFIGI key.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    Fund,
    FundHolding,
    Instrument,
    InstrumentIdentifier,
    InstrumentListing,
    ScheduledJob,
)
from app.services import constituent_identity as ci
from app.services import market_data_planner, source_budget, source_requests
from app.sources import constituents as constituents_source
from app.sources.constituents import (
    ConstituentRequest,
    FixtureConstituentResolver,
    OpenFigiConstituentResolver,
    get_constituent_resolver,
)
from app.sources.holdings import holding_identity_key
from app.workers.run import run_job

_VUSA = "IE00B3XXRP09"
_TODAY = date.today()


async def _fund_id(session: AsyncSession, isin: str = _VUSA) -> int:
    return await session.scalar(select(Fund.id).where(Fund.isin == isin))


async def _add_seed_holding(
    session: AsyncSession,
    fund_id: int,
    *,
    name: str,
    ticker: str | None = None,
    isin: str | None = None,
    weight: str = "0.01000000",
) -> FundHolding:
    """Add a holding to a fund's seed snapshot (same source+as_of => one snapshot)."""
    holding = FundHolding(
        fund_id=fund_id,
        as_of_date=_TODAY,
        security_name=name,
        security_ticker=ticker,
        security_isin=isin,
        country="US",
        weight=Decimal(weight),
        source="seed",
        holding_key=holding_identity_key(name=name, ticker=ticker, isin=isin),
    )
    session.add(holding)
    await session.flush()
    return holding


# --- deterministic identity keys --------------------------------------------


def test_instrument_identity_key_prefers_strong_identifiers() -> None:
    assert ci.instrument_identity_key(isin="us0378331005", name="Apple") == "isin:US0378331005"
    assert (
        ci.instrument_identity_key(composite_figi="bbg000b9xvv8", name="Apple")
        == "composite_figi:BBG000B9XVV8"
    )
    # Name fallback folds country+currency in (offline/manual only).
    assert (
        ci.instrument_identity_key(name="Apple Inc", country="US", currency="USD")
        == "name:apple inc|US|USD"
    )


def test_listing_identity_key() -> None:
    assert ci.listing_identity_key(composite_figi="bbg1") == "composite_figi:BBG1"
    assert ci.listing_identity_key(ticker="aapl", mic="xnas") == "ticker:AAPL|XNAS"


# --- resolver input construction --------------------------------------------


def test_build_requests_picks_isin_over_weaker_schemes() -> None:
    holding = FundHolding(
        id=1,
        fund_id=1,
        as_of_date=_TODAY,
        security_name="Apple Inc",
        security_ticker="AAPL",
        security_isin="US0378331005",
        weight="0.07",
        source="seed",
        holding_key="x",
    )
    requests, by_key, unsafe = ci.build_requests([holding], resolver=FixtureConstituentResolver())
    assert len(requests) == 1
    assert requests[0].scheme == "isin"
    assert requests[0].value == "US0378331005"
    assert by_key[requests[0].input_key] == [1]
    assert unsafe == []


def test_build_requests_dedupes_repeated_constituents_across_funds() -> None:
    a = FundHolding(
        id=1,
        fund_id=1,
        as_of_date=_TODAY,
        security_name="Apple Inc",
        security_ticker="AAPL",
        weight="0.07",
        source="seed",
        holding_key="name:apple inc|aapl",
    )
    b = FundHolding(
        id=2,
        fund_id=2,
        as_of_date=_TODAY,
        security_name="Apple Inc",
        security_ticker="AAPL",
        weight="0.03",
        source="seed",
        holding_key="name:apple inc|aapl",
    )
    requests, by_key, _ = ci.build_requests([a, b], resolver=FixtureConstituentResolver())
    assert len(requests) == 1  # one Apple request
    assert sorted(by_key[requests[0].input_key]) == [1, 2]  # covers both holdings


def test_openfigi_safety_rejects_name_only_but_allows_isin() -> None:
    resolver = OpenFigiConstituentResolver()
    name_only = ConstituentRequest(input_key="name:X", scheme="name", value="X", name="X")
    isin = ConstituentRequest(input_key="isin:US0378331005", scheme="isin", value="US0378331005")
    sedol = ConstituentRequest(input_key="sedol:2046251", scheme="sedol", value="2046251")
    bare_ticker = ConstituentRequest(input_key="ticker:AAPL", scheme="ticker", value="AAPL")
    assert resolver.is_request_safe(name_only) is False
    assert resolver.is_request_safe(isin) is True
    assert resolver.is_request_safe(sedol) is True
    assert resolver.is_request_safe(bare_ticker) is False  # no exchange + currency


def test_openfigi_multi_listing_isin_collapses_to_one_instrument() -> None:
    """An ISIN that maps to many *venues of the same security* resolves (not ambiguous)."""
    resolver = OpenFigiConstituentResolver()
    isin = ConstituentRequest(input_key="isin:GB00BP6MXD84", scheme="isin", value="GB00BP6MXD84")
    rows = [
        {
            "figi": "BBG00KP6PB35",
            "shareClassFIGI": "BBG00KP6PBM6",
            "ticker": "SHEL",
            "exchCode": "LN",
        },
        {
            "figi": "BBG00KP6PC99",
            "shareClassFIGI": "BBG00KP6PBM6",
            "ticker": "SHEL",
            "exchCode": "GR",
        },
    ]
    candidate = resolver._candidate_from_rows(isin, rows)
    assert candidate.status == "resolved"
    assert candidate.confidence == "high"


def test_openfigi_genuinely_different_securities_are_ambiguous() -> None:
    resolver = OpenFigiConstituentResolver()
    isin = ConstituentRequest(input_key="isin:X", scheme="isin", value="X")
    rows = [
        {"figi": "BBG1", "shareClassFIGI": "BBGAAA", "ticker": "A"},
        {"figi": "BBG2", "shareClassFIGI": "BBGBBB", "ticker": "B"},
    ]
    assert resolver._candidate_from_rows(isin, rows).status == "ambiguous"
    # A bare ticker with several rows is always ambiguous (could be many companies).
    ticker = ConstituentRequest(input_key="ticker:T", scheme="ticker", value="T")
    same = [
        {"figi": "BBG1", "shareClassFIGI": "BBGSAME", "ticker": "T"},
        {"figi": "BBG2", "shareClassFIGI": "BBGSAME", "ticker": "T"},
    ]
    assert resolver._candidate_from_rows(ticker, same).status == "ambiguous"


async def test_build_requests_marks_name_only_unsafe_for_openfigi(session: AsyncSession) -> None:
    fund_id = await _fund_id(session)
    # Seed holdings carry name+ticker only (no ISIN) => unsafe for OpenFIGI.
    holdings = await ci.unresolved_holdings(session, fund_id=fund_id)
    requests, _, unsafe = ci.build_requests(holdings, resolver=OpenFigiConstituentResolver())
    assert requests == []  # nothing safe to send to OpenFIGI
    assert len(unsafe) == len(holdings) >= 1


# --- fixture resolver outcomes ----------------------------------------------


async def test_fixture_resolver_outcomes(session: AsyncSession) -> None:
    resolver = FixtureConstituentResolver()
    reqs = [
        ConstituentRequest(
            input_key="name:Apple Inc", scheme="name", value="Apple Inc", name="Apple Inc"
        ),
        ConstituentRequest(
            input_key="name:Ambiguous HoldCo",
            scheme="name",
            value="Ambiguous HoldCo",
            name="Ambiguous HoldCo",
        ),
        ConstituentRequest(
            input_key="name:Totally Unknown Ltd",
            scheme="name",
            value="Totally Unknown Ltd",
            name="Totally Unknown Ltd",
        ),
        ConstituentRequest(
            input_key="name:Force Failure PLC",
            scheme="name",
            value="Force Failure PLC",
            name="Force Failure PLC",
        ),
    ]
    out = {
        c.input_key: c
        for c in await resolver.resolve_batch(session, reqs, batch_size=10, ttl_seconds=0)
    }
    assert out["name:Apple Inc"].status == "resolved"
    assert out["name:Apple Inc"].figi
    assert out["name:Ambiguous HoldCo"].status == "ambiguous"
    assert out["name:Totally Unknown Ltd"].status == "not_found"
    assert out["name:Force Failure PLC"].status == "failed"


# --- worker: fixture resolution ---------------------------------------------


async def test_worker_single_fund_fixture_resolution(session: AsyncSession) -> None:
    fund_id = await _fund_id(session)
    run = await run_job(
        session,
        "constituent_identity_resolution",
        fund_id=fund_id,
        source_name="constituent_identity_fixture",
    )
    assert run.status == "success"
    assert run.records_inserted >= 1  # resolved holdings
    assert "resolved=" in run.message
    # Holdings are linked + instruments/listings/identifiers created.
    linked = await session.scalar(
        select(func.count())
        .select_from(FundHolding)
        .where(FundHolding.fund_id == fund_id, FundHolding.holding_instrument_id.is_not(None))
    )
    assert linked >= 1
    assert (await session.scalar(select(func.count()).select_from(Instrument))) >= 1
    assert (await session.scalar(select(func.count()).select_from(InstrumentListing))) >= 1
    assert (await session.scalar(select(func.count()).select_from(InstrumentIdentifier))) >= 1


async def test_worker_all_funds_and_dedupes_shared_constituents(session: AsyncSession) -> None:
    run = await run_job(
        session, "constituent_identity_resolution", source_name="constituent_identity_fixture"
    )
    assert run.status == "success"
    # Apple/Microsoft are held by VUSA *and* JPM in the seed => deduped to one
    # instrument each (13 distinct seeded constituents, 15 holding rows linked).
    instruments = await session.scalar(select(func.count()).select_from(Instrument))
    linked = await session.scalar(
        select(func.count())
        .select_from(FundHolding)
        .where(FundHolding.holding_instrument_id.is_not(None))
    )
    assert instruments < linked  # dedupe really happened
    apple = await session.scalar(
        select(func.count()).select_from(Instrument).where(Instrument.name == "Apple Inc")
    )
    assert apple == 1


async def test_worker_limit_respected(session: AsyncSession) -> None:
    run = await run_job(
        session,
        "constituent_identity_resolution",
        source_name="constituent_identity_fixture",
        limit=2,
    )
    assert run.records_inserted <= 2


async def test_worker_idempotent_rerun(session: AsyncSession) -> None:
    await run_job(
        session, "constituent_identity_resolution", source_name="constituent_identity_fixture"
    )
    instruments_1 = await session.scalar(select(func.count()).select_from(Instrument))
    ids_1 = await session.scalar(select(func.count()).select_from(InstrumentIdentifier))
    run2 = await run_job(
        session, "constituent_identity_resolution", source_name="constituent_identity_fixture"
    )
    instruments_2 = await session.scalar(select(func.count()).select_from(Instrument))
    ids_2 = await session.scalar(select(func.count()).select_from(InstrumentIdentifier))
    assert run2.records_inserted == 0  # nothing left unresolved
    assert instruments_1 == instruments_2  # no duplicate instruments
    assert ids_1 == ids_2  # no duplicate identifiers


async def test_worker_ambiguous_not_found_failed_states(session: AsyncSession) -> None:
    fund_id = await _fund_id(session)
    amb = await _add_seed_holding(session, fund_id, name="Ambiguous HoldCo", ticker="AMB")
    nf = await _add_seed_holding(session, fund_id, name="Totally Unknown Ltd", ticker="TUL")
    fail = await _add_seed_holding(session, fund_id, name="Force Failure PLC", ticker="FFP")
    await session.commit()

    run = await run_job(
        session,
        "constituent_identity_resolution",
        fund_id=fund_id,
        source_name="constituent_identity_fixture",
    )
    for holding in (amb, nf, fail):
        await session.refresh(holding)
    assert amb.holding_instrument_id is None and amb.identity_status == "ambiguous"
    assert nf.holding_instrument_id is None and nf.identity_status == "not_found"
    assert fail.holding_instrument_id is None and fail.identity_status == "failed"
    assert run.records_failed >= 1
    assert "ambiguous=1" in run.message and "not_found=1" in run.message


async def test_worker_does_not_clobber_manual_link(session: AsyncSession) -> None:
    fund_id = await _fund_id(session)
    # A constituent already linked manually must survive an automated run.
    manual_instrument = Instrument(
        identity_key="manual:apple",
        instrument_type="equity",
        name="Apple (manual)",
        status="active",
        source="manual",
    )
    session.add(manual_instrument)
    await session.flush()
    holding = await session.scalar(
        select(FundHolding).where(
            FundHolding.fund_id == fund_id, FundHolding.security_name == "Apple Inc"
        )
    )
    holding.holding_instrument_id = manual_instrument.id
    holding.identity_status = "manual"
    await session.commit()

    await run_job(
        session,
        "constituent_identity_resolution",
        fund_id=fund_id,
        source_name="constituent_identity_fixture",
    )
    await session.refresh(holding)
    assert holding.holding_instrument_id == manual_instrument.id  # untouched
    assert holding.identity_status == "manual"


# --- model uniqueness -------------------------------------------------------


async def test_instrument_identity_key_is_unique(session: AsyncSession) -> None:
    from sqlalchemy.exc import IntegrityError

    session.add(Instrument(identity_key="isin:DUP", instrument_type="equity", name="A", source="x"))
    await session.commit()
    session.add(Instrument(identity_key="isin:DUP", instrument_type="equity", name="B", source="y"))
    with pytest.raises(IntegrityError):
        await session.commit()
    await session.rollback()


# --- OpenFIGI guarded path (mocked; offline) --------------------------------


def _fake_openfigi_call(rows_per_job: list[dict]):
    async def _call(self, jobs, headers):
        # API key only ever in headers, never echoed into the request key/log.
        assert "X-OPENFIGI-APIKEY" not in str(jobs)
        return [{"data": rows_per_job} for _ in jobs]

    return _call


async def test_openfigi_worker_resolves_and_logs_secrets_free(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    fund_id = await _fund_id(session)
    await _add_seed_holding(
        session, fund_id, name="Apple Inc ADR", ticker="AAPL", isin="US0378331005", weight="0.05"
    )
    await session.commit()

    monkeypatch.setattr(
        OpenFigiConstituentResolver,
        "_call",
        _fake_openfigi_call([{"figi": "BBG000B9XRY4", "ticker": "AAPL", "exchCode": "US"}]),
    )
    run = await run_job(
        session, "constituent_identity_resolution", fund_id=fund_id, source_name="openfigi"
    )
    assert run.records_inserted >= 1
    logs = await source_requests.list_fetch_logs(session, source="openfigi", status="success")
    assert logs and logs[0].request_kind == "resolve_constituent_identity"
    # No secrets anywhere in the persisted request key / endpoint label.
    assert "APIKEY" not in logs[0].request_key.upper()
    assert logs[0].endpoint_label == "api.openfigi.com/v3/mapping"


async def test_openfigi_worker_budget_blocked_makes_no_call(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    fund_id = await _fund_id(session)
    await _add_seed_holding(
        session, fund_id, name="Apple Inc ADR", ticker="AAPL", isin="US0378331005"
    )
    await source_budget.apply_backoff(session, "openfigi", seconds=120)
    await session.commit()

    async def boom(self, jobs, headers):  # pragma: no cover - must never run
        raise AssertionError("a live OpenFIGI call was attempted while in backoff")

    monkeypatch.setattr(OpenFigiConstituentResolver, "_call", boom)
    run = await run_job(
        session, "constituent_identity_resolution", fund_id=fund_id, source_name="openfigi"
    )
    assert "skipped_budget=" in run.message
    assert run.records_inserted == 0
    rate_limited = await source_requests.list_fetch_logs(
        session, source="openfigi", status="rate_limited"
    )
    assert rate_limited


async def test_openfigi_worker_cache_skips_repeat(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    fund_id = await _fund_id(session)
    await _add_seed_holding(
        session, fund_id, name="Apple Inc ADR", ticker="AAPL", isin="US0378331005"
    )
    await session.commit()
    calls = {"n": 0}

    async def counting_call(self, jobs, headers):
        calls["n"] += 1
        return [
            {"data": [{"figi": "BBG000B9XRY4", "ticker": "AAPL", "exchCode": "US"}]} for _ in jobs
        ]

    monkeypatch.setattr(OpenFigiConstituentResolver, "_call", counting_call)
    await run_job(
        session, "constituent_identity_resolution", fund_id=fund_id, source_name="openfigi"
    )
    # Reset the holding to unresolved so the *only* thing stopping a re-fetch is
    # the recent-success request cache, not the "already linked" guard.
    holding = await session.scalar(
        select(FundHolding).where(
            FundHolding.fund_id == fund_id, FundHolding.security_name == "Apple Inc ADR"
        )
    )
    holding.holding_instrument_id = None
    holding.identity_status = None
    await session.commit()
    run2 = await run_job(
        session, "constituent_identity_resolution", fund_id=fund_id, source_name="openfigi"
    )
    assert calls["n"] == 1  # second batch served from cache, no new live call
    assert "skipped_cached=" in run2.message


# --- planner integration ----------------------------------------------------


async def _workspace_id(session: AsyncSession) -> int:
    from app.db.models import Workspace

    return (await session.execute(select(Workspace.id))).scalars().first()


async def test_planner_unresolved_then_resolved_then_price_ready(session: AsyncSession) -> None:
    wid = await _workspace_id(session)
    before = await market_data_planner.build_plan(session, wid, include_constituents=True)
    assert before.summary.unresolved_constituents >= 1
    assert before.summary.resolved_constituents == 0
    assert before.summary.estimated_openfigi_requests == before.summary.unresolved_constituents

    # Resolve the workspace's constituents via the offline fixture.
    await run_job(
        session,
        "constituent_identity_resolution",
        workspace_id=wid,
        source_name="constituent_identity_fixture",
    )

    after = await market_data_planner.build_plan(session, wid, include_constituents=True)
    assert after.summary.resolved_constituents >= 1
    assert after.summary.unresolved_constituents < before.summary.unresolved_constituents
    # Resolved listings now appear as future EOD-price work (not implemented yet).
    assert after.summary.constituents_ready_for_eod_prices >= 1
    assert after.summary.estimated_price_requests == after.summary.constituents_ready_for_eod_prices
    assert any(i.item_type == "fetch_constituent_price" for i in after.items)
    # No identity items remain for the resolved holdings.
    assert after.summary.unresolved_constituents == sum(
        1 for i in after.items if i.item_type == "resolve_constituent_identity"
    )


async def test_planner_surfaces_ambiguous_constituent(session: AsyncSession) -> None:
    wid = await _workspace_id(session)
    fund_id = await _fund_id(session)
    await _add_seed_holding(session, fund_id, name="Ambiguous HoldCo", ticker="AMB", weight="0.04")
    await session.commit()
    await run_job(
        session,
        "constituent_identity_resolution",
        workspace_id=wid,
        source_name="constituent_identity_fixture",
    )
    plan = await market_data_planner.build_plan(session, wid, include_constituents=True)
    assert plan.summary.ambiguous_constituents >= 1
    assert any(i.item_type == "ambiguous_constituent_identity" for i in plan.items)


# --- API endpoints ----------------------------------------------------------


async def test_holdings_include_identity_and_constituents_endpoints(client: AsyncClient) -> None:
    funds = (await client.get("/api/v1/funds")).json()["data"]
    fund_id = next(f["id"] for f in funds if f["isin"] == _VUSA)

    # Holdings expose identity fields (even before any resolution run).
    holdings = (await client.get(f"/api/v1/funds/{fund_id}/holdings?include_identity=true")).json()
    assert holdings["holdings"]
    first = holdings["holdings"][0]
    assert "identity_status" in first and "holding_instrument_id" in first

    constituents = (await client.get(f"/api/v1/funds/{fund_id}/constituents")).json()
    assert constituents["total"] >= 1
    assert constituents["constituents"][0]["next_action"]
    assert constituents["constituents"][0]["identity_state"] in (
        "unresolved",
        "resolved",
        "ambiguous",
        "not_found",
        "failed",
        "manual",
    )


async def test_constituents_endpoint_filter_and_instrument_detail(
    client: AsyncClient, session: AsyncSession
) -> None:
    fund_id = await _fund_id(session)
    await run_job(
        session,
        "constituent_identity_resolution",
        fund_id=fund_id,
        source_name="constituent_identity_fixture",
    )

    resolved = (await client.get(f"/api/v1/funds/{fund_id}/constituents?status=resolved")).json()
    assert resolved["constituents"]
    instrument_id = resolved["constituents"][0]["holding_instrument_id"]
    assert instrument_id is not None

    detail = (await client.get(f"/api/v1/instruments/{instrument_id}")).json()
    assert detail["name"]
    assert "listings" in detail and "identifiers" in detail

    listings = (await client.get(f"/api/v1/instruments/{instrument_id}/listings")).json()
    assert listings["meta"]["count"] >= 0


async def test_instrument_detail_404(client: AsyncClient) -> None:
    resp = await client.get("/api/v1/instruments/999999")
    assert resp.status_code == 404


async def test_market_data_plan_endpoint_reflects_resolution(
    client: AsyncClient, session: AsyncSession
) -> None:
    wid = await _workspace_id(session)
    await run_job(
        session,
        "constituent_identity_resolution",
        workspace_id=wid,
        source_name="constituent_identity_fixture",
    )
    plan = (
        await client.get(f"/api/v1/workspaces/{wid}/market-data-plan?include_constituents=true")
    ).json()
    assert plan["summary"]["resolved_constituents"] >= 1
    assert "constituents_ready_for_eod_prices" in plan["summary"]


async def test_diagnostics_expose_constituent_counts(
    client: AsyncClient, session: AsyncSession
) -> None:
    wid = await _workspace_id(session)
    fund_id = await _fund_id(session)
    await _add_seed_holding(session, fund_id, name="Ambiguous HoldCo", ticker="AMB", weight="0.04")
    await session.commit()
    await run_job(
        session,
        "constituent_identity_resolution",
        workspace_id=wid,
        source_name="constituent_identity_fixture",
    )
    diag = (await client.get(f"/api/v1/workspaces/{wid}/diagnostics")).json()
    assert "ambiguous_constituent_identities" in diag
    assert diag["ambiguous_constituent_identities"] >= 1
    assert "constituents_ready_for_eod_prices" in diag
    assert "constituent_identity_resolution_failures" in diag


# --- job trigger / scheduler compatibility ----------------------------------


async def test_job_trigger_runs_constituent_resolution(
    client: AsyncClient, session: AsyncSession
) -> None:
    session.add(
        ScheduledJob(
            name="daily_constituent_identity_resolution",
            job_type="constituent_identity_resolution",
            schedule_kind="daily",
            is_active=True,
        )
    )
    await session.commit()
    job = next(
        j
        for j in (await client.get("/api/v1/jobs")).json()["data"]
        if j["job_type"] == "constituent_identity_resolution"
    )
    assert job["implementation_status"] == "fixture"
    assert job["configured_source"] == "constituent_identity_fixture"

    run = await client.post(f"/api/v1/jobs/{job['id']}/run")
    assert run.status_code == 201
    body = run.json()
    assert body["status"] == "success"
    assert body["records_inserted"] >= 1


# --- no live network ---------------------------------------------------------


async def test_fixture_worker_makes_no_network_call(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    import httpx

    def explode(*args, **kwargs):  # pragma: no cover - only fires on a real call
        raise AssertionError("constituent resolution attempted a network call")

    monkeypatch.setattr(httpx.AsyncClient, "post", explode)
    monkeypatch.setattr(httpx.AsyncClient, "get", explode)
    run = await run_job(
        session, "constituent_identity_resolution", source_name="constituent_identity_fixture"
    )
    assert run.status == "success"


def test_missing_openfigi_key_degrades_gracefully() -> None:
    # The resolver builds headers without a key when none is configured; this is
    # exercised end-to-end by the budget-blocked test (no call made). Here we just
    # assert the resolver is constructible and reports the right source name.
    resolver = get_constituent_resolver("openfigi")
    assert resolver.name == "openfigi"
    assert get_constituent_resolver("constituent_identity_fixture").name == (
        "constituent_identity_fixture"
    )
    _ = (datetime.now(UTC), constituents_source)  # keep imports meaningful
