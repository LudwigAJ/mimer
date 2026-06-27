"""Price source interface."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Protocol


@dataclass(frozen=True)
class PricePoint:
    price_date: date
    price: Decimal
    currency: str | None


class PriceSource(Protocol):
    name: str

    async def fetch(
        self, *, ticker: str, exchange: str | None = None, currency: str | None = None
    ) -> list[PricePoint]:
        """Return daily price points for a listing (most recent window)."""
        ...
