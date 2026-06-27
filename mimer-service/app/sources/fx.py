"""FX rate sources, isolated behind a small protocol + registry.

An *FX rate* is the price of one currency in terms of another on a given date.
The canonical convention used across the service (see `FxRate`) is::

    rate = units of ``quote_currency`` per 1 unit of ``base_currency``

so GBP/USD = 1.27 means 1 GBP buys 1.27 USD. Inverse and cross rates are *not*
stored by the fixture — they are computed by the conversion service
(`app/services/fx.py`) from the canonical pairs, so the table stays small and
there is a single source of truth per pair/date/source.

This iteration ships a robust **fixture** provider so the worker, job plumbing
and tests work with no live network access. A real ECB reference-rate adapter
(EUR-based daily rates) slots in behind the same `FxSource` protocol later; the
worker and API never depend on a specific provider. See SOURCES.md /
docs/data_sources.md for the vendor research and licensing caveats.

Note: this module is a *source adapter* (provider-specific fetch/parse). The
provider-agnostic upsert / provenance / job_runs logic lives in
``app/services/fx_ingestion.py``; lookup/triangulation lives in
``app/services/fx.py``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Any, Protocol

_RATE_Q = Decimal("0.0000000001")  # 10 dp, matches FxRate Numeric(24, 10)

# Default daily-history window the fixture generates when no explicit date range
# is requested: bounded so a single ingestion run populates a usable 1m chart
# without flooding the table.
_DEFAULT_HISTORY_DAYS = 30
_MAX_HISTORY_DAYS = 366


@dataclass(frozen=True)
class FxRateRecord:
    """A normalized FX rate for one (base, quote) pair on one ``rate_date``."""

    rate_date: date
    base_currency: str
    quote_currency: str
    # quote_currency units per 1 base_currency unit.
    rate: Decimal
    source: str
    # fixture | official | estimated | manual | ... (provider-asserted).
    status: str | None = None
    # Reserved for future provenance/debugging (raw provider payload).
    raw_payload: dict[str, Any] | None = None


class FxSource(Protocol):
    name: str

    async def fetch_rates(
        self,
        *,
        base_currency: str,
        quote_currencies: list[str],
        start_date: date | None = None,
        end_date: date | None = None,
    ) -> list[FxRateRecord]:
        """Return rates for ``base_currency`` against each requested quote.

        A provider that does not know a currency simply omits it (no error), so
        one unknown pair never fails the batch. With no date range the provider
        returns a bounded recent daily history ending today.
        """
        ...


# --- fixture provider --------------------------------------------------------
#
# Anchors are expressed as *USD per 1 unit* of each currency, so any cross rate
# is ``usd_per[base] / usd_per[quote]`` (= quote units per 1 base unit). This
# keeps the fixture internally consistent (GBP/USD * USD/EUR == GBP/EUR) and lets
# it answer any pair among the known currencies. Placeholder values — realistic
# in shape, not guaranteed current.
_USD_PER: dict[str, Decimal] = {
    "USD": Decimal("1"),
    "GBP": Decimal("1.27"),
    "EUR": Decimal("1.08"),
    "SEK": Decimal("0.095"),  # ~10.53 SEK per USD
    "CHF": Decimal("1.11"),
    "DKK": Decimal("0.145"),  # ~6.90 DKK per USD
    "NOK": Decimal("0.092"),
    "JPY": Decimal("0.0064"),  # ~156 JPY per USD
    "CAD": Decimal("0.73"),
    "AUD": Decimal("0.66"),
    "HKD": Decimal("0.128"),
}

# A small deterministic wave so generated history is not a flat line. Index 0
# (the most recent day) is exactly 1.0, so today's rate equals the clean anchor —
# which keeps tests that assert the latest rate simple.
_WAVE = (0, 1, 2, 1, 0, -1, -2, -1)
_WAVE_STEP = Decimal("0.0008")


def _q(value: Decimal) -> Decimal:
    return value.quantize(_RATE_Q)


def _modulation(days_back: int) -> Decimal:
    return Decimal(1) + Decimal(_WAVE[days_back % len(_WAVE)]) * _WAVE_STEP


def _anchor_rate(base: str, quote: str) -> Decimal | None:
    """Clean (un-modulated) ``quote per base`` rate, or None if unknown."""
    base_usd = _USD_PER.get(base.upper())
    quote_usd = _USD_PER.get(quote.upper())
    if base_usd is None or quote_usd is None or quote_usd == 0:
        return None
    return base_usd / quote_usd


def _history_window(start_date: date | None, end_date: date | None) -> list[date]:
    end = end_date or date.today()
    if start_date is None:
        start = end - timedelta(days=_DEFAULT_HISTORY_DAYS - 1)
    else:
        start = start_date
    if start > end:
        return []
    # Bound the window so an "all" backfill request cannot blow up.
    span = min((end - start).days, _MAX_HISTORY_DAYS - 1)
    start = end - timedelta(days=span)
    return [start + timedelta(days=i) for i in range((end - start).days + 1)]


class StaticFxSource:
    """Offline FX provider backed by a consistent USD-anchored cross-rate table.

    Generates a bounded daily history for each requested (base, quote) pair the
    table knows about; unknown currencies are skipped (not an error).
    """

    name = "fx_fixture"
    supported_currencies = frozenset(_USD_PER)

    async def fetch_rates(
        self,
        *,
        base_currency: str,
        quote_currencies: list[str],
        start_date: date | None = None,
        end_date: date | None = None,
    ) -> list[FxRateRecord]:
        base = base_currency.upper()
        if base not in _USD_PER:
            return []
        dates = _history_window(start_date, end_date)
        end = dates[-1] if dates else (end_date or date.today())

        records: list[FxRateRecord] = []
        seen: set[str] = set()
        for quote_raw in quote_currencies:
            quote = quote_raw.upper()
            if quote == base or quote in seen or quote not in _USD_PER:
                continue
            seen.add(quote)
            anchor = _anchor_rate(base, quote)
            if anchor is None:
                continue
            for d in dates:
                days_back = (end - d).days
                records.append(
                    FxRateRecord(
                        rate_date=d,
                        base_currency=base,
                        quote_currency=quote,
                        rate=_q(anchor * _modulation(days_back)),
                        source=self.name,
                        status="fixture",
                    )
                )
        return records


# --- registry ----------------------------------------------------------------

_SOURCES: dict[str, FxSource] = {
    StaticFxSource.name: StaticFxSource(),
}


def get_fx_source(name: str | None = None) -> FxSource:
    from app.core.config import get_settings

    source_name = name or get_settings().fx_source_default
    source = _SOURCES.get(source_name)
    if source is None:
        raise ValueError(f"Unknown FX source: {source_name!r}")
    return source
