"""Documents: fixture provider, hashing, ingestion, change detection, reads.

All offline — the fixture provider never touches the network, mirroring the
holdings/distribution/fx test pattern. No PDF parsing.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    DocumentSnapshot,
    Fund,
    FundListing,
    JobRun,
    PortfolioPosition,
    Price,
    ScheduledJob,
    Workspace,
)
from app.services import diagnostics as diagnostics_service
from app.services.documents import compute_document_hash
from app.sources.documents import DocumentRecord, StaticDocumentSource, get_document_source
from app.workers.run import run_job

_VUSA = "IE00B3XXRP09"


async def _vusa(session: AsyncSession) -> Fund:
    fund = await session.scalar(select(Fund).where(Fund.isin == _VUSA))
    assert fund is not None
    return fund


# --- fixture provider + hashing ----------------------------------------------


async def test_document_fixture_returns_records_for_seeded_isin() -> None:
    records = await StaticDocumentSource().fetch_documents(isin=_VUSA)
    assert {r.document_type for r in records} == {"factsheet", "kiid", "prospectus"}
    assert all(r.source == "document_fixture" for r in records)
    assert all(r.content_text and r.title and r.document_url for r in records)
    # Unknown ISIN -> empty, not an error.
    assert await StaticDocumentSource().fetch_documents(isin="ZZ0000000000") == []


def test_compute_document_hash_is_deterministic_and_content_sensitive() -> None:
    base = DocumentRecord(
        document_type="factsheet",
        title="X",
        source="document_fixture",
        document_url="https://x/y.pdf",
        document_date=date(2026, 5, 31),
        content_text="hello",
    )
    same = compute_document_hash(base)
    assert same == compute_document_hash(base)
    assert len(same) == 64  # sha256 hex
    changed = compute_document_hash(
        DocumentRecord(
            document_type="factsheet",
            title="X",
            source="document_fixture",
            document_url="https://x/y.pdf",
            document_date=date(2026, 5, 31),
            content_text="goodbye",
        )
    )
    assert changed != same


def test_compute_document_hash_falls_back_to_metadata() -> None:
    # No content -> stable metadata hash (so a URL/date change is still detected).
    record = DocumentRecord(
        document_type="kid", title="K", source="s", document_url="u", document_date=None
    )
    assert compute_document_hash(record) == compute_document_hash(record)


def test_document_source_registry_unknown_raises() -> None:
    assert get_document_source("document_fixture").name == "document_fixture"
    try:
        get_document_source("nope")
    except ValueError as exc:
        assert "Unknown document source" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected ValueError")


# --- ingestion via the worker ------------------------------------------------


async def test_document_ingestion_single_fund_inserts_and_counts(session: AsyncSession) -> None:
    fund = await _vusa(session)
    run = await run_job(session, "document_snapshot_ingestion", fund_id=fund.id)
    assert run.status == "success"
    assert run.records_inserted == 3
    assert run.records_updated == 0
    assert run.records_failed == 0
    assert run.source == "document_fixture"

    rows = (
        (
            await session.execute(
                select(DocumentSnapshot).where(
                    DocumentSnapshot.fund_id == fund.id,
                    DocumentSnapshot.source == "document_fixture",
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 3
    assert all(r.content_hash and r.change_status == "new" for r in rows)
    # Seed documents (different source) are left untouched.
    seed_count = await session.scalar(
        select(func.count())
        .select_from(DocumentSnapshot)
        .where(DocumentSnapshot.fund_id == fund.id, DocumentSnapshot.source == "seed")
    )
    assert seed_count == 2


async def test_document_ingestion_bulk_runs_all_funds(session: AsyncSession) -> None:
    run = await run_job(session, "document_snapshot_ingestion")
    assert run.status == "success"
    # VUSA 3 + ISF 4 + JPM 3 fixture documents.
    assert run.records_inserted == 10
    total = await session.scalar(
        select(func.count())
        .select_from(DocumentSnapshot)
        .where(DocumentSnapshot.source == "document_fixture")
    )
    assert total == 10


async def test_document_ingestion_is_idempotent(session: AsyncSession) -> None:
    fund = await _vusa(session)
    await run_job(session, "document_snapshot_ingestion", fund_id=fund.id)
    run2 = await run_job(session, "document_snapshot_ingestion", fund_id=fund.id)
    assert run2.records_inserted == 0  # unchanged content -> no new rows
    assert run2.records_updated == 0
    assert run2.status == "success"

    count = await session.scalar(
        select(func.count())
        .select_from(DocumentSnapshot)
        .where(DocumentSnapshot.fund_id == fund.id, DocumentSnapshot.source == "document_fixture")
    )
    assert count == 3


async def test_changed_content_creates_new_snapshot(session: AsyncSession, monkeypatch) -> None:
    import app.workers.run as worker

    def _rec(text: str) -> DocumentRecord:
        return DocumentRecord(
            document_type="factsheet",
            title="VUSA Factsheet",
            source="document_fixture",
            document_url="https://x/vusa.pdf",
            document_date=date(2026, 5, 31),
            content_text=text,
        )

    class Fake:
        name = "document_fixture"

        def __init__(self, text: str) -> None:
            self._text = text

        async def fetch_documents(self, *, isin):
            return [_rec(self._text)] if isin == _VUSA else []

    fund = await _vusa(session)
    monkeypatch.setattr(worker, "get_document_source", lambda name=None: Fake("v1"))
    first = await run_job(session, "document_snapshot_ingestion", fund_id=fund.id)
    assert first.records_inserted == 1

    # New content -> a NEW snapshot (history preserved), marked "changed".
    monkeypatch.setattr(worker, "get_document_source", lambda name=None: Fake("v2"))
    second = await run_job(session, "document_snapshot_ingestion", fund_id=fund.id)
    assert second.records_inserted == 1

    # Same content again -> unchanged, no new row.
    third = await run_job(session, "document_snapshot_ingestion", fund_id=fund.id)
    assert third.records_inserted == 0
    assert third.records_updated == 0

    rows = (
        (
            await session.execute(
                select(DocumentSnapshot)
                .where(
                    DocumentSnapshot.fund_id == fund.id,
                    DocumentSnapshot.document_type == "factsheet",
                    DocumentSnapshot.source == "document_fixture",
                )
                .order_by(DocumentSnapshot.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 2  # both versions kept
    assert rows[0].change_status == "new"
    assert rows[1].change_status == "changed"
    assert rows[1].previous_content_hash == rows[0].content_hash
    assert rows[1].previous_snapshot_id == rows[0].id


async def test_document_ingestion_missing_fund_records_failure(session: AsyncSession) -> None:
    run = await run_job(session, "document_snapshot_ingestion", fund_id=999999)
    assert run.status == "failed"
    assert "not found" in (run.message or "")


async def test_document_ingestion_claims_queued_backfill(session: AsyncSession) -> None:
    fund = await _vusa(session)
    queued = JobRun(job_type="document_snapshot_ingestion", status="queued", fund_id=fund.id)
    session.add(queued)
    await session.commit()
    queued_id = queued.id

    run = await run_job(session, "document_snapshot_ingestion", fund_id=fund.id)
    assert run.id == queued_id  # reused the queued backfill run
    assert run.status == "success"
    assert run.records_inserted == 3


async def test_scheduled_document_job_runs_real_not_stub(session: AsyncSession) -> None:
    from app.services import jobs as jobs_service

    job = await session.scalar(
        select(ScheduledJob).where(ScheduledJob.job_type == "document_snapshot_ingestion")
    )
    assert job is not None
    run = await jobs_service.trigger_job(session, job.id)
    assert run.status == "success"
    assert run.status != "success_stub"
    assert (run.records_inserted or 0) > 0


# --- read APIs ---------------------------------------------------------------


async def test_fund_documents_endpoint_shape_and_provenance(
    client: AsyncClient, session: AsyncSession
) -> None:
    fund = await _vusa(session)
    await run_job(session, "document_snapshot_ingestion", fund_id=fund.id)

    body = (await client.get(f"/api/v1/funds/{fund.id}/documents")).json()
    items = body["data"]
    assert items
    top = items[0]
    for field in (
        "id",
        "fund_id",
        "fund_name",
        "document_type",
        "title",
        "url",
        "document_date",
        "content_hash",
        "change_status",
        "source",
        "status",
        "fetched_at",
    ):
        assert field in top
    fixture = next(d for d in items if d["source"] == "document_fixture")
    assert fixture["fund_name"] == fund.name
    assert fixture["content_hash"]
    assert fixture["change_status"] == "new"


async def test_fund_documents_filter_by_type(client: AsyncClient, session: AsyncSession) -> None:
    fund = await _vusa(session)
    await run_job(session, "document_snapshot_ingestion", fund_id=fund.id)
    body = (await client.get(f"/api/v1/funds/{fund.id}/documents?document_type=factsheet")).json()
    assert {d["document_type"] for d in body["data"]} == {"factsheet"}


async def test_fund_documents_latest_only(
    client: AsyncClient, session: AsyncSession, monkeypatch
) -> None:
    import app.workers.run as worker

    def _rec(text: str) -> DocumentRecord:
        return DocumentRecord(
            document_type="factsheet",
            title="VUSA Factsheet",
            source="document_fixture",
            document_url="https://x/vusa.pdf",
            document_date=date.today(),  # newest, so it wins latest_only
            content_text=text,
        )

    class Fake:
        name = "document_fixture"

        def __init__(self, text: str) -> None:
            self._text = text

        async def fetch_documents(self, *, isin):
            return [_rec(self._text)] if isin == _VUSA else []

    fund = await _vusa(session)
    monkeypatch.setattr(worker, "get_document_source", lambda name=None: Fake("v1"))
    await run_job(session, "document_snapshot_ingestion", fund_id=fund.id)
    monkeypatch.setattr(worker, "get_document_source", lambda name=None: Fake("v2"))
    await run_job(session, "document_snapshot_ingestion", fund_id=fund.id)

    full = (await client.get(f"/api/v1/funds/{fund.id}/documents?document_type=factsheet")).json()[
        "data"
    ]
    latest = (
        await client.get(
            f"/api/v1/funds/{fund.id}/documents?document_type=factsheet&latest_only=true"
        )
    ).json()["data"]
    # Full history has the seed factsheet + both fixture versions; latest_only
    # collapses to one per (type) — the newest.
    assert len(full) >= 2
    assert len(latest) == 1
    assert latest[0]["change_status"] == "changed"


async def test_get_document_by_id(client: AsyncClient, session: AsyncSession) -> None:
    fund = await _vusa(session)
    await run_job(session, "document_snapshot_ingestion", fund_id=fund.id)
    doc = await session.scalar(
        select(DocumentSnapshot).where(DocumentSnapshot.source == "document_fixture")
    )
    assert doc is not None
    body = (await client.get(f"/api/v1/documents/{doc.id}")).json()
    assert body["id"] == doc.id
    assert body["content_hash"] == doc.content_hash
    # Unknown id -> 404.
    missing = await client.get("/api/v1/documents/999999")
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "document_not_found"


async def test_fund_detail_includes_documents(client: AsyncClient, session: AsyncSession) -> None:
    fund = await _vusa(session)
    await run_job(session, "document_snapshot_ingestion", fund_id=fund.id)
    detail = (await client.get(f"/api/v1/funds/{fund.id}/detail")).json()
    sources = {d["source"] for d in detail["documents"]}
    assert "document_fixture" in sources
    assert detail["freshness"]["documents"] in {"fresh", "stale"}
    assert all(d["fund_name"] == fund.name for d in detail["documents"])


async def test_dashboard_includes_documents(client: AsyncClient, session: AsyncSession) -> None:
    await run_job(session, "document_snapshot_ingestion")
    dashboard = (await client.get("/api/v1/workspaces/1/dashboard")).json()
    assert dashboard["documents"]
    assert any(d["source"] == "document_fixture" for d in dashboard["documents"])
    assert all(d["fund_name"] for d in dashboard["documents"])


# --- diagnostics -------------------------------------------------------------


async def test_diagnostics_count_new_and_changed_documents(
    client: AsyncClient, session: AsyncSession
) -> None:
    await run_job(session, "document_snapshot_ingestion")
    diag = (await client.get("/api/v1/workspaces/1/diagnostics")).json()
    # Every seeded held fund has a factsheet -> none missing key docs.
    assert diag["missing_documents"] == 0
    assert diag["new_documents"] == 10  # all fixture docs are first-version
    assert diag["changed_documents"] == 0


async def test_diagnostics_count_missing_documents(session: AsyncSession) -> None:
    ws = Workspace(name="DocGap", base_currency="GBP")
    session.add(ws)
    await session.flush()
    listing = FundListing(
        ticker="BARE",
        trading_currency="GBP",
        currency_unit="GBP",
        status="active",
        prices=[
            Price(price_date=date.today(), price=Decimal("10"), currency="GBP", source="stooq")
        ],
    )
    fund = Fund(isin="IE00NODOCS01", name="No Docs ETF", status="active", listings=[listing])
    session.add(fund)
    await session.flush()
    session.add(
        PortfolioPosition(workspace_id=ws.id, fund_listing_id=listing.id, units=Decimal("1"))
    )
    await session.commit()

    diag = await diagnostics_service.workspace_diagnostics(session, ws.id)
    assert diag.missing_documents >= 1


async def test_diagnostics_failed_document_jobs(session: AsyncSession) -> None:
    session.add(JobRun(job_type="document_snapshot_ingestion", status="failed"))
    await session.commit()
    diag = await diagnostics_service.global_diagnostics(session)
    assert diag.failed_document_jobs >= 1


# --- capability registry -----------------------------------------------------


async def test_document_capability_registered_and_implemented(client: AsyncClient) -> None:
    body = (await client.get("/api/v1/data-sources/capabilities?data_type=documents")).json()
    names = {c["source_name"] for c in body["data"]}
    assert "document_fixture" in names
    fixture = next(c for c in body["data"] if c["source_name"] == "document_fixture")
    assert fixture["adapter_status"] == "implemented"
    assert "documents" in fixture["data_types"]


async def test_capabilities_endpoint_marks_document_fixture(client: AsyncClient) -> None:
    body = (await client.get("/api/v1/capabilities")).json()
    assert body["features"]["document_snapshot_ingestion"] == "fixture"
    status = {d["name"]: d["status"] for d in body["data_types"]}["documents"]
    assert status == "fixture"
    assert body["configured_sources"]["documents"] == "document_fixture"
    assert body["environment"]["document_source_default"] == "document_fixture"
