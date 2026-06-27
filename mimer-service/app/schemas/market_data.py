"""Market-data planning schemas.

A read-only, computed plan of what would need to be resolved/fetched to fill the
gaps for a workspace's held funds and their constituents — *without* fetching
anything live. It exists so future stock/constituent backfills are deduped,
prioritised and budget-aware before a single external request is made.
"""

from __future__ import annotations

from pydantic import BaseModel

# Plan item types.
ITEM_TYPES = (
    "resolve_constituent_identity",
    # Constituent identity outcomes that need a human, not another auto-resolve.
    "ambiguous_constituent_identity",
    "not_found_constituent_identity",
    # A resolved constituent whose listing has no EOD price yet (future ingestion).
    "fetch_constituent_price",
    "fetch_listing_price",
    "fetch_fund_price",
    "fetch_fx_rate",
    "refresh_holdings",
    "refresh_documents",
    "refresh_distributions",
    "refresh_fund_facts",
    # Imported (broker CSV) directly-held instruments. An unresolved transaction
    # with a safe identifier -> resolve_imported_instrument; an ambiguous one ->
    # ambiguous_imported_instrument (manual); a name-only row ->
    # manual_review_imported_instrument (blocked); a resolved listing missing a
    # price / FX -> fetch_imported_instrument_price / fetch_imported_fx_rate.
    "resolve_imported_instrument",
    "ambiguous_imported_instrument",
    "manual_review_imported_instrument",
    "fetch_imported_instrument_price",
    "fetch_imported_fx_rate",
    # Official/reference rate readiness for a relevant currency (collection only —
    # never a curve build). ``fetch_reference_rates`` is the generic ask; the
    # family-specific variants signal which official series is missing/stale.
    "fetch_reference_rates",
    "fetch_policy_rates",
    "fetch_overnight_rates",
    "fetch_treasury_par_yields",
    # The portfolio valuation/readiness snapshot is missing or stale (a local
    # recompute over already-ingested prices/FX — NOT a fetch, NOT PnL).
    "recompute_portfolio_valuation",
)


class MarketDataPlanItem(BaseModel):
    item_type: str
    # 1 (highest) .. 5 (long tail). See market_data_planner for the policy.
    priority: int
    reason: str
    # Stable dedupe identity (e.g. one Apple item even if held via many funds).
    plan_key: str
    label: str | None = None
    related_fund_id: int | None = None
    related_fund_listing_id: int | None = None
    related_holding_id: int | None = None
    # Reserved: no instrument master yet, but the plan already accounts for it.
    related_instrument_id: int | None = None
    identifier_scheme: str | None = None
    identifier_value: str | None = None
    source_candidates: list[str] = []
    estimated_requests: int = 1
    blocked_by: str | None = None
    status: str = "pending"
    # Known issuer source-config awareness (refresh_holdings / refresh_distributions).
    # ``known_config`` is True when a usable (verified/candidate) live issuer config is
    # registered for this fund (so the live ``--source`` can run without ``--url``);
    # ``config_status`` is that config's status; ``needs_url_config`` is its inverse
    # (no usable live config — a live refresh would need a configured/explicit URL,
    # though the offline fixture default still works); ``recommended_command`` is the
    # exact worker invocation to run next (no ``--url`` when the config is known).
    known_config: bool = False
    config_status: str | None = None
    needs_url_config: bool = False
    recommended_command: str | None = None


class MarketDataPlanSummary(BaseModel):
    total_items: int = 0
    estimated_requests_by_source: dict[str, int] = {}
    blocked_items: int = 0
    high_priority_items: int = 0
    constituent_count: int = 0
    unresolved_constituents: int = 0
    # Constituent identity-resolution state rollup (see market_data_planner).
    resolved_constituents: int = 0
    ambiguous_constituents: int = 0
    not_found_constituents: int = 0
    # Resolved constituents whose listing has a missing/stale EOD price (the
    # ``constituent_eod_price_ingestion`` backlog == constituent_prices_missing +
    # constituent_prices_stale).
    constituents_ready_for_eod_prices: int = 0
    # Constituent EOD price coverage rollup (resolved listings only).
    constituent_prices_fresh: int = 0
    constituent_prices_missing: int = 0
    constituent_prices_stale: int = 0
    # True look-through readiness (resolved listings only): ``ready`` have a fresh
    # price *and* an FX path to base; the ``blocked_by_*`` counts say what fix
    # unblocks the rest, so the GUI can point at the right job/data gap.
    true_lookthrough_ready: int = 0
    blocked_by_missing_identity: int = 0
    blocked_by_missing_price: int = 0
    blocked_by_missing_fx: int = 0
    # Estimated external request cost, split by the two upcoming work phases.
    estimated_openfigi_requests: int = 0
    estimated_price_requests: int = 0
    stale_prices: int = 0
    missing_prices: int = 0
    missing_fx: int = 0
    # Imported (broker CSV) directly-held instrument readiness. ``unresolved`` have
    # a safe identifier to resolve; ``ambiguous`` / ``manual_review`` need a human;
    # ``ready_for_prices`` are resolved listings missing an EOD price; ``missing_fx``
    # are imported currencies with no path to base. All deduped/bounded.
    imported_unresolved_instruments: int = 0
    imported_ambiguous_instruments: int = 0
    imported_manual_review_instruments: int = 0
    imported_ready_for_prices: int = 0
    imported_missing_fx: int = 0
    # Official/reference rate readiness: supported, relevant currencies (base +
    # held) with no / only stale official rate observations. Collection coverage,
    # NOT curve readiness — the backend never builds curves.
    reference_rate_currencies_missing: int = 0
    reference_rate_currencies_stale: int = 0
    # Portfolio valuation/readiness: 1 when the workspace has a transaction ledger
    # but no / a stale valuation snapshot (so a ``recompute_portfolio_valuation``
    # item is emitted). A local recompute over already-ingested prices/FX — never a
    # fetch, never PnL.
    portfolio_valuation_recompute_needed: int = 0


class MarketDataPlanResponse(BaseModel):
    workspace_id: int
    base_currency: str
    include_constituents: bool
    summary: MarketDataPlanSummary
    items: list[MarketDataPlanItem]
