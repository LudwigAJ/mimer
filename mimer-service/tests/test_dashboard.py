from __future__ import annotations

from httpx import AsyncClient

_SECTIONS = {
    "workspace",
    "portfolio_summary",
    "positions",
    "funds",
    "fund_listings",
    "distributions",
    "holdings",
    "exposures",
    "documents",
    "alerts",
    "scheduled_jobs",
    "job_runs",
    "fx_rates",
    "data_quality",
    "freshness",
}


async def test_dashboard_has_all_sections(client: AsyncClient) -> None:
    response = await client.get("/api/v1/workspaces/1/dashboard")
    assert response.status_code == 200
    body = response.json()
    assert _SECTIONS <= set(body.keys())

    assert body["workspace"] == {"id": 1, "name": "Personal", "base_currency": "GBP"}
    assert body["portfolio_summary"]["total_market_value"] == "12381.10"
    assert body["portfolio_summary"]["source"] == "derived"
    # All held data is seeded, so the summary is honestly flagged as seed.
    assert body["portfolio_summary"]["status"] == "seed"
    assert len(body["positions"]) == 4
    # 3 funds held (JPMorgan contributes two listings).
    assert len(body["funds"]) == 3
    assert len(body["fund_listings"]) == 4


async def test_dashboard_listings_carry_price_and_freshness(client: AsyncClient) -> None:
    body = (await client.get("/api/v1/workspaces/1/dashboard")).json()
    vusa = next(ln for ln in body["fund_listings"] if ln["ticker"] == "VUSA")
    assert vusa["latest_price"] == "75.00000000"
    assert vusa["price_source"] == "seed"
    assert vusa["freshness"] == "fresh"
    assert vusa["latest_price_currency"] == "GBP"


async def test_dashboard_data_quality_and_freshness(client: AsyncClient) -> None:
    body = (await client.get("/api/v1/workspaces/1/dashboard")).json()
    dq = body["data_quality"]
    assert dq["fresh"] == 4  # four held listings, all priced today
    assert dq["mock_or_seed"] == 3  # three seed-sourced funds
    assert dq["failed"] == 0
    assert body["freshness"]["prices"] == "fresh"
    assert body["freshness"]["fund_facts"] == "fresh"


async def test_dashboard_unknown_workspace_is_404(client: AsyncClient) -> None:
    response = await client.get("/api/v1/workspaces/999999/dashboard")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "workspace_not_found"
