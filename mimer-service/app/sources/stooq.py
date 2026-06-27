"""Stooq price source.

Free daily CSV endpoint. Stooq returns prices in the listing's native currency
and does not report the currency itself, so we pass through the listing currency.
"""

from __future__ import annotations

import csv
import io
from datetime import date
from decimal import Decimal, InvalidOperation

import httpx

from app.sources.base import PricePoint

_STOOQ_URL = "https://stooq.com/q/d/l/"

# Map an exchange code/MIC to a Stooq market suffix.
_EXCHANGE_SUFFIX = {
    "LSE": "uk",
    "XLON": "uk",
    "LON": "uk",
    "NASDAQ": "us",
    "XNAS": "us",
    "NYSE": "us",
    "XNYS": "us",
    "XETRA": "de",
    "XETR": "de",
    "GER": "de",
}


class StooqSource:
    name = "stooq"

    def _symbol(self, ticker: str, exchange: str | None) -> str:
        suffix = _EXCHANGE_SUFFIX.get((exchange or "").upper())
        base = ticker.lower()
        return f"{base}.{suffix}" if suffix else base

    async def fetch(
        self, *, ticker: str, exchange: str | None = None, currency: str | None = None
    ) -> list[PricePoint]:
        symbol = self._symbol(ticker, exchange)
        params = {"s": symbol, "i": "d"}
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.get(_STOOQ_URL, params=params)
            response.raise_for_status()
            text = response.text
        return self._parse(text, currency)

    def _parse(self, text: str, currency: str | None) -> list[PricePoint]:
        points: list[PricePoint] = []
        for row in csv.DictReader(io.StringIO(text)):
            raw_date = (row.get("Date") or "").strip()
            raw_close = (row.get("Close") or "").strip()
            if not raw_date or not raw_close or raw_close in {"N/D", "null"}:
                continue
            try:
                point = PricePoint(
                    price_date=date.fromisoformat(raw_date),
                    price=Decimal(raw_close),
                    currency=currency,
                )
            except (ValueError, InvalidOperation):
                continue
            points.append(point)
        return points
