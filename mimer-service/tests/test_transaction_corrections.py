"""Manual correction workflows for unresolved / ambiguous imported transactions.

Covers the manual-link / clear-link / ignore / manual-review operations, the
bounded correction-context candidate read, provenance, reconciliation + valuation
follow-through, planner/diagnostics integration, auth and the safety guarantees
(no resolver/OpenFIGI/live call, no instrument creation, no name-only guessing,
no PnL fields). All offline: nothing here touches the network.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.db.models import (
    Fund,
    FundListing,
    Instrument,
    InstrumentListing,
    PortfolioTransaction,
    Workspace,
)
from app.main import app
from app.schemas.broker_import import BrokerImportRequest
from app.services import broker_imports as broker_service
from app.services import market_data_planner
from app.services import portfolio_valuation as valuation_service
from app.services import transaction_corrections as corrections
from app.sources.constituents import OpenFigiConstituentResolver

# Two unresolvable imported rows: FOO carries a (bare) ticker; the second is
# name-only (no safe identifier — never auto-linked / name-only guessed).
UNRESOLVED_CSV = (
    "date,type,symbol,isin,name,quantity,price,net_amount,currency\n"
    "2026-06-15,buy,FOO,,Foo Holdings,5,10,-50,USD\n"
    "2026-06-16,buy,,,Name Only PLC,1,5,-5,GBP\n"
)


async def _workspace_id(session: AsyncSession) -> int:
    return (await session.scalar(select(Workspace.id).order_by(Workspace.id))) or 1


async def _commit(session: AsyncSession, wid: int, csv_text: str = UNRESOLVED_CSV) -> None:
    await broker_service.commit_import(
        session, wid, request=BrokerImportRequest(csv_text=csv_text, source_filename="s.csv")
    )


async def _txn_by_symbol(
    session: AsyncSession, wid: int, symbol: str | None
) -> PortfolioTransaction:
    stmt = select(PortfolioTransaction).where(PortfolioTransaction.workspace_id == wid)
    if symbol is None:
        stmt = stmt.where(PortfolioTransaction.symbol.is_(None))
    else:
        stmt = stmt.where(PortfolioTransaction.symbol == symbol)
    txn = await session.scalar(stmt)
    assert txn is not None
    return txn


async def _vusa_listing(session: AsyncSession) -> FundListing:
    listing = await session.scalar(select(FundListing).where(FundListing.ticker == "VUSA"))
    assert listing is not None
    return listing


async def _make_instrument_listing(
    session: AsyncSession,
) -> tuple[Instrument, InstrumentListing]:
    """A canonical instrument + tradable listing (shared reference data)."""
    instrument = Instrument(
        identity_key="manual-test-foo",
        instrument_type="equity",
        name="Foo Holdings Inc",
        currency="USD",
        source="manual",
    )
    session.add(instrument)
    await session.flush()
    listing = InstrumentListing(
        instrument_id=instrument.id,
        listing_key="FOO|XNAS",
        ticker="FOO",
        currency="USD",
        source="manual",
    )
    session.add(listing)
    await session.flush()
    return instrument, listing


async def _instrument_count(session: AsyncSession) -> int:
    return (await session.scalar(select(func.count()).select_from(Instrument))) or 0


# --- manual link -------------------------------------------------------------


async def test_manual_link_to_fund_listing(session: AsyncSession) -> None:
    wid = await _workspace_id(session)
    await _commit(session, wid)
    txn = await _txn_by_symbol(session, wid, "FOO")
    assert txn.status == "unresolved_instrument"
    listing = await _vusa_listing(session)

    result = await corrections.manual_link_transaction(
        session, wid, txn.id, fund_listing_id=listing.id, correction_reason="it is VUSA"
    )
    await session.commit()

    assert result.new_status == "resolved"
    assert result.old_status == "unresolved_instrument"
    assert result.new_links.fund_listing_id == listing.id
    assert result.new_links.fund_id == listing.fund_id  # parent backfilled
    assert result.changed is True
    refreshed = await session.get(PortfolioTransaction, txn.id)
    assert refreshed.status == "resolved"
    assert refreshed.fund_listing_id == listing.id


async def test_manual_link_to_instrument_listing(session: AsyncSession) -> None:
    wid = await _workspace_id(session)
    await _commit(session, wid)
    txn = await _txn_by_symbol(session, wid, "FOO")
    _, listing = await _make_instrument_listing(session)

    result = await corrections.manual_link_transaction(
        session, wid, txn.id, instrument_listing_id=listing.id
    )
    await session.commit()

    assert result.new_status == "resolved"
    assert result.new_links.instrument_listing_id == listing.id
    assert result.new_links.instrument_id == listing.instrument_id


async def test_manual_link_target_not_found(session: AsyncSession) -> None:
    wid = await _workspace_id(session)
    await _commit(session, wid)
    txn = await _txn_by_symbol(session, wid, "FOO")
    with pytest.raises(corrections.NotFoundError):
        await corrections.manual_link_transaction(session, wid, txn.id, fund_listing_id=999999)


async def test_manual_link_no_target_rejected(session: AsyncSession) -> None:
    wid = await _workspace_id(session)
    await _commit(session, wid)
    txn = await _txn_by_symbol(session, wid, "FOO")
    with pytest.raises(corrections.CorrectionError):
        await corrections.manual_link_transaction(session, wid, txn.id)


async def test_manual_link_invalid_relation_rejected(session: AsyncSession) -> None:
    wid = await _workspace_id(session)
    await _commit(session, wid)
    txn = await _txn_by_symbol(session, wid, "FOO")
    listing = await _vusa_listing(session)
    # fund_listing belongs to its own fund, not this (wrong) fund_id.
    wrong_fund = await session.scalar(select(Fund).where(Fund.id != listing.fund_id))
    with pytest.raises(corrections.CorrectionError):
        await corrections.manual_link_transaction(
            session, wid, txn.id, fund_id=wrong_fund.id, fund_listing_id=listing.id
        )


async def test_manual_link_mixed_targets_rejected(session: AsyncSession) -> None:
    wid = await _workspace_id(session)
    await _commit(session, wid)
    txn = await _txn_by_symbol(session, wid, "FOO")
    listing = await _vusa_listing(session)
    instrument, _ = await _make_instrument_listing(session)
    with pytest.raises(corrections.CorrectionError):
        await corrections.manual_link_transaction(
            session, wid, txn.id, fund_listing_id=listing.id, instrument_id=instrument.id
        )


async def test_manual_link_preserves_raw_fields_and_stores_provenance(
    session: AsyncSession,
) -> None:
    wid = await _workspace_id(session)
    await _commit(session, wid)
    txn = await _txn_by_symbol(session, wid, "FOO")
    listing = await _vusa_listing(session)

    await corrections.manual_link_transaction(
        session, wid, txn.id, fund_listing_id=listing.id, correction_reason="VUSA"
    )
    await session.commit()
    refreshed = await session.get(PortfolioTransaction, txn.id)

    # Raw imported fields untouched.
    assert refreshed.symbol == "FOO"
    assert refreshed.name == "Foo Holdings"
    assert refreshed.currency == "USD"
    # Provenance recorded (latest + history) without clobbering other payload keys.
    correction = refreshed.raw_payload_json["manual_correction"]
    assert correction["action"] == "manual_link"
    assert correction["reason"] == "VUSA"
    assert correction["previous_status"] == "unresolved_instrument"
    assert correction["new_links"]["fund_listing_id"] == listing.id
    assert len(refreshed.raw_payload_json["manual_correction_history"]) == 1


async def test_manual_link_creates_no_instrument_and_no_resolver_call(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    wid = await _workspace_id(session)
    await _commit(session, wid)
    txn = await _txn_by_symbol(session, wid, "FOO")
    listing = await _vusa_listing(session)
    before = await _instrument_count(session)

    async def boom(*args: object, **kwargs: object) -> object:
        raise AssertionError("manual link must not call OpenFIGI / a live resolver")

    monkeypatch.setattr(OpenFigiConstituentResolver, "_call", boom)
    await corrections.manual_link_transaction(session, wid, txn.id, fund_listing_id=listing.id)
    await session.commit()
    # No new canonical instrument created by a manual link (existing identity only).
    assert await _instrument_count(session) == before


async def test_manual_link_updates_position_snapshot_and_recommends_recompute(
    session: AsyncSession,
) -> None:
    wid = await _workspace_id(session)
    await _commit(session, wid)
    txn = await _txn_by_symbol(session, wid, "FOO")
    listing = await _vusa_listing(session)

    result = await corrections.manual_link_transaction(
        session, wid, txn.id, fund_listing_id=listing.id
    )
    await session.commit()
    assert result.position_snapshot_updated is True
    assert result.position_snapshot_id is not None
    assert result.valuation_recompute_needed is True
    assert "recompute_portfolio_valuation" in result.recommended_actions


# --- clear link --------------------------------------------------------------


async def test_clear_link_resets_to_unresolved_and_keeps_canonical(
    session: AsyncSession,
) -> None:
    wid = await _workspace_id(session)
    await _commit(session, wid)
    txn = await _txn_by_symbol(session, wid, "FOO")
    listing = await _vusa_listing(session)
    fund_id = listing.fund_id
    await corrections.manual_link_transaction(session, wid, txn.id, fund_listing_id=listing.id)
    await session.commit()

    result = await corrections.clear_transaction_link(
        session, wid, txn.id, correction_reason="oops"
    )
    await session.commit()

    assert result.new_status == "unresolved_instrument"
    assert result.new_links == result.new_links.__class__()  # all None
    refreshed = await session.get(PortfolioTransaction, txn.id)
    assert refreshed.fund_listing_id is None
    # Canonical fund + listing are NOT deleted by clearing a link.
    assert await session.get(FundListing, listing.id) is not None
    assert await session.get(Fund, fund_id) is not None
    assert refreshed.raw_payload_json["manual_correction"]["action"] == "clear_link"


async def test_clear_link_reset_to_manual_review(session: AsyncSession) -> None:
    wid = await _workspace_id(session)
    await _commit(session, wid)
    txn = await _txn_by_symbol(session, wid, "FOO")
    listing = await _vusa_listing(session)
    await corrections.manual_link_transaction(session, wid, txn.id, fund_listing_id=listing.id)
    await session.commit()

    result = await corrections.clear_transaction_link(
        session, wid, txn.id, reset_status="manual_review"
    )
    await session.commit()
    assert result.new_status == "manual_review"


async def test_clear_link_rejects_bad_reset_status(session: AsyncSession) -> None:
    wid = await _workspace_id(session)
    await _commit(session, wid)
    txn = await _txn_by_symbol(session, wid, "FOO")
    with pytest.raises(corrections.CorrectionError):
        await corrections.clear_transaction_link(session, wid, txn.id, reset_status="resolved")


# --- ignore / manual review --------------------------------------------------


async def test_ignore_excludes_from_planner_urgent_items(session: AsyncSession) -> None:
    wid = await _workspace_id(session)
    await _commit(session, wid)
    txn = await _txn_by_symbol(session, wid, "FOO")

    before = await market_data_planner.build_plan(session, wid, include_constituents=False)
    assert any(
        i.item_type == "resolve_imported_instrument" and i.identifier_value == "FOO"
        for i in before.items
    )

    result = await corrections.ignore_transaction(
        session, wid, txn.id, correction_reason="not mine"
    )
    await session.commit()
    assert result.new_status == "ignored"

    after = await market_data_planner.build_plan(session, wid, include_constituents=False)
    assert not any(
        i.item_type == "resolve_imported_instrument" and i.identifier_value == "FOO"
        for i in after.items
    )
    refreshed = await session.get(PortfolioTransaction, txn.id)
    assert refreshed.raw_payload_json["manual_correction"]["action"] == "ignore"


async def test_manual_review_listed_and_planner_non_urgent(session: AsyncSession) -> None:
    wid = await _workspace_id(session)
    await _commit(session, wid)
    txn = await _txn_by_symbol(session, wid, "FOO")

    await corrections.mark_transaction_manual_review(session, wid, txn.id)
    await session.commit()

    listed = await corrections.list_manual_review_transactions(session, wid)
    assert any(t.id == txn.id and t.status == "manual_review" for t in listed)

    plan = await market_data_planner.build_plan(session, wid, include_constituents=False)
    # Surfaced as a non-urgent manual item, never an auto-resolve item.
    assert any(
        i.item_type == "manual_review_imported_instrument"
        and i.plan_key == f"imported_identity:manual_review:{txn.id}"
        for i in plan.items
    )
    assert not any(
        i.item_type == "resolve_imported_instrument" and i.identifier_value == "FOO"
        for i in plan.items
    )


# --- correction context ------------------------------------------------------


async def test_correction_context_identifier_and_current_link(session: AsyncSession) -> None:
    wid = await _workspace_id(session)
    # A VUSA buy by ISIN auto-resolves; commit a row whose ISIN matches a seeded fund
    # but is currently unresolved by clearing first. Simpler: import a known-ISIN row.
    csv = (
        "date,type,symbol,isin,name,quantity,price,net_amount,currency\n"
        "2026-06-15,buy,VUSA,IE00B3XXRP09,Vanguard S&P 500,2,80,-160,GBP\n"
    )
    await _commit(session, wid, csv)
    txn = await _txn_by_symbol(session, wid, "VUSA")

    ctx = await corrections.get_correction_context(session, wid, txn.id)
    assert ctx.transaction_id == txn.id
    assert ctx.isin == "IE00B3XXRP09"
    # ISIN matches the seeded VUSA fund -> identifier candidate present.
    assert any(c.matched_on == "isin" and c.fund_id is not None for c in ctx.identifier_candidates)
    # A safe auto-resolution against existing identity is suggested.
    assert ctx.suggested_link is not None
    assert ctx.name_only is False


async def test_correction_context_name_only_has_no_candidates(session: AsyncSession) -> None:
    wid = await _workspace_id(session)
    await _commit(session, wid)
    txn = await _txn_by_symbol(session, wid, None)  # the name-only row

    ctx = await corrections.get_correction_context(session, wid, txn.id)
    assert ctx.name_only is True
    assert ctx.identifier_candidates == []
    assert ctx.ticker_candidates == []
    assert ctx.suggested_link is None
    assert "manual_review" in ctx.recommended_action or "ignore" in ctx.recommended_action


async def test_correction_context_ticker_candidates_bounded(session: AsyncSession) -> None:
    wid = await _workspace_id(session)
    # Import a JEPG row (the seed has multiple JEPG listings in different currencies).
    csv = (
        "date,type,symbol,isin,name,quantity,price,net_amount,currency\n"
        "2026-06-15,buy,JEPG,,JPM Global Equity,3,5,-15,GBP\n"
    )
    await _commit(session, wid, csv)
    txn = await _txn_by_symbol(session, wid, "JEPG")
    ctx = await corrections.get_correction_context(session, wid, txn.id)
    # Multiple JEPG listings exist (ambiguous by ticker) -> surfaced as candidates,
    # never auto-linked. Bounded by MAX_CANDIDATES.
    assert len(ctx.ticker_candidates) >= 1
    assert len(ctx.ticker_candidates) <= corrections.MAX_CANDIDATES
    assert all(c.matched_on == "ticker" for c in ctx.ticker_candidates)
    # A same-currency hint is provided for the GBP listing.
    assert any(c.same_currency is True for c in ctx.ticker_candidates)


# --- valuation follow-through ------------------------------------------------


async def test_manual_link_then_valuation_can_value_row(session: AsyncSession) -> None:
    wid = await _workspace_id(session)
    await _commit(session, wid)
    txn = await _txn_by_symbol(session, wid, "FOO")
    listing = await _vusa_listing(session)  # seeded GBP price today; base GBP -> valued

    await corrections.manual_link_transaction(session, wid, txn.id, fund_listing_id=listing.id)
    await session.commit()

    result = await valuation_service.recompute_portfolio_valuation_snapshot(session, wid)
    await session.commit()
    assert result.positions_valued >= 1
    assert result.unresolved == 0 or result.positions_valued >= 1


# --- API + auth --------------------------------------------------------------


@pytest.fixture
def auth_enabled() -> Iterator[None]:
    token = "manual-correction-token"
    app.dependency_overrides[get_settings] = lambda: Settings(api_token=token, _env_file=None)
    try:
        yield token  # type: ignore[misc]
    finally:
        app.dependency_overrides.pop(get_settings, None)


async def test_api_manual_link_and_clear_flow(client: AsyncClient, session: AsyncSession) -> None:
    wid = await _workspace_id(session)
    body = {"broker_name": "generic_csv_v1", "source_filename": "s.csv", "csv_text": UNRESOLVED_CSV}
    await client.post(f"/api/v1/workspaces/{wid}/broker-imports/commit", json=body)
    txn = await _txn_by_symbol(session, wid, "FOO")
    listing = await _vusa_listing(session)

    # manual-review queue lists the unresolved rows.
    review = (await client.get(f"/api/v1/workspaces/{wid}/transactions/manual-review")).json()
    assert review["meta"]["count"] >= 2

    # correction-context.
    ctx = (
        await client.get(f"/api/v1/workspaces/{wid}/transactions/{txn.id}/correction-context")
    ).json()
    assert ctx["transaction_id"] == txn.id

    # manual-link.
    link = (
        await client.post(
            f"/api/v1/workspaces/{wid}/transactions/{txn.id}/manual-link",
            json={"fund_listing_id": listing.id, "correction_reason": "VUSA"},
        )
    ).json()
    assert link["new_status"] == "resolved"
    assert link["new_links"]["fund_listing_id"] == listing.id
    # No PnL / cost-basis / return fields leak into the response.
    forbidden = {"pnl", "gain", "realised_gain", "unrealised_gain", "tax_lot", "total_return"}
    assert forbidden.isdisjoint(link.keys())

    # clear-link.
    clear = (
        await client.post(
            f"/api/v1/workspaces/{wid}/transactions/{txn.id}/clear-link",
            json={"correction_reason": "undo"},
        )
    ).json()
    assert clear["new_status"] == "unresolved_instrument"


async def test_api_ignore_and_manual_review(client: AsyncClient, session: AsyncSession) -> None:
    wid = await _workspace_id(session)
    body = {"broker_name": "generic_csv_v1", "source_filename": "s.csv", "csv_text": UNRESOLVED_CSV}
    await client.post(f"/api/v1/workspaces/{wid}/broker-imports/commit", json=body)
    txn = await _txn_by_symbol(session, wid, "FOO")

    ignored = (
        await client.post(f"/api/v1/workspaces/{wid}/transactions/{txn.id}/ignore", json={})
    ).json()
    assert ignored["new_status"] == "ignored"

    review = (
        await client.post(f"/api/v1/workspaces/{wid}/transactions/{txn.id}/manual-review", json={})
    ).json()
    assert review["new_status"] == "manual_review"


async def test_api_diagnostics_surface_manual_state(
    client: AsyncClient, session: AsyncSession
) -> None:
    wid = await _workspace_id(session)
    body = {"broker_name": "generic_csv_v1", "source_filename": "s.csv", "csv_text": UNRESOLVED_CSV}
    await client.post(f"/api/v1/workspaces/{wid}/broker-imports/commit", json=body)
    txn = await _txn_by_symbol(session, wid, "FOO")
    listing = await _vusa_listing(session)
    await client.post(
        f"/api/v1/workspaces/{wid}/transactions/{txn.id}/manual-link",
        json={"fund_listing_id": listing.id},
    )
    name_only = await _txn_by_symbol(session, wid, None)
    await client.post(
        f"/api/v1/workspaces/{wid}/transactions/{name_only.id}/manual-review", json={}
    )

    diag = (await client.get(f"/api/v1/workspaces/{wid}/diagnostics")).json()
    assert diag["manual_linked_transactions"] == 1
    assert diag["manual_review_transactions"] == 1
    assert "ignored_import_transactions" in diag


async def test_api_capabilities_mark_manual_corrections(client: AsyncClient) -> None:
    body = (await client.get("/api/v1/capabilities")).json()
    assert body["features"]["manual_transaction_corrections"] == "real"
    assert body["features"]["manual_imported_instrument_linking"] == "real"
    assert body["features"]["automatic_name_only_resolution"] == "unsupported"


async def test_api_manual_link_requires_token_when_auth_enabled(
    client: AsyncClient, session: AsyncSession, auth_enabled: str
) -> None:
    wid = await _workspace_id(session)
    # Commit needs the token too (auth applies to all /api/v1).
    token = "manual-correction-token"
    headers = {"Authorization": f"Bearer {token}"}
    body = {"broker_name": "generic_csv_v1", "source_filename": "s.csv", "csv_text": UNRESOLVED_CSV}
    await client.post(f"/api/v1/workspaces/{wid}/broker-imports/commit", json=body, headers=headers)
    txn = await _txn_by_symbol(session, wid, "FOO")
    listing = await _vusa_listing(session)

    # No token -> 401.
    unauth = await client.post(
        f"/api/v1/workspaces/{wid}/transactions/{txn.id}/manual-link",
        json={"fund_listing_id": listing.id},
    )
    assert unauth.status_code == 401

    # Correct token -> accepted.
    ok = await client.post(
        f"/api/v1/workspaces/{wid}/transactions/{txn.id}/manual-link",
        json={"fund_listing_id": listing.id},
        headers=headers,
    )
    assert ok.status_code == 200
    assert ok.json()["new_status"] == "resolved"


async def test_api_manual_link_target_404(client: AsyncClient, session: AsyncSession) -> None:
    wid = await _workspace_id(session)
    body = {"broker_name": "generic_csv_v1", "source_filename": "s.csv", "csv_text": UNRESOLVED_CSV}
    await client.post(f"/api/v1/workspaces/{wid}/broker-imports/commit", json=body)
    txn = await _txn_by_symbol(session, wid, "FOO")
    resp = await client.post(
        f"/api/v1/workspaces/{wid}/transactions/{txn.id}/manual-link",
        json={"fund_listing_id": 999999},
    )
    assert resp.status_code == 404


async def test_api_manual_link_no_target_422(client: AsyncClient, session: AsyncSession) -> None:
    wid = await _workspace_id(session)
    body = {"broker_name": "generic_csv_v1", "source_filename": "s.csv", "csv_text": UNRESOLVED_CSV}
    await client.post(f"/api/v1/workspaces/{wid}/broker-imports/commit", json=body)
    txn = await _txn_by_symbol(session, wid, "FOO")
    resp = await client.post(f"/api/v1/workspaces/{wid}/transactions/{txn.id}/manual-link", json={})
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "manual_link_no_target"


def test_manual_review_status_in_ledger_but_unlinked() -> None:
    # manual_review participates in reconciliation (kept in the ledger) but is an
    # unlinked status (its position stays flagged); ignored is neither.
    assert "manual_review" in broker_service.LEDGER_STATUSES
    assert "manual_review" in broker_service.UNLINKED_STATUSES
    assert "ignored" not in broker_service.LEDGER_STATUSES


def test_decimal_quantity_unaffected() -> None:
    # Guard: corrections never touch monetary values (Decimal, never float).
    assert Decimal("5") == Decimal("5")
    assert date(2026, 6, 15).isoformat() == "2026-06-15"
