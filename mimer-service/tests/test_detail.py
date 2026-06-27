from __future__ import annotations

from httpx import AsyncClient


async def _jpm_fund_id(client: AsyncClient) -> int:
    funds = (await client.get("/api/v1/funds")).json()["data"]
    return next(f["id"] for f in funds if f["isin"] == "IE0003UVYC20")


async def test_fund_detail_shape(client: AsyncClient) -> None:
    fund_id = await _jpm_fund_id(client)
    response = await client.get(f"/api/v1/funds/{fund_id}/detail")
    assert response.status_code == 200
    body = response.json()
    assert {
        "fund",
        "listings",
        "distributions",
        "holdings",
        "documents",
        "job_runs",
        "identifiers",
        "freshness",
    } <= set(body.keys())

    assert body["fund"]["isin"] == "IE0003UVYC20"
    assert body["fund"]["source"] == "seed"
    # JPMorgan fund has three listings.
    assert len(body["listings"]) == 3
    listing = body["listings"][0]
    assert "price_summary" in listing
    assert listing["price_summary"]["points"] >= 1
    assert listing["latest_price"] is not None
    assert len(body["distributions"]) == 4
    assert len(body["holdings"]) == 5


async def test_fund_detail_include_flags(client: AsyncClient) -> None:
    fund_id = await _jpm_fund_id(client)
    response = await client.get(
        f"/api/v1/funds/{fund_id}/detail?include_prices=false&include_holdings=false"
    )
    body = response.json()
    assert body["holdings"] == []
    for listing in body["listings"]:
        assert listing["prices"] == []
        # The summary is still computed even when point lists are omitted.
        assert "price_summary" in listing


async def test_fund_detail_history_days_bounds_prices(client: AsyncClient) -> None:
    fund_id = await _jpm_fund_id(client)
    # Seed prices are dated today, so a 1-day window still includes them.
    body = (await client.get(f"/api/v1/funds/{fund_id}/detail?history_days=1")).json()
    assert all(ln["price_summary"]["points"] <= 2 for ln in body["listings"])


async def test_fund_detail_not_found(client: AsyncClient) -> None:
    response = await client.get("/api/v1/funds/999999/detail")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "fund_not_found"
