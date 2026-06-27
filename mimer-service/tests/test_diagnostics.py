from __future__ import annotations

from httpx import AsyncClient


async def test_global_diagnostics(client: AsyncClient) -> None:
    response = await client.get("/api/v1/diagnostics")
    assert response.status_code == 200
    body = response.json()
    # Five seeded listings, all priced today.
    assert body["fresh"] == 5
    assert body["stale"] == 0
    assert body["mock_or_seed"] == 3
    assert body["failed_jobs"] == 0
    assert body["queued_jobs"] == 0


async def test_workspace_diagnostics_is_scoped(client: AsyncClient) -> None:
    response = await client.get("/api/v1/workspaces/1/diagnostics")
    assert response.status_code == 200
    body = response.json()
    assert body["workspace_id"] == 1
    # The workspace holds four of the five listings.
    assert body["fresh"] == 4


async def test_diagnostics_counts_queued_jobs(client: AsyncClient) -> None:
    # Resolving a known ISIN queues backfill runs (queued job_runs).
    resolved = await client.post(
        "/api/v1/instruments", json={"symbol": "IE00B3XXRP09", "symbol_type": "isin"}
    )
    assert resolved.status_code == 202
    body = (await client.get("/api/v1/diagnostics")).json()
    assert body["queued_jobs"] >= 1


async def test_workspace_diagnostics_unknown_workspace_is_404(client: AsyncClient) -> None:
    response = await client.get("/api/v1/workspaces/999999/diagnostics")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "workspace_not_found"


async def test_diagnostics_source_readiness_counts(client: AsyncClient) -> None:
    body = (await client.get("/api/v1/diagnostics")).json()
    # Matrix-derived (deterministic): iShares ISF holdings is the one verified-live source.
    assert body["verified_live_sources"] == 1
    assert body["candidate_live_sources"] >= 2  # jpm holdings + vanguard distributions
    assert body["planned_live_sources"] >= 1
    assert body["scheduler_safe_sources"] >= 1
    # Required data types still lacking a verified-live source (rates/fx/prices).
    assert body["missing_required_live_sources"] >= 1
    # The seed schedules one live source (price_ingestion -> stooq) and several fixtures —
    # a fixture default scheduled in production must be visible, never mistaken for live.
    assert body["scheduled_live_jobs"] == 1
    assert body["fixture_scheduled_jobs"] >= 4
    # No live fetches happen in tests, so no live-source failures.
    assert body["live_source_failures"] == 0


async def test_diagnostics_fund_coverage_counts(client: AsyncClient) -> None:
    body = (await client.get("/api/v1/diagnostics")).json()
    # Target-fund coverage (deterministic from the in-code matrix).
    assert body["target_funds_total"] == 3
    assert body["target_funds_with_live_price"] == 3  # Stooq for all three
    assert body["target_funds_with_live_holdings"] == 1  # ISF only (verified iShares config)
    # A fixture-fed data type is never counted as live readiness.
    assert body["target_funds_with_live_facts"] == 0
    assert body["target_funds_with_live_documents"] == 0
    assert body["fund_sources_verified_live"] == 1
    assert body["fund_sources_fixture_only"] == 6  # facts + documents across 3 funds
    assert body["fund_source_blockers"] >= 1


async def test_diagnostics_does_not_mark_fixtures_as_live(client: AsyncClient) -> None:
    body = (await client.get("/api/v1/diagnostics")).json()
    # Distributions are candidate/planned only — no target fund has live distributions yet.
    assert body["target_funds_with_live_distributions"] == 0
