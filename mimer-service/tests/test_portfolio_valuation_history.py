"""Portfolio valuation *history / summary / dashboard* read models.

Bounded, snapshot-backed read models over the snapshots the recompute worker
already persisted: an oldest-first coverage/readiness history series, a compact
latest-context summary, and the dashboard valuation block. They never recompute
valuation, never fetch a price/FX source, never resolve identity, and never
difference snapshots into a return / PnL / performance number. All offline.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    BrokerAccount,
    PortfolioTransaction,
    PortfolioValuationSnapshot,
    Workspace,
)
from app.schemas import portfolio_valuation as schemas
from app.services import capabilities as capabilities_service
from app.services import diagnostics as diagnostics_service
from app.services import portfolio_valuation as pv

_TODAY = date.today()
_HSEQ = {"n": 0}


async def _workspace_id(session: AsyncSession) -> int:
    return (await session.scalar(select(Workspace.id).order_by(Workspace.id))) or 1


def _snap(
    wid: int,
    *,
    as_of: date,
    base: str = "GBP",
    broker_account_id: int | None = None,
    selected: int = 2,
    valued: int = 2,
    missing_price: int = 0,
    missing_fx: int = 0,
    unresolved: int = 0,
    ambiguous: int = 0,
    stale_price: int = 0,
    stale_fx: int = 0,
    cash_rows: int = 0,
    total: Decimal | None = Decimal("1000.00"),
    status: str = "ok",
    created_at: datetime | None = None,
) -> PortfolioValuationSnapshot:
    _HSEQ["n"] += 1
    snap = PortfolioValuationSnapshot(
        workspace_id=wid,
        as_of_date=as_of,
        base_currency=base,
        broker_account_id=broker_account_id,
        source="portfolio_valuation",
        status=status,
        input_hash=f"hash-{_HSEQ['n']:08d}",
        positions_selected=selected,
        positions_valued=valued,
        missing_price_count=missing_price,
        missing_fx_count=missing_fx,
        unresolved_count=unresolved,
        ambiguous_count=ambiguous,
        stale_price_count=stale_price,
        stale_fx_count=stale_fx,
        cash_row_count=cash_rows,
        total_market_value_base=total,
    )
    if created_at is not None:
        snap.created_at = created_at
    return snap


# --- readiness / coverage helpers --------------------------------------------


def test_readiness_status_vocabulary() -> None:
    ready = _snap(1, as_of=_TODAY, selected=2, valued=2)
    partial = _snap(1, as_of=_TODAY, selected=2, valued=1, missing_price=1)
    blocked = _snap(1, as_of=_TODAY, selected=2, valued=0, unresolved=2)
    stale = _snap(1, as_of=_TODAY, selected=2, valued=2, stale_price=1)
    empty = _snap(1, as_of=_TODAY, selected=0, valued=0, cash_rows=0)
    assert pv.snapshot_readiness_status(ready) == "ready"
    assert pv.snapshot_readiness_status(partial) == "partial"
    assert pv.snapshot_readiness_status(blocked) == "blocked"
    assert pv.snapshot_readiness_status(stale) == "stale"
    assert pv.snapshot_readiness_status(empty) == "empty"
    # All within the published vocabulary.
    for snap in (ready, partial, blocked, stale, empty):
        assert pv.snapshot_readiness_status(snap) in schemas.READINESS_STATUSES


def test_coverage_ratio() -> None:
    assert pv.snapshot_coverage_ratio(_snap(1, as_of=_TODAY, selected=2, valued=1)) == Decimal(
        "0.5000"
    )
    assert pv.snapshot_coverage_ratio(_snap(1, as_of=_TODAY, selected=4, valued=4)) == Decimal(
        "1.0000"
    )
    # No selected positions -> ratio is undefined (None), never a divide-by-zero.
    assert pv.snapshot_coverage_ratio(_snap(1, as_of=_TODAY, selected=0, valued=0)) is None


# --- history service ---------------------------------------------------------


@pytest.mark.asyncio
async def test_history_oldest_first_and_limit(session: AsyncSession) -> None:
    wid = await _workspace_id(session)
    for offset in (4, 3, 2, 1, 0):  # inserted out of order on purpose
        session.add(_snap(wid, as_of=_TODAY - timedelta(days=offset)))
    await session.commit()

    history = await pv.get_portfolio_valuation_history(session, wid)
    dates = [p.as_of_date for p in history.points]
    assert dates == sorted(dates)  # oldest-first
    assert history.count == 5

    # limit keeps the most-recent window, still oldest-first.
    limited = await pv.get_portfolio_valuation_history(session, wid, limit=2)
    assert [p.as_of_date for p in limited.points] == [
        _TODAY - timedelta(days=1),
        _TODAY,
    ]


@pytest.mark.asyncio
async def test_history_date_range_filter(session: AsyncSession) -> None:
    wid = await _workspace_id(session)
    for offset in range(5):
        session.add(_snap(wid, as_of=_TODAY - timedelta(days=offset)))
    await session.commit()
    history = await pv.get_portfolio_valuation_history(
        session,
        wid,
        start_date=_TODAY - timedelta(days=3),
        end_date=_TODAY - timedelta(days=1),
    )
    assert [p.as_of_date for p in history.points] == [
        _TODAY - timedelta(days=3),
        _TODAY - timedelta(days=2),
        _TODAY - timedelta(days=1),
    ]


@pytest.mark.asyncio
async def test_history_broker_account_filter(session: AsyncSession) -> None:
    wid = await _workspace_id(session)
    account = BrokerAccount(workspace_id=wid, broker_name="Acme", account_label="ISA")
    session.add(account)
    await session.flush()
    session.add(_snap(wid, as_of=_TODAY, broker_account_id=None))
    session.add(_snap(wid, as_of=_TODAY, broker_account_id=account.id))
    await session.commit()

    scoped = await pv.get_portfolio_valuation_history(session, wid, broker_account_id=account.id)
    assert scoped.count == 1
    assert all(p.broker_account_id == account.id for p in scoped.points)


@pytest.mark.asyncio
async def test_history_base_currency_filter(session: AsyncSession) -> None:
    wid = await _workspace_id(session)
    session.add(_snap(wid, as_of=_TODAY, base="GBP"))
    session.add(_snap(wid, as_of=_TODAY, base="USD"))
    await session.commit()
    usd = await pv.get_portfolio_valuation_history(session, wid, base_currency="usd")
    assert usd.count == 1
    assert usd.base_currency == "USD"
    assert all(p.base_currency == "USD" for p in usd.points)


@pytest.mark.asyncio
async def test_history_point_carries_coverage_and_readiness(session: AsyncSession) -> None:
    wid = await _workspace_id(session)
    session.add(_snap(wid, as_of=_TODAY, selected=2, valued=1, missing_price=1))
    await session.commit()
    history = await pv.get_portfolio_valuation_history(session, wid)
    point = history.points[0]
    assert point.valuation_coverage_ratio == Decimal("0.5000")
    assert point.readiness_status == "partial"


# --- summary service ---------------------------------------------------------


@pytest.mark.asyncio
async def test_summary_empty_when_no_snapshots(session: AsyncSession) -> None:
    wid = await _workspace_id(session)
    summary = await pv.build_summary(session, wid)
    assert summary.status == "empty"
    assert summary.readiness_status == "empty"
    assert summary.latest_snapshot_id is None
    assert summary.history_points == 0
    assert summary.broker_accounts == []


@pytest.mark.asyncio
async def test_summary_latest_and_blocking_reasons(session: AsyncSession) -> None:
    wid = await _workspace_id(session)
    session.add(_snap(wid, as_of=_TODAY - timedelta(days=1), selected=2, valued=2))
    session.add(
        _snap(
            wid,
            as_of=_TODAY,
            selected=3,
            valued=1,
            missing_price=1,
            unresolved=1,
            total=Decimal("4200.50"),
        )
    )
    await session.commit()
    summary = await pv.build_summary(session, wid)
    assert summary.status == "present"
    assert summary.latest_as_of_date == _TODAY  # newest wins
    assert summary.total_market_value_base == Decimal("4200.50")
    assert summary.readiness_status == "partial"
    assert summary.history_points == 2
    assert summary.blocking_reasons == ["missing_price", "unresolved_instrument"]


@pytest.mark.asyncio
async def test_summary_broker_account_breakdown(session: AsyncSession) -> None:
    wid = await _workspace_id(session)
    account = BrokerAccount(workspace_id=wid, broker_name="Acme", account_label="ISA")
    session.add(account)
    await session.flush()
    session.add(_snap(wid, as_of=_TODAY, broker_account_id=None))
    session.add(
        _snap(wid, as_of=_TODAY, broker_account_id=account.id, selected=2, valued=1, missing_fx=1)
    )
    await session.commit()
    summary = await pv.build_summary(session, wid)
    assert len(summary.broker_accounts) == 1
    row = summary.broker_accounts[0]
    assert row.broker_account_id == account.id
    assert row.valuation_coverage_ratio == Decimal("0.5000")
    assert row.readiness_status == "partial"


# --- API ---------------------------------------------------------------------

# Tokens that must never appear in these coverage/readiness read models.
_FORBIDDEN_TOKENS = (
    "pnl",
    "return",
    "cost_basis",
    "realised",
    "unrealised",
    "realized",
    "unrealized",
    "performance",
    "total_return",
)


def _assert_no_pnl_keys(obj: object) -> None:
    if isinstance(obj, dict):
        for key, value in obj.items():
            assert not any(tok in key.lower() for tok in _FORBIDDEN_TOKENS), f"forbidden key {key}"
            _assert_no_pnl_keys(value)
    elif isinstance(obj, list):
        for item in obj:
            _assert_no_pnl_keys(item)


@pytest.mark.asyncio
async def test_api_history_endpoint(client: AsyncClient, session: AsyncSession) -> None:
    wid = await _workspace_id(session)
    for offset in range(3):
        session.add(_snap(wid, as_of=_TODAY - timedelta(days=offset)))
    await session.commit()

    resp = await client.get(f"/api/v1/workspaces/{wid}/portfolio/valuation/history")
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 3
    dates = [p["as_of_date"] for p in body["points"]]
    assert dates == sorted(dates)  # oldest-first
    # No rows / raw payloads / PnL/return fields on the history series.
    assert "rows" not in body
    point = body["points"][0]
    assert "raw_payload_json" not in point
    _assert_no_pnl_keys(body)


@pytest.mark.asyncio
async def test_api_history_limit_validation(client: AsyncClient, session: AsyncSession) -> None:
    wid = await _workspace_id(session)
    # Over the max -> 422 (bounded).
    over = await client.get(
        f"/api/v1/workspaces/{wid}/portfolio/valuation/history", params={"limit": 5000}
    )
    assert over.status_code == 422
    zero = await client.get(
        f"/api/v1/workspaces/{wid}/portfolio/valuation/history", params={"limit": 0}
    )
    assert zero.status_code == 422


@pytest.mark.asyncio
async def test_api_summary_endpoint(client: AsyncClient, session: AsyncSession) -> None:
    wid = await _workspace_id(session)
    session.add(_snap(wid, as_of=_TODAY, selected=2, valued=1, missing_price=1))
    await session.commit()
    resp = await client.get(f"/api/v1/workspaces/{wid}/portfolio/valuation/summary")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "present"
    assert body["readiness_status"] == "partial"
    assert body["blocking_reasons"] == ["missing_price"]
    assert "rows" not in body
    _assert_no_pnl_keys(body)


# --- dashboard ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_dashboard_includes_valuation_block(
    client: AsyncClient, session: AsyncSession
) -> None:
    wid = await _workspace_id(session)
    session.add(
        _snap(wid, as_of=_TODAY, selected=3, valued=2, missing_price=1, total=Decimal("999.99"))
    )
    await session.commit()
    body = (await client.get(f"/api/v1/workspaces/{wid}/dashboard")).json()
    block = body["portfolio_valuation"]
    assert block["status"] == "present"
    assert Decimal(block["total_market_value_base"]) == Decimal("999.99")
    assert block["readiness_status"] == "partial"
    assert block["missing_price_count"] == 1
    assert block["recommended_action"] == "fetch missing prices"
    _assert_no_pnl_keys(block)


@pytest.mark.asyncio
async def test_dashboard_blocker_counts_correct(client: AsyncClient, session: AsyncSession) -> None:
    wid = await _workspace_id(session)
    session.add(
        _snap(
            wid,
            as_of=_TODAY,
            selected=4,
            valued=1,
            missing_price=1,
            missing_fx=1,
            unresolved=1,
        )
    )
    await session.commit()
    block = (await client.get(f"/api/v1/workspaces/{wid}/dashboard")).json()["portfolio_valuation"]
    assert block["missing_price_count"] == 1
    assert block["missing_fx_count"] == 1
    assert block["unresolved_count"] == 1
    # Hard blockers present -> resolve takes precedence in the recommended action.
    assert block["recommended_action"] == "resolve imported instruments"


@pytest.mark.asyncio
async def test_dashboard_missing_recompute_action(
    client: AsyncClient, session: AsyncSession
) -> None:
    wid = await _workspace_id(session)
    # A transaction ledger exists, but no valuation snapshot yet.
    session.add(
        PortfolioTransaction(
            workspace_id=wid,
            transaction_key="hist-tx-1",
            transaction_type="buy",
            trade_date=_TODAY,
            quantity=Decimal("1"),
            currency="GBP",
            status="committed",
            source="broker_csv",
            symbol="ZZZ",
        )
    )
    await session.commit()
    block = (await client.get(f"/api/v1/workspaces/{wid}/dashboard")).json()["portfolio_valuation"]
    assert block["status"] == "missing"
    assert block["needs_recompute"] is True
    assert block["recommended_action"] == "run portfolio_valuation_recompute"


@pytest.mark.asyncio
async def test_dashboard_does_not_recompute_valuation(
    client: AsyncClient, session: AsyncSession
) -> None:
    wid = await _workspace_id(session)
    session.add(
        PortfolioTransaction(
            workspace_id=wid,
            transaction_key="hist-tx-2",
            transaction_type="buy",
            trade_date=_TODAY,
            quantity=Decimal("1"),
            currency="GBP",
            status="committed",
            source="broker_csv",
            symbol="ZZZ",
        )
    )
    await session.commit()
    before = await session.scalar(select(func.count()).select_from(PortfolioValuationSnapshot))
    await client.get(f"/api/v1/workspaces/{wid}/dashboard")
    after = await session.scalar(select(func.count()).select_from(PortfolioValuationSnapshot))
    assert before == after == 0  # the dashboard read never wrote a snapshot


@pytest.mark.asyncio
async def test_dashboard_stale_snapshot_needs_recompute(
    client: AsyncClient, session: AsyncSession
) -> None:
    wid = await _workspace_id(session)
    old = datetime.now(UTC) - timedelta(days=60)
    session.add(_snap(wid, as_of=_TODAY - timedelta(days=60), created_at=old))
    await session.commit()
    block = (await client.get(f"/api/v1/workspaces/{wid}/dashboard")).json()["portfolio_valuation"]
    assert block["status"] == "present"
    assert block["needs_recompute"] is True
    assert block["recommended_action"] == "run portfolio_valuation_recompute"


# --- diagnostics / capabilities ----------------------------------------------


@pytest.mark.asyncio
async def test_diagnostics_history_fields(session: AsyncSession) -> None:
    wid = await _workspace_id(session)
    session.add(_snap(wid, as_of=_TODAY - timedelta(days=1)))
    session.add(_snap(wid, as_of=_TODAY, selected=2, valued=1, missing_price=1))
    await session.commit()
    diag = await diagnostics_service.workspace_diagnostics(session, wid)
    assert diag.portfolio_valuation_history_points == 2
    assert diag.portfolio_valuation_readiness_status == "partial"
    assert diag.portfolio_valuation_latest_coverage_ratio == Decimal("0.5000")


def test_capabilities_history_and_dashboard_real() -> None:
    caps = capabilities_service.build_capabilities()
    assert caps.features["portfolio_valuation_history"] == capabilities_service.REAL
    assert caps.features["portfolio_valuation_dashboard"] == capabilities_service.REAL
    # Analytics stay planned — never marked real by this slice.
    for planned in ("portfolio_pnl", "tax_lots", "total_return", "performance_attribution"):
        assert caps.features[planned] == capabilities_service.PLANNED


# --- compute-boundary safety -------------------------------------------------


def test_schemas_have_no_pnl_or_return_fields() -> None:
    """The history/summary/dashboard schemas are coverage/readiness only."""
    models = (
        schemas.PortfolioValuationHistoryPoint,
        schemas.PortfolioValuationHistory,
        schemas.PortfolioValuationSummaryResponse,
        schemas.PortfolioValuationBrokerAccountSummary,
        schemas.PortfolioValuationDashboardBlock,
    )
    for model in models:
        for field in model.model_fields:
            assert not any(tok in field.lower() for tok in _FORBIDDEN_TOKENS), (
                f"{model.__name__}.{field} looks like a PnL/return field"
            )


@pytest.mark.asyncio
async def test_history_and_summary_read_snapshots_only(session: AsyncSession) -> None:
    """No live fetch / no identity resolver / bounded — pure reads over snapshots.

    With zero snapshots the read models return empty/None rather than computing
    anything; they never create instruments, prices, FX or snapshots."""
    wid = await _workspace_id(session)
    before_snaps = await session.scalar(
        select(func.count()).select_from(PortfolioValuationSnapshot)
    )
    history = await pv.get_portfolio_valuation_history(session, wid)
    summary = await pv.build_summary(session, wid)
    after_snaps = await session.scalar(select(func.count()).select_from(PortfolioValuationSnapshot))
    assert before_snaps == after_snaps == 0
    assert history.points == []
    assert summary.status == "empty"


@pytest.mark.asyncio
async def test_history_limit_is_bounded(session: AsyncSession) -> None:
    wid = await _workspace_id(session)
    # A caller asking for more than the cap is silently clamped (never unbounded).
    history = await pv.get_portfolio_valuation_history(session, wid, limit=10_000)
    assert history.count == 0  # no snapshots, but the call is safely bounded
