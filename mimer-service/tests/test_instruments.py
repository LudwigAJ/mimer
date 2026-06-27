from __future__ import annotations

import pytest
from httpx import AsyncClient

from app.schemas.instrument import InstrumentCandidate, InstrumentRequest
from app.services import resolver


async def test_resolve_known_isin_reuses_existing_fund(client: AsyncClient) -> None:
    """A seeded ISIN resolves (via the stub provider) and reuses the fund."""
    before = (await client.get("/api/v1/funds")).json()["meta"]["count"]

    response = await client.post(
        "/api/v1/instruments",
        json={"symbol": "IE00B3XXRP09", "symbol_type": "isin"},
    )
    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "pending"
    assert body["resolved"]["isin"] == "IE00B3XXRP09"
    assert body["created"] == {"fund": False, "listing": False}
    assert len(body["job_run_ids"]) == 5  # one queued backfill run per job type

    after = (await client.get("/api/v1/funds")).json()["meta"]["count"]
    assert after == before  # no duplicate fund


async def test_resolve_creates_new_fund_and_listing(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_resolve(query: InstrumentRequest, provider_name: str | None = None, **_):
        return [
            InstrumentCandidate(
                isin="IE00NEWFUND01",
                figi="BBG00NEWFUND1",
                ticker="NEWX",
                exchange="LSE",
                trading_currency="GBP",
                name="Brand New UCITS ETF",
                confidence="high",
                source="stub",
            )
        ]

    monkeypatch.setattr(resolver, "resolve_identifier", fake_resolve)

    before = (await client.get("/api/v1/funds")).json()["meta"]["count"]
    response = await client.post(
        "/api/v1/instruments",
        json={"symbol": "NEWX", "symbol_type": "ticker", "exchange": "LSE", "currency": "GBP"},
    )
    assert response.status_code == 202
    body = response.json()
    assert body["created"] == {"fund": True, "listing": True}
    new_fund = (await client.get(f"/api/v1/funds/{body['fund_id']}")).json()
    assert new_fund["isin"] == "IE00NEWFUND01"
    assert new_fund["status"] == "pending"

    after = (await client.get("/api/v1/funds")).json()["meta"]["count"]
    assert after == before + 1


async def test_duplicate_isin_does_not_duplicate_fund(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_resolve(query: InstrumentRequest, provider_name: str | None = None, **_):
        return [
            InstrumentCandidate(
                isin="IE00DUPENS001",
                ticker="DUPE",
                exchange="LSE",
                trading_currency="GBP",
                name="Dup Test ETF",
                confidence="high",
                source="stub",
            )
        ]

    monkeypatch.setattr(resolver, "resolve_identifier", fake_resolve)

    first = await client.post(
        "/api/v1/instruments",
        json={"symbol": "DUPE", "symbol_type": "ticker", "exchange": "LSE", "currency": "GBP"},
    )
    second = await client.post(
        "/api/v1/instruments",
        json={"symbol": "DUPE", "symbol_type": "ticker", "exchange": "LSE", "currency": "GBP"},
    )
    assert first.json()["created"]["fund"] is True
    assert second.json()["created"]["fund"] is False
    assert first.json()["fund_id"] == second.json()["fund_id"]


async def test_same_fund_can_have_multiple_listings(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Resolving a new listing of an existing fund (by ISIN) adds a listing only."""

    async def fake_resolve(query: InstrumentRequest, provider_name: str | None = None, **_):
        return [
            InstrumentCandidate(
                isin="IE00B3XXRP09",  # existing VUSA fund
                ticker="VUSD",  # new USD listing
                exchange="LSE",
                trading_currency="USD",
                name="Vanguard S&P 500 UCITS ETF",
                confidence="high",
                source="stub",
            )
        ]

    monkeypatch.setattr(resolver, "resolve_identifier", fake_resolve)

    funds = (await client.get("/api/v1/funds")).json()["data"]
    vusa = next(f for f in funds if f["isin"] == "IE00B3XXRP09")
    before = (await client.get(f"/api/v1/funds/{vusa['id']}/listings")).json()["meta"]["count"]

    response = await client.post(
        "/api/v1/instruments",
        json={"symbol": "VUSD", "symbol_type": "ticker", "exchange": "LSE", "currency": "USD"},
    )
    assert response.status_code == 202
    assert response.json()["created"] == {"fund": False, "listing": True}

    after = (await client.get(f"/api/v1/funds/{vusa['id']}/listings")).json()["meta"]["count"]
    assert after == before + 1


async def test_ambiguous_ticker_returns_candidates(client: AsyncClient) -> None:
    response = await client.post(
        "/api/v1/instruments",
        json={"symbol": "AMBI", "symbol_type": "ticker"},
    )
    assert response.status_code == 409
    body = response.json()
    assert body["status"] == "ambiguous"
    assert len(body["candidates"]) == 2


async def test_unknown_symbol_is_404(client: AsyncClient) -> None:
    response = await client.post(
        "/api/v1/instruments",
        json={"symbol": "ZZZZ", "symbol_type": "ticker"},
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "instrument_not_found"
