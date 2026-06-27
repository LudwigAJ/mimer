from __future__ import annotations

from httpx import AsyncClient


async def test_list_funds(client: AsyncClient) -> None:
    response = await client.get("/api/v1/funds")
    assert response.status_code == 200
    body = response.json()
    assert body["meta"]["count"] == 3
    names = {fund["name"] for fund in body["data"]}
    assert "Vanguard S&P 500 UCITS ETF" in names


async def test_fund_has_multiple_listings(client: AsyncClient) -> None:
    """The JPMorgan fund is one fund with several listings (ticker != identity)."""
    funds = (await client.get("/api/v1/funds")).json()["data"]
    jpm = next(f for f in funds if f["isin"] == "IE0003UVYC20")

    response = await client.get(f"/api/v1/funds/{jpm['id']}/listings")
    assert response.status_code == 200
    listings = response.json()["data"]
    assert len(listings) == 3
    tickers = {listing["ticker"] for listing in listings}
    assert {"JEPG", "JEGP"} <= tickers


async def test_fund_not_found_returns_structured_error(client: AsyncClient) -> None:
    response = await client.get("/api/v1/funds/999999")
    assert response.status_code == 404
    assert response.json() == {"error": {"code": "fund_not_found", "message": "Fund not found"}}


async def test_fund_exposes_source_status_provenance(client: AsyncClient) -> None:
    fund = (await client.get("/api/v1/funds")).json()["data"][0]
    # Provenance / lifecycle / freshness fields the GUI relies on.
    for field in ("source", "status", "last_refreshed_at", "created_at", "updated_at"):
        assert field in fund
    assert fund["source"] == "seed"
    assert fund["status"] == "active"


async def test_listing_exposes_status_and_freshness(client: AsyncClient) -> None:
    funds = (await client.get("/api/v1/funds")).json()["data"]
    listings = (await client.get(f"/api/v1/funds/{funds[0]['id']}/listings")).json()["data"]
    listing = listings[0]
    for field in ("status", "last_price_at", "last_resolved_at"):
        assert field in listing
