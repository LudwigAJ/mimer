"""Official / reference-rate sources, isolated behind a small protocol + registry.

A *reference rate* here is one official/published rate *observation* for a given
date — a central-bank policy rate (ECB main refinancing / deposit / marginal
lending, BoE Bank Rate), an overnight benchmark (€STR, SONIA, SOFR, Fed Funds
effective) or a government par yield at a tenor (US Treasury 1M..30Y). This layer
only *fetches and normalises* observations; it never builds curves.

NON-GOALS (see AGENTS.md): no curve fitting, bootstrapping, interpolation,
forward rates, discount factors or bond pricing live here or anywhere in the
backend. Those belong in the Rust GUI / local pricer. A source returns the rates
it published; the ingestion service persists them as-is.

This iteration ships a robust **fixture** provider (``rates_fixture``, the default)
so the worker, job plumbing and tests all work with no network access, plus two live
adapters that fetch official machine-readable feeds through ``guarded_fetch`` (source
budget + fetch log + request cache + timeouts), store no secrets and never scrape:

* **US Treasury** (``us_treasury_rates``) — the official daily par yield curve XML
  feed (USD par yields 1M..30Y);
* **ECB** (``ecb_rates``) — the official ECB Data Portal SDMX REST API (EUR key
  interest rates + €STR).

Both are explicit-only: the default stays the offline fixture, so the worker/
scheduler never makes a surprise live call.

``boe_rates`` remains a *planned* placeholder behind the same protocol: its official
machine-readable export (Bank of England IADB, series ``IUDBEDR``/``IUDSOIA``)
returns HTTP 403 to a plain client, so until a clean non-brittle access path is
verified it raises a clear ``NotImplementedError`` (recorded as a clean failed run)
and a ``--source boe_rates`` invocation never makes a surprise live call. See
docs/data_sources.md for the exact follow-up.

Note: this module is a *source adapter* (provider-specific fetch/parse). The
provider-agnostic upsert / provenance / job_runs logic lives in
``app/services/rates_ingestion.py``; read shaping in ``app/services/rates.py``.
"""

from __future__ import annotations

import asyncio
import csv
import io
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any, Protocol

import httpx

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

_RATE_Q = Decimal("0.0000000001")  # 10 dp, matches ReferenceRate Numeric(24, 10)

# Default daily-history window the fixture generates with no explicit date range:
# bounded so one ingestion run populates a usable recent series without flooding
# the table. Bounded again so an "all" backfill request cannot blow up.
_DEFAULT_HISTORY_DAYS = 30
_MAX_HISTORY_DAYS = 366

# Currencies for which the service intends to collect official/reference rates.
# Diagnostics + the planner use this set as the coverage expectation.
SUPPORTED_RATE_CURRENCIES = ("EUR", "GBP", "USD")

# Controlled vocabularies (kept in sync with the ReferenceRate model comments).
RATE_FAMILIES = (
    "policy_rate",
    "overnight_rate",
    "treasury_par_yield",
    "benchmark_yield",
    "deposit_facility",
    "lending_facility",
    "reserve_rate",
    "other",
)
RATE_UNITS = ("percent", "decimal", "basis_points")


@dataclass(frozen=True)
class ReferenceRateRecord:
    """A normalised reference-rate observation for one date."""

    rate_date: date
    currency: str
    country_or_region: str
    rate_family: str
    rate_name: str
    rate_value: Decimal
    unit: str = "percent"
    tenor: str | None = None
    tenor_months: int | None = None
    observed_at: datetime | None = None
    source: str = "unknown"
    # fixture | official | estimated | manual | ... (provider-asserted).
    status: str | None = None
    source_url: str | None = None
    # Reserved for provenance/debugging (raw provider payload).
    raw_payload: dict[str, Any] | None = None


class RatesSource(Protocol):
    name: str

    async def fetch_rates(
        self,
        *,
        session: AsyncSession | None = None,
        currency: str | None = None,
        country_or_region: str | None = None,
        rate_family: str | None = None,
        start_date: date | None = None,
        end_date: date | None = None,
    ) -> list[ReferenceRateRecord]:
        """Return reference-rate observations matching the (optional) filters.

        A provider that does not know a currency/family simply omits it (no
        error). With no date range it returns a bounded recent daily history
        ending today. Live providers must route every call through
        ``guarded_fetch`` (budget + fetch log + cache) and never scrape, so they
        need the ``session`` the ingestion service passes; offline fixtures ignore
        it.
        """
        ...


# --- fixture catalogue -------------------------------------------------------
#
# One spec per published series. ``varies`` rates (overnight benchmarks + par
# yields) get a tiny deterministic daily wiggle so a chart is not a flat line;
# policy/facility rates stay flat between decisions. Index 0 (the most recent
# day) is exactly the clean base value, so latest-rate assertions stay simple.
# Placeholder values — realistic in shape, NOT guaranteed current.


@dataclass(frozen=True)
class _Series:
    rate_name: str
    rate_family: str
    currency: str
    country_or_region: str
    base_value: str
    tenor: str | None = None
    tenor_months: int | None = None
    varies: bool = False
    unit: str = "percent"


_CATALOGUE: tuple[_Series, ...] = (
    # --- EUR / euro area (ECB) ---
    _Series("ECB_MAIN_REFINANCING_RATE", "policy_rate", "EUR", "euro_area", "4.25"),
    _Series("ECB_DEPOSIT_FACILITY_RATE", "deposit_facility", "EUR", "euro_area", "3.75"),
    _Series("ECB_MARGINAL_LENDING_RATE", "lending_facility", "EUR", "euro_area", "4.50"),
    _Series("ESTR", "overnight_rate", "EUR", "euro_area", "3.66", varies=True),
    # --- GBP / United Kingdom (BoE) ---
    _Series("BOE_BANK_RATE", "policy_rate", "GBP", "united_kingdom", "4.25"),
    _Series("SONIA", "overnight_rate", "GBP", "united_kingdom", "4.18", varies=True),
    # --- USD / United States (Treasury + money-market benchmarks) ---
    _Series(
        "US_TREASURY_PAR_YIELD",
        "treasury_par_yield",
        "USD",
        "united_states",
        "5.40",
        tenor="1M",
        tenor_months=1,
        varies=True,
    ),
    _Series(
        "US_TREASURY_PAR_YIELD",
        "treasury_par_yield",
        "USD",
        "united_states",
        "5.38",
        tenor="3M",
        tenor_months=3,
        varies=True,
    ),
    _Series(
        "US_TREASURY_PAR_YIELD",
        "treasury_par_yield",
        "USD",
        "united_states",
        "5.30",
        tenor="6M",
        tenor_months=6,
        varies=True,
    ),
    _Series(
        "US_TREASURY_PAR_YIELD",
        "treasury_par_yield",
        "USD",
        "united_states",
        "5.05",
        tenor="1Y",
        tenor_months=12,
        varies=True,
    ),
    _Series(
        "US_TREASURY_PAR_YIELD",
        "treasury_par_yield",
        "USD",
        "united_states",
        "4.70",
        tenor="2Y",
        tenor_months=24,
        varies=True,
    ),
    _Series(
        "US_TREASURY_PAR_YIELD",
        "treasury_par_yield",
        "USD",
        "united_states",
        "4.35",
        tenor="5Y",
        tenor_months=60,
        varies=True,
    ),
    _Series(
        "US_TREASURY_PAR_YIELD",
        "treasury_par_yield",
        "USD",
        "united_states",
        "4.30",
        tenor="10Y",
        tenor_months=120,
        varies=True,
    ),
    _Series(
        "US_TREASURY_PAR_YIELD",
        "treasury_par_yield",
        "USD",
        "united_states",
        "4.45",
        tenor="30Y",
        tenor_months=360,
        varies=True,
    ),
    _Series("SOFR", "overnight_rate", "USD", "united_states", "5.31", varies=True),
    _Series("FED_FUNDS_EFFECTIVE", "overnight_rate", "USD", "united_states", "5.33", varies=True),
)

# A small deterministic wave so a "varies" series is not a flat line. Index 0 (the
# most recent day) is exactly 0 so today's rate equals the clean base value.
_WAVE = (0, 1, 2, 1, 0, -1, -2, -1)
_WAVE_STEP = Decimal("0.01")  # 1bp steps in percent terms


def _q(value: Decimal) -> Decimal:
    return value.quantize(_RATE_Q)


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


def _value_for(series: _Series, days_back: int) -> Decimal:
    base = Decimal(series.base_value)
    if not series.varies:
        return _q(base)
    return _q(base + Decimal(_WAVE[days_back % len(_WAVE)]) * _WAVE_STEP)


def _matches(
    series: _Series,
    *,
    currency: str | None,
    country_or_region: str | None,
    rate_family: str | None,
) -> bool:
    if currency and series.currency != currency.upper():
        return False
    if country_or_region and series.country_or_region != country_or_region.lower():
        return False
    if rate_family and series.rate_family != rate_family.lower():
        return False
    return True


class FixtureRatesSource:
    """Offline deterministic reference-rate provider (no network, no randomness)."""

    name = "rates_fixture"
    is_fixture = True
    requires_live_fetch = False
    supported_currencies = frozenset(s.currency for s in _CATALOGUE)
    supported_rate_names = frozenset(s.rate_name for s in _CATALOGUE)

    async def fetch_rates(
        self,
        *,
        session: AsyncSession | None = None,  # offline fixture ignores it
        currency: str | None = None,
        country_or_region: str | None = None,
        rate_family: str | None = None,
        start_date: date | None = None,
        end_date: date | None = None,
    ) -> list[ReferenceRateRecord]:
        dates = _history_window(start_date, end_date)
        if not dates:
            return []
        end = dates[-1]
        records: list[ReferenceRateRecord] = []
        for series in _CATALOGUE:
            if not _matches(
                series,
                currency=currency,
                country_or_region=country_or_region,
                rate_family=rate_family,
            ):
                continue
            for d in dates:
                days_back = (end - d).days
                records.append(
                    ReferenceRateRecord(
                        rate_date=d,
                        currency=series.currency,
                        country_or_region=series.country_or_region,
                        rate_family=series.rate_family,
                        rate_name=series.rate_name,
                        rate_value=_value_for(series, days_back),
                        unit=series.unit,
                        tenor=series.tenor,
                        tenor_months=series.tenor_months,
                        source=self.name,
                        status="fixture",
                        raw_payload={"provider": self.name, "rate_name": series.rate_name},
                    )
                )
        return records


# --- US Treasury live adapter (official daily par yield curve XML feed) -------
#
# Source of truth: the U.S. Department of the Treasury "Daily Treasury Par Yield
# Curve Rates" XML data feed — the same machine-readable feed the Treasury website
# itself renders. It is an Atom/OData document of <m:properties> blocks, one per
# business day, each carrying NEW_DATE plus BC_* par-yield columns. We fetch ONE
# request per calendar year (the feed's native granularity) through guarded_fetch
# and store the observations as-is. NO curve fitting / bootstrapping / interpolation
# (see AGENTS.md) — we persist exactly what Treasury published, per tenor.

_TREASURY_XML_URL = (
    "https://home.treasury.gov/resource-center/data-chart-center/interest-rates/pages/xml"
)
# A host/path *class* for the fetch log — never a tokenised/full URL (no secrets).
_TREASURY_ENDPOINT_LABEL = "home.treasury.gov/.../interest-rates/pages/xml"
_TREASURY_SOURCE_URL = (
    "https://home.treasury.gov/resource-center/data-chart-center/interest-rates/"
    "TextView?type=daily_treasury_yield_curve"
)
# Bound a backfill so a wide date range can never fan out into many year requests.
_MAX_TREASURY_YEARS = 6

# Treasury XML property element -> (tenor label, tenor_months). Maps only the
# canonical par-yield tenors; the 6-week BC_1_5MONTH (non-integer months) and the
# duplicate BC_30YEARDISPLAY are intentionally skipped.
_TREASURY_TENORS: dict[str, tuple[str, int]] = {
    "BC_1MONTH": ("1M", 1),
    "BC_2MONTH": ("2M", 2),
    "BC_3MONTH": ("3M", 3),
    "BC_4MONTH": ("4M", 4),
    "BC_6MONTH": ("6M", 6),
    "BC_1YEAR": ("1Y", 12),
    "BC_2YEAR": ("2Y", 24),
    "BC_3YEAR": ("3Y", 36),
    "BC_5YEAR": ("5Y", 60),
    "BC_7YEAR": ("7Y", 84),
    "BC_10YEAR": ("10Y", 120),
    "BC_20YEAR": ("20Y", 240),
    "BC_30YEAR": ("30Y", 360),
}


def _local_name(tag: str) -> str:
    """Strip the XML namespace, returning just the local element name."""
    return tag.rsplit("}", 1)[-1]


def parse_treasury_par_yield_xml(
    text: str, *, source: str = "us_treasury_rates"
) -> list[ReferenceRateRecord]:
    """Parse the official Treasury par-yield XML feed into normalised observations.

    Pure + offline (no network): hand it the feed text and it returns one
    ``ReferenceRateRecord`` per (date, tenor) cell that is present and parses to a
    Decimal. A missing tenor cell is skipped; a non-numeric cell or a bad row is
    isolated (skipped) rather than failing the whole parse. No interpolation: only
    the tenors Treasury actually published for a date are returned.
    """
    records: list[ReferenceRateRecord] = []
    root = ET.fromstring(text)  # noqa: S314 - trusted official feed; ET resolves no entities
    for props in root.iter():
        if _local_name(props.tag) != "properties":
            continue
        cells = {_local_name(child.tag): (child.text or "").strip() for child in props}
        raw_date = cells.get("NEW_DATE", "")
        if not raw_date:
            continue
        try:
            rate_date = date.fromisoformat(raw_date[:10])
        except ValueError:
            continue  # isolate a malformed date row
        for element, (tenor, tenor_months) in _TREASURY_TENORS.items():
            raw_value = cells.get(element, "")
            if not raw_value:
                continue  # tenor not published that day
            try:
                rate_value = _q(Decimal(raw_value))
            except (InvalidOperation, ValueError):
                continue  # isolate a bad numeric cell
            records.append(
                ReferenceRateRecord(
                    rate_date=rate_date,
                    currency="USD",
                    country_or_region="united_states",
                    rate_family="treasury_par_yield",
                    rate_name="US_TREASURY_PAR_YIELD",
                    rate_value=rate_value,
                    unit="percent",
                    tenor=tenor,
                    tenor_months=tenor_months,
                    source=source,
                    status="official",
                    source_url=_TREASURY_SOURCE_URL,
                )
            )
    return records


def _treasury_years(start: date, end: date) -> list[int]:
    """Calendar years spanning [start, end], bounded to the most recent few."""
    years = list(range(start.year, end.year + 1))
    return years[-_MAX_TREASURY_YEARS:]


class TreasuryRatesSource:
    """Live US Treasury par-yield adapter (official daily XML feed, guarded).

    Explicit-only: the configured default stays the offline fixture, so this never
    fires on the scheduler unless ``--source us_treasury_rates`` is named. Every
    request goes through ``guarded_fetch`` (recent-success cache -> source budget ->
    fetch log -> fetch), one calendar year at a time, so a backfill can never spam
    the endpoint. Collection only — it returns published observations; it never
    builds a curve.
    """

    name = "us_treasury_rates"
    is_fixture = False
    requires_live_fetch = True
    request_kind = "fetch_treasury_par_yields"
    supported_currencies = ("USD",)
    supported_rate_families = ("treasury_par_yield",)
    description = (
        "US Treasury daily par yield curve rates (official home.treasury.gov XML "
        "feed). Observations only — no curve fitting/bootstrapping."
    )

    async def _fetch_year_xml(self, year: int) -> str:
        params = {"data": "daily_treasury_yield_curve", "field_tdr_date_value": str(year)}
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.get(_TREASURY_XML_URL, params=params)
            response.raise_for_status()
            return response.text

    async def fetch_rates(
        self,
        *,
        session: AsyncSession | None = None,
        currency: str | None = None,
        country_or_region: str | None = None,
        rate_family: str | None = None,
        start_date: date | None = None,
        end_date: date | None = None,
    ) -> list[ReferenceRateRecord]:
        if session is None:  # the live path needs the budget/fetch-log session
            raise RuntimeError(
                "us_treasury_rates requires a database session "
                "(run it via the rates_ingestion worker, not directly)."
            )
        # Filters this provider cannot satisfy => empty (a clean no-op, not error).
        if currency and currency.upper() != "USD":
            return []
        if country_or_region and country_or_region.lower() != "united_states":
            return []
        if rate_family and rate_family.lower() != "treasury_par_yield":
            return []

        from app.core.config import get_settings
        from app.services import source_budget, source_requests

        end = end_date or date.today()
        start = start_date or date(end.year, 1, 1)
        if start > end:
            return []
        ttl_seconds = get_settings().request_cache_ttl_seconds

        records: list[ReferenceRateRecord] = []
        for year in _treasury_years(start, end):
            params = {"data": "daily_treasury_yield_curve", "field_tdr_date_value": str(year)}
            try:
                fetch_result, payload = await source_budget.guarded_fetch(
                    session,
                    source=self.name,
                    request_kind=self.request_kind,
                    params=params,
                    endpoint_label=_TREASURY_ENDPOINT_LABEL,
                    method="GET",
                    ttl_seconds=ttl_seconds,
                    fetch=lambda y=year: self._fetch_year_xml(y),
                )
            except Exception:  # noqa: BLE001 - guarded_fetch logged it; skip this year
                continue
            # Budget-blocked or served from the recent-success cache => no payload.
            if fetch_result.status == source_requests.RATE_LIMITED or payload is None:
                continue
            records.extend(parse_treasury_par_yield_xml(payload, source=self.name))

        records = [
            r
            for r in records
            if (start_date is None or r.rate_date >= start_date)
            and (end_date is None or r.rate_date <= end_date)
        ]
        records.sort(key=lambda r: (r.rate_date, r.tenor_months or 0))
        return records


# --- ECB live adapter (official ECB Data Portal SDMX REST API) ----------------
#
# Source of truth: the ECB Data Portal SDMX 2.1 REST API
# (https://data-api.ecb.europa.eu/service/data/<flow>/<key>?format=csvdata) — the
# same machine-readable API behind data.ecb.europa.eu. Two dataflows are used:
#   * FM  — Financial market data -> ECB key interest rates (a change-date series:
#           one observation per rate change, NOT a daily series).
#   * EST — Euro short-term rate (€STR), a daily business-day series.
# Verified series keys (each flow fetched in ONE combined request via the SDMX `+`
# operator):
#   FM.B.U2.EUR.4F.KR.MRR_FR.LEV   Main refinancing operations, fixed rate (level)
#   FM.B.U2.EUR.4F.KR.DFR.LEV      Deposit facility (level)
#   FM.B.U2.EUR.4F.KR.MLFR.LEV     Marginal lending facility (level)
#   EST.B.EU000A2X2A25.WT          €STR, volume-weighted trimmed mean rate (level)
# We fetch one bounded request per dataflow through guarded_fetch and store the
# observations AS SUPPLIED. Policy rates arrive as change-date events — we never
# forward-fill them into a daily series (see AGENTS.md: collection only, no
# interpolation/curve building anywhere).

_ECB_BASE_URL = "https://data-api.ecb.europa.eu/service/data"
# Default lookback when no start_date is given: wide enough to capture the latest
# policy-rate change (those change only a few times a year) while staying bounded.
_ECB_DEFAULT_HISTORY_DAYS = 730
# Bound a backfill so a wide date range can never fetch an unbounded span.
_ECB_MAX_HISTORY_DAYS = 366 * 6


@dataclass(frozen=True)
class _ECBSeries:
    """One ECB SDMX series mapped to a normalised reference-rate identity."""

    full_key: str  # the SDMX series KEY (matches the CSV ``KEY`` column)
    flow: str  # SDMX dataflow ref (FM | EST)
    key_in_flow: str  # the key part after the flow (what the URL path needs)
    rate_name: str
    rate_family: str


# rate_name / rate_family chosen to match the offline fixture catalogue exactly, so
# ecb_rates and rates_fixture rows for the same series line up (only ``source`` and
# ``status`` differ). €STR uses tenor=NULL, like the fixture + overnight benchmarks.
_ECB_SERIES: tuple[_ECBSeries, ...] = (
    _ECBSeries(
        "FM.B.U2.EUR.4F.KR.MRR_FR.LEV",
        "FM",
        "B.U2.EUR.4F.KR.MRR_FR.LEV",
        "ECB_MAIN_REFINANCING_RATE",
        "policy_rate",
    ),
    _ECBSeries(
        "FM.B.U2.EUR.4F.KR.DFR.LEV",
        "FM",
        "B.U2.EUR.4F.KR.DFR.LEV",
        "ECB_DEPOSIT_FACILITY_RATE",
        "deposit_facility",
    ),
    _ECBSeries(
        "FM.B.U2.EUR.4F.KR.MLFR.LEV",
        "FM",
        "B.U2.EUR.4F.KR.MLFR.LEV",
        "ECB_MARGINAL_LENDING_RATE",
        "lending_facility",
    ),
    _ECBSeries(
        "EST.B.EU000A2X2A25.WT",
        "EST",
        "B.EU000A2X2A25.WT",
        "ESTR",
        "overnight_rate",
    ),
)
_ECB_SERIES_BY_KEY: dict[str, _ECBSeries] = {s.full_key: s for s in _ECB_SERIES}
_ECB_RATE_FAMILIES: tuple[str, ...] = tuple(dict.fromkeys(s.rate_family for s in _ECB_SERIES))


def _ecb_source_url(series: _ECBSeries) -> str:
    """Human-friendly ECB Data Portal page for this series (provenance only)."""
    return f"https://data.ecb.europa.eu/data/datasets/{series.flow}/{series.full_key}"


def parse_ecb_sdmx_csv(text: str, *, source: str = "ecb_rates") -> list[ReferenceRateRecord]:
    """Parse an ECB SDMX ``csvdata`` payload into normalised observations.

    Pure + offline (no network): hand it the CSV text and it returns one
    ``ReferenceRateRecord`` per recognised (series, date) row that parses to a
    Decimal. Reads columns BY NAME (``KEY`` / ``TIME_PERIOD`` / ``OBS_VALUE``) so it
    is robust to the differing column order ECB uses across dataflows (FM vs EST). A
    row for an unknown series KEY is skipped; a missing/empty value is skipped; a
    non-numeric value is isolated (skipped) rather than failing the whole parse. No
    interpolation / forward-fill — only the dates ECB actually published are returned
    (policy rates as change-date events, €STR daily).
    """
    records: list[ReferenceRateRecord] = []
    reader = csv.DictReader(io.StringIO(text))
    for row in reader:
        series = _ECB_SERIES_BY_KEY.get((row.get("KEY") or "").strip())
        if series is None:
            continue  # an extra/unknown series in the payload — ignore it
        raw_date = (row.get("TIME_PERIOD") or "").strip()
        raw_value = (row.get("OBS_VALUE") or "").strip()
        if not raw_date or not raw_value:
            continue  # no observation in this row
        try:
            rate_date = date.fromisoformat(raw_date[:10])  # daily series -> YYYY-MM-DD
        except ValueError:
            continue  # isolate a malformed/non-daily period
        try:
            rate_value = _q(Decimal(raw_value))
        except (InvalidOperation, ValueError):
            continue  # isolate a bad numeric cell
        records.append(
            ReferenceRateRecord(
                rate_date=rate_date,
                currency="EUR",
                country_or_region="euro_area",
                rate_family=series.rate_family,
                rate_name=series.rate_name,
                rate_value=rate_value,
                unit="percent",
                tenor=None,
                source=source,
                status="official",
                source_url=_ecb_source_url(series),
            )
        )
    return records


def _combine_flow_keys(keys_in_flow: list[str]) -> str:
    """Merge several same-dataflow series keys into one SDMX key (``+`` per dim).

    e.g. the three FM key-rate keys (differing only in the PROVIDER_FM_ID position)
    collapse to ``B.U2.EUR.4F.KR.MRR_FR+DFR+MLFR.LEV`` — one request, three series.
    All keys passed here share a dataflow, so they have the same dimension count.
    """
    split = [k.split(".") for k in keys_in_flow]
    merged: list[str] = []
    for position in zip(*split, strict=True):
        seen: list[str] = []
        for value in position:
            if value not in seen:
                seen.append(value)
        merged.append("+".join(seen))
    return ".".join(merged)


class ECBRatesSource:
    """Live ECB reference-rate adapter (official Data Portal SDMX API, guarded).

    Collects ECB key interest rates (main refinancing / deposit facility / marginal
    lending) and €STR as published observations. Explicit-only: the configured
    default stays the offline fixture, so this never fires on the scheduler unless
    ``--source ecb_rates`` is named. One bounded request per dataflow goes through
    ``guarded_fetch`` (recent-success cache -> source budget -> fetch log -> fetch),
    spaced by the budget's min delay. Collection only — it returns published
    observations (policy rates as change-date events, €STR daily); it never builds a
    curve, bootstraps or interpolates.
    """

    name = "ecb_rates"
    is_fixture = False
    requires_live_fetch = True
    request_kind = "fetch_ecb_rates"
    supported_currencies = ("EUR",)
    supported_rate_families = _ECB_RATE_FAMILIES
    description = (
        "ECB key interest rates (main refinancing / deposit facility / marginal "
        "lending) + €STR (official ECB Data Portal SDMX API). Observations only — "
        "no curve fitting/bootstrapping."
    )

    async def _fetch_flow_csv(
        self, flow: str, series_key: str, start: date | None, end: date | None
    ) -> str:
        params = {"format": "csvdata"}
        if start is not None:
            params["startPeriod"] = start.isoformat()
        if end is not None:
            params["endPeriod"] = end.isoformat()
        url = f"{_ECB_BASE_URL}/{flow}/{series_key}"
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.get(url, params=params, headers={"Accept": "text/csv"})
            response.raise_for_status()
            return response.text

    async def fetch_rates(
        self,
        *,
        session: AsyncSession | None = None,
        currency: str | None = None,
        country_or_region: str | None = None,
        rate_family: str | None = None,
        start_date: date | None = None,
        end_date: date | None = None,
    ) -> list[ReferenceRateRecord]:
        if session is None:  # the live path needs the budget/fetch-log session
            raise RuntimeError(
                "ecb_rates requires a database session "
                "(run it via the rates_ingestion worker, not directly)."
            )
        # Filters this provider cannot satisfy => empty (a clean no-op, not error).
        if currency and currency.upper() != "EUR":
            return []
        if country_or_region and country_or_region.lower() != "euro_area":
            return []
        selected = [
            s for s in _ECB_SERIES if not rate_family or s.rate_family == rate_family.lower()
        ]
        if not selected:
            return []

        from app.core.config import get_settings
        from app.services import source_budget, source_requests

        end = end_date or date.today()
        start = start_date or (end - timedelta(days=_ECB_DEFAULT_HISTORY_DAYS))
        if start > end:
            return []
        if (end - start).days > _ECB_MAX_HISTORY_DAYS:
            start = end - timedelta(days=_ECB_MAX_HISTORY_DAYS)
        ttl_seconds = get_settings().request_cache_ttl_seconds
        budget = await source_budget.get_budget(session, self.name)
        delay_s = ((budget.min_delay_ms or 0) / 1000) if budget else 0.0

        # Group selected series by dataflow so each flow is ONE combined request.
        flows: dict[str, list[_ECBSeries]] = {}
        for series in selected:
            flows.setdefault(series.flow, []).append(series)

        records: list[ReferenceRateRecord] = []
        for i, (flow, series_list) in enumerate(flows.items()):
            # Politeness spacing so a 2nd flow's guarded_fetch is not rejected as
            # min_delay (and the endpoint is never hammered) — same pattern the
            # instrument-price live adapter uses.
            if i and delay_s:
                await asyncio.sleep(delay_s)
            combined_key = _combine_flow_keys([s.key_in_flow for s in series_list])
            params = {
                "flow": flow,
                "key": combined_key,
                "startPeriod": start.isoformat(),
                "endPeriod": end.isoformat(),
            }
            try:
                fetch_result, payload = await source_budget.guarded_fetch(
                    session,
                    source=self.name,
                    request_kind=self.request_kind,
                    params=params,
                    endpoint_label=f"data-api.ecb.europa.eu/service/data/{flow}",
                    method="GET",
                    ttl_seconds=ttl_seconds,
                    fetch=lambda f=flow, k=combined_key: self._fetch_flow_csv(f, k, start, end),
                )
            except Exception:  # noqa: BLE001 - guarded_fetch logged it; skip this flow
                continue
            # Budget-blocked or served from the recent-success cache => no payload.
            if fetch_result.status == source_requests.RATE_LIMITED or payload is None:
                continue
            records.extend(parse_ecb_sdmx_csv(payload, source=self.name))

        records = [
            r
            for r in records
            if (start_date is None or r.rate_date >= start_date)
            and (end_date is None or r.rate_date <= end_date)
        ]
        records.sort(key=lambda r: (r.rate_date, r.rate_name, r.tenor_months or 0))
        return records


# --- planned live providers (placeholders behind the same protocol) ----------


class _PlannedRatesSource:
    """A named-but-unimplemented live rates adapter.

    Recognised so ``--source ecb_rates`` etc. resolve to a clear, offline failure
    (recorded as a clean failed job_run) instead of a crash or a surprise live
    call. Wiring it means fetching the official machine-readable file/API through
    ``guarded_fetch`` (budget + fetch log + cache + timeout), storing no secrets,
    and never scraping a brittle page — see the module docstring + AGENTS.md +
    docs/data_sources.md for the exact series keys that remain to verify.
    """

    is_fixture = False
    requires_live_fetch = True

    def __init__(self, name: str, description: str) -> None:
        self.name = name
        self.description = description

    async def fetch_rates(
        self,
        *,
        session: AsyncSession | None = None,
        currency: str | None = None,
        country_or_region: str | None = None,
        rate_family: str | None = None,
        start_date: date | None = None,
        end_date: date | None = None,
    ) -> list[ReferenceRateRecord]:
        raise NotImplementedError(
            f"{self.name} live adapter is planned, not implemented. "
            f"Use 'rates_fixture' (offline) or 'us_treasury_rates' (live USD)."
        )


@dataclass(frozen=True)
class RatesSourceInfo:
    """A row for the ``GET /api/v1/rates/sources`` catalogue."""

    source: str
    adapter_status: str  # implemented | planned
    is_fixture: bool
    requires_live_fetch: bool  # makes official network calls when run
    is_default: bool  # the configured default rates source
    description: str
    currencies: tuple[str, ...]
    rate_families: tuple[str, ...]


# --- registry ----------------------------------------------------------------

_FIXTURE = FixtureRatesSource()
_TREASURY = TreasuryRatesSource()
_ECB = ECBRatesSource()
_PLANNED: dict[str, _PlannedRatesSource] = {
    "boe_rates": _PlannedRatesSource(
        "boe_rates", "Bank of England Bank Rate + SONIA (official IADB / statistics)."
    ),
}

_SOURCES: dict[str, RatesSource] = {
    _FIXTURE.name: _FIXTURE,
    _TREASURY.name: _TREASURY,
    _ECB.name: _ECB,
    **_PLANNED,
}


def get_rates_source(name: str | None = None) -> RatesSource:
    from app.core.config import get_settings

    source_name = name or get_settings().rates_source_default
    source = _SOURCES.get(source_name)
    if source is None:
        raise ValueError(f"Unknown rates source: {source_name!r}")
    return source


def list_rates_sources() -> list[RatesSourceInfo]:
    """The rates-source catalogue (implemented fixture + Treasury + ECB; planned BoE).

    Each row says whether the adapter is implemented, whether it is the offline
    fixture, whether running it makes official network calls, and whether it is the
    configured default — so a client can tell ``rates_fixture`` (default, offline)
    from ``us_treasury_rates`` / ``ecb_rates`` (implemented but explicit-only, live).
    """
    from app.core.config import get_settings

    default = get_settings().rates_source_default
    families = tuple(sorted({s.rate_family for s in _CATALOGUE}))
    fixture_currencies = tuple(sorted({s.currency for s in _CATALOGUE}))
    infos = [
        RatesSourceInfo(
            source=_FIXTURE.name,
            adapter_status="implemented",
            is_fixture=True,
            requires_live_fetch=False,
            is_default=_FIXTURE.name == default,
            description="Offline deterministic reference rates (ECB/BoE/Treasury/benchmarks).",
            currencies=fixture_currencies,
            rate_families=families,
        ),
        RatesSourceInfo(
            source=_TREASURY.name,
            adapter_status="implemented",
            is_fixture=False,
            requires_live_fetch=True,
            is_default=_TREASURY.name == default,
            description=_TREASURY.description,
            currencies=_TREASURY.supported_currencies,
            rate_families=_TREASURY.supported_rate_families,
        ),
        RatesSourceInfo(
            source=_ECB.name,
            adapter_status="implemented",
            is_fixture=False,
            requires_live_fetch=True,
            is_default=_ECB.name == default,
            description=_ECB.description,
            currencies=_ECB.supported_currencies,
            rate_families=_ECB.supported_rate_families,
        ),
    ]
    coverage = {
        "boe_rates": (("GBP",), ("policy_rate", "overnight_rate")),
    }
    for name, planned in _PLANNED.items():
        currencies, fams = coverage[name]
        infos.append(
            RatesSourceInfo(
                source=name,
                adapter_status="planned",
                is_fixture=False,
                requires_live_fetch=True,
                is_default=name == default,
                description=planned.description,
                currencies=currencies,
                rate_families=fams,
            )
        )
    return infos
