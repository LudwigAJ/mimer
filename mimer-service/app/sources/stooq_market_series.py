"""Stooq market-series classification (design path; storage deferred).

Stooq publishes generic *market series* that are useful as curve/market context but are
**not** tradable securities. This module is a pure classifier that names what a Stooq
symbol is, so the rest of the system never mis-models one as something it is not:

* **Sovereign benchmark yield series** (``10YDEY.B`` = Germany 10Y benchmark *yield*) — a
  country/tenor generic yield series, **NOT an ISIN-level bond security**.
* **Sovereign benchmark price series** (``10YDEP.B`` = Germany 10Y benchmark *price*) — a
  country/tenor generic price series, **NOT an ISIN-level bond security**.
* **Rates futures series** (``ZN.F`` = 10Y T-Note futures, ``G.F`` = Long Gilt) — a
  root/continuous/generic futures series, **NOT an expiry-specific contract** (an
  expiry-specific contract looks like ``ZNM6`` = root + month code + year, which Stooq's
  ``.F`` root symbols are not).

Critical modelling rules (see AGENTS.md):

* Do **not** treat a sovereign benchmark yield/price series as an actual bond holding.
* Do **not** treat a ``.F`` futures series as an expiry-specific tradable contract unless
  verified. The ``.F`` symbols here are roots/continuous series.
* Store these (when storage lands) as ``market_series`` / the specific benchmark category,
  never on the bond/instrument security master.

**Scope of this slice:** classification + capability typing + docs only. Implementing the
``market_series`` table is deliberately **deferred** (it needs a migration and would be a
large slice on its own); the concrete schema proposal lives in ``docs/data_sources.md``.
This module performs no DB and no network I/O.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# --- categories (mirrors the registry data-type vocabulary) ------------------

SOVEREIGN_YIELD_BENCHMARK_SERIES = "sovereign_yield_benchmark_series"
SOVEREIGN_BENCHMARK_PRICE_SERIES = "sovereign_benchmark_price_series"
RATES_FUTURES_SERIES = "rates_futures_series"
MARKET_SERIES = "market_series"  # generic fallback for a recognised-but-unspecialised series
UNKNOWN = "unknown"

CATEGORIES = (
    SOVEREIGN_YIELD_BENCHMARK_SERIES,
    SOVEREIGN_BENCHMARK_PRICE_SERIES,
    RATES_FUTURES_SERIES,
    MARKET_SERIES,
    UNKNOWN,
)

# Two-letter Stooq country codes seen in sovereign benchmark symbols -> a label.
_COUNTRY = {
    "DE": "Germany",
    "FR": "France",
    "IT": "Italy",
    "ES": "Spain",
    "UK": "United Kingdom",
    "US": "United States",
    "JP": "Japan",
    "NL": "Netherlands",
    "BE": "Belgium",
    "PT": "Portugal",
    "IE": "Ireland",
    "GR": "Greece",
    "AT": "Austria",
    "CH": "Switzerland",
}

# A sovereign benchmark symbol: <tenor><CC><Y|P>.B  (e.g. 10YDEY.B, 1MFRY.B, 2YITP.B).
# The tenor is a number plus a unit (M=months, Y=years); the trailing letter before ".B"
# is the series kind: Y = yield, P = price. This is a *generic* country/tenor series.
_SOVEREIGN_RE = re.compile(r"^(?P<num>\d+)(?P<unit>[MY])(?P<country>[A-Z]{2})(?P<kind>[YP])\.B$")

# A Stooq rates/bond futures *root* series: <ROOT>.F  (e.g. ZN.F, ZB.F, G.F, GG.F).
_FUTURES_RE = re.compile(r"^(?P<root>[A-Z]{1,3})\.F$")

# Known rates/bond futures roots Stooq publishes (root/continuous series, not an expiry).
_FUTURES_ROOTS = {
    "G": "10-Year Long Gilt",
    "GG": "Euro Bund",
    "GX": "Euro Buxl",
    "HF": "Euro Schatz",
    "HR": "Euro Bobl",
    "IM": "3M Euribor",
    "JGB": "JGB 10Y Future",
    "TN": "Ultra 10-Year T-Note",
    "UD": "Ultra T-Bond",
    "ZB": "30-Year T-Bond",
    "ZF": "5-Year T-Note",
    "ZN": "10-Year T-Note",
    "ZQ": "30-Day Fed Funds",
    "ZT": "2-Year T-Note",
}

# An expiry-specific futures contract looks like <ROOT><MONTH><YEAR>, e.g. ZNM6 (ZN + M + 6).
# Stooq's CME month codes; used only to *reject* an expiry-specific symbol from the generic
# ``.F`` root path (we never claim a root series is a specific contract).
_MONTH_CODES = set("FGHJKMNQUVXZ")
_EXPIRY_RE = re.compile(r"^(?P<root>[A-Z]{1,3})(?P<month>[FGHJKMNQUVXZ])(?P<year>\d{1,2})$")


@dataclass(frozen=True)
class StooqMarketSeriesClassification:
    """What a Stooq market-series symbol is — explicitly NOT a security/bond/contract."""

    symbol: str
    category: str  # one of CATEGORIES
    description: str
    country: str | None = None
    tenor: str | None = None
    # Hard modelling guards — these stay False for every series this module classifies.
    is_security: bool = False
    is_bond: bool = False
    is_expiry_specific_future: bool = False


def classify_stooq_symbol(symbol: str) -> StooqMarketSeriesClassification:
    """Classify a Stooq market-series symbol into a generic series category.

    Never returns a bond/security/expiry-specific-contract classification: a sovereign
    benchmark is a country/tenor generic series, and a ``.F`` symbol is a futures *root*
    (continuous/generic) series. An expiry-specific-looking symbol (e.g. ``ZNM6``) is
    reported as ``unknown`` here (this slice does not model specific contracts) rather than
    being mis-typed as a tradable future.
    """
    raw = (symbol or "").strip().upper()

    sovereign = _SOVEREIGN_RE.match(raw)
    if sovereign:
        unit = "Y" if sovereign["unit"] == "Y" else "M"
        tenor = f"{sovereign['num']}{unit}"
        country = _COUNTRY.get(sovereign["country"], sovereign["country"])
        if sovereign["kind"] == "Y":
            return StooqMarketSeriesClassification(
                symbol=raw,
                category=SOVEREIGN_YIELD_BENCHMARK_SERIES,
                description=f"{country} {tenor} sovereign benchmark yield series",
                country=country,
                tenor=tenor,
            )
        return StooqMarketSeriesClassification(
            symbol=raw,
            category=SOVEREIGN_BENCHMARK_PRICE_SERIES,
            description=f"{country} {tenor} sovereign benchmark price series",
            country=country,
            tenor=tenor,
        )

    futures = _FUTURES_RE.match(raw)
    if futures and futures["root"] in _FUTURES_ROOTS:
        label = _FUTURES_ROOTS[futures["root"]]
        return StooqMarketSeriesClassification(
            symbol=raw,
            category=RATES_FUTURES_SERIES,
            description=f"{label} futures series (root/continuous — not an expiry contract)",
        )

    # An expiry-specific-looking symbol (root + month code + year) is deliberately NOT
    # classified as a tradable future here — we do not model specific contracts this slice.
    expiry = _EXPIRY_RE.match(raw)
    if expiry and expiry["month"] in _MONTH_CODES and expiry["root"] in _FUTURES_ROOTS:
        return StooqMarketSeriesClassification(
            symbol=raw,
            category=UNKNOWN,
            description="looks like an expiry-specific futures contract — not modelled "
            "(only root/continuous .F series are recognised; verify before trusting)",
        )

    return StooqMarketSeriesClassification(
        symbol=raw,
        category=UNKNOWN,
        description="unrecognised Stooq market-series symbol",
    )


def is_market_series_symbol(symbol: str) -> bool:
    """True if the symbol is a recognised generic market series (not unknown)."""
    return classify_stooq_symbol(symbol).category != UNKNOWN
