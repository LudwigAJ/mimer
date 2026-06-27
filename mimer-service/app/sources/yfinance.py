"""Yahoo Finance price source (fallback).

Uses Yahoo's public chart JSON endpoint via httpx. This is the "yfinance-style"
fallback but intentionally does NOT depend on the heavyweight `yfinance` package.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import httpx

from app.sources.base import PricePoint

_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"

# Map an exchange code/MIC to a Yahoo symbol suffix ("" = US, no suffix).
_EXCHANGE_SUFFIX = {
    "LSE": ".L",
    "XLON": ".L",
    "LON": ".L",
    "XETRA": ".DE",
    "XETR": ".DE",
    "GER": ".DE",
}


class YFinanceSource:
    name = "yfinance"

    def _symbol(self, ticker: str, exchange: str | None) -> str:
        return f"{ticker.upper()}{_EXCHANGE_SUFFIX.get((exchange or '').upper(), '')}"

    async def fetch(
        self, *, ticker: str, exchange: str | None = None, currency: str | None = None
    ) -> list[PricePoint]:
        symbol = self._symbol(ticker, exchange)
        url = _CHART_URL.format(symbol=symbol)
        params = {"range": "1mo", "interval": "1d"}
        headers = {"User-Agent": "mimer-service/0.1"}
        async with httpx.AsyncClient(timeout=15.0, headers=headers) as client:
            response = await client.get(url, params=params)
            response.raise_for_status()
            payload = response.json()
        return self._parse(payload, currency)

    def _parse(self, payload: dict, currency: str | None) -> list[PricePoint]:
        results = (payload.get("chart") or {}).get("result") or []
        if not results:
            return []
        result = results[0]
        timestamps = result.get("timestamp") or []
        quote = ((result.get("indicators") or {}).get("quote") or [{}])[0]
        closes = quote.get("close") or []
        meta_currency = (result.get("meta") or {}).get("currency") or currency

        points: list[PricePoint] = []
        for ts, close in zip(timestamps, closes, strict=False):
            if close is None:
                continue
            points.append(
                PricePoint(
                    price_date=datetime.fromtimestamp(ts, tz=UTC).date(),
                    price=Decimal(str(close)),
                    currency=meta_currency,
                )
            )
        return points
