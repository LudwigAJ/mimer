"""Broker CSV import + transaction ledger + position reconciliation tests.

All offline (no network, no live resolver). Covers the parser (pure), the
preview/commit flow (idempotency, isolation, resolution), bounded position
reconciliation, the API surface, diagnostics and capabilities.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    BrokerImport,
    BrokerImportRow,
    Fund,
    Instrument,
    PortfolioPositionSnapshot,
    PortfolioTransaction,
    Workspace,
)
from app.schemas.broker_import import BrokerImportRequest
from app.services import broker_imports as service
from app.sources.broker_imports import (
    BrokerImportError,
    GenericCsvV1Parser,
    compute_source_hash,
)
from app.workers.run import run_job

VUSA_ISIN = "IE00B3XXRP09"
ISF_ISIN = "IE0005042456"

# A realistic mixed CSV: resolvable buy/sell (VUSA by ISIN), a resolvable buy by
# ticker (ISF), a dividend + cash deposit, an unresolved direct equity (TSLA),
# and a deliberately bad row.
GOOD_CSV = (
    "date,type,symbol,isin,name,quantity,price,net_amount,fees,currency\n"
    f"2026-06-01,buy,VUSA,{VUSA_ISIN},Vanguard S&P 500,10,80.50,-806,1.00,GBP\n"
    f"2026-06-20,sell,VUSA,{VUSA_ISIN},Vanguard S&P 500,4,82.00,327,1.00,GBP\n"
    f"2026-06-02,buy,ISF,{ISF_ISIN},iShares FTSE 100,50,8.50,-426,1.00,GBP\n"
    f"2026-06-10,dividend,VUSA,{VUSA_ISIN},Vanguard S&P 500,,,2.55,0,GBP\n"
    "2026-06-12,cash_deposit,,,,,,500,0,GBP\n"
    "2026-06-15,buy,TSLA,US88160R1014,Tesla Inc,5,210.00,-1051,1.00,USD\n"
)


async def _workspace_id(session: AsyncSession) -> int:
    return (await session.scalar(select(Workspace.id).order_by(Workspace.id))) or 1


def _request(csv_text: str, **kwargs) -> BrokerImportRequest:
    return BrokerImportRequest(csv_text=csv_text, source_filename="sample.csv", **kwargs)


# --- parser (pure) -----------------------------------------------------------


def test_parser_handles_all_transaction_types() -> None:
    result = GenericCsvV1Parser().parse(GOOD_CSV)
    assert result.row_count == 6
    types = [r.transaction.transaction_type for r in result.parsed_rows]
    assert types == ["buy", "sell", "buy", "dividend", "cash_deposit", "buy"]
    assert result.error_count == 0
    assert result.cash_movement_count == 2  # dividend + cash_deposit


def test_parser_column_aliases() -> None:
    csv_text = (
        "Trade Date,Transaction Type,Ticker,Security Name,Qty,Price,Total,Commission,CCY\n"
        "2026-06-01,Buy,VUSA,Vanguard S&P 500,10,80.50,-805,1.00,GBP\n"
    )
    result = GenericCsvV1Parser().parse(csv_text)
    assert result.parsed_count == 1
    txn = result.parsed_rows[0].transaction
    assert txn.transaction_type == "buy"
    assert txn.symbol == "VUSA"
    assert txn.quantity == Decimal("10")
    assert txn.fees == Decimal("1.00")
    assert txn.net_amount == Decimal("-805")
    assert txn.currency == "GBP"


def test_parser_isolates_bad_date() -> None:
    csv_text = "date,type,symbol,quantity,currency\nnot-a-date,buy,VUSA,10,GBP\n"
    result = GenericCsvV1Parser().parse(csv_text)
    assert result.error_count == 1
    assert result.parsed_count == 0
    assert "date" in (result.rows[0].parse_error or "")


def test_parser_isolates_bad_decimal() -> None:
    csv_text = "date,type,symbol,quantity,currency\n2026-06-01,buy,VUSA,ten,GBP\n"
    result = GenericCsvV1Parser().parse(csv_text)
    assert result.error_count == 1
    assert "number" in (result.rows[0].parse_error or "")


def test_parser_missing_required_fields() -> None:
    # buy with no quantity; cash movement with no amount; missing currency.
    csv_text = (
        "date,type,symbol,quantity,net_amount,currency\n"
        "2026-06-01,buy,VUSA,,,GBP\n"
        "2026-06-02,dividend,VUSA,,,GBP\n"
        "2026-06-03,buy,VUSA,10,,\n"
    )
    result = GenericCsvV1Parser().parse(csv_text)
    assert result.error_count == 3
    assert result.parsed_count == 0


def test_parser_unknown_type_is_warning_not_error() -> None:
    csv_text = "date,type,symbol,quantity,net_amount,currency\n2026-06-01,teleport,VUSA,1,5,GBP\n"
    result = GenericCsvV1Parser().parse(csv_text)
    assert result.error_count == 0
    row = result.rows[0]
    assert row.parse_status == "warning"
    assert row.transaction.transaction_type == "unknown"


def test_parser_requires_date_and_type_columns() -> None:
    with pytest.raises(BrokerImportError):
        GenericCsvV1Parser().parse("foo,bar\n1,2\n")


def test_compute_source_hash_is_deterministic() -> None:
    a = compute_source_hash("generic_csv_v1", GOOD_CSV)
    b = compute_source_hash("generic_csv_v1", GOOD_CSV)
    c = compute_source_hash("generic_csv_v1", GOOD_CSV + "\n")
    assert a == b
    assert a != c
    assert len(a) == 64


# --- preview -----------------------------------------------------------------


async def test_preview_writes_nothing(session: AsyncSession) -> None:
    wid = await _workspace_id(session)
    before = await session.scalar(select(func.count()).select_from(PortfolioTransaction))
    result = await service.preview_import(session, wid, request=_request(GOOD_CSV))
    after = await session.scalar(select(func.count()).select_from(PortfolioTransaction))
    assert result.committed is False
    assert before == after == 0
    assert await session.scalar(select(func.count()).select_from(BrokerImport)) == 0


async def test_preview_resolves_by_isin_and_flags_unresolved(session: AsyncSession) -> None:
    wid = await _workspace_id(session)
    result = await service.preview_import(session, wid, request=_request(GOOD_CSV))
    by_symbol = {r.symbol: r for r in result.transactions if r.symbol}
    assert by_symbol["VUSA"].resolution_status == "resolved"
    assert by_symbol["VUSA"].fund_listing_id is not None
    assert by_symbol["TSLA"].resolution_status == "unresolved_instrument"
    assert result.summary.unresolved_count == 1
    assert result.summary.transaction_count == 6


# --- commit ------------------------------------------------------------------


async def test_commit_creates_import_rows_and_transactions(session: AsyncSession) -> None:
    wid = await _workspace_id(session)
    result = await service.commit_import(session, wid, request=_request(GOOD_CSV))
    assert result.committed is True
    assert result.import_id is not None

    txns = await session.scalar(select(func.count()).select_from(PortfolioTransaction))
    assert txns == 6
    rows = await session.scalar(
        select(func.count())
        .select_from(BrokerImportRow)
        .where(BrokerImportRow.broker_import_id == result.import_id)
    )
    assert rows == 6
    # Every committed transaction's row links back to the canonical transaction.
    linked = await session.scalar(
        select(func.count())
        .select_from(BrokerImportRow)
        .where(
            BrokerImportRow.broker_import_id == result.import_id,
            BrokerImportRow.canonical_transaction_id.is_not(None),
        )
    )
    assert linked == 6


async def test_commit_is_idempotent_for_duplicate_file(session: AsyncSession) -> None:
    wid = await _workspace_id(session)
    first = await service.commit_import(session, wid, request=_request(GOOD_CSV))
    second = await service.commit_import(session, wid, request=_request(GOOD_CSV))
    assert second.duplicate is True
    assert second.import_id == first.import_id
    # No duplicate transactions / imports.
    assert await session.scalar(select(func.count()).select_from(PortfolioTransaction)) == 6
    assert await session.scalar(select(func.count()).select_from(BrokerImport)) == 1


async def test_commit_same_transaction_in_two_files_not_duplicated(session: AsyncSession) -> None:
    wid = await _workspace_id(session)
    csv_one = (
        "date,type,symbol,isin,quantity,price,net_amount,currency\n"
        f"2026-06-01,buy,VUSA,{VUSA_ISIN},10,80.50,-805,GBP\n"
    )
    # Same economic transaction, different surrounding file (extra row).
    csv_two = csv_one + f"2026-06-02,buy,ISF,{ISF_ISIN},5,8.50,-42.5,GBP\n"
    await service.commit_import(session, wid, request=_request(csv_one))
    await service.commit_import(session, wid, request=_request(csv_two))
    # The shared VUSA buy is stored once; only the new ISF buy is added => 2 total.
    assert await session.scalar(select(func.count()).select_from(PortfolioTransaction)) == 2


async def test_commit_isolates_bad_rows_and_stores_unresolved(session: AsyncSession) -> None:
    wid = await _workspace_id(session)
    csv_text = (
        "date,type,symbol,isin,quantity,price,net_amount,currency\n"
        f"2026-06-01,buy,VUSA,{VUSA_ISIN},10,80.50,-805,GBP\n"
        "bad-date,buy,VUSA,,1,1,-1,GBP\n"
        "2026-06-02,buy,TSLA,US88160R1014,5,210,-1050,USD\n"
    )
    result = await service.commit_import(session, wid, request=_request(csv_text))
    assert result.summary.error_count == 1
    assert result.summary.transaction_count == 2
    # The unresolved TSLA transaction is stored (never dropped), flagged.
    unresolved = await session.scalar(
        select(func.count())
        .select_from(PortfolioTransaction)
        .where(PortfolioTransaction.status == "unresolved_instrument")
    )
    assert unresolved == 1
    # A failed row is recorded with no canonical transaction.
    failed = await session.scalar(
        select(func.count())
        .select_from(BrokerImportRow)
        .where(BrokerImportRow.parse_status == "failed")
    )
    assert failed == 1


async def test_commit_does_not_autocreate_instruments_from_names(session: AsyncSession) -> None:
    wid = await _workspace_id(session)
    funds_before = await session.scalar(select(func.count()).select_from(Fund))
    instruments_before = await session.scalar(select(func.count()).select_from(Instrument))
    csv_text = (
        "date,type,name,quantity,price,net_amount,currency\n"
        "2026-06-01,buy,Some Unknown Equity PLC,10,5,-50,GBP\n"
    )
    result = await service.commit_import(session, wid, request=_request(csv_text))
    assert result.summary.unresolved_count == 1
    # No funds / instruments invented from a name-only row.
    assert await session.scalar(select(func.count()).select_from(Fund)) == funds_before
    assert await session.scalar(select(func.count()).select_from(Instrument)) == instruments_before


# --- position reconciliation -------------------------------------------------


async def test_positions_reconcile_quantities_and_cash(session: AsyncSession) -> None:
    wid = await _workspace_id(session)
    await service.commit_import(session, wid, request=_request(GOOD_CSV))
    positions = await service.reconcile_positions(session, wid)

    by_symbol = {p.symbol: p for p in positions.positions}
    assert by_symbol["VUSA"].quantity == Decimal("6")  # 10 bought - 4 sold
    assert by_symbol["ISF"].quantity == Decimal("50")
    assert by_symbol["TSLA"].quantity == Decimal("5")
    assert by_symbol["TSLA"].resolution_status == "unresolved_instrument"
    assert positions.unresolved_count == 1

    cash = {c.currency: c.amount for c in positions.cash}
    assert cash["USD"] == Decimal("-1051")
    # GBP: -806 +327 -426 +2.55 +500
    assert cash["GBP"] == Decimal("-402.45")


async def test_position_snapshot_is_idempotent(session: AsyncSession) -> None:
    wid = await _workspace_id(session)
    await service.commit_import(session, wid, request=_request(GOOD_CSV))
    count_one = await session.scalar(select(func.count()).select_from(PortfolioPositionSnapshot))
    assert count_one == 1
    # Re-committing the same file is a duplicate no-op => still one snapshot.
    await service.commit_import(session, wid, request=_request(GOOD_CSV))
    assert (await session.scalar(select(func.count()).select_from(PortfolioPositionSnapshot))) == 1
    # A different ledger writes a new snapshot.
    extra = GOOD_CSV + f"2026-06-25,buy,ISF,{ISF_ISIN},10,8.5,-85,GBP\n"
    await service.commit_import(session, wid, request=_request(extra))
    assert (await session.scalar(select(func.count()).select_from(PortfolioPositionSnapshot))) == 2


# --- API ---------------------------------------------------------------------


async def test_api_preview_then_commit_flow(client: AsyncClient) -> None:
    body = {"broker_name": "generic_csv_v1", "source_filename": "s.csv", "csv_text": GOOD_CSV}
    preview = (await client.post("/api/v1/workspaces/1/broker-imports/preview", json=body)).json()
    assert preview["committed"] is False
    assert preview["summary"]["unresolved_count"] == 1
    assert preview["import_id"] is None

    commit = (await client.post("/api/v1/workspaces/1/broker-imports/commit", json=body)).json()
    assert commit["committed"] is True
    assert commit["import_id"] is not None
    assert commit["position_snapshot"]["created"] is True


async def test_api_import_history_and_detail(client: AsyncClient) -> None:
    body = {"broker_name": "generic_csv_v1", "source_filename": "s.csv", "csv_text": GOOD_CSV}
    commit = (await client.post("/api/v1/workspaces/1/broker-imports/commit", json=body)).json()
    import_id = commit["import_id"]

    listing = (await client.get("/api/v1/workspaces/1/broker-imports")).json()
    assert listing["meta"]["count"] == 1

    detail = (await client.get(f"/api/v1/workspaces/1/broker-imports/{import_id}")).json()
    assert detail["id"] == import_id
    assert len(detail["rows"]) == 6
    # Unknown import 404s.
    missing = await client.get("/api/v1/workspaces/1/broker-imports/999999")
    assert missing.status_code == 404


async def test_api_transactions_and_positions(client: AsyncClient) -> None:
    body = {"broker_name": "generic_csv_v1", "source_filename": "s.csv", "csv_text": GOOD_CSV}
    await client.post("/api/v1/workspaces/1/broker-imports/commit", json=body)

    txns = (await client.get("/api/v1/workspaces/1/transactions")).json()
    assert txns["meta"]["count"] == 6
    # Bounded limit honoured.
    limited = (await client.get("/api/v1/workspaces/1/transactions?limit=2")).json()
    assert limited["meta"]["count"] == 2
    # Filter by status.
    unresolved = (
        await client.get("/api/v1/workspaces/1/transactions?status=unresolved_instrument")
    ).json()
    assert unresolved["meta"]["count"] == 1

    positions = (await client.get("/api/v1/workspaces/1/positions")).json()
    assert len(positions["positions"]) == 3
    assert positions["snapshot_id"] is not None


async def test_api_diagnostics_surface_broker_fields(client: AsyncClient) -> None:
    body = {"broker_name": "generic_csv_v1", "source_filename": "s.csv", "csv_text": GOOD_CSV}
    await client.post("/api/v1/workspaces/1/broker-imports/commit", json=body)
    diag = (await client.get("/api/v1/workspaces/1/diagnostics")).json()
    assert diag["broker_imports"] == 1
    assert diag["portfolio_transactions"] == 6
    assert diag["unresolved_import_transactions"] == 1
    assert diag["latest_broker_import_status"] in ("committed", "partial")


async def test_api_capabilities_marks_transactions_real(client: AsyncClient) -> None:
    body = (await client.get("/api/v1/capabilities")).json()
    assert body["features"]["broker_csv_import"] == "real"
    assert body["features"]["portfolio_transaction_ledger"] == "real"
    data_types = {d["name"]: d["status"] for d in body["data_types"]}
    assert data_types["transactions"] == "real"


# --- worker ------------------------------------------------------------------


async def test_worker_broker_csv_import_runs_offline(session: AsyncSession) -> None:
    wid = await _workspace_id(session)
    run = await run_job(session, "broker_csv_import", workspace_id=wid)
    assert run.status == "success"
    assert run.job_type == "broker_csv_import"
    # The bundled offline sample produced canonical transactions.
    assert (await session.scalar(select(func.count()).select_from(PortfolioTransaction))) >= 1
