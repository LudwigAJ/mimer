"""Imported-instrument resolution bridge — request construction, resolver safety,
linking/idempotency, position recompute, worker, API, planner, diagnostics.

All offline: the fixture resolver never touches the network and the OpenFIGI path
is never called (only its *safety* gate is exercised). No test requires a live
OpenFIGI key.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    Fund,
    Instrument,
    PortfolioPositionSnapshot,
    PortfolioTransaction,
    Workspace,
)
from app.schemas.broker_import import BrokerImportRequest
from app.services import broker_imports as broker_service
from app.services import imported_instrument_resolution as iir
from app.services import market_data_planner
from app.sources.constituents import (
    FixtureConstituentResolver,
    OpenFigiConstituentResolver,
)
from app.workers.run import run_job

TSLA_ISIN = "US88160R1014"
VUSA_ISIN = "IE00B3XXRP09"

# A mixed broker CSV of *directly-held* instruments:
#   TSLA  - resolvable by ISIN (fixture)
#   AAPL  - resolvable by ticker+name (fixture)
#   AMB   - resolves ambiguously (fixture: name "Ambiguous HoldCo")
#   ZZZZ  - not found (fixture)
#   <name-only> - no safe identifier (skipped_unsafe; never auto-created)
DIRECT_CSV = (
    "date,type,symbol,isin,name,quantity,price,net_amount,currency\n"
    f"2026-06-15,buy,TSLA,{TSLA_ISIN},Tesla Inc,5,210,-1050,USD\n"
    "2026-06-16,buy,AAPL,,Apple Inc,3,180,-540,USD\n"
    "2026-06-17,buy,AMB,,Ambiguous HoldCo,2,10,-20,USD\n"
    "2026-06-18,buy,ZZZZ,,Totally Unknown Ltd,1,5,-5,USD\n"
    "2026-06-19,buy,,,Some Name Only PLC,1,5,-5,GBP\n"
)


async def _workspace_id(session: AsyncSession) -> int:
    return (await session.scalar(select(Workspace.id).order_by(Workspace.id))) or 1


async def _commit(session: AsyncSession, wid: int, csv_text: str = DIRECT_CSV) -> None:
    await broker_service.commit_import(
        session, wid, request=BrokerImportRequest(csv_text=csv_text, source_filename="s.csv")
    )


def _txn(
    symbol=None, isin=None, figi=None, name=None, currency="USD", tid=1
) -> PortfolioTransaction:
    return PortfolioTransaction(
        id=tid,
        workspace_id=1,
        transaction_key=f"k{tid}",
        transaction_type="buy",
        trade_date=__import__("datetime").date(2026, 6, 15),
        symbol=symbol,
        isin=isin,
        figi=figi,
        name=name,
        currency=currency,
        status="unresolved_instrument",
        source="broker_csv",
    )


# --- request construction ----------------------------------------------------


def test_request_prefers_isin_then_figi_then_ticker() -> None:
    resolver = FixtureConstituentResolver()
    isin = _txn(symbol="TSLA", isin=TSLA_ISIN, tid=1)
    figi = _txn(symbol="TSLA", figi="BBG000N9MNX3", tid=2)
    ticker = _txn(symbol="TSLA", tid=3)
    plan = iir.build_requests([isin], resolver=resolver)
    assert plan.requests[0].scheme == "isin"
    plan = iir.build_requests([figi], resolver=resolver)
    assert plan.requests[0].scheme == "figi"
    plan = iir.build_requests([ticker], resolver=resolver)
    assert plan.requests[0].scheme == "ticker"


def test_name_only_is_unsafe_and_never_a_request() -> None:
    resolver = FixtureConstituentResolver()
    name_only = _txn(name="Some Name Only PLC", tid=1)
    plan = iir.build_requests([name_only], resolver=resolver)
    assert plan.requests == []
    assert plan.unsafe_txn_ids == [1]


def test_requests_are_deduped_and_cover_all_transactions() -> None:
    resolver = FixtureConstituentResolver()
    a = _txn(symbol="TSLA", isin=TSLA_ISIN, tid=1)
    b = _txn(symbol="TSLA", isin=TSLA_ISIN, tid=2)
    plan = iir.build_requests([a, b], resolver=resolver)
    assert len(plan.requests) == 1  # one TSLA request
    assert sorted(plan.txn_ids_by_key[plan.requests[0].input_key]) == [1, 2]


def test_openfigi_ticker_only_is_unsafe_but_isin_is_safe() -> None:
    resolver = OpenFigiConstituentResolver()
    ticker_only = _txn(symbol="AAPL", currency="USD", tid=1)  # no exchange/MIC
    isin = _txn(symbol="TSLA", isin=TSLA_ISIN, tid=2)
    plan = iir.build_requests([ticker_only, isin], resolver=resolver)
    # Only the ISIN row is safe to send to OpenFIGI; the bare ticker is left.
    assert [r.scheme for r in plan.requests] == ["isin"]
    assert plan.unsafe_txn_ids == [1]


# --- fixture resolver outcomes (via the service) -----------------------------


async def test_resolve_links_resolved_marks_ambiguous_leaves_others(session: AsyncSession) -> None:
    wid = await _workspace_id(session)
    await _commit(session, wid)
    result = await iir.resolve_imported_instruments(
        session, workspace_id=wid, source="constituent_identity_fixture"
    )
    await session.commit()

    by_symbol = {
        t.symbol: t
        for t in (
            await session.execute(
                select(PortfolioTransaction).where(PortfolioTransaction.workspace_id == wid)
            )
        )
        .scalars()
        .all()
    }
    assert by_symbol["TSLA"].status == "resolved"
    assert by_symbol["TSLA"].instrument_id is not None
    assert by_symbol["TSLA"].instrument_listing_id is not None
    assert by_symbol["AAPL"].status == "resolved"
    assert by_symbol["AMB"].status == "ambiguous_instrument"
    assert by_symbol["AMB"].instrument_id is None  # ambiguous never links
    assert by_symbol["ZZZZ"].status == "unresolved_instrument"  # not_found, not linked
    # The name-only row stays unresolved and is never auto-created.
    name_only = by_symbol[None]
    assert name_only.status == "unresolved_instrument"
    assert name_only.instrument_id is None

    assert result.linked == 2
    assert result.ambiguous == 1
    assert result.not_found == 1
    assert result.skipped_unsafe == 1
    assert result.instruments_created == 2


async def test_raw_symbol_name_isin_preserved_after_resolution(session: AsyncSession) -> None:
    wid = await _workspace_id(session)
    await _commit(session, wid)
    await iir.resolve_imported_instruments(
        session, workspace_id=wid, source="constituent_identity_fixture"
    )
    await session.commit()
    tsla = await session.scalar(
        select(PortfolioTransaction).where(PortfolioTransaction.symbol == "TSLA")
    )
    assert tsla.symbol == "TSLA" and tsla.isin == TSLA_ISIN and tsla.name == "Tesla Inc"


async def test_no_instruments_created_from_name_only(session: AsyncSession) -> None:
    wid = await _workspace_id(session)
    instruments_before = await session.scalar(select(func.count()).select_from(Instrument))
    funds_before = await session.scalar(select(func.count()).select_from(Fund))
    await _commit(
        session,
        wid,
        "date,type,name,quantity,price,net_amount,currency\n"
        "2026-06-19,buy,Some Name Only PLC,1,5,-5,GBP\n",
    )
    result = await iir.resolve_imported_instruments(
        session, workspace_id=wid, source="constituent_identity_fixture"
    )
    await session.commit()
    assert result.skipped_unsafe == 1
    assert result.linked == 0
    assert await session.scalar(select(func.count()).select_from(Instrument)) == instruments_before
    assert await session.scalar(select(func.count()).select_from(Fund)) == funds_before


# --- dry-run -----------------------------------------------------------------


async def test_dry_run_writes_nothing(session: AsyncSession) -> None:
    wid = await _workspace_id(session)
    await _commit(session, wid)
    instruments_before = await session.scalar(select(func.count()).select_from(Instrument))
    result = await iir.resolve_imported_instruments(
        session, workspace_id=wid, source="constituent_identity_fixture", dry_run=True
    )
    # Counts are reported...
    assert result.linked == 2
    assert result.transactions_selected == 5
    # ...but nothing is persisted.
    assert result.snapshot_created is False
    assert await session.scalar(select(func.count()).select_from(Instrument)) == instruments_before
    still_unresolved = await session.scalar(
        select(func.count())
        .select_from(PortfolioTransaction)
        .where(PortfolioTransaction.status == "unresolved_instrument")
    )
    assert still_unresolved == 5  # all 5 rows untouched (dry-run linked nothing)


# --- idempotency + existing-identity reuse -----------------------------------


async def test_idempotent_rerun_creates_no_duplicates(session: AsyncSession) -> None:
    wid = await _workspace_id(session)
    await _commit(session, wid)
    await iir.resolve_imported_instruments(
        session, workspace_id=wid, source="constituent_identity_fixture"
    )
    await session.commit()
    instruments_1 = await session.scalar(select(func.count()).select_from(Instrument))
    result2 = await iir.resolve_imported_instruments(
        session, workspace_id=wid, source="constituent_identity_fixture"
    )
    await session.commit()
    instruments_2 = await session.scalar(select(func.count()).select_from(Instrument))
    # Nothing newly *linked*: TSLA/AAPL already resolved, the rest are not retried.
    assert result2.linked == 0
    assert instruments_1 == instruments_2


async def test_existing_identity_reused_without_resolver(session: AsyncSession) -> None:
    from app.db.models import InstrumentIdentifier, InstrumentListing

    wid = await _workspace_id(session)
    # Commit a TSLA buy while no instrument exists yet => it is left unresolved.
    await _commit(
        session,
        wid,
        f"date,type,symbol,isin,name,quantity,price,net_amount,currency\n"
        f"2026-06-15,buy,TSLA,{TSLA_ISIN},Tesla Inc,5,210,-1050,USD\n",
    )
    # Simulate TSLA being resolved elsewhere (e.g. as an ETF constituent): the
    # canonical instrument + ISIN crosswalk + listing already exist.
    instrument = Instrument(
        identity_key=f"isin:{TSLA_ISIN}",
        instrument_type="equity",
        name="Tesla Inc",
        status="active",
        source="constituent_identity_fixture",
    )
    session.add(instrument)
    await session.flush()
    session.add(
        InstrumentIdentifier(
            instrument_id=instrument.id,
            scheme="isin",
            value=TSLA_ISIN,
            source="constituent_identity_fixture",
            status="active",
        )
    )
    session.add(
        InstrumentListing(
            instrument_id=instrument.id,
            listing_key="ticker:TSLA|XNAS",
            ticker="TSLA",
            mic="XNAS",
            currency="USD",
            source="constituent_identity_fixture",
            status="active",
        )
    )
    await session.commit()
    # The bridge links via *existing identity* (the ISIN crosswalk), no resolver.
    result = await iir.resolve_imported_instruments(
        session, workspace_id=wid, source="constituent_identity_fixture"
    )
    await session.commit()
    assert result.linked == 1
    assert result.linked_existing == 1  # linked via crosswalk, not the resolver
    assert result.instruments_created == 0  # no duplicate Tesla instrument


async def test_manual_link_is_not_clobbered(session: AsyncSession) -> None:
    wid = await _workspace_id(session)
    await _commit(session, wid)
    manual = Instrument(
        identity_key="manual:tesla",
        instrument_type="equity",
        name="Tesla (manual)",
        status="active",
        source="manual",
    )
    session.add(manual)
    await session.flush()
    tsla = await session.scalar(
        select(PortfolioTransaction).where(PortfolioTransaction.symbol == "TSLA")
    )
    tsla.instrument_id = manual.id
    tsla.status = "ready"  # a higher-priority (already-linked) status
    await session.commit()

    await iir.resolve_imported_instruments(
        session, workspace_id=wid, source="constituent_identity_fixture"
    )
    await session.commit()
    await session.refresh(tsla)
    # The manual link survives (a "ready" row is not in the retryable set).
    assert tsla.instrument_id == manual.id
    assert tsla.status == "ready"


# --- position reconciliation -------------------------------------------------


async def test_resolution_updates_position_rows_and_snapshot(session: AsyncSession) -> None:
    wid = await _workspace_id(session)
    await _commit(session, wid)
    before = await broker_service.reconcile_positions(session, wid)
    tsla_before = next(p for p in before.positions if p.symbol == "TSLA")
    assert tsla_before.resolution_status == "unresolved_instrument"
    assert tsla_before.instrument_id is None
    snapshots_before = await session.scalar(
        select(func.count()).select_from(PortfolioPositionSnapshot)
    )

    result = await iir.resolve_imported_instruments(
        session, workspace_id=wid, source="constituent_identity_fixture"
    )
    await session.commit()
    assert result.snapshot_created is True
    snapshots_after = await session.scalar(
        select(func.count()).select_from(PortfolioPositionSnapshot)
    )
    assert snapshots_after == snapshots_before + 1  # a new snapshot after relink

    after = await broker_service.reconcile_positions(session, wid)
    tsla_after = next(
        p for p in after.positions if p.instrument_id is not None and p.symbol == "TSLA"
    )
    assert tsla_after.resolution_status == "resolved"
    assert tsla_after.quantity == Decimal("5")
    # AMB/ZZZZ/name-only are still unresolved => the snapshot stays partial.
    assert after.unresolved_count >= 1


# --- worker ------------------------------------------------------------------


async def test_worker_workspace_scope(session: AsyncSession) -> None:
    wid = await _workspace_id(session)
    await _commit(session, wid)
    run = await run_job(
        session,
        "imported_instrument_resolution",
        workspace_id=wid,
        source_name="constituent_identity_fixture",
    )
    assert run.status == "success"
    assert run.records_inserted == 2  # linked
    assert run.records_updated == 2  # ambiguous + not_found
    assert "linked=2" in run.message
    assert "skipped_unsafe=1" in run.message


async def test_worker_broker_import_scope(session: AsyncSession) -> None:
    wid = await _workspace_id(session)
    result = await broker_service.commit_import(
        session, wid, request=BrokerImportRequest(csv_text=DIRECT_CSV, source_filename="s.csv")
    )
    import_id = result.import_id
    run = await run_job(
        session,
        "imported_instrument_resolution",
        broker_import_id=import_id,
        source_name="constituent_identity_fixture",
    )
    assert run.status == "success"
    assert run.records_inserted == 2


async def test_worker_transaction_scope(session: AsyncSession) -> None:
    wid = await _workspace_id(session)
    await _commit(session, wid)
    tsla = await session.scalar(
        select(PortfolioTransaction).where(PortfolioTransaction.symbol == "TSLA")
    )
    run = await run_job(
        session,
        "imported_instrument_resolution",
        transaction_id=tsla.id,
        source_name="constituent_identity_fixture",
    )
    assert run.status == "success"
    assert run.records_inserted == 1  # only the one targeted transaction
    await session.refresh(tsla)
    assert tsla.status == "resolved"


async def test_worker_no_unresolved_is_clean_success(session: AsyncSession) -> None:
    wid = await _workspace_id(session)
    run = await run_job(
        session,
        "imported_instrument_resolution",
        workspace_id=wid,
        source_name="constituent_identity_fixture",
    )
    assert run.status == "success"
    assert "No unresolved imported transactions" in run.message


# --- OpenFIGI safety (no live call) ------------------------------------------


async def test_openfigi_not_called_for_ticker_only(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    wid = await _workspace_id(session)
    # Only ticker-only direct holdings (no ISIN) => nothing safe for OpenFIGI.
    await _commit(
        session,
        wid,
        "date,type,symbol,name,quantity,price,net_amount,currency\n"
        "2026-06-16,buy,AAPL,Apple Inc,3,180,-540,USD\n",
    )

    async def boom(self, jobs, headers):  # pragma: no cover - must never run
        raise AssertionError("a live OpenFIGI call was attempted")

    monkeypatch.setattr(OpenFigiConstituentResolver, "_call", boom)
    result = await iir.resolve_imported_instruments(session, workspace_id=wid, source="openfigi")
    assert result.linked == 0
    assert result.skipped_unsafe == 1


async def test_fixture_path_makes_no_network_call(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    import httpx

    def explode(*args, **kwargs):  # pragma: no cover - only fires on a real call
        raise AssertionError("imported resolution attempted a network call")

    monkeypatch.setattr(httpx.AsyncClient, "post", explode)
    monkeypatch.setattr(httpx.AsyncClient, "get", explode)
    wid = await _workspace_id(session)
    await _commit(session, wid)
    result = await iir.resolve_imported_instruments(
        session, workspace_id=wid, source="constituent_identity_fixture"
    )
    assert result.linked == 2


# --- API ---------------------------------------------------------------------


async def test_api_unresolved_then_dry_run_then_commit(client: AsyncClient) -> None:
    body = {"broker_name": "generic_csv_v1", "source_filename": "s.csv", "csv_text": DIRECT_CSV}
    await client.post("/api/v1/workspaces/1/broker-imports/commit", json=body)

    unresolved = (await client.get("/api/v1/workspaces/1/transactions/unresolved")).json()
    assert unresolved["meta"]["count"] == 5  # TSLA/AAPL/AMB/ZZZZ/name-only

    dry = (
        await client.post(
            "/api/v1/workspaces/1/transactions/resolve",
            json={"source": "constituent_identity_fixture", "dry_run": True},
        )
    ).json()
    assert dry["dry_run"] is True
    assert dry["linked"] == 2
    # Dry-run wrote nothing: still unresolved.
    again = (await client.get("/api/v1/workspaces/1/transactions/unresolved")).json()
    assert again["meta"]["count"] == 5

    commit = (
        await client.post(
            "/api/v1/workspaces/1/transactions/resolve",
            json={"source": "constituent_identity_fixture", "dry_run": False},
        )
    ).json()
    assert commit["linked"] == 2
    assert commit["instruments_created"] == 2
    assert commit["snapshot_created"] is True


async def test_api_transactions_and_positions_show_links(client: AsyncClient) -> None:
    body = {"broker_name": "generic_csv_v1", "source_filename": "s.csv", "csv_text": DIRECT_CSV}
    await client.post("/api/v1/workspaces/1/broker-imports/commit", json=body)
    await client.post(
        "/api/v1/workspaces/1/transactions/resolve",
        json={"source": "constituent_identity_fixture"},
    )
    txns = (await client.get("/api/v1/workspaces/1/transactions?status=resolved")).json()
    assert txns["meta"]["count"] == 2
    assert all(t["instrument_id"] is not None for t in txns["data"])

    positions = (await client.get("/api/v1/workspaces/1/positions")).json()
    linked = [p for p in positions["positions"] if p["instrument_id"] is not None]
    assert linked  # at least TSLA / AAPL now carry an instrument link


async def test_api_transaction_detail_and_404(client: AsyncClient) -> None:
    body = {"broker_name": "generic_csv_v1", "source_filename": "s.csv", "csv_text": DIRECT_CSV}
    await client.post("/api/v1/workspaces/1/broker-imports/commit", json=body)
    listing = (await client.get("/api/v1/workspaces/1/transactions")).json()
    tid = listing["data"][0]["id"]
    detail = (await client.get(f"/api/v1/workspaces/1/transactions/{tid}")).json()
    assert detail["id"] == tid
    missing = await client.get("/api/v1/workspaces/1/transactions/999999")
    assert missing.status_code == 404


async def test_api_import_scoped_resolve(client: AsyncClient) -> None:
    body = {"broker_name": "generic_csv_v1", "source_filename": "s.csv", "csv_text": DIRECT_CSV}
    commit = (await client.post("/api/v1/workspaces/1/broker-imports/commit", json=body)).json()
    import_id = commit["import_id"]
    resolved = (
        await client.post(
            f"/api/v1/workspaces/1/broker-imports/{import_id}/resolve",
            json={"source": "constituent_identity_fixture"},
        )
    ).json()
    assert resolved["linked"] == 2


async def test_api_diagnostics_surface_imported_fields(client: AsyncClient) -> None:
    body = {"broker_name": "generic_csv_v1", "source_filename": "s.csv", "csv_text": DIRECT_CSV}
    await client.post("/api/v1/workspaces/1/broker-imports/commit", json=body)
    await client.post(
        "/api/v1/workspaces/1/transactions/resolve",
        json={"source": "constituent_identity_fixture"},
    )
    diag = (await client.get("/api/v1/workspaces/1/diagnostics")).json()
    assert diag["ambiguous_import_transactions"] == 1
    assert diag["imported_instruments_ready_for_prices"] == 2
    assert "missing_imported_instrument_prices" in diag
    assert "imported_instrument_resolution_failures" in diag


async def test_api_capabilities_mark_imported_resolution(client: AsyncClient) -> None:
    body = (await client.get("/api/v1/capabilities")).json()
    assert body["features"]["imported_instrument_resolution"] == "fixture"
    assert body["features"]["imported_instrument_planner"] == "real"


# --- planner integration -----------------------------------------------------


async def test_planner_surfaces_imported_resolve_and_price_items(session: AsyncSession) -> None:
    wid = await _workspace_id(session)
    await _commit(session, wid)
    before = await market_data_planner.build_plan(session, wid, include_constituents=True)
    assert before.summary.imported_unresolved_instruments >= 1
    assert any(i.item_type == "resolve_imported_instrument" for i in before.items)
    assert any(i.item_type == "manual_review_imported_instrument" for i in before.items)
    # Items stay in priority order (dedupe + bound invariant preserved).
    priorities = [i.priority for i in before.items]
    assert priorities == sorted(priorities)

    await iir.resolve_imported_instruments(
        session, workspace_id=wid, source="constituent_identity_fixture"
    )
    await session.commit()
    after = await market_data_planner.build_plan(session, wid, include_constituents=True)
    # Resolved imported listings now show as price work; ambiguous shows as manual.
    assert after.summary.imported_ready_for_prices >= 1
    assert any(i.item_type == "fetch_imported_instrument_price" for i in after.items)
    assert any(i.item_type == "ambiguous_imported_instrument" for i in after.items)
    assert after.summary.imported_unresolved_instruments < (
        before.summary.imported_unresolved_instruments
    )


async def test_planner_imported_fx_item_for_usd(session: AsyncSession) -> None:
    wid = await _workspace_id(session)
    # Remove GBP<->USD rates so the USD direct holdings have no FX path to base.
    from app.db.models import FxRate

    for rate in (await session.execute(select(FxRate))).scalars().all():
        if "USD" in (rate.base_currency, rate.quote_currency):
            await session.delete(rate)
    await _commit(session, wid)
    await session.commit()
    plan = await market_data_planner.build_plan(session, wid, include_constituents=True)
    fx_items = [
        i
        for i in plan.items
        if i.item_type in ("fetch_imported_fx_rate", "fetch_fx_rate") and "USD" in (i.label or "")
    ]
    assert fx_items, "a USD direct holding with no FX path should produce an FX item"


# --- duplicate-file resolution scope -----------------------------------------


async def test_resolve_requires_a_scope(session: AsyncSession) -> None:
    from app.core.errors import NotFoundError

    with pytest.raises(NotFoundError):
        await iir.resolve_imported_instruments(session, source="constituent_identity_fixture")
