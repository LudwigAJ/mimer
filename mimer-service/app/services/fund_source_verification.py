"""Bounded, safe live verification of a target fund's data sources.

Answers, per *(target fund, data type)*, the operational question the live-readiness
slice exists for: *does this work live right now?* — by running only **bounded, safe**
checks and reporting honestly, without ingesting anything or promoting any source.

For each of ``VUSA`` / ``ISF`` / ``JEPG`` and each data type:

* **facts / nav / documents** — no *live* adapter exists (only offline fixtures), so
  these are reported ``skipped_no_live_source`` with the recorded blocker. No network.
* **listing_price** — one bounded guarded Stooq fetch for the fund's primary listing
  (cache → budget → fetch log → fetch), reusing the instrument-price live adapter so
  the source budget and fetch log apply. Nothing is stored.
* **holdings / distributions** — reuses ``issuer_source_verification`` (one guarded
  fetch + parse of the known issuer config) when a *usable* per-fund config exists;
  otherwise reported ``blocked`` with the recorded blocker (no live attempt).

Hard rules (see AGENTS.md):

* No fixtures are ever called as if they were live.
* Nothing is promoted to ``verified_live`` — promotion stays a deliberate code change
  after a clean live check (this only *reports* the live outcome).
* One provider blocking (binary ``.xls``, TLS handshake, no config) never fails the
  whole run — each cell is isolated.
* If the network is unavailable, the affected cells report ``fetch_error`` /
  ``blocked`` honestly; they never silently fall back to fixtures.

The only side effects are the fetch-log rows ``guarded_fetch`` writes (exactly as a
normal live call). No canonical-table writes.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Fund, FundListing
from app.services import issuer_source_verification as verification_service
from app.sources import fund_source_coverage as fund_coverage
from app.sources import issuer_source_config
from app.sources import source_readiness as readiness
from app.sources.instrument_prices import (
    FAILED as PRICE_FAILED,
)
from app.sources.instrument_prices import (
    NO_DATA as PRICE_NO_DATA,
)
from app.sources.instrument_prices import (
    OK as PRICE_OK,
)
from app.sources.instrument_prices import (
    SKIPPED_BUDGET as PRICE_SKIPPED_BUDGET,
)
from app.sources.instrument_prices import (
    SKIPPED_CACHED as PRICE_SKIPPED_CACHED,
)
from app.sources.instrument_prices import (
    InstrumentPriceRequest,
    get_instrument_price_source,
)

# --- per-cell verification outcomes ------------------------------------------

VERIFIED = "verified"  # a clean live fetch+parse succeeded for this cell
BLOCKED = "blocked"  # a real blocker (binary / TLS / no usable config / planned)
SKIPPED_NO_LIVE_SOURCE = "skipped_no_live_source"  # no live adapter exists (facts/nav/documents)
SKIPPED_BUDGET = "skipped_budget"  # source budget / backoff — no live call made
SKIPPED_CACHED = "skipped_cached"  # recent-success cache — a recent live fetch succeeded
FETCH_ERROR = "fetch_error"  # the live fetch failed (network/HTTP/parse)
FUND_NOT_FOUND = "fund_not_found"  # the fund is not seeded in this database

# Outcomes that count as a successful live confirmation this run.
_OK_OUTCOMES = (VERIFIED,)
# Outcomes that represent a genuine live failure (vs a by-design skip / data blocker).
_FETCH_FAILURE_OUTCOMES = (FETCH_ERROR,)

# Default recent-success TTL for the bounded price probe (15 min): dedupes rapid
# re-runs against the fetch log while still allowing a periodic real check.
_PRICE_TTL_SECONDS = 900


@dataclass
class DataTypeVerification:
    """The bounded live-check outcome for one *(fund, data type)* cell."""

    data_type: str
    source_name: str | None
    coverage_status: str  # the in-code coverage status (verified_live / candidate / ...)
    outcome: str
    ok: bool
    attempted_live: bool
    row_count: int = 0
    known_blocker: str | None = None
    detail: str = ""


@dataclass
class FundVerification:
    """All data-type cell outcomes for one target fund."""

    fund_symbol: str
    isin: str
    issuer: str
    found_in_db: bool
    results: list[DataTypeVerification] = field(default_factory=list)

    def _count(self, outcomes: tuple[str, ...]) -> int:
        return sum(1 for r in self.results if r.outcome in outcomes)

    @property
    def verified_count(self) -> int:
        return self._count(_OK_OUTCOMES)

    @property
    def fetch_error_count(self) -> int:
        return self._count(_FETCH_FAILURE_OUTCOMES)


@dataclass
class FundVerificationReport:
    """The whole bounded verification run (one or more target funds)."""

    funds: list[FundVerification] = field(default_factory=list)

    @property
    def verified_count(self) -> int:
        return sum(f.verified_count for f in self.funds)

    @property
    def fetch_error_count(self) -> int:
        return sum(f.fetch_error_count for f in self.funds)

    @property
    def blocked_count(self) -> int:
        return sum(sum(1 for r in f.results if r.outcome == BLOCKED) for f in self.funds)

    @property
    def attempted_live_count(self) -> int:
        return sum(sum(1 for r in f.results if r.attempted_live) for f in self.funds)

    def message(self) -> str:
        per_fund = "; ".join(
            f.fund_symbol + "[" + " ".join(f"{r.data_type}={r.outcome}" for r in f.results) + "]"
            for f in self.funds
        )
        return (
            f"verify_fund_sources funds={len(self.funds)} "
            f"attempted_live={self.attempted_live_count} verified={self.verified_count} "
            f"blocked={self.blocked_count} fetch_errors={self.fetch_error_count} :: {per_fund}"
        )


# --- per-data-type checks ----------------------------------------------------


def _no_live_source_cell(cell: fund_coverage.FundCoverageRow) -> DataTypeVerification:
    return DataTypeVerification(
        data_type=cell.data_type,
        source_name=cell.source_name,
        coverage_status=cell.status,
        outcome=SKIPPED_NO_LIVE_SOURCE,
        ok=False,
        attempted_live=False,
        known_blocker=cell.known_blocker,
        detail="no live adapter for this data type (offline fixture / planned) — not attempted",
    )


async def _primary_listing(session: AsyncSession, fund_id: int) -> FundListing | None:
    return await session.scalar(
        select(FundListing).where(FundListing.fund_id == fund_id).order_by(FundListing.id).limit(1)
    )


async def _verify_listing_price(
    session: AsyncSession,
    fund_db: Fund,
    cell: fund_coverage.FundCoverageRow,
    *,
    ttl_seconds: int,
) -> DataTypeVerification:
    """One bounded guarded Stooq fetch for the fund's primary listing (no store)."""
    listing = await _primary_listing(session, fund_db.id)
    if listing is None:
        return DataTypeVerification(
            data_type=cell.data_type,
            source_name=cell.source_name,
            coverage_status=cell.status,
            outcome=BLOCKED,
            ok=False,
            attempted_live=False,
            known_blocker="fund has no listing to price",
            detail="no fund_listing row for this fund",
        )

    source = get_instrument_price_source(fund_coverage._LISTING_PRICE_SOURCE)
    request = InstrumentPriceRequest(
        instrument_listing_id=listing.id,
        ticker=listing.ticker,
        exchange=listing.exchange,
        currency=listing.currency_unit or listing.trading_currency,
    )
    result = await source.fetch_eod_prices(session, [request], ttl_seconds=ttl_seconds)
    outcome_code = result.outcomes.get(listing.id)
    records = [r for r in result.records if r.instrument_listing_id == listing.id]

    if outcome_code == PRICE_OK and records:
        return DataTypeVerification(
            data_type=cell.data_type,
            source_name=source.name,
            coverage_status=cell.status,
            outcome=VERIFIED,
            ok=True,
            attempted_live=True,
            row_count=len(records),
            detail=f"live Stooq fetch returned {len(records)} EOD point(s) for "
            f"{listing.ticker} (newest {max(r.price_date for r in records).isoformat()})",
        )
    if outcome_code == PRICE_SKIPPED_BUDGET:
        return DataTypeVerification(
            data_type=cell.data_type,
            source_name=source.name,
            coverage_status=cell.status,
            outcome=SKIPPED_BUDGET,
            ok=False,
            attempted_live=False,
            detail="Stooq is budget-blocked / in backoff — no live call made (clean no-op)",
        )
    if outcome_code == PRICE_SKIPPED_CACHED:
        return DataTypeVerification(
            data_type=cell.data_type,
            source_name=source.name,
            coverage_status=cell.status,
            outcome=SKIPPED_CACHED,
            ok=False,
            attempted_live=False,
            detail="served from the recent-success cache (a recent live Stooq fetch succeeded)",
        )
    if outcome_code == PRICE_NO_DATA:
        return DataTypeVerification(
            data_type=cell.data_type,
            source_name=source.name,
            coverage_status=cell.status,
            outcome=BLOCKED,
            ok=False,
            attempted_live=True,
            known_blocker="Stooq returned no rows for this symbol (symbol mapping is best-effort)",
            detail=f"no EOD rows for {listing.ticker} on {listing.exchange!r}",
        )
    # PRICE_FAILED or unknown -> a genuine live fetch failure (e.g. network down).
    return DataTypeVerification(
        data_type=cell.data_type,
        source_name=source.name,
        coverage_status=cell.status,
        outcome=FETCH_ERROR,
        ok=False,
        attempted_live=True,
        detail=f"Stooq fetch failed for {listing.ticker} (outcome={outcome_code or PRICE_FAILED})",
    )


_COVERAGE_TO_CONFIG_DATA_TYPE = {
    fund_coverage.HOLDINGS: issuer_source_config.DATA_TYPE_HOLDINGS,
    fund_coverage.DISTRIBUTIONS: issuer_source_config.DATA_TYPE_DISTRIBUTIONS,
}


async def _verify_issuer_doc(
    session: AsyncSession,
    fund_db: Fund,
    cell: fund_coverage.FundCoverageRow,
) -> DataTypeVerification:
    """Verify a holdings/distributions cell via the issuer-config verify path."""
    source_name = cell.source_name
    config = (
        issuer_source_config.get_source_config(fund_db.isin, source_name) if source_name else None
    )
    # No usable per-fund config (planned, or no config at all) -> honest blocked, no network.
    if source_name is None or config is None or not config.is_usable:
        return DataTypeVerification(
            data_type=cell.data_type,
            source_name=source_name,
            coverage_status=cell.status,
            outcome=BLOCKED,
            ok=False,
            attempted_live=False,
            known_blocker=cell.known_blocker,
            detail="no usable live source config for this fund — nothing to fetch (not attempted)",
        )

    report = await verification_service.verify_issuer_source_config(
        session,
        isin=fund_db.isin,
        source_name=source_name,
        data_type=_COVERAGE_TO_CONFIG_DATA_TYPE[cell.data_type],
        url=None,
    )
    if report.ok:
        outcome, ok, attempted, blocker = VERIFIED, True, True, None
    elif report.fetch_outcome == verification_service.CACHE_HIT:
        outcome, ok, attempted, blocker = SKIPPED_CACHED, False, False, None
    elif report.fetch_outcome == verification_service.BUDGET_BLOCKED:
        outcome, ok, attempted, blocker = SKIPPED_BUDGET, False, False, None
    else:
        # zero_rows / missing_fields / binary_unsupported / fetch_error / no_url
        outcome = BLOCKED if report.reason != verification_service.R_FETCH_ERROR else FETCH_ERROR
        ok, attempted, blocker = False, report.attempted, cell.known_blocker
    return DataTypeVerification(
        data_type=cell.data_type,
        source_name=source_name,
        coverage_status=cell.status,
        outcome=outcome,
        ok=ok,
        attempted_live=attempted,
        row_count=report.row_count,
        known_blocker=blocker,
        detail=report.detail or report.message(),
    )


# --- orchestration -----------------------------------------------------------


async def _verify_one_fund(
    session: AsyncSession,
    target: fund_coverage.TargetFund,
    *,
    ttl_seconds: int,
) -> FundVerification:
    fund_db = await session.scalar(select(Fund).where(Fund.isin == target.isin))
    fv = FundVerification(
        fund_symbol=target.symbol,
        isin=target.isin,
        issuer=target.issuer,
        found_in_db=fund_db is not None,
    )
    cells = fund_coverage.coverage_for_fund(target)
    for cell in cells:
        # Each cell is isolated: a crash in one provider never aborts the fund/run.
        try:
            if cell.data_type in (
                fund_coverage.FACTS,
                fund_coverage.NAV,
                fund_coverage.DOCUMENTS,
            ):
                fv.results.append(_no_live_source_cell(cell))
            elif fund_db is None:
                fv.results.append(
                    DataTypeVerification(
                        data_type=cell.data_type,
                        source_name=cell.source_name,
                        coverage_status=cell.status,
                        outcome=FUND_NOT_FOUND,
                        ok=False,
                        attempted_live=False,
                        detail="fund is not seeded in this database — cannot verify live",
                    )
                )
            elif cell.data_type == fund_coverage.LISTING_PRICE:
                fv.results.append(
                    await _verify_listing_price(session, fund_db, cell, ttl_seconds=ttl_seconds)
                )
            elif cell.data_type in (fund_coverage.HOLDINGS, fund_coverage.DISTRIBUTIONS):
                fv.results.append(await _verify_issuer_doc(session, fund_db, cell))
        except Exception as exc:  # noqa: BLE001 - never let one cell abort the run
            fv.results.append(
                DataTypeVerification(
                    data_type=cell.data_type,
                    source_name=cell.source_name,
                    coverage_status=cell.status,
                    outcome=FETCH_ERROR,
                    ok=False,
                    attempted_live=True,
                    detail=f"verifier raised: {type(exc).__name__}: {exc}",
                )
            )
    return fv


async def verify_fund_sources(
    session: AsyncSession,
    *,
    fund_symbol: str | None = None,
    all_target_funds: bool = False,
    limit: int | None = None,
    ttl_seconds: int = _PRICE_TTL_SECONDS,
) -> FundVerificationReport:
    """Run bounded, safe live checks for one target fund or all of them.

    Exactly one of ``fund_symbol`` / ``all_target_funds`` selects the scope. ``limit``
    bounds the number of funds verified (a guard for the ``--all-target-funds`` path).
    Returns a report; never ingests, never promotes, never raises for a blocked
    provider. Unknown ``fund_symbol`` yields an empty report (the worker surfaces it).
    """
    if fund_symbol is not None:
        target = fund_coverage.get_target_fund(fund_symbol)
        targets = [target] if target is not None else []
    elif all_target_funds:
        targets = list(fund_coverage.TARGET_FUNDS)
    else:
        targets = list(fund_coverage.TARGET_FUNDS)

    if limit is not None and limit >= 0:
        targets = targets[:limit]

    report = FundVerificationReport()
    for target in targets:
        report.funds.append(await _verify_one_fund(session, target, ttl_seconds=ttl_seconds))
    return report


# Re-exported so callers/tests can reference the readiness vocabulary without a
# second import (the coverage statuses on each cell come from there).
__all__ = [
    "VERIFIED",
    "BLOCKED",
    "SKIPPED_NO_LIVE_SOURCE",
    "SKIPPED_BUDGET",
    "SKIPPED_CACHED",
    "FETCH_ERROR",
    "FUND_NOT_FOUND",
    "DataTypeVerification",
    "FundVerification",
    "FundVerificationReport",
    "verify_fund_sources",
    "readiness",
]
