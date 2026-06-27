"""Constituent / instrument-listing EOD price sources, behind a protocol.

Fetch end-of-day prices for a *resolved* ``instrument_listing`` (a tradable
constituent like AAPL / XNAS / USD). Three providers ship:

* ``instrument_price_fixture`` — an offline, deterministic provider that knows the
  seeded constituents (Apple, Microsoft, Shell, AstraZeneca, ...). It generates a
  bounded daily OHLC history ending today with no network and no randomness, so
  the worker, budgets, fetch logs and tests all work offline. Unknown listings
  simply return no data (``no_data``), never an error.
* ``stooq`` / ``yfinance`` — live wrappers around the existing fund-price
  adapters. Every symbol is fetched through ``guarded_fetch`` (cache → budget →
  fetch-log → fetch), one symbol at a time with a politeness delay, so a broad
  ETF constituent pull can never spam a free endpoint. A missing/failed symbol is
  isolated per listing and never fails the whole batch.

This module is a *source adapter*: it only fetches/parses and returns normalized
``InstrumentPriceRecord`` rows plus a per-listing outcome. The provider-agnostic
upsert / freshness / job-bookkeeping lives in
``app/services/instrument_prices.py``. See AGENTS.md (no uncontrolled
per-holding source loops; two-layer ingestion split; tests offline; no secrets).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Any, Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from app.sources.base import PriceSource
from app.sources.stooq import StooqSource
from app.sources.yfinance import YFinanceSource

# Per-listing fetch outcomes (mirrors the constituent resolver's status vocab).
OK = "ok"
NO_DATA = "no_data"
SKIPPED_BUDGET = "skipped_budget"
SKIPPED_CACHED = "skipped_cached"
FAILED = "failed"

_PRICE_Q = Decimal("0.0001")  # 4dp is plenty inside Numeric(24, 8)
# Default daily-history window the fixture generates with no explicit range:
# enough for a usable constituent chart without flooding the table. Bounded so an
# "all" backfill request cannot blow up.
_DEFAULT_HISTORY_DAYS = 90
_MAX_HISTORY_DAYS = 366


@dataclass(frozen=True)
class InstrumentPriceRequest:
    """A deduped price request for one resolved ``instrument_listing``.

    Carries enough to drive a provider (ticker + mic/exchange + currency) without
    re-resolving identity, plus the ``instrument_listing_id`` so returned records
    map back to the listing they belong to.
    """

    instrument_listing_id: int
    ticker: str | None
    mic: str | None = None
    exchange: str | None = None
    currency: str | None = None


@dataclass(frozen=True)
class InstrumentPriceRecord:
    """A normalized EOD bar for one listing on one ``price_date``."""

    instrument_listing_id: int
    price_date: date
    close: Decimal
    open: Decimal | None = None
    high: Decimal | None = None
    low: Decimal | None = None
    adjusted_close: Decimal | None = None
    volume: Decimal | None = None
    currency: str | None = None
    source: str = "unknown"
    # fixture | official | estimated | manual | ... (provider-asserted provenance).
    status: str | None = None
    raw_payload: dict[str, Any] | None = None


@dataclass
class InstrumentPriceFetchResult:
    """A provider's normalized output: bars + a per-listing outcome.

    ``outcomes`` lets the ingestion layer count skipped_budget / skipped_cached /
    failed / no_data per listing without the source touching the DB or job state.
    """

    records: list[InstrumentPriceRecord] = field(default_factory=list)
    outcomes: dict[int, str] = field(default_factory=dict)


class InstrumentPriceSource(Protocol):
    name: str

    async def fetch_eod_prices(
        self,
        session: AsyncSession,
        requests: list[InstrumentPriceRequest],
        *,
        start_date: date | None = None,
        end_date: date | None = None,
        batch_size: int = 1,
        ttl_seconds: int = 0,
    ) -> InstrumentPriceFetchResult:
        """Return EOD bars for the (already deduped) listing requests.

        Batch-oriented even when a provider fetches one symbol at a time. A
        provider that does not know a listing records ``no_data`` for it (never an
        error). Live providers must route every call through ``guarded_fetch``.
        """
        ...


def _q(value: Decimal) -> Decimal:
    return value.quantize(_PRICE_Q)


def _history_window(start_date: date | None, end_date: date | None) -> list[date]:
    end = end_date or date.today()
    if start_date is None:
        start = end - timedelta(days=_DEFAULT_HISTORY_DAYS - 1)
    else:
        start = start_date
    if start > end:
        return []
    span = min((end - start).days, _MAX_HISTORY_DAYS - 1)
    start = end - timedelta(days=span)
    return [start + timedelta(days=i) for i in range((end - start).days + 1)]


# --- fixture provider --------------------------------------------------------
#
# Clean base close + native currency per known constituent ticker. Realistic in
# shape, NOT guaranteed current. The same universe the constituent identity
# fixture resolves, so the offline demo flow (holdings -> identity -> prices)
# lines up end to end.
_FIXTURE_BASE: dict[str, tuple[str, str]] = {
    # ticker: (base_close, currency)
    "AAPL": ("195.00", "USD"),
    "MSFT": ("420.00", "USD"),
    "NVDA": ("120.00", "USD"),
    "AMZN": ("185.00", "USD"),
    "META": ("500.00", "USD"),
    "GOOGL": ("175.00", "USD"),
    "GOOG": ("177.00", "USD"),
    "AVGO": ("165.00", "USD"),
    "BRK.B": ("410.00", "USD"),
    "JPM": ("200.00", "USD"),
    "MA": ("460.00", "USD"),
    "PGR": ("230.00", "USD"),
    "TT": ("380.00", "USD"),
    # Directly-held imported instruments (broker CSV) the constituent identity
    # fixture also knows, so the offline import -> resolve -> price flow lines up
    # end to end for TSLA and the JEPG ETF.
    "TSLA": ("210.00", "USD"),
    "JEPG": ("55.00", "USD"),
    "SHEL": ("28.00", "GBP"),
    "AZN": ("105.00", "GBP"),
    "HSBA": ("6.50", "GBP"),
    "ULVR": ("45.00", "GBP"),
    "BP": ("4.70", "GBP"),
    "GSK": ("15.00", "GBP"),
    "RIO": ("52.00", "GBP"),
    "DGE": ("27.00", "GBP"),
    "BA": ("13.00", "GBP"),
    "REL": ("35.00", "GBP"),
    "ASML": ("900.00", "EUR"),
    "NESN": ("95.00", "CHF"),
    "NOVO-B": ("600.00", "DKK"),
}

# A small deterministic wave so generated history is not a flat line.
_WAVE = (0, 1, 2, 1, 0, -1, -2, -1)
_WAVE_STEP = Decimal("0.004")
_DRIFT_STEP = Decimal("0.0010")  # gentle uptrend toward today


def _close_for(base: Decimal, days_back: int, span: int) -> Decimal:
    drift = _DRIFT_STEP * Decimal(span - days_back)
    wave = Decimal(_WAVE[days_back % len(_WAVE)]) * _WAVE_STEP
    return _q(base * (Decimal(1) + drift + wave))


class FixtureInstrumentPriceSource:
    """Offline deterministic EOD provider (no network, no randomness)."""

    name = "instrument_price_fixture"
    supported_tickers = frozenset(_FIXTURE_BASE)

    def _records_for(
        self, request: InstrumentPriceRequest, dates: list[date]
    ) -> list[InstrumentPriceRecord]:
        ticker = (request.ticker or "").strip().upper()
        spec = _FIXTURE_BASE.get(ticker)
        if spec is None or not dates:
            return []
        base = Decimal(spec[0])
        currency = request.currency or spec[1]
        end = dates[-1]
        span = (end - dates[0]).days or 1
        out: list[InstrumentPriceRecord] = []
        for d in dates:
            days_back = (end - d).days
            close = _close_for(base, days_back, span)
            volume = Decimal(1_000_000 + ((days_back * 37) % 500) * 1000)
            out.append(
                InstrumentPriceRecord(
                    instrument_listing_id=request.instrument_listing_id,
                    price_date=d,
                    close=close,
                    open=_q(close * Decimal("0.997")),
                    high=_q(close * Decimal("1.006")),
                    low=_q(close * Decimal("0.994")),
                    adjusted_close=close,
                    volume=volume,
                    currency=currency,
                    source=self.name,
                    status="fixture",
                    raw_payload={"provider": self.name, "ticker": ticker},
                )
            )
        return out

    async def fetch_eod_prices(
        self,
        session: AsyncSession,
        requests: list[InstrumentPriceRequest],
        *,
        start_date: date | None = None,
        end_date: date | None = None,
        batch_size: int = 1,
        ttl_seconds: int = 0,
    ) -> InstrumentPriceFetchResult:
        # Offline + permissive: no guarded_fetch (no network, no budget to protect).
        dates = _history_window(start_date, end_date)
        result = InstrumentPriceFetchResult()
        for request in requests:
            records = self._records_for(request, dates)
            result.outcomes[request.instrument_listing_id] = OK if records else NO_DATA
            result.records.extend(records)
        return result


# --- live providers (stooq / yfinance) ---------------------------------------


class _LiveInstrumentPriceSource:
    """Live EOD wrapper around a fund-price adapter, guarded per symbol.

    Reuses the existing ``stooq`` / ``yfinance`` HTTP adapters (symbol mapping +
    CSV/JSON parsing) but routes every call through ``guarded_fetch`` so the
    source budget, fetch log and request cache all apply. One symbol at a time
    with a politeness delay between symbols — never an uncontrolled loop. Those
    adapters return close-only points, so OHLC/volume are left null.
    """

    request_kind = "fetch_eod_prices"

    def __init__(self, name: str, http: PriceSource, endpoint_label: str) -> None:
        self.name = name
        self._http = http
        self._endpoint_label = endpoint_label

    async def fetch_eod_prices(
        self,
        session: AsyncSession,
        requests: list[InstrumentPriceRequest],
        *,
        start_date: date | None = None,
        end_date: date | None = None,
        batch_size: int = 1,
        ttl_seconds: int = 0,
    ) -> InstrumentPriceFetchResult:
        # Imported here to avoid a module import cycle (services import sources).
        from app.services import source_budget, source_requests

        budget = await source_budget.get_budget(session, self.name)
        delay_s = ((budget.min_delay_ms or 0) / 1000) if budget else 0.0

        result = InstrumentPriceFetchResult()
        for i, request in enumerate(requests):
            ticker = (request.ticker or "").strip()
            if not ticker:
                result.outcomes[request.instrument_listing_id] = NO_DATA
                continue
            # Politeness spacing between symbols so subsequent guarded_fetch calls
            # are not all rejected as min_delay (and the endpoint is never hammered).
            if i and delay_s:
                await asyncio.sleep(delay_s)

            exchange = request.exchange or request.mic
            params = {"symbol": ticker, "exchange": exchange, "currency": request.currency}
            try:
                fetch_result, payload = await source_budget.guarded_fetch(
                    session,
                    source=self.name,
                    request_kind=self.request_kind,
                    params=params,
                    endpoint_label=self._endpoint_label,
                    method="GET",
                    ttl_seconds=ttl_seconds,
                    fetch=lambda tk=ticker, ex=exchange, cur=request.currency: self._http.fetch(
                        ticker=tk, exchange=ex, currency=cur
                    ),
                )
            except Exception:  # noqa: BLE001 - guarded_fetch logged it; isolate this listing
                result.outcomes[request.instrument_listing_id] = FAILED
                continue

            if fetch_result.status == source_requests.RATE_LIMITED:
                result.outcomes[request.instrument_listing_id] = SKIPPED_BUDGET
                continue
            if fetch_result.cache_hit or payload is None:
                result.outcomes[request.instrument_listing_id] = SKIPPED_CACHED
                continue

            records = [
                InstrumentPriceRecord(
                    instrument_listing_id=request.instrument_listing_id,
                    price_date=point.price_date,
                    close=point.price,
                    adjusted_close=point.price,
                    currency=point.currency or request.currency,
                    source=self.name,
                    status="official",
                )
                for point in payload
            ]
            result.records.extend(records)
            result.outcomes[request.instrument_listing_id] = OK if records else NO_DATA
        return result


# --- registry ----------------------------------------------------------------

_SOURCES: dict[str, InstrumentPriceSource] = {
    FixtureInstrumentPriceSource.name: FixtureInstrumentPriceSource(),
    "stooq": _LiveInstrumentPriceSource("stooq", StooqSource(), "stooq.com/q/d/l"),
    "yfinance": _LiveInstrumentPriceSource(
        "yfinance", YFinanceSource(), "query1.finance.yahoo.com/v8/finance/chart"
    ),
}


def get_instrument_price_source(name: str | None = None) -> InstrumentPriceSource:
    from app.core.config import get_settings

    source_name = name or get_settings().constituent_price_source_default
    source = _SOURCES.get(source_name)
    if source is None:
        raise ValueError(f"Unknown instrument price source: {source_name!r}")
    return source
