"""The OpenFIGI resolver, when given a session, honours the source budget and
records fetch logs — and never makes a live call while budget-blocked."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.instrument import InstrumentRequest
from app.services import source_budget, source_requests
from app.services.resolver import OpenFigiResolverProvider


async def test_openfigi_resolver_blocked_by_budget_makes_no_call(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = datetime.now(UTC)
    await source_budget.apply_backoff(session, "openfigi", seconds=120, now=now)
    await session.commit()

    provider = OpenFigiResolverProvider()

    async def boom(*args, **kwargs):  # pragma: no cover - must never run
        raise AssertionError("a live OpenFIGI call was attempted while in backoff")

    monkeypatch.setattr(provider, "_call", boom)

    req = InstrumentRequest(symbol="IE00B3XXRP09", symbol_type="isin")
    result = await provider.resolve(req, session=session)

    # Degrades gracefully (no candidates) and records a rate_limited attempt.
    assert result == []
    logs = await source_requests.list_fetch_logs(session, source="openfigi", status="rate_limited")
    assert logs and logs[0].request_kind == "resolve_identity"
    # The request key carries no secret (only idType/idValue).
    assert "APIKEY" not in logs[0].request_key.upper()


async def test_openfigi_resolver_guarded_success_logs_and_consumes_budget(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = OpenFigiResolverProvider()

    async def fake_call(job, headers):
        return [{"data": [{"figi": "BBG00JN2X9V8", "ticker": "VUSA", "exchCode": "LN"}]}]

    monkeypatch.setattr(provider, "_call", fake_call)

    req = InstrumentRequest(symbol="IE00B3XXRP09", symbol_type="isin")
    result = await provider.resolve(req, session=session)
    assert result and result[0].figi == "BBG00JN2X9V8"

    logs = await source_requests.list_fetch_logs(session, source="openfigi", status="success")
    assert logs and logs[0].endpoint_label == "api.openfigi.com/v3/mapping"
