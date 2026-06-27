"""Alert generation, idempotency, worker, and API tests.

Pure-rule tests build an `AlertContext` by hand (no DB). Integration tests reuse
the seeded in-memory DB (`session` / `client` share it) and mutate it to trigger
specific rules, then assert on generation, idempotency, the worker, and the API.
Everything is offline — no rule touches the network.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    Alert,
    DocumentSnapshot,
    Fund,
    FundHolding,
    FundListing,
    FxRate,
    JobRun,
    Price,
    ScheduledJob,
    Workspace,
)
from app.services import alert_generation, alert_rules
from app.services import alerts as alerts_service
from app.services.alert_generation import AlertContext
from app.workers.run import run_job

WS = 1


# --- helpers -----------------------------------------------------------------


def _ctx(**overrides) -> AlertContext:
    base = {
        "workspace_id": WS,
        "base_currency": "GBP",
        "now": datetime.now(UTC),
    }
    base.update(overrides)
    return AlertContext(**base)


async def _held_listing(session: AsyncSession, ticker: str) -> FundListing:
    return await session.scalar(select(FundListing).where(FundListing.ticker == ticker))


async def _fund_by_isin(session: AsyncSession, isin: str) -> Fund:
    return await session.scalar(select(Fund).where(Fund.isin == isin))


async def _alerts_for(session: AsyncSession, workspace_id: int = WS) -> list[Alert]:
    return list(
        (await session.execute(select(Alert).where(Alert.workspace_id == workspace_id)))
        .scalars()
        .all()
    )


async def _generate(session: AsyncSession, workspace_id: int = WS):
    res = await alert_generation.generate_for_workspace(session, workspace_id)
    await session.commit()
    return res


# --- pure rule tests ---------------------------------------------------------


def test_document_changed_candidate() -> None:
    fund = Fund(id=1, isin="IE00B3XXRP09", name="VUSA Fund")
    snap = DocumentSnapshot(
        id=10, fund_id=1, document_type="factsheet", change_status="changed", content_hash="abc"
    )
    ctx = _ctx(
        funds=[fund],
        fund_by_id={1: fund},
        latest_documents={(1, "factsheet"): snap},
        doc_types_by_fund={1: {"factsheet"}},
    )
    candidates = alert_rules.rule_document_changed(ctx)
    assert len(candidates) == 1
    c = candidates[0]
    assert c.severity == "warning"
    assert c.category == "document"
    assert c.dedupe_key == "document_changed:1:1:factsheet:abc"
    assert c.related_document_snapshot_id == 10


def test_document_new_candidate() -> None:
    fund = Fund(id=1, isin="X", name="Fund")
    snap = DocumentSnapshot(
        id=11, fund_id=1, document_type="kid", change_status="new", content_hash="h"
    )
    ctx = _ctx(funds=[fund], fund_by_id={1: fund}, latest_documents={(1, "kid"): snap})
    candidates = alert_rules.rule_document_new(ctx)
    assert len(candidates) == 1
    assert candidates[0].severity == "info"
    assert candidates[0].dedupe_key == "document_new:1:1:kid:h"


def test_document_missing_candidate() -> None:
    fund = Fund(id=1, isin="X", name="Fund")
    ctx = _ctx(funds=[fund], fund_by_id={1: fund}, doc_types_by_fund={1: {"annual_report"}})
    candidates = alert_rules.rule_document_missing(ctx)
    assert len(candidates) == 1
    assert candidates[0].dedupe_key == "document_missing:1:1"
    # A fund that has a key doc type produces nothing.
    ctx2 = _ctx(funds=[fund], fund_by_id={1: fund}, doc_types_by_fund={1: {"factsheet"}})
    assert alert_rules.rule_document_missing(ctx2) == []


def test_fx_missing_candidate() -> None:
    from app.services.fx import FxIndex

    ctx = _ctx(position_currencies={"USD", "GBP"}, fx_index=FxIndex.from_rows([]))
    candidates = alert_rules.rule_fx(ctx)
    # GBP==base is skipped; USD has no path in an empty index.
    assert len(candidates) == 1
    assert candidates[0].category == "fx"
    assert candidates[0].dedupe_key == "fx_missing:1:USD:GBP"


def test_broker_import_unresolved_and_ambiguous_candidates() -> None:
    # Unresolved imported transactions -> one grouped INFO; ambiguous -> one
    # grouped WARNING. A clean ledger (both zero) stays silent.
    ctx = _ctx(unresolved_import_transaction_count=3, ambiguous_import_transaction_count=2)
    candidates = alert_rules.rule_broker_import(ctx)
    keys = {c.dedupe_key for c in candidates}
    assert f"broker_import_unresolved:{WS}" in keys
    assert f"broker_import_ambiguous:{WS}" in keys
    ambiguous = next(c for c in candidates if c.dedupe_key == f"broker_import_ambiguous:{WS}")
    assert ambiguous.severity == "warning"
    assert ambiguous.category == "instrument"
    assert ambiguous.raw_payload_json == {"ambiguous_count": 2}
    # Both auto-resolve once the underlying issue is gone.
    assert alert_rules.is_auto_resolvable(f"broker_import_ambiguous:{WS}") is True
    assert alert_rules.rule_broker_import(_ctx()) == []


def test_is_auto_resolvable() -> None:
    assert alert_rules.is_auto_resolvable("price_stale:1:2") is True
    assert alert_rules.is_auto_resolvable("document_new:1:2:factsheet:h") is False
    assert alert_rules.is_auto_resolvable("distribution_new:1:2:2026-01-01") is False


def test_evaluate_dedupes_by_key() -> None:
    fund = Fund(id=1, isin="X", name="Fund")
    snap = DocumentSnapshot(
        id=1, fund_id=1, document_type="factsheet", change_status="changed", content_hash="h"
    )
    ctx = _ctx(funds=[fund], fund_by_id={1: fund}, latest_documents={(1, "factsheet"): snap})
    candidates = alert_rules.evaluate(ctx)
    keys = [c.dedupe_key for c in candidates]
    assert len(keys) == len(set(keys))


# --- generation: baseline + per-rule integration -----------------------------


async def test_clean_seed_generates_no_alerts(session: AsyncSession) -> None:
    res = await _generate(session)
    assert res.inserted == 0
    assert await _alerts_for(session) == []


async def test_stale_price_alert(session: AsyncSession) -> None:
    listing = await _held_listing(session, "VUSA")
    await session.execute(delete(Price).where(Price.fund_listing_id == listing.id))
    session.add(
        Price(
            fund_listing_id=listing.id,
            price_date=date.today() - timedelta(days=30),
            price=Decimal("75.00"),
            currency="GBP",
            source="seed",
        )
    )
    await session.commit()

    await _generate(session)
    alerts = await alerts_service.list_alerts(session, WS, category="price")
    stale = [a for a in alerts if a.dedupe_key.startswith("price_stale")]
    assert len(stale) == 1
    assert stale[0].severity == "warning"
    assert stale[0].related_fund_listing_id == listing.id


async def test_missing_price_alert(session: AsyncSession) -> None:
    listing = await _held_listing(session, "VUSA")
    await session.execute(delete(Price).where(Price.fund_listing_id == listing.id))
    await session.commit()

    await _generate(session)
    alerts = await alerts_service.list_alerts(session, WS, category="price")
    missing = [a for a in alerts if a.dedupe_key.startswith("price_missing")]
    assert len(missing) == 1
    assert missing[0].severity == "error"


async def test_changed_document_alert(session: AsyncSession) -> None:
    fund = await _fund_by_isin(session, "IE00B3XXRP09")
    session.add(
        DocumentSnapshot(
            fund_id=fund.id,
            document_type="factsheet",
            change_status="changed",
            content_hash="newhash",
            source="document_fixture",
        )
    )
    await session.commit()

    await _generate(session)
    alerts = await alerts_service.list_alerts(session, WS, category="document")
    changed = [a for a in alerts if a.dedupe_key.startswith("document_changed")]
    assert len(changed) == 1
    assert "newhash" in changed[0].dedupe_key
    assert changed[0].severity == "warning"


async def test_new_document_alert(session: AsyncSession) -> None:
    fund = await _fund_by_isin(session, "IE00B3XXRP09")
    session.add(
        DocumentSnapshot(
            fund_id=fund.id,
            document_type="prospectus",
            change_status="new",
            content_hash="proh",
            source="document_fixture",
        )
    )
    await session.commit()

    await _generate(session)
    alerts = await alerts_service.list_alerts(session, WS, category="document")
    new = [a for a in alerts if a.dedupe_key.startswith("document_new")]
    assert len(new) == 1
    assert new[0].severity == "info"


async def test_missing_document_alert(session: AsyncSession) -> None:
    fund = await _fund_by_isin(session, "IE00B3XXRP09")
    await session.execute(delete(DocumentSnapshot).where(DocumentSnapshot.fund_id == fund.id))
    await session.commit()

    await _generate(session)
    alerts = await alerts_service.list_alerts(session, WS, category="document")
    missing = [a for a in alerts if a.dedupe_key == f"document_missing:{WS}:{fund.id}"]
    assert len(missing) == 1


async def test_failed_job_alert(session: AsyncSession) -> None:
    listing = await _held_listing(session, "VUSA")
    session.add(
        JobRun(
            job_type="price_ingestion",
            status="failed",
            fund_listing_id=listing.id,
            started_at=datetime.now(UTC),
            finished_at=datetime.now(UTC),
            message="boom",
        )
    )
    await session.commit()

    await _generate(session)
    alerts = await alerts_service.list_alerts(session, WS, category="job")
    assert len(alerts) == 1
    assert alerts[0].severity == "error"
    assert alerts[0].related_job_run_id is not None


async def test_missing_fx_alert(session: AsyncSession) -> None:
    # Drop the GBP/USD rate; held USD positions then have no path to GBP base.
    await session.execute(
        delete(FxRate).where(FxRate.base_currency == "GBP", FxRate.quote_currency == "USD")
    )
    await session.commit()

    await _generate(session)
    alerts = await alerts_service.list_alerts(session, WS, category="fx")
    missing = [a for a in alerts if a.dedupe_key.startswith("fx_missing")]
    assert any("USD" in a.dedupe_key for a in missing)


async def test_stale_fx_alert(session: AsyncSession) -> None:
    await session.execute(
        update(FxRate)
        .where(FxRate.base_currency == "GBP", FxRate.quote_currency == "USD")
        .values(rate_date=date.today() - timedelta(days=30))
    )
    await session.commit()

    await _generate(session)
    alerts = await alerts_service.list_alerts(session, WS, category="fx")
    stale = [a for a in alerts if a.dedupe_key.startswith("fx_stale")]
    assert any("USD" in a.dedupe_key for a in stale)


async def test_missing_holdings_alert(session: AsyncSession) -> None:
    fund = await _fund_by_isin(session, "IE00B3XXRP09")
    await session.execute(delete(FundHolding).where(FundHolding.fund_id == fund.id))
    await session.commit()

    await _generate(session)
    alerts = await alerts_service.list_alerts(session, WS, category="holdings")
    missing = [a for a in alerts if a.dedupe_key == f"holdings_missing:{WS}:{fund.id}"]
    assert len(missing) == 1


async def test_stale_holdings_alert(session: AsyncSession) -> None:
    fund = await _fund_by_isin(session, "IE00B3XXRP09")
    await session.execute(
        update(FundHolding)
        .where(FundHolding.fund_id == fund.id)
        .values(as_of_date=date.today() - timedelta(days=90))
    )
    await session.commit()

    await _generate(session)
    alerts = await alerts_service.list_alerts(session, WS, category="holdings")
    stale = [a for a in alerts if a.dedupe_key == f"holdings_stale:{WS}:{fund.id}"]
    assert len(stale) == 1


# --- idempotency / lifecycle -------------------------------------------------


async def _make_stale_price(session: AsyncSession) -> FundListing:
    listing = await _held_listing(session, "VUSA")
    await session.execute(delete(Price).where(Price.fund_listing_id == listing.id))
    session.add(
        Price(
            fund_listing_id=listing.id,
            price_date=date.today() - timedelta(days=30),
            price=Decimal("75.00"),
            currency="GBP",
            source="seed",
        )
    )
    await session.commit()
    return listing


async def test_rerun_is_idempotent(session: AsyncSession) -> None:
    await _make_stale_price(session)
    first = await _generate(session)
    assert first.inserted >= 1
    count_after_first = len(await _alerts_for(session))

    second = await _generate(session)
    assert second.inserted == 0
    assert len(await _alerts_for(session)) == count_after_first


async def test_rerun_updates_last_seen_at(session: AsyncSession) -> None:
    await _make_stale_price(session)
    await _generate(session)
    alert = (await _alerts_for(session))[0]
    first_seen = alert.last_seen_at

    await _generate(session)
    session.expire_all()
    alert = (await _alerts_for(session))[0]
    assert alert.last_seen_at >= first_seen


async def test_resolved_when_issue_disappears(session: AsyncSession) -> None:
    listing = await _make_stale_price(session)
    await _generate(session)
    active = await alerts_service.list_alerts(session, WS, status="active")
    assert any(a.dedupe_key.startswith("price_stale") for a in active)

    # Fix the price -> the candidate is no longer generated -> auto-resolve.
    session.add(
        Price(
            fund_listing_id=listing.id,
            price_date=date.today(),
            price=Decimal("75.00"),
            currency="GBP",
            source="seed",
        )
    )
    await session.commit()
    await _generate(session)

    resolved = await alerts_service.list_alerts(session, WS, status="resolved")
    assert any(a.dedupe_key.startswith("price_stale") for a in resolved)
    assert await alerts_service.list_alerts(session, WS, status="active") == []


async def test_dismissed_alert_does_not_reappear(session: AsyncSession) -> None:
    await _make_stale_price(session)
    await _generate(session)
    alert = (await _alerts_for(session))[0]
    await alerts_service.mark_dismissed(session, WS, alert.id)

    # The issue still exists; re-running must not resurrect or duplicate it.
    await _generate(session)
    all_alerts = await _alerts_for(session)
    assert len(all_alerts) == 1
    assert all_alerts[0].status == "dismissed"


# --- worker ------------------------------------------------------------------


async def test_worker_single_workspace(session: AsyncSession) -> None:
    await _make_stale_price(session)
    run = await run_job(session, "alert_generation", workspace_id=WS)
    assert run.job_type == "alert_generation"
    assert run.status == "success"
    assert (run.records_inserted or 0) >= 1


async def test_worker_all_workspaces_clean(session: AsyncSession) -> None:
    run = await run_job(session, "alert_generation")
    assert run.status == "success"
    assert run.records_inserted == 0
    assert "workspaces=" in (run.message or "")


async def test_worker_isolates_workspace_failure(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    session.add(Workspace(name="Second", base_currency="GBP"))
    await session.commit()
    ids = await alert_generation.active_workspace_ids(session)
    assert len(ids) == 2

    real = alert_generation.generate_for_workspace

    async def flaky(sess, wid):
        if wid == ids[0]:
            raise RuntimeError("boom")
        return await real(sess, wid)

    monkeypatch.setattr("app.workers.run.alerts_service.generate_for_workspace", flaky)
    run = await run_job(session, "alert_generation")
    assert run.records_failed == 1
    # The second workspace still processed -> partial, not a total failure.
    assert run.status == "partial_success"


# --- job trigger wiring ------------------------------------------------------


async def test_trigger_alert_generation_is_real(client: AsyncClient, session: AsyncSession) -> None:
    session.add(ScheduledJob(name="nightly_alerts", job_type="alert_generation", is_active=True))
    await session.commit()
    job = next(
        j
        for j in (await client.get("/api/v1/jobs")).json()["data"]
        if j["job_type"] == "alert_generation"
    )
    assert job["implementation_status"] == "real"

    run = await client.post(f"/api/v1/jobs/{job['id']}/run")
    assert run.status_code == 201
    assert run.json()["status"] == "success"  # real worker, not success_stub


# --- API ---------------------------------------------------------------------


async def test_list_alerts_and_filters(client: AsyncClient, session: AsyncSession) -> None:
    await _make_stale_price(session)
    fund = await _fund_by_isin(session, "IE0005042456")  # ISF
    await session.execute(delete(DocumentSnapshot).where(DocumentSnapshot.fund_id == fund.id))
    await session.commit()
    await _generate(session)

    body = (await client.get(f"/api/v1/workspaces/{WS}/alerts")).json()
    assert body["meta"]["count"] >= 2

    by_cat = (await client.get(f"/api/v1/workspaces/{WS}/alerts?category=price")).json()
    assert all(a["category"] == "price" for a in by_cat["data"])
    assert by_cat["meta"]["count"] >= 1

    by_sev = (await client.get(f"/api/v1/workspaces/{WS}/alerts?severity=warning")).json()
    assert all(a["severity"] == "warning" for a in by_sev["data"])

    by_status = (await client.get(f"/api/v1/workspaces/{WS}/alerts?status=active")).json()
    assert all(a["status"] == "active" for a in by_status["data"])


async def test_read_dismiss_resolve_endpoints(client: AsyncClient, session: AsyncSession) -> None:
    await _make_stale_price(session)
    await _generate(session)
    alert_id = (await _alerts_for(session))[0].id

    read = await client.post(f"/api/v1/workspaces/{WS}/alerts/{alert_id}/read")
    assert read.status_code == 200
    assert read.json()["status"] == "read"
    assert read.json()["read_at"] is not None

    dismiss = await client.post(f"/api/v1/workspaces/{WS}/alerts/{alert_id}/dismiss")
    assert dismiss.json()["status"] == "dismissed"

    resolve = await client.post(f"/api/v1/workspaces/{WS}/alerts/{alert_id}/resolve")
    assert resolve.json()["status"] == "resolved"
    assert resolve.json()["resolved_at"] is not None


async def test_mark_all_read_endpoint(client: AsyncClient, session: AsyncSession) -> None:
    await _make_stale_price(session)
    fund = await _fund_by_isin(session, "IE0005042456")
    await session.execute(delete(DocumentSnapshot).where(DocumentSnapshot.fund_id == fund.id))
    await session.commit()
    await _generate(session)

    resp = await client.post(f"/api/v1/workspaces/{WS}/alerts/mark-all-read")
    assert resp.status_code == 200
    assert resp.json()["marked_read"] >= 2

    active = (await client.get(f"/api/v1/workspaces/{WS}/alerts?status=active")).json()
    assert active["meta"]["count"] == 0


async def test_unknown_alert_is_404(client: AsyncClient) -> None:
    resp = await client.post(f"/api/v1/workspaces/{WS}/alerts/999999/read")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "alert_not_found"


# --- dashboard / diagnostics integration -------------------------------------


async def test_dashboard_includes_alert_summary(client: AsyncClient, session: AsyncSession) -> None:
    await _make_stale_price(session)
    await _generate(session)

    body = (await client.get(f"/api/v1/workspaces/{WS}/dashboard")).json()
    assert "alert_summary" in body
    assert body["alert_summary"]["active"] >= 1
    assert body["alert_summary"]["highest_severity"] in {"warning", "error", "critical"}
    assert any(a["category"] == "price" for a in body["alerts"])


async def test_diagnostics_includes_alert_counts(
    client: AsyncClient, session: AsyncSession
) -> None:
    listing = await _held_listing(session, "VUSA")
    await session.execute(delete(Price).where(Price.fund_listing_id == listing.id))
    await session.commit()  # missing price -> error alert
    await _generate(session)

    body = (await client.get(f"/api/v1/workspaces/{WS}/diagnostics")).json()
    assert body["active_alerts"] >= 1
    assert body["unread_alerts"] >= 1
    assert body["price_alerts"] >= 1
    assert body["error_alerts"] >= 1
