"""Production data-source readiness matrix (operational honesty for the VPS).

A single, explicit, **in-code** catalogue that answers one operational question per
source: *can this source actually ingest real data on a VPS, and is it safe to put
on the scheduler — or is it fixture-only, blocked, or not implemented?* It composes
the existing in-code registries (``app/sources/registry.py`` capabilities,
``app/sources/issuer_source_config.py`` per-fund URLs, the configured ``*_source_default``
settings) into one matrix the capabilities/data-source endpoints and diagnostics read.

Why a separate module (not just ``registry.py``): the capability registry says what a
source *can provide* (asset classes / data types / adapter implemented yes-no). It does
**not** capture the *operational* truth this slice needs — whether a live fetch+parse was
actually verified, whether the source is safe to schedule, what concrete blocker stops it,
and the recommended next action. Those cannot be derived from ``adapter_status`` alone
(an adapter can exist yet be blocked by a binary ``.xls`` body or a TLS handshake), so they
are curated here, honestly, one row at a time. See AGENTS.md (prefer explicit, boring code).

Status taxonomy (worst-to-best honesty, deliberately conservative):

* ``fixture``          — offline deterministic provider; dev/testing/smoke only, NOT a VPS
  production source. Never marked scheduler-safe as a production default.
* ``implemented_live`` — an adapter exists and works with an explicit command/config, but a
  clean live fetch+parse has not been *recorded* in this environment (so we do not inflate
  it to ``verified_live``). Offline manual exported-file parsers also live here.
* ``verified_live``    — a live fetch+parse succeeded for at least one known target; safe to
  recommend for scheduled use subject to its source budget.
* ``candidate``        — a plausible source whose endpoint shape is known but which is
  blocked or not yet verified (carry the exact blocker).
* ``planned``          — not implemented yet (carry the exact next action).
* ``unsupported``      — intentionally not supported (a non-goal, never ``planned``).

This module is a pure leaf: no DB, no network, no other source-adapter imports (only the
sibling in-code registries + the lazily-read settings defaults), so services/endpoints can
import it freely.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from app.sources import issuer_source_config, registry

# --- status vocabulary -------------------------------------------------------

FIXTURE = "fixture"
IMPLEMENTED_LIVE = "implemented_live"
VERIFIED_LIVE = "verified_live"
CANDIDATE = "candidate"
PLANNED = "planned"
UNSUPPORTED = "unsupported"

READINESS_STATUSES = (
    FIXTURE,
    IMPLEMENTED_LIVE,
    VERIFIED_LIVE,
    CANDIDATE,
    PLANNED,
    UNSUPPORTED,
)

# Data types for which the service ultimately wants a *verified live* VPS source. A
# required data type whose only sources are fixture/implemented_live/candidate/planned is
# reported as a readiness gap (``missing_required_live_sources``) — honestly, never hidden.
REQUIRED_LIVE_DATA_TYPES = ("reference_rates", "fx_rates", "prices", "holdings")


@dataclass(frozen=True)
class SourceReadinessRow:
    """One source's operational readiness for live VPS ingestion + scheduling."""

    data_type: str
    source_name: str
    provider: str
    status: str  # one of READINESS_STATUSES
    worker_name: str | None
    recommended_cadence: str
    safe_for_scheduler: bool
    requires_secret: bool = False
    requires_url_config: bool = False
    requires_running_gateway: bool = False
    last_verified_at: date | None = None
    known_blockers: str | None = None
    next_action: str | None = None
    # The ``*_source_default`` settings attribute this source is the default for (so
    # ``default_for_worker`` is derived from live config, never hard-coded), or an explicit
    # override for sources whose adapter id differs from the registry source_name.
    default_setting_attr: str | None = None
    default_override: bool | None = None
    notes: str | None = None
    # Public reference identifiers only (e.g. ``ticker:ISIN``) — NEVER secrets/tokenised URLs.
    example_targets: tuple[str, ...] = field(default_factory=tuple)


# Convenience: the ISF holdings config is the one verified-live issuer endpoint today; pull
# its verified date from the single source of truth so the matrix never drifts from it.
def _isf_verified_at() -> date | None:
    cfg = issuer_source_config.get_source_config("IE0005042456", "blackrock_ishares_holdings")
    return cfg.verified_at if cfg else None


_ROWS: tuple[SourceReadinessRow, ...] = (
    # =====================================================================
    # Official / reference rates  (worker: rates_ingestion)
    # =====================================================================
    SourceReadinessRow(
        data_type="reference_rates",
        source_name="rates_fixture",
        provider="(offline fixture)",
        status=FIXTURE,
        worker_name="rates_ingestion",
        recommended_cadence="dev/smoke only",
        safe_for_scheduler=False,
        default_setting_attr="rates_source_default",
        notes="Offline deterministic ECB/BoE/Treasury/benchmark observations. Default; "
        "dev/demo only — not a VPS production source.",
    ),
    SourceReadinessRow(
        data_type="reference_rates",
        source_name="us_treasury_rates",
        provider="U.S. Department of the Treasury",
        status=IMPLEMENTED_LIVE,
        worker_name="rates_ingestion",
        recommended_cadence="daily (early UTC, after US close)",
        safe_for_scheduler=True,
        next_action="run a bounded live verify (--limit 10), then optionally add an explicit "
        "scheduled job naming --source us_treasury_rates (never the default).",
        notes="Official daily par-yield XML feed via guarded_fetch (budget/cache/log), one "
        "year per request. Explicit-only: the default stays the offline fixture. Collection "
        "only — no curve building.",
    ),
    SourceReadinessRow(
        data_type="reference_rates",
        source_name="ecb_rates",
        provider="European Central Bank (Data Portal SDMX)",
        status=IMPLEMENTED_LIVE,
        worker_name="rates_ingestion",
        recommended_cadence="business daily",
        safe_for_scheduler=True,
        next_action="run a bounded live verify (--limit 10), then optionally add an explicit "
        "scheduled job naming --source ecb_rates (never the default).",
        notes="Official ECB Data Portal SDMX API (FM key rates + EST €STR) via guarded_fetch, "
        "one bounded request per dataflow. Explicit-only. Collection only — no curve building.",
    ),
    SourceReadinessRow(
        data_type="reference_rates",
        source_name="boe_rates",
        provider="Bank of England (IADB)",
        status=PLANNED,
        worker_name="rates_ingestion",
        recommended_cadence="daily (when implemented)",
        safe_for_scheduler=False,
        known_blockers="Official IADB CSV export (IUDBEDR Bank Rate / IUDSOIA SONIA) returns "
        "HTTP 403 to a plain client.",
        next_action="verify a clean, non-brittle machine-readable access path (official source "
        "only — no FRED/third-party feed, no HTML scraping) before wiring behind guarded_fetch.",
    ),
    # =====================================================================
    # FX rates  (worker: fx_ingestion)
    # =====================================================================
    SourceReadinessRow(
        data_type="fx_rates",
        source_name="fx_fixture",
        provider="(offline fixture)",
        status=FIXTURE,
        worker_name="fx_ingestion",
        recommended_cadence="dev/smoke only",
        safe_for_scheduler=False,
        default_setting_attr="fx_source_default",
        notes="Offline USD-anchored cross-rate provider (consistent triangulation). Default; "
        "FX is still fixture-only — no live FX adapter is implemented yet.",
    ),
    SourceReadinessRow(
        data_type="fx_rates",
        source_name="ecb",
        provider="European Central Bank (EUR reference rates)",
        status=PLANNED,
        worker_name="fx_ingestion",
        recommended_cadence="daily (~16:00 CET, when implemented)",
        safe_for_scheduler=False,
        known_blockers="No live ECB FX adapter implemented yet (fx_ingestion stays fixture-only).",
        next_action="implement a live ECB EUR-reference-rate adapter behind the FxSource "
        "protocol + guarded_fetch (information-only; enable only when configured).",
    ),
    # =====================================================================
    # Prices  (workers: price_ingestion / instrument_eod_price_ingestion)
    # =====================================================================
    SourceReadinessRow(
        data_type="prices",
        source_name="stooq",
        provider="Stooq (free EOD)",
        status=IMPLEMENTED_LIVE,
        worker_name="price_ingestion / instrument_eod_price_ingestion",
        recommended_cadence="daily EOD",
        safe_for_scheduler=True,
        default_setting_attr="price_source_default",
        known_blockers="Free / non-contractual / fragile (no SLA; symbol mapping is best-effort).",
        notes="Free daily CSV. Configured default for fund-listing prices and reused for "
        "instrument/constituent EOD prices; every call is budgeted + cached + logged.",
    ),
    SourceReadinessRow(
        data_type="prices",
        source_name="yfinance",
        provider="Yahoo (unofficial chart endpoint)",
        status=IMPLEMENTED_LIVE,
        worker_name="price_ingestion / instrument_eod_price_ingestion",
        recommended_cadence="daily EOD (explicit fallback only)",
        safe_for_scheduler=False,
        known_blockers="Unofficial Yahoo endpoint; explicit-only fallback, not the default.",
        notes="Fallback price source; only used when explicitly named with --source yfinance.",
    ),
    SourceReadinessRow(
        data_type="prices",
        source_name="instrument_price_fixture",
        provider="(offline fixture)",
        status=FIXTURE,
        worker_name="instrument_eod_price_ingestion",
        recommended_cadence="dev/smoke only",
        safe_for_scheduler=False,
        default_setting_attr="constituent_price_source_default",
        notes="Offline deterministic EOD provider for resolved constituents/imported holdings. "
        "Default for the instrument price worker; dev/demo only.",
    ),
    # =====================================================================
    # ETF holdings  (worker: issuer_holdings_ingestion)
    # =====================================================================
    SourceReadinessRow(
        data_type="holdings",
        source_name="holdings_fixture",
        provider="(offline fixture)",
        status=FIXTURE,
        worker_name="issuer_holdings_ingestion",
        recommended_cadence="dev/smoke only",
        safe_for_scheduler=False,
        default_setting_attr="holdings_source_default",
        notes="Offline holdings provider mirroring the seeded funds. Default; dev/demo only.",
    ),
    SourceReadinessRow(
        data_type="holdings",
        source_name="blackrock_ishares_holdings",
        provider="BlackRock / iShares",
        status=VERIFIED_LIVE,
        worker_name="issuer_holdings_ingestion",
        recommended_cadence="daily or weekly (per the verified ISF config)",
        safe_for_scheduler=True,
        requires_url_config=True,
        last_verified_at=_isf_verified_at(),
        next_action="optionally add an explicit scheduled job naming --source "
        "blackrock_ishares_holdings for ISF (verified config). Verify a new URL per product.",
        notes="Live issuer-hosted holdings CSV via guarded_fetch. Verified for ISF "
        "(clean CSV -> ~107 holdings). Explicit-only; the default stays the offline fixture. "
        "ajaxId is not globally constant — each URL is the exact verified one.",
        example_targets=("ISF:IE0005042456",),
    ),
    SourceReadinessRow(
        data_type="holdings",
        source_name="jpmorgan_etf_holdings",
        provider="J.P. Morgan Asset Management",
        status=CANDIDATE,
        worker_name="issuer_holdings_ingestion",
        recommended_cadence="not scheduled (format varies across runs)",
        safe_for_scheduler=False,
        requires_url_config=True,
        known_blockers="JEPG export FORMAT VARIES across runs: a 2026-06-27 bounded live "
        "verify returned a clean OOXML .xlsx (247 holdings, parseable) but a 2026-06-25 "
        "fetch returned a legacy binary .xls (OLE2 -> binary_unsupported; the stdlib parses "
        ".xlsx/CSV/TSV/HTML-table but not old binary .xls). Not reliably verified across runs.",
        next_action="re-verify for stability (the endpoint returned .xlsx on 2026-06-27 vs "
        "binary .xls on 2026-06-25); promote to verified_live + scheduler-safe once a clean "
        "machine-readable export is consistent across runs/environments.",
        example_targets=("JEPG:IE0003UVYC20",),
    ),
    SourceReadinessRow(
        data_type="holdings",
        source_name="vanguard_holdings_export",
        provider="Vanguard (manual export)",
        status=IMPLEMENTED_LIVE,
        worker_name="issuer_holdings_ingestion",
        recommended_cadence="manual (no scheduled fetch)",
        safe_for_scheduler=False,
        requires_url_config=True,
        notes="Offline parser for a manually exported official Vanguard holdings file "
        "(pass the local path via --url). No live fetch — not a scheduled source.",
    ),
    SourceReadinessRow(
        data_type="holdings",
        source_name="vanguard_holdings",
        provider="Vanguard",
        status=PLANNED,
        worker_name="issuer_holdings_ingestion",
        recommended_cadence="not scheduled (planned)",
        safe_for_scheduler=False,
        requires_url_config=True,
        known_blockers="No stable official machine-readable holdings endpoint verified.",
        next_action="verify a stable official spreadsheet/API URL before wiring behind "
        "guarded_fetch — do NOT scrape brittle product-page HTML.",
    ),
    # =====================================================================
    # Distributions  (worker: distribution_ingestion)
    # =====================================================================
    SourceReadinessRow(
        data_type="distributions",
        source_name="distribution_fixture",
        provider="(offline fixture)",
        status=FIXTURE,
        worker_name="distribution_ingestion",
        recommended_cadence="dev/smoke only",
        safe_for_scheduler=False,
        default_setting_attr="distribution_source_default",
        notes="Offline distribution provider mirroring the seeded distributing funds. "
        "Default; dev/demo only.",
    ),
    SourceReadinessRow(
        data_type="distributions",
        source_name="jpmorgan_distributions",
        provider="J.P. Morgan Asset Management",
        status=IMPLEMENTED_LIVE,
        worker_name="distribution_ingestion",
        recommended_cadence="not scheduled (no verified URL yet)",
        safe_for_scheduler=False,
        requires_url_config=True,
        known_blockers="No verified per-product fundDistribution URL is registered yet (do NOT "
        "assume it from the holdings download).",
        next_action="verify the exact fundDistribution export URL per product, then register a "
        "candidate config and run --verify-source.",
    ),
    SourceReadinessRow(
        data_type="distributions",
        source_name="vanguard_distributions",
        provider="Vanguard",
        status=CANDIDATE,
        worker_name="distribution_ingestion",
        recommended_cadence="not scheduled (blocked)",
        safe_for_scheduler=False,
        requires_url_config=True,
        known_blockers="VUSA product-data live fetch was rejected at the TLS handshake "
        "(SSLV3_ALERT_HANDSHAKE_FAILURE) — a transport-layer rejection.",
        next_action="re-verify from a network where the endpoint is reachable; do NOT use "
        "browser/TLS fingerprint spoofing. Keep the offline export parser as fallback.",
        example_targets=("VUSA:IE00B3XXRP09",),
    ),
    SourceReadinessRow(
        data_type="distributions",
        source_name="vanguard_distributions_export",
        provider="Vanguard (manual export)",
        status=IMPLEMENTED_LIVE,
        worker_name="distribution_ingestion",
        recommended_cadence="manual (no scheduled fetch)",
        safe_for_scheduler=False,
        requires_url_config=True,
        notes="Offline parser for a manually exported official Vanguard distribution file "
        "(JSON/JSONP/CSV via --url). No live fetch — not a scheduled source.",
    ),
    SourceReadinessRow(
        data_type="distributions",
        source_name="blackrock_ishares_distributions",
        provider="BlackRock / iShares",
        status=PLANNED,
        worker_name="distribution_ingestion",
        recommended_cadence="not scheduled (planned)",
        safe_for_scheduler=False,
        requires_url_config=True,
        known_blockers="No clean official machine-readable iShares distribution endpoint verified.",
        next_action="verify the product-page Distributions data export before wiring — NEVER "
        "guess it from the holdings ...ajax URL pattern.",
    ),
    # =====================================================================
    # Instrument identity  (workers: constituent_identity_resolution /
    #                       imported_instrument_resolution)
    # =====================================================================
    SourceReadinessRow(
        data_type="identity",
        source_name="constituent_identity_fixture",
        provider="(offline fixture)",
        status=FIXTURE,
        worker_name="constituent_identity_resolution / imported_instrument_resolution",
        recommended_cadence="dev/smoke only",
        safe_for_scheduler=False,
        default_setting_attr="constituent_identity_source_default",
        notes="Offline deterministic constituent/imported resolver. Default; dev/demo only.",
    ),
    SourceReadinessRow(
        data_type="identity",
        source_name="openfigi",
        provider="OpenFIGI (Bloomberg)",
        status=IMPLEMENTED_LIVE,
        worker_name="constituent_identity_resolution / imported_instrument_resolution",
        recommended_cadence="after new holdings/imports; or daily unresolved queue (strict budget)",
        safe_for_scheduler=True,
        requires_secret=False,
        next_action="resolve the planner's unresolved-identity backlog in budgeted batches; "
        "never name-only. An API key is optional (raises the rate limit), never logged.",
        notes="Live FIGI mapping, batched (<=10/request) behind guarded_fetch; key in the "
        "header only. Explicit-only; the default stays the offline fixture.",
    ),
    # =====================================================================
    # Broker / account data  (worker: broker_csv_import; IBKR planned)
    # =====================================================================
    SourceReadinessRow(
        data_type="transactions",
        source_name="broker_csv",
        provider="Generic broker CSV (generic_csv_v1)",
        status=IMPLEMENTED_LIVE,
        worker_name="broker_csv_import",
        recommended_cadence="on user upload (not a scheduled fetch)",
        safe_for_scheduler=False,
        default_override=True,
        notes="Real, offline, provider-agnostic CSV import into the canonical transaction "
        "ledger + bounded position reconciliation. Production-ready, but file-driven — there "
        "is no remote endpoint to schedule.",
    ),
    SourceReadinessRow(
        data_type="transactions",
        source_name="ibkr_flex_import",
        provider="Interactive Brokers (Flex Web Service)",
        status=PLANNED,
        worker_name=None,
        recommended_cadence="daily (high-value; when implemented)",
        safe_for_scheduler=False,
        requires_secret=True,
        known_blockers="Not implemented. High-priority: broker/account truth (positions, "
        "trades, cash, dividends, fees, FX conversions, corporate actions).",
        next_action="implement a Flex Web Service client (Flex token + query id) that is "
        "idempotent, NEVER logs the token, feeds the existing broker_imports / "
        "portfolio_transactions path, and triggers the resolve -> price -> FX -> valuation "
        "cascade.",
    ),
    SourceReadinessRow(
        data_type="prices",
        source_name="ibkr_market_data",
        provider="Interactive Brokers (market data)",
        status=PLANNED,
        worker_name=None,
        recommended_cadence="not default (optional)",
        safe_for_scheduler=False,
        requires_secret=True,
        requires_running_gateway=True,
        known_blockers="Entitlement / session / subscription dependent; needs a running "
        "TWS / IB Gateway session.",
        next_action="optional, low priority — keep distinct from ibkr_flex_import; not a "
        "default source.",
    ),
    # =====================================================================
    # Stooq market series  (generic benchmark/futures series; storage deferred)
    # =====================================================================
    SourceReadinessRow(
        data_type="market_series",
        source_name="stooq_market_series",
        provider="Stooq (benchmark + rates-futures series)",
        status=PLANNED,
        worker_name=None,
        recommended_cadence="daily EOD (when implemented)",
        safe_for_scheduler=False,
        known_blockers="No market_series table yet (storage migration deferred). "
        "Classification-only this slice (app/sources/stooq_market_series.py).",
        next_action="implement a generic market_series ingestion path + table per the schema "
        "proposal in docs/data_sources.md. Classify as sovereign_yield_benchmark_series / "
        "sovereign_benchmark_price_series / rates_futures_series — NEVER as actual bonds or "
        "expiry-specific futures.",
    ),
)


# --- derivation + lookups (pure) ---------------------------------------------


def _is_default(row: SourceReadinessRow) -> bool:
    """Whether this source is the configured default for its worker (live config)."""
    if row.default_override is not None:
        return row.default_override
    if row.default_setting_attr is None:
        return False
    from app.core.config import get_settings

    return getattr(get_settings(), row.default_setting_attr, None) == row.source_name


def list_rows() -> list[SourceReadinessRow]:
    """The full readiness matrix (stable order)."""
    return list(_ROWS)


def default_for_worker(row: SourceReadinessRow) -> bool:
    """Public accessor: is ``row`` the configured default for its worker?"""
    return _is_default(row)


def get_row(source_name: str) -> SourceReadinessRow | None:
    for row in _ROWS:
        if row.source_name == source_name:
            return row
    return None


def rows_for_data_type(data_type: str) -> list[SourceReadinessRow]:
    return [r for r in _ROWS if r.data_type == data_type]


def scheduler_safe_sources() -> list[SourceReadinessRow]:
    return [r for r in _ROWS if r.safe_for_scheduler]


def status_counts() -> dict[str, int]:
    counts = {status: 0 for status in READINESS_STATUSES}
    for row in _ROWS:
        counts[row.status] = counts.get(row.status, 0) + 1
    return counts


def missing_required_live_sources() -> list[str]:
    """Required data types that have NO ``verified_live`` source (honest readiness gaps)."""
    verified_types = {r.data_type for r in _ROWS if r.status == VERIFIED_LIVE}
    return [dt for dt in REQUIRED_LIVE_DATA_TYPES if dt not in verified_types]


@dataclass(frozen=True)
class ReadinessSummary:
    """A compact rollup of the readiness matrix for capabilities/diagnostics."""

    total_sources: int
    status_counts: dict[str, int]
    scheduler_safe_count: int
    verified_live_count: int
    candidate_count: int
    planned_count: int
    fixture_count: int
    required_live_data_types: list[str]
    missing_required_live_sources: list[str]
    scheduler_safe_sources: list[str]


def summary() -> ReadinessSummary:
    counts = status_counts()
    return ReadinessSummary(
        total_sources=len(_ROWS),
        status_counts=counts,
        scheduler_safe_count=sum(1 for r in _ROWS if r.safe_for_scheduler),
        verified_live_count=counts.get(VERIFIED_LIVE, 0),
        candidate_count=counts.get(CANDIDATE, 0),
        planned_count=counts.get(PLANNED, 0),
        fixture_count=counts.get(FIXTURE, 0),
        required_live_data_types=list(REQUIRED_LIVE_DATA_TYPES),
        missing_required_live_sources=missing_required_live_sources(),
        scheduler_safe_sources=[r.source_name for r in _ROWS if r.safe_for_scheduler],
    )


def known_registry_source_names() -> set[str]:
    """Source names in the readiness matrix that are also in the capability registry.

    Used by a consistency test: every readiness row whose source maps to a registered
    adapter must reference a real registry source_name (no typos/drift). Sources that are
    deliberately not in the capability registry (e.g. ``us_treasury_rates``/``ecb_rates``
    live rates adapters, or the future ``ibkr_*`` rows) are exempt.
    """
    registry_names = {c.source_name for c in registry.list_capabilities()}
    return {r.source_name for r in _ROWS if r.source_name in registry_names}
