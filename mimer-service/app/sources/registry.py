"""Programmatic registry of data-source *capabilities*.

This is a code-only catalogue describing what each candidate data source *can*
provide (asset classes, data types, whether it needs an API key, whether it
supports history/intraday/live, its reliability tier) and whether an adapter is
implemented yet. It is intentionally provider-agnostic documentation that the
ingestion layer and the `GET /api/v1/data-sources/capabilities` endpoint read,
so provider assumptions are not hard-coded across the codebase.

It complements two other things rather than duplicating them:
  * the `data_sources` DB table — runtime source *priority/ranking* + activation;
  * `SOURCES.md` / `docs/data_sources.md` — the human research + strategy.

Adding a new source means: append a `SourceCapability` here (status="planned"),
write the adapter behind the relevant `app/sources` protocol, then flip its
`adapter_status` to "implemented". See AGENTS.md.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# --- controlled vocabularies (kept in sync with docs/data_sources.md) --------

SOURCE_TYPES = (
    "identifier",
    "issuer",
    "market_data",
    "fx",
    "broker",
    "manual",
    "derived",
    "seed",
)

ASSET_CLASSES = (
    "etf",
    "mutual_fund",
    "equity",
    "bond",
    "future",
    "option",
    "fx",
    "cash",
    "index",
    "crypto",
    "commodity",
)

DATA_TYPES = (
    "identity",
    "fund_facts",
    "holdings",
    "distributions",
    "prices",
    "nav",
    "fx_rates",
    "documents",
    "corporate_actions",
    "option_chain",
    "futures_contracts",
    "bond_reference",
    "bond_prices",
    "yield_curves",
    # Official/reference rate *observations* (policy/overnight/par-yield), NOT a
    # constructed curve. ``yield_curves`` stays a separate, planned data type.
    "reference_rates",
    "transactions",
    # Generic Stooq *market series* (curve/market context), NOT tradable securities.
    # A sovereign benchmark yield/price series is a country/tenor generic series, NOT an
    # ISIN-level bond; a rates-futures series is a root/continuous series, NOT an
    # expiry-specific contract. See app/sources/stooq_market_series.py. Storage deferred.
    "market_series",
    "sovereign_yield_benchmark_series",
    "sovereign_benchmark_price_series",
    "rates_futures_series",
)

# Reliability tiers, roughly best-to-most-caveated for the data they assert.
RELIABILITY_TIERS = (
    "fixture",  # offline placeholder shipped with the service
    "official",  # issuer / exchange / central bank source of truth
    "free",  # free/public, usable but not contractual (delays, fragility)
    "freemium",  # API key, small free tier, paid for scale
    "paid",  # commercial / licensed
    "manual",  # human entry / override
    "derived",  # computed by this service
)

ADAPTER_STATUSES = ("implemented", "planned")


@dataclass(frozen=True)
class SourceCapability:
    source_name: str
    source_type: str  # one of SOURCE_TYPES
    asset_classes: tuple[str, ...]  # subset of ASSET_CLASSES
    data_types: tuple[str, ...]  # subset of DATA_TYPES
    reliability_tier: str  # one of RELIABILITY_TIERS
    requires_api_key: bool = False
    supports_history: bool = False
    supports_intraday: bool = False
    supports_live: bool = False
    supports_identifiers: bool = False
    adapter_status: str = "planned"  # one of ADAPTER_STATUSES
    notes: str | None = None
    tags: tuple[str, ...] = field(default_factory=tuple)


# --- the catalogue -----------------------------------------------------------
#
# Not exhaustive and deliberately conservative: a representative set covering the
# identifier/issuer/market-data/fx/broker spectrum and the future asset classes.
# See SOURCES.md for the full vendor research and licensing caveats.

_CAPABILITIES: list[SourceCapability] = [
    # --- identifier / security master ---
    SourceCapability(
        source_name="stub",
        source_type="identifier",
        asset_classes=("etf", "equity"),
        data_types=("identity",),
        reliability_tier="fixture",
        supports_identifiers=True,
        adapter_status="implemented",
        notes="Offline deterministic resolver fixture (knows the seeded ISINs).",
    ),
    SourceCapability(
        source_name="openfigi",
        source_type="identifier",
        asset_classes=("etf", "equity", "bond", "future", "option"),
        data_types=("identity",),
        reliability_tier="freemium",
        requires_api_key=False,
        supports_identifiers=True,
        adapter_status="implemented",
        notes=(
            "FIGI mapping; API key optional (raises rate limit). FIGI != ISIN. "
            "Used for single instrument resolution and batched constituent identity."
        ),
    ),
    SourceCapability(
        source_name="constituent_identity_fixture",
        source_type="identifier",
        asset_classes=("equity", "etf"),
        data_types=("identity",),
        reliability_tier="fixture",
        supports_identifiers=True,
        adapter_status="implemented",
        notes="Offline deterministic constituent resolver (seeded equities + a few "
        "ambiguous/not-found cases). Backs offline tests and local demos.",
    ),
    # --- ETF / fund issuer facts ---
    SourceCapability(
        source_name="issuer_fixture",
        source_type="issuer",
        asset_classes=("etf", "mutual_fund"),
        data_types=("fund_facts",),
        reliability_tier="fixture",
        adapter_status="implemented",
        notes="Offline issuer-facts provider mirroring the seeded funds.",
    ),
    SourceCapability(
        source_name="vanguard",
        source_type="issuer",
        asset_classes=("etf", "mutual_fund"),
        data_types=("fund_facts", "documents", "nav", "distributions"),
        reliability_tier="official",
        supports_history=True,
        notes="Issuer product pages (NAV/price/docs). JS/jurisdiction-specific.",
    ),
    SourceCapability(
        source_name="ishares",
        source_type="issuer",
        asset_classes=("etf",),
        data_types=("fund_facts", "holdings", "documents", "nav", "distributions"),
        reliability_tier="official",
        supports_history=True,
        notes="Strong for facts + holdings + premium/discount; web structure shifts.",
    ),
    SourceCapability(
        source_name="jpmam",
        source_type="issuer",
        asset_classes=("etf", "mutual_fund"),
        data_types=("fund_facts", "documents"),
        reliability_tier="official",
        notes="Holdings/distribution downloads need per-product verification.",
    ),
    # --- distributions (issuer-official; default stays the offline fixture) ---
    SourceCapability(
        source_name="distribution_fixture",
        source_type="issuer",
        asset_classes=("etf", "mutual_fund"),
        data_types=("distributions",),
        reliability_tier="fixture",
        supports_history=True,
        adapter_status="implemented",
        notes="Offline distribution provider mirroring the seeded distributing funds. "
        "Configured default for distribution_ingestion (live sources are explicit-only).",
    ),
    SourceCapability(
        source_name="jpmorgan_distributions",
        source_type="issuer",
        asset_classes=("etf", "mutual_fund"),
        data_types=("distributions",),
        reliability_tier="official",
        supports_history=True,
        supports_live=True,
        adapter_status="implemented",
        notes="Live J.P. Morgan AM fund distribution export (FundsMarketingHandler "
        "?type=fundDistribution), fetched through guarded_fetch. Explicit-only: needs a "
        "configured download URL (--url); the distribution default stays the offline "
        "fixture. Content-sniffed (CSV/TSV/HTML-table or OOXML .xlsx via the stdlib); legacy "
        "binary .xls (OLE2) deferred. Collection only — no dividend forecasting / yield "
        "projection / tax treatment.",
    ),
    SourceCapability(
        source_name="vanguard_distributions",
        source_type="issuer",
        asset_classes=("etf", "mutual_fund"),
        data_types=("distributions",),
        reliability_tier="official",
        supports_history=True,
        supports_live=True,
        adapter_status="implemented",
        notes="Live Vanguard product-data distributionHistory (official JSON/JSONP "
        "product-data API: urd-product-port-specific.json?vars=portId:<id>,issueType:F), "
        "fetched through guarded_fetch with conservative identifying official headers "
        "(UA/Accept/Accept-Language; NO cookies / NO TLS fingerprint spoofing / NO browser "
        "automation). Explicit-only: needs a configured product-data URL (--url); the "
        "distribution default stays the offline fixture. JSONP wrapper stripped; collection "
        "only — never scrapes brittle product-page HTML. VUSA config stays candidate (a prior "
        "live fetch was rejected at the TLS handshake).",
    ),
    SourceCapability(
        source_name="vanguard_distributions_export",
        source_type="issuer",
        asset_classes=("etf", "mutual_fund"),
        data_types=("distributions",),
        reliability_tier="manual",
        supports_history=True,
        adapter_status="implemented",
        notes="Offline parser for a manually exported official Vanguard distribution file "
        "(JSON/JSONP/CSV; no live fetch; pass the local path via --url). Status "
        "official_export for a parsed file, fixture for the bundled demo sample.",
    ),
    SourceCapability(
        source_name="blackrock_ishares_distributions",
        source_type="issuer",
        asset_classes=("etf",),
        data_types=("distributions",),
        reliability_tier="official",
        supports_history=True,
        supports_live=True,
        adapter_status="planned",
        notes="Live iShares/BlackRock distributions (planned): no clean official "
        "machine-readable distribution endpoint verified. Do NOT guess the holdings "
        "...ajax URL pattern for distributions; verify the product-page data export first. "
        "Use distribution_fixture until a clean endpoint is confirmed. See docs/data_sources.md.",
    ),
    # --- holdings (look-through constituents) ---
    SourceCapability(
        source_name="holdings_fixture",
        source_type="issuer",
        asset_classes=("etf", "mutual_fund"),
        data_types=("holdings",),
        reliability_tier="fixture",
        supports_history=True,
        adapter_status="implemented",
        notes="Offline holdings provider mirroring the seeded funds' top holdings. "
        "Configured default for issuer_holdings_ingestion (live sources are explicit-only).",
    ),
    SourceCapability(
        source_name="blackrock_ishares_holdings",
        source_type="issuer",
        asset_classes=("etf",),
        data_types=("holdings",),
        reliability_tier="official",
        supports_history=True,
        supports_live=True,
        adapter_status="implemented",
        notes="Live iShares/BlackRock issuer-hosted holdings CSV download "
        "(...ajax?dataType=fund&fileName=<TICKER>_holdings&fileType=csv), fetched through "
        "guarded_fetch. Explicit-only: needs a configured/known download URL (the numeric "
        "ajaxId is not globally constant); the holdings default stays the offline fixture. "
        "Parser scans past the metadata preamble for the holdings header. Collection only — "
        "no look-through analytics/PnL (those live in the Rust local pricer).",
    ),
    SourceCapability(
        source_name="jpmorgan_etf_holdings",
        source_type="issuer",
        asset_classes=("etf",),
        data_types=("holdings",),
        reliability_tier="official",
        supports_history=True,
        supports_live=True,
        adapter_status="implemented",
        notes="Live J.P. Morgan AM daily ETF holdings export (FundsMarketingHandler "
        "?type=dailyETFHoldings), fetched through guarded_fetch. Explicit-only: needs a "
        "configured/known download URL; the holdings default stays the offline fixture. "
        "Content-sniffed (CSV/TSV/HTML-table or OOXML .xlsx via the stdlib); legacy binary "
        ".xls (OLE2) is deferred (no pandas / no binary-Excel dependency) — verify reports "
        "reason=binary_unsupported. The 'cusip' query param may carry an ISIN-like UCITS "
        "identifier. Collection only.",
    ),
    SourceCapability(
        source_name="vanguard_holdings_export",
        source_type="issuer",
        asset_classes=("etf", "mutual_fund"),
        data_types=("holdings",),
        reliability_tier="manual",
        supports_history=True,
        adapter_status="implemented",
        notes="Offline parser for a manually exported official Vanguard holdings file "
        "(no live fetch; pass the local path via --url). Status official_export for a parsed "
        "file, fixture for the bundled demo sample.",
    ),
    SourceCapability(
        source_name="vanguard_holdings",
        source_type="issuer",
        asset_classes=("etf", "mutual_fund"),
        data_types=("holdings",),
        reliability_tier="official",
        supports_history=True,
        supports_live=True,
        adapter_status="planned",
        notes="Live Vanguard holdings (planned): no stable official machine-readable "
        "endpoint verified. Do NOT scrape brittle product-page HTML as a canonical source; "
        "use vanguard_holdings_export until a clean endpoint is confirmed. See "
        "docs/data_sources.md.",
    ),
    SourceCapability(
        source_name="tiingo",
        source_type="market_data",
        asset_classes=("equity", "etf", "mutual_fund", "fx"),
        data_types=("prices", "distributions", "corporate_actions", "fx_rates"),
        reliability_tier="freemium",
        requires_api_key=True,
        supports_history=True,
        notes="Clean EOD + corporate actions covering stocks/ETFs/funds.",
    ),
    # --- market prices ---
    SourceCapability(
        source_name="instrument_price_fixture",
        source_type="market_data",
        asset_classes=("equity", "etf"),
        data_types=("prices",),
        reliability_tier="fixture",
        supports_history=True,
        adapter_status="implemented",
        notes="Offline deterministic EOD provider for resolved constituents "
        "(seeded equities). Backs offline constituent-price tests and local demos.",
    ),
    SourceCapability(
        source_name="stooq",
        source_type="market_data",
        asset_classes=("equity", "etf", "index", "fx", "commodity"),
        data_types=("prices",),
        reliability_tier="free",
        supports_history=True,
        adapter_status="implemented",
        notes="Free EOD CSV; fragile/non-contractual. Default price source.",
    ),
    SourceCapability(
        source_name="yfinance",
        source_type="market_data",
        asset_classes=("equity", "etf", "index", "fx"),
        data_types=("prices",),
        reliability_tier="free",
        supports_history=True,
        adapter_status="implemented",
        notes="Yahoo chart endpoint; unofficial. Fallback price source.",
    ),
    SourceCapability(
        source_name="stooq_market_series",
        source_type="market_data",
        asset_classes=("bond", "future", "index"),
        data_types=(
            "market_series",
            "sovereign_yield_benchmark_series",
            "sovereign_benchmark_price_series",
            "rates_futures_series",
        ),
        reliability_tier="free",
        supports_history=True,
        adapter_status="planned",
        notes="Stooq generic market series (curve/market context), classification-only this "
        "slice (app/sources/stooq_market_series.py); storage deferred (no market_series table "
        "yet — schema proposal in docs/data_sources.md). A sovereign benchmark yield/price "
        "series (e.g. 10YDEY.B / 10YDEP.B) is a country/tenor generic series, NOT an ISIN-level "
        "bond; a .F rates-futures series (e.g. ZN.F / G.F) is a root/continuous series, NOT an "
        "expiry-specific contract. Never store these on the bond/security master.",
    ),
    SourceCapability(
        source_name="alpha_vantage",
        source_type="market_data",
        asset_classes=("equity", "etf", "mutual_fund", "fx", "option"),
        data_types=("prices", "corporate_actions", "fx_rates", "option_chain"),
        reliability_tier="freemium",
        requires_api_key=True,
        supports_history=True,
        notes="Adjusted EOD incl. split/dividend events. Tiny free tier.",
    ),
    SourceCapability(
        source_name="fmp",
        source_type="market_data",
        asset_classes=("etf", "mutual_fund", "equity", "fx", "index", "commodity"),
        data_types=("prices", "holdings", "fund_facts", "nav"),
        reliability_tier="freemium",
        requires_api_key=True,
        supports_history=True,
        notes="Broad API incl. ETF/fund holdings; display/redistribution licensed.",
    ),
    # --- FX ---
    SourceCapability(
        source_name="fx_fixture",
        source_type="fx",
        asset_classes=("fx",),
        data_types=("fx_rates",),
        reliability_tier="fixture",
        supports_history=True,
        adapter_status="implemented",
        notes="Offline USD-anchored cross-rate provider (consistent triangulation).",
    ),
    SourceCapability(
        source_name="ecb",
        source_type="fx",
        asset_classes=("fx",),
        data_types=("fx_rates",),
        reliability_tier="official",
        supports_history=True,
        notes="ECB EUR reference rates (information-only, ~16:00 CET).",
    ),
    SourceCapability(
        source_name="boe",
        source_type="fx",
        asset_classes=("fx",),
        data_types=("fx_rates", "yield_curves"),
        reliability_tier="official",
        supports_history=True,
        notes="Bank of England daily spot vs sterling + yield curves.",
    ),
    # --- official / reference rates (observations only; NOT curves) ---
    SourceCapability(
        source_name="rates_fixture",
        source_type="market_data",
        asset_classes=("cash", "bond"),
        data_types=("reference_rates",),
        reliability_tier="fixture",
        supports_history=True,
        adapter_status="implemented",
        notes="Offline deterministic reference rates: ECB policy rates + €STR, BoE "
        "Bank Rate + SONIA, US Treasury par yields, SOFR/Fed Funds. Observations "
        "only — no curve building/bootstrapping (that lives in the local pricer).",
    ),
    SourceCapability(
        source_name="ecb_rates",
        source_type="market_data",
        asset_classes=("cash",),
        data_types=("reference_rates",),
        reliability_tier="official",
        supports_history=True,
        adapter_status="implemented",
        notes="ECB key interest rates (main refinancing / deposit facility / marginal "
        "lending) + €STR (official ECB Data Portal SDMX API: FM + EST dataflows, "
        "format=csvdata), fetched one bounded request per dataflow through "
        "guarded_fetch. Explicit-only: the rates default stays the offline fixture. "
        "Observations stored as-is (policy rates as change-date events, €STR daily) — "
        "no curve fitting/bootstrapping (that lives in the Rust local pricer).",
    ),
    SourceCapability(
        source_name="boe_rates",
        source_type="market_data",
        asset_classes=("cash",),
        data_types=("reference_rates",),
        reliability_tier="official",
        supports_history=True,
        notes="Bank of England Bank Rate (IUDBEDR) + SONIA (IUDSOIA), official IADB "
        "statistics. Planned: the IADB CSV export returns HTTP 403 to a plain client, "
        "so a clean non-brittle machine-readable access path must be verified before "
        "wiring (no HTML scraping, no third-party/FRED feed). See docs/data_sources.md.",
    ),
    SourceCapability(
        source_name="us_treasury_rates",
        source_type="market_data",
        asset_classes=("cash", "bond"),
        data_types=("reference_rates",),
        reliability_tier="official",
        supports_history=True,
        adapter_status="implemented",
        notes="US Treasury daily par yield curve rates (official home.treasury.gov "
        "XML feed), fetched per-year through guarded_fetch. Explicit-only: the rates "
        "default stays the offline fixture. Observations stored as-is, no curve "
        "fitting/bootstrapping (that lives in the Rust local pricer).",
    ),
    # --- documents ---
    SourceCapability(
        source_name="document_fixture",
        source_type="issuer",
        asset_classes=("etf", "mutual_fund"),
        data_types=("documents",),
        reliability_tier="fixture",
        supports_history=True,
        adapter_status="implemented",
        notes="Offline document provider (factsheet/KID/prospectus) with hashed content.",
    ),
    SourceCapability(
        source_name="issuer_documents",
        source_type="issuer",
        asset_classes=("etf", "mutual_fund"),
        data_types=("documents",),
        reliability_tier="official",
        notes="Factsheet/KID/prospectus PDFs; hash/version locally.",
    ),
    # --- broker / user imports ---
    SourceCapability(
        source_name="broker_csv",
        source_type="broker",
        asset_classes=("etf", "equity", "bond", "future", "option", "cash"),
        data_types=("transactions",),
        reliability_tier="manual",
        adapter_status="implemented",
        notes="Generic broker CSV import (generic_csv_v1): parses buys/sells/"
        "dividends/cash into the canonical transaction ledger + bounded position "
        "reconciliation. Offline; resolves instruments against existing identity only.",
    ),
    SourceCapability(
        source_name="ibkr_flex_import",
        source_type="broker",
        asset_classes=("etf", "equity", "bond", "future", "option", "cash", "fx"),
        data_types=("transactions", "corporate_actions"),
        reliability_tier="official",
        requires_api_key=True,
        supports_history=True,
        adapter_status="planned",
        notes="Interactive Brokers Flex Web Service (planned, HIGH-PRIORITY): broker/account "
        "truth — positions, trades, cash, dividends, fees, FX conversions, corporate actions. "
        "Needs a Flex token + query id (NEVER logged). Must be idempotent and feed the existing "
        "broker_imports / portfolio_transactions path, then trigger the resolve -> price -> FX -> "
        "valuation cascade. Not implemented in this slice. See docs/data_sources.md §IBKR.",
        tags=("high_priority",),
    ),
    SourceCapability(
        source_name="ibkr_market_data",
        source_type="market_data",
        asset_classes=("equity", "etf", "future", "option", "index", "fx"),
        data_types=("prices",),
        reliability_tier="paid",
        requires_api_key=True,
        supports_history=True,
        supports_intraday=True,
        adapter_status="planned",
        notes="Interactive Brokers market data (planned, optional): entitlement / session / "
        "subscription dependent; needs a running TWS / IB Gateway session. Kept distinct from "
        "ibkr_flex_import and NOT a default source.",
    ),
    # --- manual / derived / seed (this service) ---
    SourceCapability(
        source_name="manual",
        source_type="manual",
        asset_classes=ASSET_CLASSES,
        data_types=DATA_TYPES,
        reliability_tier="manual",
        adapter_status="implemented",
        notes="Human overrides; outranks automated ingestion in source priority.",
    ),
    SourceCapability(
        source_name="derived",
        source_type="derived",
        asset_classes=("etf", "equity", "fx"),
        data_types=("nav", "fx_rates"),
        reliability_tier="derived",
        adapter_status="implemented",
        notes="Values computed by this service (portfolio value/income series).",
    ),
    SourceCapability(
        source_name="seed",
        source_type="seed",
        asset_classes=("etf",),
        data_types=("fund_facts", "prices", "holdings", "distributions", "documents"),
        reliability_tier="fixture",
        adapter_status="implemented",
        notes="Placeholder seed data for local development.",
    ),
    # --- future asset classes (interfaces only; no live adapters now) ---
    SourceCapability(
        source_name="us_treasury",
        source_type="market_data",
        asset_classes=("bond",),
        data_types=("yield_curves", "bond_reference"),
        reliability_tier="official",
        supports_history=True,
        notes="Daily par yield curves / bill rates for USD govt curves.",
    ),
    SourceCapability(
        source_name="uk_dmo",
        source_type="market_data",
        asset_classes=("bond",),
        data_types=("bond_reference", "bond_prices"),
        reliability_tier="official",
        supports_history=True,
        notes="Gilt reference/ISINs; FTSE-Tradeweb prices restrict commercial use.",
    ),
    SourceCapability(
        source_name="databento",
        source_type="market_data",
        asset_classes=("future", "option", "equity"),
        data_types=("futures_contracts", "prices", "option_chain"),
        reliability_tier="paid",
        requires_api_key=True,
        supports_history=True,
        supports_intraday=True,
        supports_live=True,
        notes="Strong futures/options symbology + data; commercial.",
    ),
    SourceCapability(
        source_name="tradier",
        source_type="broker",
        asset_classes=("option", "equity"),
        data_types=("option_chain", "prices", "transactions"),
        reliability_tier="freemium",
        requires_api_key=True,
        supports_history=True,
        supports_intraday=True,
        notes="US options chains + broker import; realtime needs a brokerage account.",
    ),
]


def list_capabilities() -> list[SourceCapability]:
    """Return the full source-capability catalogue (stable order)."""
    return list(_CAPABILITIES)


def get_capability(source_name: str) -> SourceCapability | None:
    for capability in _CAPABILITIES:
        if capability.source_name == source_name:
            return capability
    return None


def implemented_sources() -> list[SourceCapability]:
    return [c for c in _CAPABILITIES if c.adapter_status == "implemented"]
