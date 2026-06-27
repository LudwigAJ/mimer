"""Market-data planner — what to resolve/fetch next, without fetching live.

Given a workspace's current data, produce a *deduped, prioritised* plan of the
work needed to fill gaps for its held funds and (optionally) their constituents:

    held funds
      -> held listings with missing/stale prices       (fetch_listing_price)
      -> currencies with no FX path to base            (fetch_fx_rate)
      -> constituents not yet resolved to an identity  (resolve_constituent_identity)
      -> funds with missing/stale holdings/docs/...     (refresh_*)

This is the gate before pulling EOD prices for ETFs with hundreds of
constituents: it dedupes (Apple held via VUSA *and* JEPG => one identity item),
prioritises (held positions and top-weight constituents first), and reports the
estimated request cost per source — so a future ingestion layer never loops
uncontrolled over every holding. It performs NO network I/O (DB reads only).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.db.models import (
    Distribution,
    Fund,
    FundHolding,
    FundListing,
    InstrumentListing,
    PortfolioPosition,
    PortfolioTransaction,
    PortfolioValuationSnapshot,
    ReferenceRate,
    SecurityIdentifier,
)
from app.schemas.market_data import (
    MarketDataPlanItem,
    MarketDataPlanResponse,
    MarketDataPlanSummary,
)
from app.services import freshness as freshness_service
from app.services import holdings_ingestion as holdings_service
from app.services import workspaces as workspaces_service
from app.services.conversion import normalise_currency
from app.services.fx import MISSING, load_fx_index
from app.sources import issuer_source_config
from app.sources.distributions import known_distribution_source
from app.sources.holdings import known_holdings_source

# Worker job types the recommended-command hints point at (no worker imports here).
_HOLDINGS_JOB = "issuer_holdings_ingestion"
_DISTRIBUTION_JOB = "distribution_ingestion"

# Weight thresholds for constituent prioritisation.
_TOP_WEIGHT = Decimal("0.03")  # >=3% => top constituent (priority 2)
_MIN_WEIGHT = Decimal("0.005")  # >=0.5% => material (priority 3); below = long tail

# Default upper bound on constituent items so the plan stays bounded even when a
# future holdings source returns hundreds of rows per fund.
_MAX_CONSTITUENT_ITEMS = 500

# Bound on the imported-transaction scan so the plan stays bounded even for a
# large broker ledger (items are further deduped by identifier).
_MAX_IMPORTED_TXN_SCAN = 2000

# Imported (broker CSV) transaction statuses the planner inspects. ``manual_review``
# is included so a human-parked row surfaces as a non-urgent manual item; ``ignored``
# is deliberately ABSENT so an ignored row stops emitting any resolve/price/FX work.
_IMPORTED_STATUSES = (
    "unresolved_instrument",
    "ambiguous_instrument",
    "manual_review",
    "resolved",
    "ready",
)

# The (planned) live official-rate adapter per supported currency. The plan costs
# reference-rate gaps against the offline fixture plus the right official source.
_RATES_LIVE_SOURCE = {"EUR": "ecb_rates", "GBP": "boe_rates", "USD": "us_treasury_rates"}


@dataclass
class _Accumulator:
    items: dict[str, MarketDataPlanItem] = field(default_factory=dict)
    constituents: set[str] = field(default_factory=set)
    unresolved: int = 0
    resolved: int = 0
    ambiguous: int = 0
    not_found: int = 0
    ready_for_prices: int = 0
    stale_prices: int = 0
    missing_prices: int = 0
    missing_fx: int = 0
    # Constituent EOD price coverage (resolved listings only).
    constituent_prices_fresh: int = 0
    constituent_prices_missing: int = 0
    constituent_prices_stale: int = 0
    # True look-through readiness (resolved listings only).
    true_lookthrough_ready: int = 0
    blocked_by_missing_price: int = 0
    blocked_by_missing_fx: int = 0
    # Official/reference rate coverage (supported, relevant currencies).
    reference_rate_missing: int = 0
    reference_rate_stale: int = 0
    # Portfolio valuation/readiness recompute backlog (0/1).
    portfolio_valuation_recompute_needed: int = 0

    def add(self, item: MarketDataPlanItem) -> None:
        # Dedupe by plan_key; keep the highest-priority (lowest number) variant.
        existing = self.items.get(item.plan_key)
        if existing is None or item.priority < existing.priority:
            self.items[item.plan_key] = item


def _primary_source(item: MarketDataPlanItem) -> str:
    return item.source_candidates[0] if item.source_candidates else "unknown"


def _holdings_source_candidates(fund: Fund | None, *, default: str) -> list[str]:
    """Holdings source candidates for a fund: the configured default first, then any
    verified live issuer source configured for the fund's ISIN (explicit-only)."""
    candidates = [default]
    live = known_holdings_source(fund.isin if fund else None)
    if live and live not in candidates:
        candidates.append(live)
    return candidates


def _distribution_source_candidates(fund: Fund | None, *, default: str) -> list[str]:
    """Distribution source candidates for a fund: the configured default first, then
    any verified live issuer source configured for the fund's ISIN (explicit-only).

    No distribution URLs are bundled as verified yet, so today this returns just the
    fixture default for every fund; the plumbing mirrors holdings so a verified live
    source can be wired per-ISIN later (see app/sources/distributions.py)."""
    candidates = [default]
    live = known_distribution_source(fund.isin if fund else None)
    if live and live not in candidates:
        candidates.append(live)
    return candidates


def _config_plan_fields(
    fund: Fund | None, fund_id: int | None, *, data_type: str, job_type: str
) -> dict[str, object]:
    """Known-source-config awareness fields for a refresh_* plan item.

    When a usable (verified/candidate) live issuer config is registered for the
    fund, the recommended command runs that live ``--source`` with no ``--url``;
    otherwise the item is flagged ``needs_url_config`` and the recommended action is
    to configure an issuer source URL (the offline fixture default still works)."""
    config = issuer_source_config.find_source_config(
        fund.isin if fund else None, data_type=data_type, usable_only=True
    )
    if config is not None:
        cmd = (
            f"uv run python -m app.workers.run {job_type} "
            f"--fund-id {fund_id} --source {config.source_name}"
        )
        return {
            "known_config": True,
            "config_status": config.source_status,
            "needs_url_config": False,
            "recommended_command": cmd,
        }
    return {
        "known_config": False,
        "config_status": None,
        "needs_url_config": True,
        "recommended_command": (
            f"configure a known issuer source URL (issuer_source_config) or pass --url: "
            f"uv run python -m app.workers.run {job_type} --fund-id {fund_id} "
            f"--source <issuer_{data_type}_source> --url <issuer_download_url>"
        ),
    }


async def _latest_distribution_dates(session: AsyncSession, fund_ids: list[int]) -> dict[int, date]:
    """Newest ex-date per fund across all distribution sources (bounded aggregate)."""
    if not fund_ids:
        return {}
    rows = (
        await session.execute(
            select(Distribution.fund_id, func.max(Distribution.ex_date))
            .where(Distribution.fund_id.in_(fund_ids))
            .group_by(Distribution.fund_id)
        )
    ).all()
    return {fund_id: latest for fund_id, latest in rows if latest is not None}


async def _resolved_identifiers(session: AsyncSession) -> set[str]:
    """ISINs already mapped to an instrument identity (crosswalk or known fund)."""
    ids = set(
        (
            await session.execute(
                select(SecurityIdentifier.value).where(SecurityIdentifier.scheme == "isin")
            )
        )
        .scalars()
        .all()
    )
    fund_isins = set((await session.execute(select(Fund.isin))).scalars().all())
    return {v.upper() for v in ids if v} | {v.upper() for v in fund_isins if v}


def _weight_priority(weight: Decimal, *, base: int) -> int:
    """Bump a constituent item's priority up for heavier weights."""
    if weight >= _TOP_WEIGHT:
        return min(base, 2)
    if weight >= _MIN_WEIGHT:
        return min(base, 3)
    return base


def _constituent_identity_item(
    holding: FundHolding, *, resolved_isins: set[str]
) -> MarketDataPlanItem | None:
    """A plan item describing an *unresolved* constituent's identity state.

    Resolved constituents (already linked to an instrument, or whose ISIN maps to
    a known identity) produce nothing here. Ambiguous / not-found constituents
    produce a *blocked* item (a human must intervene — not another auto-resolve).
    Everything else produces a ``resolve_constituent_identity`` item costed
    against OpenFIGI.
    """
    if holding.holding_instrument_id is not None:
        return None  # resolved (handled by the price-readiness pass)

    isin = (holding.security_isin or "").strip().upper()
    if isin and isin in resolved_isins and holding.identity_status is None:
        return None  # already resolvable to a known identity

    weight = holding.weight or Decimal("0")

    if holding.identity_status == "ambiguous":
        return MarketDataPlanItem(
            item_type="ambiguous_constituent_identity",
            priority=_weight_priority(weight, base=4),
            reason="constituent identity is ambiguous (needs manual disambiguation)",
            plan_key=f"identity:{holding.holding_key}",
            label=holding.security_name,
            related_fund_id=holding.fund_id,
            related_holding_id=holding.id,
            source_candidates=["manual"],
            estimated_requests=0,
            blocked_by="ambiguous",
        )
    if holding.identity_status == "not_found":
        return MarketDataPlanItem(
            item_type="not_found_constituent_identity",
            priority=_weight_priority(weight, base=5),
            reason="constituent identity could not be found by the resolver",
            plan_key=f"identity:{holding.holding_key}",
            label=holding.security_name,
            related_fund_id=holding.fund_id,
            related_holding_id=holding.id,
            source_candidates=["manual"],
            estimated_requests=0,
            blocked_by="not_found",
        )

    # Unresolved (never attempted, or a retryable failure).
    if isin:
        scheme, value, plan_key = "isin", isin, f"identity:isin:{isin}"
        blocked_by = None
    elif holding.holding_key:
        scheme = "ticker" if holding.security_ticker else None
        value = (holding.security_ticker or "").strip().upper() or None
        plan_key = f"identity:{holding.holding_key}"
        blocked_by = None if value else "no_identifier"
    else:
        return None

    return MarketDataPlanItem(
        item_type="resolve_constituent_identity",
        priority=_weight_priority(weight, base=5),
        reason="constituent has no resolved instrument identity",
        plan_key=plan_key,
        label=holding.security_name,
        related_fund_id=holding.fund_id,
        related_holding_id=holding.id,
        identifier_scheme=scheme,
        identifier_value=value,
        source_candidates=["openfigi"],
        estimated_requests=1,
        blocked_by=blocked_by,
    )


def _imported_primary(txn: PortfolioTransaction) -> tuple[str, str] | None:
    """Highest-priority (scheme, value) identifier on an imported transaction.

    Strong identifiers only — a broker-supplied *name* is never identity, so a
    name-only row returns None (manual review, never auto-resolved)."""
    for scheme, value in (("isin", txn.isin), ("figi", txn.figi), ("ticker", txn.symbol)):
        if value and value.strip():
            return scheme, value.strip().upper()
    return None


async def _add_imported_instrument_items(
    session: AsyncSession,
    acc: _Accumulator,
    *,
    workspace_id: int,
    base: str,
    today: date,
    fx_index,  # type: ignore[no-untyped-def]
    price_sources: list[str],
) -> None:
    """Plan items for imported (broker CSV) directly-held instruments (read-only).

    Unresolved transactions with a safe identifier -> ``resolve_imported_instrument``
    (fixture always; OpenFIGI only for ISIN/FIGI); ambiguous -> manual item;
    name-only -> blocked manual_review; resolved listings missing a price ->
    ``fetch_imported_instrument_price``; imported currencies with no FX path ->
    ``fetch_imported_fx_rate``. Deduped by identifier/listing; never resolves."""
    txns = list(
        (
            await session.execute(
                select(PortfolioTransaction)
                .where(
                    PortfolioTransaction.workspace_id == workspace_id,
                    PortfolioTransaction.status.in_(_IMPORTED_STATUSES),
                )
                .order_by(PortfolioTransaction.id)
                .limit(_MAX_IMPORTED_TXN_SCAN)
            )
        )
        .scalars()
        .all()
    )
    if not txns:
        return

    resolved_listing_ids: set[int] = set()
    for txn in txns:
        if txn.status in ("resolved", "ready"):
            if txn.instrument_listing_id is not None:
                resolved_listing_ids.add(txn.instrument_listing_id)
        elif txn.status == "ambiguous_instrument":
            acc.add(
                MarketDataPlanItem(
                    item_type="ambiguous_imported_instrument",
                    priority=4,
                    reason="imported transaction resolved ambiguously (needs manual review)",
                    plan_key=f"imported_identity:ambiguous:{txn.id}",
                    label=txn.symbol or txn.name,
                    source_candidates=["manual"],
                    estimated_requests=0,
                    blocked_by="ambiguous",
                )
            )
        elif txn.status == "manual_review":
            # A human explicitly parked this row for review — surface it as a
            # non-urgent manual item, never an auto-resolve (and never costed).
            acc.add(
                MarketDataPlanItem(
                    item_type="manual_review_imported_instrument",
                    priority=5,
                    reason="imported transaction is flagged for manual review",
                    plan_key=f"imported_identity:manual_review:{txn.id}",
                    label=txn.symbol or txn.name,
                    source_candidates=["manual"],
                    estimated_requests=0,
                    blocked_by="manual_review",
                )
            )
        else:  # unresolved_instrument
            primary = _imported_primary(txn)
            if primary is None:
                acc.add(
                    MarketDataPlanItem(
                        item_type="manual_review_imported_instrument",
                        priority=5,
                        reason="imported transaction is name-only (no safe identifier to resolve)",
                        plan_key=f"imported_identity:manual:{txn.id}",
                        label=txn.name,
                        source_candidates=["manual"],
                        estimated_requests=0,
                        blocked_by="name_only",
                    )
                )
            else:
                scheme, value = primary
                # Fixture can resolve any identifier; OpenFIGI only ISIN/FIGI (a
                # bare imported ticker has no exchange, so it is fixture-only).
                sources = ["constituent_identity_fixture"]
                if scheme in ("isin", "figi"):
                    sources.append("openfigi")
                acc.add(
                    MarketDataPlanItem(
                        item_type="resolve_imported_instrument",
                        priority=3,
                        reason="imported transaction has no resolved instrument identity",
                        plan_key=f"imported_identity:{scheme}:{value}",
                        label=txn.symbol or txn.name,
                        identifier_scheme=scheme,
                        identifier_value=value,
                        source_candidates=sources,
                        estimated_requests=1,
                    )
                )

        # FX coverage for the transaction's traded / cash currency.
        for currency in (txn.cash_currency, txn.currency):
            local = normalise_currency(currency, base)
            if not local or local == base:
                continue
            if fx_index.get_fx_rate(local, base, as_of_date=today).status != MISSING:
                continue
            # Share the fetch with any held-fund FX item (same plan_key); a fresh
            # imported-only currency keeps the imported item type.
            acc.add(
                MarketDataPlanItem(
                    item_type="fetch_imported_fx_rate",
                    priority=2,
                    reason=f"imported {local} holdings have no FX path to {base}",
                    plan_key=f"fx:{local}:{base}",
                    label=f"{local}/{base}",
                    identifier_scheme="currency_pair",
                    identifier_value=f"{local}{base}",
                    source_candidates=[get_settings().fx_source_default, "ecb"],
                    estimated_requests=1,
                )
            )

    # Resolved imported listings missing an EOD price (skip any already covered by
    # a constituent price item — same listing, one fetch).
    if resolved_listing_ids:
        listings = (
            (
                await session.execute(
                    select(InstrumentListing).where(InstrumentListing.id.in_(resolved_listing_ids))
                )
            )
            .scalars()
            .all()
        )
        for listing in sorted(listings, key=lambda ln: ln.id):
            if f"constituent_price:{listing.instrument_id}" in acc.items:
                continue
            state = freshness_service.freshness_state(listing.last_price_at, kind="price")
            if state == freshness_service.FRESH:
                continue
            reason = (
                "imported instrument has no EOD price series yet"
                if state == freshness_service.MISSING
                else "imported instrument EOD price series is stale"
            )
            acc.add(
                MarketDataPlanItem(
                    item_type="fetch_imported_instrument_price",
                    priority=3,
                    reason=reason,
                    plan_key=f"imported_price:listing:{listing.id}",
                    label=listing.ticker,
                    related_instrument_id=listing.instrument_id,
                    identifier_scheme="ticker" if listing.ticker else None,
                    identifier_value=listing.ticker,
                    source_candidates=price_sources,
                    estimated_requests=1,
                )
            )


async def _add_reference_rate_items(
    session: AsyncSession,
    acc: _Accumulator,
    *,
    currencies: set[str],
) -> None:
    """Plan items for missing/stale official reference rates (read-only).

    For each *supported* relevant currency (base + held, intersected with the set
    the service collects official rates for), check the newest observation: none ->
    a ``fetch_reference_rates`` item (missing); aged past the window -> the same
    item (stale). Bounded (<= 3 currencies). Collection coverage only — the plan
    never asks anything to build a curve."""
    from app.sources.rates import SUPPORTED_RATE_CURRENCIES

    relevant = sorted(currencies & set(SUPPORTED_RATE_CURRENCIES))
    for currency in relevant:
        latest = await session.scalar(
            select(func.max(ReferenceRate.rate_date)).where(ReferenceRate.currency == currency)
        )
        state = freshness_service.freshness_state(latest, kind="reference_rate")
        if state == freshness_service.FRESH:
            continue
        missing = state == freshness_service.MISSING
        if missing:
            acc.reference_rate_missing += 1
        else:
            acc.reference_rate_stale += 1
        live = _RATES_LIVE_SOURCE.get(currency)
        sources = ["rates_fixture"] + ([live] if live else [])
        acc.add(
            MarketDataPlanItem(
                item_type="fetch_reference_rates",
                priority=4,
                reason=(
                    f"no official/reference rates collected for {currency}"
                    if missing
                    else f"official/reference rates for {currency} are stale"
                ),
                plan_key=f"reference_rates:{currency}",
                label=currency,
                identifier_scheme="currency",
                identifier_value=currency,
                source_candidates=sources,
                estimated_requests=1,
            )
        )


async def _add_portfolio_valuation_item(
    session: AsyncSession,
    acc: _Accumulator,
    *,
    workspace_id: int,
) -> None:
    """Emit ``recompute_portfolio_valuation`` when the ledger has no / a stale snapshot.

    Read-only + bounded (two scalar queries). It is a *local* recompute over
    already-ingested prices/FX — never a fetch and never PnL — so its estimated
    request cost is 0. We surface it only when there is a transaction ledger to
    value (committed/resolved/unresolved rows) and either no valuation snapshot
    exists yet or the latest one has aged past the freshness window."""
    from app.services.broker_imports import LEDGER_STATUSES

    txn_count = (
        await session.scalar(
            select(func.count())
            .select_from(PortfolioTransaction)
            .where(
                PortfolioTransaction.workspace_id == workspace_id,
                PortfolioTransaction.status.in_(LEDGER_STATUSES),
            )
        )
    ) or 0
    if not txn_count:
        return
    latest = await session.scalar(
        select(PortfolioValuationSnapshot)
        .where(PortfolioValuationSnapshot.workspace_id == workspace_id)
        .order_by(
            PortfolioValuationSnapshot.as_of_date.desc(), PortfolioValuationSnapshot.id.desc()
        )
        .limit(1)
    )
    if latest is not None and (
        freshness_service.freshness_state(latest.created_at) == freshness_service.FRESH
    ):
        return
    missing = latest is None
    acc.portfolio_valuation_recompute_needed = 1
    acc.add(
        MarketDataPlanItem(
            item_type="recompute_portfolio_valuation",
            priority=2,
            reason=(
                "portfolio has a transaction ledger but no valuation/readiness snapshot"
                if missing
                else "portfolio valuation/readiness snapshot is stale"
            ),
            plan_key="portfolio_valuation:recompute",
            label="portfolio valuation",
            source_candidates=["portfolio_valuation"],
            estimated_requests=0,
            recommended_command=(
                f"uv run python -m app.workers.run portfolio_valuation_recompute "
                f"--workspace-id {workspace_id}"
            ),
        )
    )


async def build_plan(
    session: AsyncSession,
    workspace_id: int,
    *,
    include_constituents: bool = True,
    now: datetime | None = None,
) -> MarketDataPlanResponse:
    """Compute the (read-only) market-data plan for a workspace."""
    workspace = await workspaces_service.get_workspace(session, workspace_id)
    base = workspace.base_currency.upper()
    today = now.date() if now else date.today()

    # --- held listings / funds -------------------------------------------------
    positions = list(
        (
            await session.execute(
                select(PortfolioPosition).where(PortfolioPosition.workspace_id == workspace_id)
            )
        )
        .scalars()
        .all()
    )
    listing_ids = sorted({p.fund_listing_id for p in positions})
    listings: list[FundListing] = []
    if listing_ids:
        listings = list(
            (await session.execute(select(FundListing).where(FundListing.id.in_(listing_ids))))
            .scalars()
            .all()
        )
    fund_ids = sorted({ln.fund_id for ln in listings})

    acc = _Accumulator()
    settings = get_settings()
    price_sources = [settings.price_source_default, "yfinance"]
    # Distinct resolved constituents (by holding_key) and the instruments they
    # link to (for the price-readiness pass after the fund loop). The weight map
    # prioritises heavier constituents when planning their EOD price fetch.
    resolved_keys: set[str] = set()
    resolved_instrument_ids: set[int] = set()
    resolved_weight_by_instrument: dict[int, Decimal] = {}

    # --- 1) held listing prices (missing/stale) => highest priority -----------
    for listing in listings:
        state = freshness_service.freshness_state(listing.last_price_at, kind="price")
        if state == freshness_service.FRESH:
            continue
        missing = state == freshness_service.MISSING
        if missing:
            acc.missing_prices += 1
        else:
            acc.stale_prices += 1
        acc.add(
            MarketDataPlanItem(
                item_type="fetch_listing_price",
                priority=1,
                reason="held listing has a missing price"
                if missing
                else "held listing price is stale",
                plan_key=f"price:listing:{listing.id}",
                label=listing.ticker,
                related_fund_id=listing.fund_id,
                related_fund_listing_id=listing.id,
                source_candidates=price_sources,
                estimated_requests=1,
            )
        )

    # --- 2) missing FX for held non-base currencies => highest priority -------
    fx_index = await load_fx_index(session)
    seen_currencies: set[str] = set()
    # Relevant currencies for official/reference-rate coverage (base + held).
    rate_currencies: set[str] = {base}
    for listing in listings:
        local = normalise_currency(listing.currency_unit or listing.trading_currency, base)
        if local:
            rate_currencies.add(local)
        if not local or local == base or local in seen_currencies:
            continue
        seen_currencies.add(local)
        if fx_index.get_fx_rate(local, base, as_of_date=today).status == MISSING:
            acc.missing_fx += 1
            acc.add(
                MarketDataPlanItem(
                    item_type="fetch_fx_rate",
                    priority=1,
                    reason=f"no FX path {local}->{base} for held positions",
                    plan_key=f"fx:{local}:{base}",
                    label=f"{local}/{base}",
                    identifier_scheme="currency_pair",
                    identifier_value=f"{local}{base}",
                    source_candidates=[settings.fx_source_default, "ecb"],
                    estimated_requests=1,
                )
            )

    # --- 3) per-fund refresh gaps + 4) constituent identity resolution --------
    if fund_ids:
        funds = {
            f.id: f
            for f in (await session.execute(select(Fund).where(Fund.id.in_(fund_ids)))).scalars()
        }
        snapshots = await holdings_service.latest_holdings_by_fund(session, fund_ids)
        resolved_isins = await _resolved_identifiers(session) if include_constituents else set()
        distribution_dates = await _latest_distribution_dates(session, fund_ids)

        for fund_id in fund_ids:
            fund = funds.get(fund_id)
            snapshot = snapshots.get(fund_id) or []
            holdings_cfg = _config_plan_fields(
                fund, fund_id, data_type="holdings", job_type=_HOLDINGS_JOB
            )

            # refresh_holdings: missing or stale holdings snapshot.
            if not snapshot:
                acc.add(
                    MarketDataPlanItem(
                        item_type="refresh_holdings",
                        priority=4,
                        reason="fund has no holdings snapshot (look-through blind)",
                        plan_key=f"holdings:{fund_id}",
                        label=fund.name if fund else None,
                        related_fund_id=fund_id,
                        source_candidates=_holdings_source_candidates(
                            fund, default=settings.holdings_source_default
                        ),
                        estimated_requests=1,
                        **holdings_cfg,
                    )
                )
            else:
                as_of = max(h.as_of_date for h in snapshot)
                if (
                    freshness_service.freshness_state(as_of, kind="holdings")
                    == freshness_service.STALE
                ):
                    acc.add(
                        MarketDataPlanItem(
                            item_type="refresh_holdings",
                            priority=4,
                            reason="holdings snapshot is stale",
                            plan_key=f"holdings:{fund_id}",
                            label=fund.name if fund else None,
                            related_fund_id=fund_id,
                            source_candidates=_holdings_source_candidates(
                                fund, default=settings.holdings_source_default
                            ),
                            estimated_requests=1,
                            **holdings_cfg,
                        )
                    )

            # refresh_fund_facts: still on seed/unknown provenance or pending.
            if fund is not None and (fund.status == "pending" or fund.source in (None, "seed")):
                acc.add(
                    MarketDataPlanItem(
                        item_type="refresh_fund_facts",
                        priority=5,
                        reason="fund facts are seed/placeholder provenance",
                        plan_key=f"facts:{fund_id}",
                        label=fund.name,
                        related_fund_id=fund_id,
                        source_candidates=[settings.issuer_facts_source_default],
                        estimated_requests=1,
                    )
                )

            # refresh_distributions: a distributing (or unknown-policy) fund with no
            # distributions collected yet, or whose newest distribution has aged past
            # the freshness window. Accumulating funds pay nothing, so they are skipped
            # (no false "missing distributions"). Collection coverage only — the plan
            # never asks anything to forecast a dividend or project yield.
            if fund is not None and (fund.distribution_policy or "") != "accumulating":
                latest = distribution_dates.get(fund_id)
                state = freshness_service.freshness_state(latest, kind="distribution")
                if state != freshness_service.FRESH:
                    missing = state == freshness_service.MISSING
                    dist_cfg = _config_plan_fields(
                        fund, fund_id, data_type="distributions", job_type=_DISTRIBUTION_JOB
                    )
                    acc.add(
                        MarketDataPlanItem(
                            item_type="refresh_distributions",
                            priority=5,
                            reason=(
                                "fund has no distributions collected yet"
                                if missing
                                else "distribution history is stale"
                            ),
                            plan_key=f"distributions:{fund_id}",
                            label=fund.name,
                            related_fund_id=fund_id,
                            source_candidates=_distribution_source_candidates(
                                fund, default=settings.distribution_source_default
                            ),
                            estimated_requests=1,
                            **dist_cfg,
                        )
                    )

            # constituent identity resolution (deduped across all funds).
            if include_constituents:
                for holding in snapshot:
                    acc.constituents.add(holding.holding_key)
                    if holding.holding_instrument_id is not None:
                        iid = holding.holding_instrument_id
                        resolved_keys.add(holding.holding_key)
                        resolved_instrument_ids.add(iid)
                        weight = holding.weight or Decimal("0")
                        if weight > resolved_weight_by_instrument.get(iid, Decimal("-1")):
                            resolved_weight_by_instrument[iid] = weight
                        continue
                    item = _constituent_identity_item(holding, resolved_isins=resolved_isins)
                    if item is not None:
                        acc.add(item)

    # --- resolved constituents: EOD price coverage ----------------------------
    # A resolved constituent with a tradable listing is the constituent_eod_price
    # ingestion phase. Fresh listings drop out of the plan; missing/stale ones
    # produce a ``fetch_constituent_price`` item (priority bumped by weight).
    if include_constituents and resolved_instrument_ids:
        listings = (
            (
                await session.execute(
                    select(InstrumentListing).where(
                        InstrumentListing.instrument_id.in_(resolved_instrument_ids)
                    )
                )
            )
            .scalars()
            .all()
        )
        primary_listing: dict[int, InstrumentListing] = {}
        for listing in sorted(listings, key=lambda ln: ln.id):
            primary_listing.setdefault(listing.instrument_id, listing)
        for instrument_id, listing in primary_listing.items():
            state = freshness_service.freshness_state(listing.last_price_at, kind="price")
            # FX readiness for the constituent's price currency -> base. A fresh
            # price still cannot be valued in base without an FX path, so it does
            # not count as "true look-through ready".
            local = normalise_currency(listing.currency, base)
            fx_ok = (
                local == base
                or fx_index.get_fx_rate(local, base, as_of_date=today).status != MISSING
            )
            if state == freshness_service.FRESH:
                acc.constituent_prices_fresh += 1
                if fx_ok:
                    acc.true_lookthrough_ready += 1
                else:
                    acc.blocked_by_missing_fx += 1
                continue
            acc.blocked_by_missing_price += 1
            if not fx_ok:
                acc.blocked_by_missing_fx += 1
            if state == freshness_service.MISSING:
                acc.constituent_prices_missing += 1
                reason = "resolved constituent has no EOD price series yet"
            else:
                acc.constituent_prices_stale += 1
                reason = "resolved constituent EOD price series is stale"
            weight = resolved_weight_by_instrument.get(instrument_id, Decimal("0"))
            acc.add(
                MarketDataPlanItem(
                    item_type="fetch_constituent_price",
                    priority=_weight_priority(weight, base=5),
                    reason=reason,
                    plan_key=f"constituent_price:{instrument_id}",
                    label=listing.ticker,
                    related_instrument_id=instrument_id,
                    identifier_scheme="ticker" if listing.ticker else None,
                    identifier_value=listing.ticker,
                    source_candidates=price_sources,
                    estimated_requests=1,
                )
            )

    # --- imported (broker CSV) directly-held instruments ----------------------
    # Read-only: surfaces unresolved/ambiguous imported transactions to resolve
    # and resolved imported listings missing prices/FX. Runs after the constituent
    # pass so a shared instrument's price item is not duplicated.
    await _add_imported_instrument_items(
        session,
        acc,
        workspace_id=workspace_id,
        base=base,
        today=today,
        fx_index=fx_index,
        price_sources=price_sources,
    )

    # --- official / reference rates (collection coverage; no curve building) ---
    await _add_reference_rate_items(session, acc, currencies=rate_currencies)

    # --- portfolio valuation/readiness recompute backlog (local; no fetch) ------
    await _add_portfolio_valuation_item(session, acc, workspace_id=workspace_id)

    # --- bound + order ---------------------------------------------------------
    items = list(acc.items.values())
    # Non-constituent items always kept; constituent identity tail bounded by
    # weight order so the plan stays bounded for hundreds of holdings.
    identity_types = {
        "resolve_constituent_identity",
        "ambiguous_constituent_identity",
        "not_found_constituent_identity",
    }
    constituent_items = [i for i in items if i.item_type in identity_types]
    other_items = [i for i in items if i.item_type not in identity_types]
    constituent_items.sort(key=lambda i: i.priority)
    constituent_items = constituent_items[:_MAX_CONSTITUENT_ITEMS]
    items = other_items + constituent_items
    items.sort(key=lambda i: (i.priority, i.item_type, i.plan_key))

    # Roll up constituent identity state from the (bounded, deduped) item set.
    acc.unresolved = sum(1 for i in items if i.item_type == "resolve_constituent_identity")
    acc.ambiguous = sum(1 for i in items if i.item_type == "ambiguous_constituent_identity")
    acc.not_found = sum(1 for i in items if i.item_type == "not_found_constituent_identity")
    acc.resolved = len(resolved_keys)
    acc.ready_for_prices = sum(1 for i in items if i.item_type == "fetch_constituent_price")

    # Imported (broker CSV) directly-held instrument rollup.
    imported_unresolved = sum(1 for i in items if i.item_type == "resolve_imported_instrument")
    imported_ambiguous = sum(1 for i in items if i.item_type == "ambiguous_imported_instrument")
    imported_manual = sum(1 for i in items if i.item_type == "manual_review_imported_instrument")
    imported_ready = sum(1 for i in items if i.item_type == "fetch_imported_instrument_price")
    imported_fx = sum(1 for i in items if i.item_type == "fetch_imported_fx_rate")

    # --- summary ---------------------------------------------------------------
    by_source: dict[str, int] = {}
    blocked = 0
    high_priority = 0
    for item in items:
        by_source[_primary_source(item)] = (
            by_source.get(_primary_source(item), 0) + item.estimated_requests
        )
        if item.blocked_by:
            blocked += 1
        if item.priority <= 2:
            high_priority += 1

    summary = MarketDataPlanSummary(
        total_items=len(items),
        estimated_requests_by_source=by_source,
        blocked_items=blocked,
        high_priority_items=high_priority,
        constituent_count=len(acc.constituents),
        unresolved_constituents=acc.unresolved,
        resolved_constituents=acc.resolved,
        ambiguous_constituents=acc.ambiguous,
        not_found_constituents=acc.not_found,
        constituents_ready_for_eod_prices=acc.ready_for_prices,
        estimated_openfigi_requests=by_source.get("openfigi", 0),
        estimated_price_requests=acc.ready_for_prices,
        stale_prices=acc.stale_prices,
        missing_prices=acc.missing_prices,
        missing_fx=acc.missing_fx,
        constituent_prices_fresh=acc.constituent_prices_fresh,
        constituent_prices_missing=acc.constituent_prices_missing,
        constituent_prices_stale=acc.constituent_prices_stale,
        true_lookthrough_ready=acc.true_lookthrough_ready,
        blocked_by_missing_identity=acc.unresolved,
        blocked_by_missing_price=acc.blocked_by_missing_price,
        blocked_by_missing_fx=acc.blocked_by_missing_fx,
        imported_unresolved_instruments=imported_unresolved,
        imported_ambiguous_instruments=imported_ambiguous,
        imported_manual_review_instruments=imported_manual,
        imported_ready_for_prices=imported_ready,
        imported_missing_fx=imported_fx,
        reference_rate_currencies_missing=acc.reference_rate_missing,
        reference_rate_currencies_stale=acc.reference_rate_stale,
        portfolio_valuation_recompute_needed=acc.portfolio_valuation_recompute_needed,
    )
    return MarketDataPlanResponse(
        workspace_id=workspace_id,
        base_currency=base,
        include_constituents=include_constituents,
        summary=summary,
        items=items,
    )
