"""Instrument identity resolution.

Maps an external identifier (ticker/ISIN/FIGI/SEDOL/CUSIP) to one or more
candidate instruments. Providers are isolated behind a small protocol so the API
route never depends on a specific data source.

Two providers ship:

* ``stub`` (default) — an offline, deterministic fixture so the system works
  with no network access or API key. Knows the seeded instruments and one
  intentionally ambiguous ticker (``AMBI``).
* ``openfigi`` — calls the OpenFIGI v3 mapping API. Note OpenFIGI returns FIGI
  (not ISIN), so ticker lookups there are deliberately reported as lower
  confidence and will not auto-create a fund (see `app.services.identity`).
"""

from __future__ import annotations

from typing import Protocol

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.schemas.instrument import InstrumentCandidate, InstrumentRequest


class ResolverProvider(Protocol):
    name: str

    async def resolve(
        self, query: InstrumentRequest, *, session: AsyncSession | None = None
    ) -> list[InstrumentCandidate]: ...


# --- stub provider -----------------------------------------------------------


_STUB_INSTRUMENTS: list[dict[str, str]] = [
    {
        "isin": "IE00B3XXRP09",
        "figi": "BBG00JN2X9V8",
        "ticker": "VUSA",
        "exchange": "London Stock Exchange",
        "trading_currency": "GBP",
        "name": "Vanguard S&P 500 UCITS ETF",
    },
    {
        "isin": "IE0005042456",
        "figi": "BBG000RY8T29",
        "ticker": "ISF",
        "exchange": "London Stock Exchange",
        "trading_currency": "GBP",
        "name": "iShares Core FTSE 100 UCITS ETF",
    },
    {
        "isin": "IE0003UVYC20",
        "figi": "BBG01HRXQX17",
        "ticker": "JEPG",
        "exchange": "London Stock Exchange",
        "trading_currency": "GBP",
        "name": "JPMorgan Global Equity Premium Income Active UCITS ETF",
    },
]

# A deliberately ambiguous ticker (two different funds) for dev/testing.
_STUB_AMBIGUOUS_TICKERS: dict[str, list[dict[str, str]]] = {
    "AMBI": [
        {
            "isin": "US0000000001",
            "figi": "BBG000000001",
            "ticker": "AMBI",
            "exchange": "XNAS",
            "trading_currency": "USD",
            "name": "Ambiguous Co A",
        },
        {
            "isin": "GB0000000002",
            "figi": "BBG000000002",
            "ticker": "AMBI",
            "exchange": "LSE",
            "trading_currency": "GBP",
            "name": "Ambiguous Co B",
        },
    ],
}


class StubResolverProvider:
    name = "stub"

    async def resolve(
        self, query: InstrumentRequest, *, session: AsyncSession | None = None
    ) -> list[InstrumentCandidate]:
        value = query.symbol.strip().upper()

        if query.symbol_type == "ticker" and value in _STUB_AMBIGUOUS_TICKERS:
            return [
                InstrumentCandidate(source=self.name, confidence="medium", **row)
                for row in _STUB_AMBIGUOUS_TICKERS[value]
            ]

        field = {"ticker": "ticker", "isin": "isin", "figi": "figi"}.get(query.symbol_type)
        if field is None:  # sedol / cusip not modelled in the stub fixture
            return []

        matches = [row for row in _STUB_INSTRUMENTS if row.get(field, "").upper() == value]
        if query.exchange:
            matches = [r for r in matches if r["exchange"].upper() == query.exchange.upper()]
        if query.currency:
            matches = [
                r for r in matches if r["trading_currency"].upper() == query.currency.upper()
            ]

        strong_type = query.symbol_type in ("isin", "figi")
        single = len(matches) == 1
        confidence = "high" if (strong_type and single) else ("medium" if single else "medium")
        return [
            InstrumentCandidate(source=self.name, confidence=confidence, **row) for row in matches
        ]


# --- openfigi provider -------------------------------------------------------


_OPENFIGI_ID_TYPE = {
    "ticker": "TICKER",
    "isin": "ID_ISIN",
    "figi": "ID_BB_GLOBAL",
    "sedol": "ID_SEDOL",
    "cusip": "ID_CUSIP",
}
_OPENFIGI_URL = "https://api.openfigi.com/v3/mapping"


class OpenFigiResolverProvider:
    name = "openfigi"

    async def _call(self, job: dict[str, str], headers: dict[str, str]) -> object:
        """Perform the live OpenFIGI POST and return the parsed JSON payload."""
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(_OPENFIGI_URL, json=[job], headers=headers)
            response.raise_for_status()
            return response.json()

    def _candidates(self, payload: object, query: InstrumentRequest) -> list[InstrumentCandidate]:
        rows = payload[0].get("data", []) if payload else []  # type: ignore[index]
        # OpenFIGI does not return ISIN; only echo it when the input *was* an ISIN.
        isin = query.symbol if query.symbol_type == "isin" else None
        strong_type = query.symbol_type in ("isin", "figi", "sedol", "cusip")
        confidence = "high" if (strong_type and len(rows) == 1) else "medium"
        return [
            InstrumentCandidate(
                isin=isin,
                figi=row.get("figi"),
                ticker=row.get("ticker"),
                exchange=row.get("exchCode"),
                trading_currency=query.currency,
                name=row.get("name"),
                confidence=confidence,
                source=self.name,
            )
            for row in rows
        ]

    async def resolve(
        self, query: InstrumentRequest, *, session: AsyncSession | None = None
    ) -> list[InstrumentCandidate]:
        settings = get_settings()
        job: dict[str, str] = {
            "idType": _OPENFIGI_ID_TYPE[query.symbol_type],
            "idValue": query.symbol,
        }
        if query.exchange:
            job["exchCode"] = query.exchange
        if query.currency:
            job["currency"] = query.currency

        # The API key only ever travels in the request header — never in the
        # request key / fetch log (which use the secrets-free ``params`` below).
        headers = {"Content-Type": "application/json"}
        if settings.openfigi_api_key:
            headers["X-OPENFIGI-APIKEY"] = settings.openfigi_api_key

        if session is None:
            # Unguarded path (back-compat). Still no network access in tests.
            return self._candidates(await self._call(job, headers), query)

        # Guarded path: budget check + recent-success cache + fetch logging.
        # Imported here to avoid a module-level import cycle.
        from app.services import source_budget

        result, payload = await source_budget.guarded_fetch(
            session,
            source=self.name,
            request_kind="resolve_identity",
            params=job,  # idType/idValue/exchCode/currency only — no secrets
            endpoint_label="api.openfigi.com/v3/mapping",
            method="POST",
            ttl_seconds=settings.request_cache_ttl_seconds,
            fetch=lambda: self._call(job, headers),
        )
        await session.commit()
        if result.cache_hit or payload is None:
            # Cache hit or budget block: degrade gracefully (no stale parse).
            return []
        return self._candidates(payload, query)


# --- registry ----------------------------------------------------------------


_PROVIDERS: dict[str, ResolverProvider] = {
    StubResolverProvider.name: StubResolverProvider(),
    OpenFigiResolverProvider.name: OpenFigiResolverProvider(),
}


def get_provider(name: str | None = None) -> ResolverProvider:
    provider_name = name or get_settings().resolver_default_provider
    provider = _PROVIDERS.get(provider_name)
    if provider is None:
        raise ValueError(f"Unknown resolver provider: {provider_name!r}")
    return provider


async def resolve_identifier(
    query: InstrumentRequest,
    provider_name: str | None = None,
    *,
    session: AsyncSession | None = None,
) -> list[InstrumentCandidate]:
    """Resolve an identifier. Passing ``session`` enables budget/fetch-log
    guarding for live providers (e.g. OpenFIGI); the offline stub ignores it."""
    return await get_provider(provider_name).resolve(query, session=session)
