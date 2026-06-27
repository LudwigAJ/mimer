"""API surface for the operational foundation: scheduler, budgets, fetch logs,
market-data plan, plus diagnostics/capabilities additions."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ScheduledJob, SourceFetchLog

# --- scheduler endpoints -----------------------------------------------------


async def test_scheduler_status(client: AsyncClient) -> None:
    body = (await client.get("/api/v1/scheduler/status")).json()
    assert body["active_jobs"] >= 1
    assert "poll_seconds" in body and "lease_seconds" in body
    assert isinstance(body["jobs"], list)
    # Seeded recurring jobs are not due yet (next_run_at one interval out).
    assert body["due_jobs"] == 0


async def test_scheduler_run_once_claims_due_job(
    client: AsyncClient, session: AsyncSession
) -> None:
    session.add(
        ScheduledJob(
            name="due_now",
            job_type="broker_csv_import",
            schedule_kind="daily",
            interval_seconds=86400,
            is_active=True,
            next_run_at=datetime.now(UTC) - timedelta(minutes=1),
        )
    )
    await session.commit()

    due = (await client.get("/api/v1/scheduler/due-jobs")).json()
    assert any(j["name"] == "due_now" for j in due["data"])

    result = (await client.post("/api/v1/scheduler/run-once")).json()
    assert result["claimed"] >= 1
    assert any(r["job"] == "due_now" for r in result["ran"])


# --- source budgets ----------------------------------------------------------


async def test_source_budgets_list_and_detail(client: AsyncClient) -> None:
    body = (await client.get("/api/v1/source-budgets")).json()
    assert body["meta"]["count"] >= 1
    by_name = {b["source_name"]: b for b in body["data"]}
    assert "openfigi" in by_name
    assert by_name["openfigi"]["batch_size"] == 10
    assert by_name["openfigi"]["allowed"] is True

    detail = (await client.get("/api/v1/source-budgets/openfigi")).json()
    assert detail["source_name"] == "openfigi"
    assert (await client.get("/api/v1/source-budgets/nope")).status_code == 404


async def test_source_budgets_never_expose_keys(client: AsyncClient) -> None:
    raw = (await client.get("/api/v1/source-budgets")).text
    # No credential-bearing field names in the serialized payload.
    assert "api_key" not in raw
    assert "apikey" not in raw.lower()


# --- source fetch logs -------------------------------------------------------


async def test_source_fetch_logs_listing_and_filter(
    client: AsyncClient, session: AsyncSession
) -> None:
    session.add(
        SourceFetchLog(
            source_name="openfigi",
            request_kind="resolve_identity",
            request_key="openfigi:resolve_identity:idvalue=IE00B3XXRP09",
            request_hash="abc123",
            status="success",
        )
    )
    await session.commit()

    body = (await client.get("/api/v1/source-fetch-logs?source=openfigi")).json()
    assert body["meta"]["count"] >= 1
    assert all(r["source_name"] == "openfigi" for r in body["data"])
    # Logs carry safe keys/labels only — never a raw secret.
    assert "SUPER" not in (await client.get("/api/v1/source-fetch-logs")).text


# --- market-data plan --------------------------------------------------------


async def test_market_data_plan_endpoint(client: AsyncClient) -> None:
    body = (await client.get("/api/v1/workspaces/1/market-data-plan")).json()
    assert body["workspace_id"] == 1
    assert body["include_constituents"] is True
    summary = body["summary"]
    assert summary["total_items"] >= 1
    assert summary["unresolved_constituents"] >= 1
    assert "openfigi" in summary["estimated_requests_by_source"]
    assert isinstance(body["items"], list)

    no_constituents = (
        await client.get("/api/v1/workspaces/1/market-data-plan?include_constituents=false")
    ).json()
    assert no_constituents["include_constituents"] is False
    assert not any(
        i["item_type"] == "resolve_constituent_identity" for i in no_constituents["items"]
    )


# --- diagnostics additions ---------------------------------------------------


async def test_diagnostics_include_operational_counts(
    client: AsyncClient, session: AsyncSession
) -> None:
    session.add(
        ScheduledJob(
            name="due_for_diag",
            job_type="broker_csv_import",
            schedule_kind="daily",
            interval_seconds=86400,
            is_active=True,
            next_run_at=datetime.now(UTC) - timedelta(minutes=1),
        )
    )
    await session.commit()

    glob = (await client.get("/api/v1/diagnostics")).json()
    assert glob["due_scheduled_jobs"] >= 1
    for field in ("running_jobs", "stuck_jobs", "recent_failed_fetches", "sources_in_backoff"):
        assert field in glob

    ws = (await client.get("/api/v1/workspaces/1/diagnostics")).json()
    assert ws["market_data_plan_items"] >= 1
    assert ws["unresolved_constituent_identities"] >= 1
    assert ws["estimated_market_data_requests"] >= 1


# --- capabilities additions --------------------------------------------------


async def test_capabilities_expose_operational_features(client: AsyncClient) -> None:
    body = (await client.get("/api/v1/capabilities")).json()
    features = body["features"]
    assert features["scheduler"] == "real"
    assert features["job_leasing"] == "real"
    assert features["source_rate_budgets"] == "real"
    assert features["source_fetch_logs"] == "real"
    assert features["market_data_planner"] == "real"
    # Future market-data workers still on the roadmap are advertised as planned.
    assert features["rates_curve_ingestion"] == "planned"
    assert "rates_curve_ingestion" in body["workers"]["planned"]
    # Constituent identity resolution + EOD price ingestion are real plumbing
    # (fixture-backed tests, OpenFIGI/Stooq/yfinance optional), advertised as fixture.
    assert features["constituent_identity_resolution"] == "fixture"
    assert "constituent_identity_resolution" in body["workers"]["fixture"]
    assert features["constituent_eod_price_ingestion"] == "fixture"
    assert "constituent_eod_price_ingestion" in body["workers"]["fixture"]
