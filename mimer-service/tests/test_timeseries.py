from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import FundListing, Price


async def test_listing_price_time_series(client: AsyncClient) -> None:
    response = await client.get("/api/v1/fund-listings/1/time-series?kind=price&range=1y")
    assert response.status_code == 200
    body = response.json()
    assert body["subject"] == {"type": "fund_listing", "id": 1, "label": "VUSA"}
    assert body["kind"] == "price"
    assert body["status"] == "active"
    assert len(body["points"]) == 1
    point = body["points"][0]
    assert point["value"] == "75.00000000"
    assert point["source"] == "seed"


async def test_listing_distribution_time_series(client: AsyncClient) -> None:
    body = (
        await client.get("/api/v1/fund-listings/1/time-series?kind=distribution&range=all")
    ).json()
    assert body["kind"] == "distribution"
    assert len(body["points"]) == 4  # VUSA seeds four distributions


async def test_fund_distribution_time_series(client: AsyncClient) -> None:
    funds = (await client.get("/api/v1/funds")).json()["data"]
    vusa = next(f for f in funds if f["isin"] == "IE00B3XXRP09")
    body = (await client.get(f"/api/v1/funds/{vusa['id']}/time-series?kind=distribution")).json()
    assert body["subject"]["type"] == "fund"
    assert body["currency"] == "USD"
    assert len(body["points"]) == 4


async def test_fund_nav_is_unavailable_not_faked(client: AsyncClient) -> None:
    body = (await client.get("/api/v1/funds/1/time-series?kind=nav&range=all")).json()
    assert body["status"] == "unavailable"
    assert body["points"] == []


async def test_portfolio_value_time_series_is_derived(client: AsyncClient) -> None:
    body = (
        await client.get("/api/v1/workspaces/1/portfolio/time-series?kind=portfolio_value&range=1y")
    ).json()
    assert body["subject"]["type"] == "portfolio"
    assert body["currency"] == "GBP"
    assert body["source"] == "derived"
    assert body["status"] == "derived"
    assert len(body["points"]) == 1
    assert body["points"][0]["value"] == "12381.10"


async def test_portfolio_value_series_grows_with_price_history(
    client: AsyncClient, session: AsyncSession
) -> None:
    listing = await session.scalar(select(FundListing).order_by(FundListing.id))
    for i in range(3):
        session.add(
            Price(
                fund_listing_id=listing.id,
                price_date=date.today() - timedelta(days=i + 1),
                price=Decimal("70.00"),
                currency="GBP",
                source="stooq",
            )
        )
    await session.commit()

    body = (
        await client.get("/api/v1/workspaces/1/portfolio/time-series?kind=portfolio_value&range=1y")
    ).json()
    # One point per distinct price date now present.
    assert len(body["points"]) == 4


async def test_listing_time_series_not_found(client: AsyncClient) -> None:
    response = await client.get("/api/v1/fund-listings/999999/time-series?kind=price")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "fund_listing_not_found"
