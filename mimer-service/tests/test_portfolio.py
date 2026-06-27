from __future__ import annotations

from httpx import AsyncClient


async def test_summary_shape_and_decimals(client: AsyncClient) -> None:
    response = await client.get("/api/v1/portfolio/summary")
    assert response.status_code == 200
    body = response.json()

    assert body["base_currency"] == "GBP"
    # Decimal values are serialised as strings to avoid float precision issues.
    assert isinstance(body["total_market_value"], str)
    assert isinstance(body["trailing_12m_income"], str)
    assert len(body["positions"]) == 4

    vusa = next(p for p in body["positions"] if p["ticker"] == "VUSA")
    assert vusa["isin"] == "IE00B3XXRP09"
    assert vusa["market_value"] == "7500.00"  # 100 units * 75.00 GBP
    assert isinstance(vusa["units"], str)


async def test_gbx_listing_converted_to_pounds(client: AsyncClient) -> None:
    body = (await client.get("/api/v1/portfolio/summary")).json()
    isf = next(p for p in body["positions"] if p["ticker"] == "ISF")
    # 200 units * 850 GBX = 170000 pence => £1700.00
    assert isf["market_value"] == "1700.00"


async def test_create_then_delete_position(client: AsyncClient) -> None:
    funds = (await client.get("/api/v1/funds")).json()["data"]
    fund_id = funds[0]["id"]
    listings = (await client.get(f"/api/v1/funds/{fund_id}/listings")).json()["data"]
    listing_id = listings[0]["id"]

    payload = {
        "fund_listing_id": listing_id,
        "units": "10",
        "average_cost": "10.00",
        "cost_currency": "GBP",
        "account_name": "TEST",
    }
    created = await client.post("/api/v1/portfolio/positions", json=payload)
    assert created.status_code == 201
    position_id = created.json()["id"]

    deleted = await client.delete(f"/api/v1/portfolio/positions/{position_id}")
    assert deleted.status_code == 204
