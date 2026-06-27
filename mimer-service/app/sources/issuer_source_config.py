"""Known issuer source configuration registry (per-fund live download URLs).

A small, explicit, **in-code** registry of issuer-published holdings/distribution
download URLs keyed by fund ISIN + ``source_name``. It is the single home for the
"which verified URL does this fund use for this live source" question that the
holdings/distribution adapters, the market-data planner, the capabilities catalogue
and diagnostics all need to agree on.

Why in-code (not a DB table): the rest of the source layer is already in-code
registries (``app/sources/registry.py`` capabilities, ``source_budget`` budgets),
the set of verified issuer endpoints is tiny and hand-curated, and the URLs are
public issuer-hosted files (no secrets). A DB-backed admin/config system would be
overbuilt for this slice — see AGENTS.md (prefer explicit, boring code).

Each entry carries a ``source_status``:

* ``verified``  — a clean live fetch + parse has been confirmed for this product.
* ``candidate`` — the URL/endpoint shape is known/observed but not yet confirmed
  by a live fetch+parse in this environment (honest default — we do not inflate).
* ``planned``   — recorded for documentation, not usable yet.
* ``disabled``  — explicitly turned off (kept for provenance).

**Usage convention (conservative, consistent with the existing holdings adapters).**
A config is *usable* (its URL is auto-supplied to the worker without ``--url``)
only when its status is ``verified`` or ``candidate`` **and** the live ``--source``
is explicitly named — the configured ingestion default always stays the offline
fixture, so the worker/scheduler never makes a surprise live call. ``planned`` /
``disabled`` configs are never auto-used. We do **not** mark a config ``verified``
without a successful live fetch+parse, and we never guess a distribution URL from a
holdings URL (see AGENTS.md).

This module is a pure leaf (no DB / no network / no other source-adapter imports),
so the adapters and services can import it freely.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

# --- controlled vocabulary ---------------------------------------------------

DATA_TYPE_HOLDINGS = "holdings"
DATA_TYPE_DISTRIBUTIONS = "distributions"

VERIFIED = "verified"
CANDIDATE = "candidate"
PLANNED = "planned"
DISABLED = "disabled"

SOURCE_STATUSES = (VERIFIED, CANDIDATE, PLANNED, DISABLED)
# Statuses a worker/planner may auto-use (supply the URL without an explicit --url).
USABLE_STATUSES = (VERIFIED, CANDIDATE)


@dataclass(frozen=True)
class IssuerSourceConfig:
    """A verified/candidate issuer download config for one fund + live source."""

    fund_isin: str
    provider: str
    data_type: str  # DATA_TYPE_HOLDINGS | DATA_TYPE_DISTRIBUTIONS
    source_name: str
    url: str
    source_status: str = CANDIDATE
    ticker: str | None = None
    verified_at: date | None = None
    notes: str | None = None

    @property
    def is_usable(self) -> bool:
        """Whether the worker/planner may auto-use this config (no --url needed)."""
        return self.source_status in USABLE_STATUSES


# --- the registry ------------------------------------------------------------
#
# Seeded with the verified-style endpoints for the funds the user supplied. Every
# entry is ``candidate`` until a live fetch+parse confirms it in this environment
# (we do not inflate to ``verified`` without that check). The numeric BlackRock
# ``ajaxId`` is NOT globally constant, so each holdings URL is the exact observed
# one (no page discovery); query params are case-sensitive.
_CONFIGS: tuple[IssuerSourceConfig, ...] = (
    # ISF — iShares Core FTSE 100 UCITS ETF (issuer-hosted holdings CSV download).
    IssuerSourceConfig(
        fund_isin="IE0005042456",
        ticker="ISF",
        provider="blackrock_ishares",
        data_type=DATA_TYPE_HOLDINGS,
        source_name="blackrock_ishares_holdings",
        url=(
            "https://www.blackrock.com/uk/individual/products/251795/"
            "ishares-ftse-100-ucits-etf-inc-fund/1472631233320.ajax"
            "?dataType=fund&fileName=ISF_holdings&fileType=csv"
        ),
        source_status=VERIFIED,
        verified_at=date(2026, 6, 25),
        notes="iShares Core FTSE 100 issuer holdings CSV. Exact observed ajax URL "
        "(ajaxId is not globally constant). Verified 2026-06-25 via --verify-source: "
        "one guarded live fetch returned a clean CSV that parsed to ~107 holdings.",
    ),
    # JEPG — JPM Global Equity Premium Income Active UCITS ETF (daily ETF holdings).
    IssuerSourceConfig(
        fund_isin="IE0003UVYC20",
        ticker="JEPG",
        provider="jpmorgan",
        data_type=DATA_TYPE_HOLDINGS,
        source_name="jpmorgan_etf_holdings",
        url=(
            "https://am.jpmorgan.com/FundsMarketingHandler/excel"
            "?type=dailyETFHoldings&cusip=IE0003UVYC20&country=gb&role=adv"
            "&fundType=N_ETF&locale=en-GB&isUnderlyingHolding=false&isProxyHolding=false"
        ),
        source_status=CANDIDATE,
        notes="JPM AM daily ETF holdings export (FundsMarketingHandler). The 'cusip' "
        "param carries the UCITS ISIN. Candidate: a 2026-06-25 live fetch succeeded "
        "(HTTP 200) but returned a legacy binary .xls (OLE2) body — verify reports "
        "reason=binary_unsupported (the backend parses CSV/TSV/HTML-table and OOXML "
        ".xlsx via the stdlib, but does NOT decode old binary .xls: no pandas / no "
        "binary-Excel dependency). Promote only once a CSV / HTML-table / .xlsx export "
        "shape is confirmed (the optional xlrd/calamine follow-up for OLE2 .xls is "
        "documented in docs/data_sources.md but deliberately not wired).",
    ),
    # VUSA — Vanguard S&P 500 UCITS ETF USD Distributing (product-data JSON).
    IssuerSourceConfig(
        fund_isin="IE00B3XXRP09",
        ticker="VUSA",
        provider="vanguard",
        data_type=DATA_TYPE_DISTRIBUTIONS,
        source_name="vanguard_distributions",
        url=(
            "https://api.vanguard.com/rs/gre/gra/1.7.0/datasets/"
            "urd-product-port-specific.json?vars=portId:9503,issueType:F"
        ),
        source_status=CANDIDATE,
        notes="Vanguard product-data distributionHistory JSON (portId 9503, issueType F). "
        "Candidate ONLY — a 2026-06-25 live fetch failed at the TLS handshake "
        "(SSLV3_ALERT_HANDSHAKE_FAILURE). The adapter now sends conservative, identifying "
        "official headers (UA + Accept + Accept-Language; NO cookies / NO TLS fingerprint "
        "spoofing), but a handshake rejection is transport-layer, so headers alone may not "
        "resolve it — re-verify from a network where the endpoint is reachable. Do not mark "
        "verified until the endpoint returns a clean machine-readable payload and the parser "
        "finds the expected distributionHistory. Never scrape the product-page HTML; the "
        "offline vanguard_distributions_export parser stays the safe fallback.",
    ),
)


# --- lookups (pure) ----------------------------------------------------------


def _norm_isin(isin: str | None) -> str | None:
    return isin.strip().upper() if isin and isin.strip() else None


def list_source_configs(
    *,
    data_type: str | None = None,
    source_name: str | None = None,
    provider: str | None = None,
    status: str | None = None,
    usable_only: bool = False,
) -> list[IssuerSourceConfig]:
    """All configs matching the given filters (stable order)."""
    result: list[IssuerSourceConfig] = []
    for config in _CONFIGS:
        if data_type is not None and config.data_type != data_type:
            continue
        if source_name is not None and config.source_name != source_name:
            continue
        if provider is not None and config.provider != provider:
            continue
        if status is not None and config.source_status != status:
            continue
        if usable_only and not config.is_usable:
            continue
        result.append(config)
    return result


def get_source_config(isin: str | None, source_name: str) -> IssuerSourceConfig | None:
    """The config for an exact (ISIN, source_name) pair, if any (any status)."""
    norm = _norm_isin(isin)
    if norm is None:
        return None
    for config in _CONFIGS:
        if config.fund_isin == norm and config.source_name == source_name:
            return config
    return None


def find_source_config(
    isin: str | None, *, data_type: str | None = None, usable_only: bool = True
) -> IssuerSourceConfig | None:
    """The (first) usable config for a fund ISIN, optionally narrowed by data type."""
    norm = _norm_isin(isin)
    if norm is None:
        return None
    for config in _CONFIGS:
        if config.fund_isin != norm:
            continue
        if data_type is not None and config.data_type != data_type:
            continue
        if usable_only and not config.is_usable:
            continue
        return config
    return None


def known_source_url(isin: str | None, source_name: str) -> str | None:
    """The configured URL for (ISIN, source) — only if the config is usable."""
    config = get_source_config(isin, source_name)
    return config.url if config and config.is_usable else None


def known_source_name(isin: str | None, data_type: str) -> str | None:
    """The usable live source configured for a fund's ISIN + data type (planner hint)."""
    config = find_source_config(isin, data_type=data_type, usable_only=True)
    return config.source_name if config else None


def config_status(isin: str | None, source_name: str) -> str | None:
    """The configured status for (ISIN, source), or None if no config exists."""
    config = get_source_config(isin, source_name)
    return config.source_status if config else None


def has_source_config(isin: str | None, data_type: str, *, usable_only: bool = True) -> bool:
    """Whether a (usable) live config exists for this fund ISIN + data type."""
    return find_source_config(isin, data_type=data_type, usable_only=usable_only) is not None


def configs_for_source(source_name: str) -> list[IssuerSourceConfig]:
    """All configs for a given live source name (any status, stable order)."""
    return [c for c in _CONFIGS if c.source_name == source_name]


def example_identifiers(source_name: str, *, limit: int = 3) -> list[str]:
    """A small list of example fund identifiers (ticker:ISIN) for a live source.

    Used by the capabilities endpoint to show *which* funds a live source is
    configured for. Public issuer identifiers only — no secrets.
    """
    out: list[str] = []
    for config in configs_for_source(source_name):
        label = f"{config.ticker}:{config.fund_isin}" if config.ticker else config.fund_isin
        out.append(label)
        if len(out) >= limit:
            break
    return out


def status_counts(*, data_type: str | None = None) -> dict[str, int]:
    """Count of configs per status (optionally for one data type)."""
    counts = {status: 0 for status in SOURCE_STATUSES}
    for config in list_source_configs(data_type=data_type):
        counts[config.source_status] = counts.get(config.source_status, 0) + 1
    return counts
