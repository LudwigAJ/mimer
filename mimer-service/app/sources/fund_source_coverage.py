"""Target-fund data-source coverage matrix (live readiness, per fund + data type).

A small, explicit, **in-code** matrix that answers — for the funds the live-data
hardening slice cares about (``VUSA`` / ``ISF`` / ``JEPG``) — one operational
question per *(fund, data type)* cell: *can the backend actually fetch, parse and
store this data type for this fund live, is it safe to schedule, or what exactly
blocks it?*

It is a pure composition of the two authoritative in-code registries so it can never
drift from them:

* ``app/sources/source_readiness.py`` — the operational status of each *source*
  (fixture / implemented_live / verified_live / candidate / planned / unsupported)
  and whether it is scheduler-safe.
* ``app/sources/issuer_source_config.py`` — the *per-fund* (ISIN) verified/candidate
  issuer download config for holdings/distributions (so ISF's verified iShares
  holdings config and JEPG's blocked JPM ``.xls`` are reflected per fund).

Why a separate module (not just ``source_readiness.py``): the readiness matrix is
keyed by *source*; this one is keyed by *(fund, data type)*. A source can be
``verified_live`` in general (iShares holdings) yet only verified for one fund
(ISF), and a fund needs six distinct data types (facts / listing price / NAV /
holdings / distributions / documents) whose sources differ. Curating the
fund×data-type grid here keeps both registries small and the per-fund truth honest.

Honesty rules (identical spirit to the sibling registries — see AGENTS.md):

* A fund's listing-price cell is ``implemented_live`` (a real Stooq adapter exists,
  scheduler-safe) — NOT ``verified_live`` (no live fetch for that exact ticker has
  been recorded here). ``live_fetch_verified`` stays ``False`` until a real fetch is
  recorded.
* Holdings/distributions cells inherit the per-fund issuer-config status, so only
  ISF holdings is ``verified_live`` (its config is ``verified``); JEPG holdings stays
  ``candidate`` (issuer export format VARIES across runs — a 2026-06-27 bounded verify
  returned a clean ``.xlsx``, but a 2026-06-25 fetch returned binary ``.xls``; promotion
  waits for a stable re-verify); VUSA distributions stays ``candidate`` (TLS handshake).
* Facts and documents are ``fixture`` — the funds are fed by the offline
  ``issuer_fixture`` / ``document_fixture`` providers (and seed); no *live* issuer
  facts/document adapter exists, so these are never counted as live coverage.
* NAV is ``planned`` for every fund: there is no NAV/iNAV source or schema field, and
  the exchange close price is NEVER conflated with NAV.
* ``stored_verified`` is conservative: the verify path is fetch+parse only (it does
  not ingest), so it stays ``False`` even for ISF until a live ingestion is recorded.

This module is a pure leaf: no DB, no network, no other source-adapter imports (only
the two sibling in-code registries), so services/endpoints can import it freely.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from app.sources import issuer_source_config
from app.sources import source_readiness as readiness

# --- target funds ------------------------------------------------------------


@dataclass(frozen=True)
class TargetFund:
    """One fund the live-readiness slice tracks (public reference identifiers only)."""

    symbol: str
    isin: str
    issuer: str
    name: str


TARGET_FUNDS: tuple[TargetFund, ...] = (
    TargetFund(
        symbol="VUSA",
        isin="IE00B3XXRP09",
        issuer="Vanguard",
        name="Vanguard S&P 500 UCITS ETF",
    ),
    TargetFund(
        symbol="ISF",
        isin="IE0005042456",
        issuer="iShares (BlackRock)",
        name="iShares Core FTSE 100 UCITS ETF",
    ),
    TargetFund(
        symbol="JEPG",
        isin="IE0003UVYC20",
        issuer="J.P. Morgan Asset Management",
        name="JPMorgan Global Equity Premium Income Active UCITS ETF",
    ),
)


# --- data types --------------------------------------------------------------

FACTS = "facts"
LISTING_PRICE = "listing_price"
NAV = "nav"
HOLDINGS = "holdings"
DISTRIBUTIONS = "distributions"
DOCUMENTS = "documents"

FUND_DATA_TYPES: tuple[str, ...] = (
    FACTS,
    LISTING_PRICE,
    NAV,
    HOLDINGS,
    DISTRIBUTIONS,
    DOCUMENTS,
)

# A cell is "live coverage" when a real live adapter can fetch it (implemented or
# verified). Candidate/planned/fixture/unsupported are NOT live coverage — a fixture
# success must never look like production readiness (see AGENTS.md).
_LIVE_STATUSES = (readiness.VERIFIED_LIVE, readiness.IMPLEMENTED_LIVE)


def is_live_status(status: str) -> bool:
    return status in _LIVE_STATUSES


# --- per-fund intended sources ----------------------------------------------
#
# The live source each fund's holdings/distributions cell is keyed to. These name the
# *intended* live adapter even when no usable config exists yet, so the matrix can
# report the exact blocker (rather than silently hiding the gap).
_HOLDINGS_SOURCE_BY_ISIN: dict[str, str] = {
    "IE0005042456": "blackrock_ishares_holdings",  # ISF — verified config
    "IE0003UVYC20": "jpmorgan_etf_holdings",  # JEPG — candidate (binary .xls)
    "IE00B3XXRP09": "vanguard_holdings",  # VUSA — planned (export-only offline parser)
}
_DISTRIBUTIONS_SOURCE_BY_ISIN: dict[str, str] = {
    "IE0005042456": "blackrock_ishares_distributions",  # ISF — planned
    "IE0003UVYC20": "jpmorgan_distributions",  # JEPG — implemented_live adapter, no verified URL
    "IE00B3XXRP09": "vanguard_distributions",  # VUSA — candidate (TLS handshake)
}
# Funds that have an offline export parser as a manual fallback (NOT a live source).
_OFFLINE_EXPORT_HOLDINGS: frozenset[str] = frozenset({"IE00B3XXRP09"})  # vanguard_holdings_export
_OFFLINE_EXPORT_DISTRIBUTIONS: frozenset[str] = frozenset(
    {"IE00B3XXRP09"}  # vanguard_distributions_export
)

# Listing prices come from the configured fund-price default (Stooq), reused for the
# scheduler-safe EOD path. One source for every target fund.
_LISTING_PRICE_SOURCE = "stooq"


@dataclass(frozen=True)
class FundCoverageRow:
    """One *(fund, data type)* cell of live-readiness truth."""

    fund_symbol: str
    isin: str
    issuer: str
    data_type: str
    source_name: str | None
    status: str  # one of readiness.READINESS_STATUSES
    live_fetch_verified: bool
    parse_verified: bool
    stored_verified: bool
    safe_for_scheduler: bool
    last_verified_at: date | None = None
    known_blocker: str | None = None
    next_action: str | None = None
    notes: str | None = None
    # An offline manual-export parser exists as a fallback (never counts as live).
    offline_export_available: bool = False

    @property
    def is_live(self) -> bool:
        return is_live_status(self.status)

    @property
    def is_blocked(self) -> bool:
        """Status indicates a real blocker preventing live use (carry a blocker)."""
        return self.status in (readiness.CANDIDATE, readiness.PLANNED) and bool(self.known_blocker)


# --- per-data-type resolvers (pure) ------------------------------------------


def _config_status_to_coverage(config_status: str) -> str:
    """Map an issuer ``source_status`` to a coverage status."""
    if config_status == issuer_source_config.VERIFIED:
        return readiness.VERIFIED_LIVE
    if config_status == issuer_source_config.CANDIDATE:
        return readiness.CANDIDATE
    # planned / disabled
    return readiness.PLANNED


def _readiness_status_no_config(readiness_status: str | None) -> str:
    """Coverage status for a fund cell whose source has no usable per-fund config.

    An ``implemented_live`` adapter with no usable per-fund URL cannot actually fetch
    *this* fund yet, so it is honestly a ``candidate`` for the fund (not live).
    """
    if readiness_status == readiness.IMPLEMENTED_LIVE:
        return readiness.CANDIDATE
    if readiness_status in (readiness.VERIFIED_LIVE, readiness.CANDIDATE, readiness.PLANNED):
        return readiness_status
    return readiness.PLANNED


def _issuer_doc_cell(
    fund: TargetFund,
    *,
    data_type: str,
    source_name: str,
    offline_export: bool,
) -> FundCoverageRow:
    """Build a holdings/distributions cell from the per-fund issuer config + readiness."""
    config = issuer_source_config.get_source_config(fund.isin, source_name)
    readiness_row = readiness.get_row(source_name)
    readiness_status = readiness_row.status if readiness_row else None

    if config is not None:
        status = _config_status_to_coverage(config.source_status)
        verified = status == readiness.VERIFIED_LIVE
        last_verified_at = config.verified_at if verified else None
    else:
        status = _readiness_status_no_config(readiness_status)
        verified = False
        last_verified_at = None

    # Scheduler-safe only when this fund's cell is genuinely verified-live AND the
    # source is marked scheduler-safe in the readiness matrix (never for candidate/
    # planned/export-only).
    safe = bool(
        status == readiness.VERIFIED_LIVE and readiness_row and readiness_row.safe_for_scheduler
    )
    blocker = None if verified else (readiness_row.known_blockers if readiness_row else None)
    next_action = readiness_row.next_action if readiness_row else None

    notes = None
    if offline_export:
        notes = "An offline manual-export parser exists as a fallback (not a live source)."

    return FundCoverageRow(
        fund_symbol=fund.symbol,
        isin=fund.isin,
        issuer=fund.issuer,
        data_type=data_type,
        source_name=source_name,
        status=status,
        live_fetch_verified=verified,
        parse_verified=verified,
        stored_verified=False,  # verify path is fetch+parse only; no recorded live store
        safe_for_scheduler=safe,
        last_verified_at=last_verified_at,
        known_blocker=blocker,
        next_action=next_action,
        notes=notes,
        offline_export_available=offline_export,
    )


def _facts_cell(fund: TargetFund) -> FundCoverageRow:
    return FundCoverageRow(
        fund_symbol=fund.symbol,
        isin=fund.isin,
        issuer=fund.issuer,
        data_type=FACTS,
        source_name="issuer_fixture",
        status=readiness.FIXTURE,
        live_fetch_verified=False,
        parse_verified=False,
        stored_verified=False,
        safe_for_scheduler=False,
        known_blocker="No live issuer-facts adapter — funds are enriched only from the offline "
        "issuer_fixture (and seed). Fund facts carry fixture/seed provenance, not live.",
        next_action="implement a live issuer-facts adapter (or accept curated manual facts); do "
        "NOT scrape brittle product-page HTML.",
        notes="Modelled fields: name / provider / domicile / base_currency / distribution_policy "
        "/ strategy / OCF. NAV / AUM / benchmark / inception / TER are not modelled.",
    )


def _listing_price_cell(fund: TargetFund) -> FundCoverageRow:
    row = readiness.get_row(_LISTING_PRICE_SOURCE)
    safe = bool(row and row.safe_for_scheduler)
    return FundCoverageRow(
        fund_symbol=fund.symbol,
        isin=fund.isin,
        issuer=fund.issuer,
        data_type=LISTING_PRICE,
        source_name=_LISTING_PRICE_SOURCE,
        status=readiness.IMPLEMENTED_LIVE,
        live_fetch_verified=False,  # no recorded clean CSV for this exact ticker yet
        parse_verified=False,
        stored_verified=False,
        safe_for_scheduler=safe,
        known_blocker="A 2026-06-27 bounded live verify did NOT get a clean Stooq EOD CSV for "
        "this LSE ETF symbol (404 / HTML interstitial; Stooq is free / non-contractual / "
        "fragile, and its symbol mapping is best-effort). The adapter + path are scheduler-safe "
        "in general, but a clean live fetch for this exact ticker is not yet confirmed.",
        next_action="confirm the exact Stooq symbol for this LSE ETF, or use the yfinance "
        "fallback (--source yfinance); then optionally schedule the daily EOD price job.",
        notes="Exchange close / EOD listing mark via the scheduler-safe Stooq adapter "
        "(budgeted + cached + logged). This is the listing price, NOT NAV.",
    )


def _nav_cell(fund: TargetFund) -> FundCoverageRow:
    return FundCoverageRow(
        fund_symbol=fund.symbol,
        isin=fund.isin,
        issuer=fund.issuer,
        data_type=NAV,
        source_name=None,
        status=readiness.PLANNED,
        live_fetch_verified=False,
        parse_verified=False,
        stored_verified=False,
        safe_for_scheduler=False,
        known_blocker="No NAV/iNAV source or schema field. The exchange close price is NOT NAV "
        "and is never relabelled as NAV.",
        next_action="model NAV/iNAV as a distinct series (issuer NAV endpoint) before claiming "
        "NAV coverage.",
        notes="listing_price = live; nav = planned (kept distinct on purpose).",
    )


def _holdings_cell(fund: TargetFund) -> FundCoverageRow:
    source_name = _HOLDINGS_SOURCE_BY_ISIN[fund.isin]
    return _issuer_doc_cell(
        fund,
        data_type=HOLDINGS,
        source_name=source_name,
        offline_export=fund.isin in _OFFLINE_EXPORT_HOLDINGS,
    )


def _distributions_cell(fund: TargetFund) -> FundCoverageRow:
    source_name = _DISTRIBUTIONS_SOURCE_BY_ISIN[fund.isin]
    return _issuer_doc_cell(
        fund,
        data_type=DISTRIBUTIONS,
        source_name=source_name,
        offline_export=fund.isin in _OFFLINE_EXPORT_DISTRIBUTIONS,
    )


def _documents_cell(fund: TargetFund) -> FundCoverageRow:
    return FundCoverageRow(
        fund_symbol=fund.symbol,
        isin=fund.isin,
        issuer=fund.issuer,
        data_type=DOCUMENTS,
        source_name="document_fixture",
        status=readiness.FIXTURE,
        live_fetch_verified=False,
        parse_verified=False,
        stored_verified=False,
        safe_for_scheduler=False,
        known_blocker="No live document/factsheet/KID metadata adapter — only the offline "
        "document_fixture (and seed product-page links). Change detection exists but runs "
        "against fixture data.",
        next_action="add a live document-metadata adapter (factsheet / KID / KIID URLs + change "
        "detection) per issuer product page before claiming live document coverage.",
        notes="Document types tracked when present: factsheet / KID / KIID / prospectus / "
        "annual-report / product-page links.",
    )


_CELL_BUILDERS = {
    FACTS: _facts_cell,
    LISTING_PRICE: _listing_price_cell,
    NAV: _nav_cell,
    HOLDINGS: _holdings_cell,
    DISTRIBUTIONS: _distributions_cell,
    DOCUMENTS: _documents_cell,
}


# --- public API (pure) -------------------------------------------------------


def get_target_fund(fund_symbol: str) -> TargetFund | None:
    norm = fund_symbol.strip().upper()
    for fund in TARGET_FUNDS:
        if fund.symbol == norm:
            return fund
    return None


def get_target_fund_by_isin(isin: str) -> TargetFund | None:
    norm = isin.strip().upper()
    for fund in TARGET_FUNDS:
        if fund.isin == norm:
            return fund
    return None


def coverage_for_fund(fund: TargetFund) -> list[FundCoverageRow]:
    """The six data-type cells for one fund (stable data-type order)."""
    return [_CELL_BUILDERS[data_type](fund) for data_type in FUND_DATA_TYPES]


def list_coverage_rows(fund_symbol: str | None = None) -> list[FundCoverageRow]:
    """The full fund×data-type coverage matrix (stable order), optionally one fund."""
    if fund_symbol is not None:
        fund = get_target_fund(fund_symbol)
        return coverage_for_fund(fund) if fund else []
    rows: list[FundCoverageRow] = []
    for fund in TARGET_FUNDS:
        rows.extend(coverage_for_fund(fund))
    return rows


def coverage_cell(fund_symbol: str, data_type: str) -> FundCoverageRow | None:
    fund = get_target_fund(fund_symbol)
    if fund is None or data_type not in _CELL_BUILDERS:
        return None
    return _CELL_BUILDERS[data_type](fund)


# --- rollups (pure) ----------------------------------------------------------


@dataclass(frozen=True)
class FundCoverageFundSummary:
    """Per-fund rollup: which data types are live, and the fund's blockers."""

    fund_symbol: str
    issuer: str
    isin: str
    live_price: bool
    live_holdings: bool
    live_distributions: bool
    live_facts: bool
    live_documents: bool
    data_type_status: dict[str, str]
    blockers: list[str]


def fund_summary(fund: TargetFund) -> FundCoverageFundSummary:
    cells = {row.data_type: row for row in coverage_for_fund(fund)}
    blockers = [f"{row.data_type}: {row.known_blocker}" for row in cells.values() if row.is_blocked]
    return FundCoverageFundSummary(
        fund_symbol=fund.symbol,
        issuer=fund.issuer,
        isin=fund.isin,
        live_price=cells[LISTING_PRICE].is_live,
        live_holdings=cells[HOLDINGS].is_live,
        live_distributions=cells[DISTRIBUTIONS].is_live,
        live_facts=cells[FACTS].is_live,
        live_documents=cells[DOCUMENTS].is_live,
        data_type_status={dt: cells[dt].status for dt in FUND_DATA_TYPES},
        blockers=blockers,
    )


@dataclass(frozen=True)
class FundCoverageSummary:
    """Compact rollup of the target-fund coverage matrix for capabilities/diagnostics."""

    target_funds_total: int
    target_funds_with_live_price: int
    target_funds_with_live_holdings: int
    target_funds_with_live_distributions: int
    target_funds_with_live_facts: int
    target_funds_with_live_documents: int
    fund_sources_verified_live: int
    fund_sources_candidate: int
    fund_sources_planned: int
    fund_sources_fixture_only: int
    fund_source_blockers: int
    funds: list[FundCoverageFundSummary]


def summary() -> FundCoverageSummary:
    rows = list_coverage_rows()
    fund_summaries = [fund_summary(f) for f in TARGET_FUNDS]
    return FundCoverageSummary(
        target_funds_total=len(TARGET_FUNDS),
        target_funds_with_live_price=sum(1 for f in fund_summaries if f.live_price),
        target_funds_with_live_holdings=sum(1 for f in fund_summaries if f.live_holdings),
        target_funds_with_live_distributions=sum(1 for f in fund_summaries if f.live_distributions),
        target_funds_with_live_facts=sum(1 for f in fund_summaries if f.live_facts),
        target_funds_with_live_documents=sum(1 for f in fund_summaries if f.live_documents),
        fund_sources_verified_live=sum(1 for r in rows if r.status == readiness.VERIFIED_LIVE),
        fund_sources_candidate=sum(1 for r in rows if r.status == readiness.CANDIDATE),
        fund_sources_planned=sum(1 for r in rows if r.status == readiness.PLANNED),
        fund_sources_fixture_only=sum(1 for r in rows if r.status == readiness.FIXTURE),
        fund_source_blockers=sum(1 for r in rows if r.is_blocked),
        funds=fund_summaries,
    )


# Convenience for tests / docs that need the stable list of target symbols.
TARGET_FUND_SYMBOLS: tuple[str, ...] = tuple(f.symbol for f in TARGET_FUNDS)
