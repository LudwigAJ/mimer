"""Alert rule engine — pure, deterministic rules over a prepared context.

The rules here take a fully-loaded `AlertContext` (built by
`app.services.alert_generation`) and return `AlertCandidate` values. They do *no*
database or network I/O, which keeps them trivially unit-testable: hand-build a
context and assert on the candidates.

Design (deliberately small — not a DSL):

* `AlertCandidate` — what a rule wants to assert (severity/category/message + a
  stable ``dedupe_key`` that identifies the underlying issue).
* `AlertRule` — a named callable ``evaluate(ctx) -> list[AlertCandidate]``.
* `RULES` — the ordered registry the worker evaluates.

Thresholds live here as module constants so they are centralised and easy to
tune. ``dedupe_key`` is the contract with the persistence layer
(`app.services.alert_generation`): same key => same issue => upsert, not insert.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # avoid an import cycle (alert_generation imports this module)
    from app.services.alert_generation import AlertContext

# --- vocabulary --------------------------------------------------------------

# Severities (ascending order of urgency; used for sorting/highest-severity).
INFO = "info"
WARNING = "warning"
ERROR = "error"
CRITICAL = "critical"
SEVERITIES = (INFO, WARNING, ERROR, CRITICAL)
SEVERITY_RANK: dict[str, int] = {s: i for i, s in enumerate(SEVERITIES)}

# Categories.
CAT_DOCUMENT = "document"
CAT_PRICE = "price"
CAT_FX = "fx"
CAT_HOLDINGS = "holdings"
CAT_DISTRIBUTION = "distribution"
CAT_JOB = "job"
CAT_INSTRUMENT = "instrument"
CAT_SOURCE = "source"
CAT_EXPOSURE = "exposure"
CAT_DATA_QUALITY = "data_quality"
CAT_SYSTEM = "system"
CATEGORIES = (
    CAT_DOCUMENT,
    CAT_PRICE,
    CAT_FX,
    CAT_HOLDINGS,
    CAT_DISTRIBUTION,
    CAT_JOB,
    CAT_INSTRUMENT,
    CAT_SOURCE,
    CAT_EXPOSURE,
    CAT_DATA_QUALITY,
    CAT_SYSTEM,
)

# Statuses (persisted on the alert row).
STATUS_ACTIVE = "active"
STATUS_READ = "read"
STATUS_DISMISSED = "dismissed"
STATUS_RESOLVED = "resolved"
STATUSES = (STATUS_ACTIVE, STATUS_READ, STATUS_DISMISSED, STATUS_RESOLVED)

ALERT_SOURCE = "alert_generation"
KEY_DOCUMENT_TYPES = ("factsheet", "kid", "kiid", "prospectus")

# --- thresholds (centralised; tune here) -------------------------------------

PRICE_STALE_DAYS = 5
FX_STALE_DAYS = 5  # informational; FX freshness is derived via the FxIndex
HOLDINGS_STALE_DAYS = 45
DOCUMENT_STALE_DAYS = 400
FAILED_JOB_LOOKBACK_DAYS = 7
# Exposure freshness / quality thresholds (shared by diagnostics + alerts).
EXPOSURE_STALE_DAYS = 7
EXPOSURE_MIN_COVERAGE = Decimal("0.80")
# True constituent look-through coverage thresholds. Deliberately measured *of the
# looked-through holdings weight* (identity / price as a fraction of holdings
# coverage), so a fund whose holdings simply aren't disclosed yet is not punished
# twice — that is the low-exposure-coverage signal's job.
CONSTITUENT_MIN_IDENTITY_COVERAGE = Decimal("0.80")
CONSTITUENT_MIN_PRICE_COVERAGE = Decimal("0.80")
# Exposure drift thresholds (latest vs previous snapshot). ``total_abs_*_delta``
# sums the absolute look-through weight that moved across a dimension's buckets;
# ~0.20 (20%) is a deliberately conservative "materially different" bar so clean
# seed data and tiny rebalances stay quiet. Coverage deterioration fires when a
# coverage fraction drops by more than ``COVERAGE_DETERIORATION``.
EXPOSURE_DRIFT_WEIGHT_THRESHOLD = Decimal("0.20")
COVERAGE_DETERIORATION = Decimal("0.10")

# dedupe_key prefixes whose alerts auto-resolve when no longer generated. The
# one-time informational alerts (a *new* document / distribution) are recorded
# once and left for the user to dismiss — they are NOT auto-resolved.
_AUTO_RESOLVABLE_PREFIXES = frozenset(
    {
        "document_changed",
        "document_missing",
        "price_stale",
        "price_missing",
        "fx_missing",
        "fx_stale",
        "holdings_missing",
        "holdings_stale",
        "job_failed",
        "instrument_pending",
        "instrument_ambiguous",
        "constituent_ambiguous",
        "source_conflict",
        "exposure_stale",
        "exposure_low_coverage",
        "exposure_recompute_failed",
        "constituent_identity_coverage_low",
        "constituent_price_coverage_low",
        "constituent_valuation_fx_missing",
        "exposure_drift_constituent",
        "exposure_drift_sector",
        "constituent_price_coverage_deteriorated",
        "constituent_fx_coverage_deteriorated",
        "broker_import_failed_rows",
        "broker_import_unresolved",
        "broker_import_ambiguous",
    }
)


def is_auto_resolvable(dedupe_key: str) -> bool:
    """Whether an alert with this key should auto-resolve when its issue is gone."""
    return dedupe_key.split(":", 1)[0] in _AUTO_RESOLVABLE_PREFIXES


# --- candidate ---------------------------------------------------------------


@dataclass(frozen=True)
class AlertCandidate:
    """An asserted issue, ready to be upserted into the ``alerts`` table."""

    workspace_id: int
    severity: str
    category: str
    title: str
    message: str
    dedupe_key: str
    source: str = ALERT_SOURCE
    related_entity_type: str | None = None
    related_entity_id: str | None = None
    related_fund_id: int | None = None
    related_fund_listing_id: int | None = None
    related_document_snapshot_id: int | None = None
    related_job_run_id: int | None = None
    raw_payload_json: dict[str, Any] | None = None


# --- helpers -----------------------------------------------------------------


def _age_days(value: date | datetime | None, now: datetime) -> int | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        moment = value if value.tzinfo else value.replace(tzinfo=UTC)
        return (now - moment).days
    return (now.date() - value).days


def _fund_label(ctx: AlertContext, fund_id: int) -> str:
    fund = ctx.fund_by_id.get(fund_id)
    if fund is None:
        return f"fund {fund_id}"
    return fund.name or fund.isin or f"fund {fund_id}"


def _listing_label(ctx: AlertContext, listing_id: int) -> str:
    listing = ctx.listing_by_id.get(listing_id)
    return listing.ticker if listing is not None else f"listing {listing_id}"


# --- rules -------------------------------------------------------------------


def rule_document_changed(ctx: AlertContext) -> list[AlertCandidate]:
    out: list[AlertCandidate] = []
    for (fund_id, doc_type), snap in ctx.latest_documents.items():
        if snap.change_status != "changed":
            continue
        label = _fund_label(ctx, fund_id)
        out.append(
            AlertCandidate(
                workspace_id=ctx.workspace_id,
                severity=WARNING,
                category=CAT_DOCUMENT,
                title=f"{doc_type.title()} changed for {label}",
                message=(
                    f"The latest {doc_type} snapshot for {label} differs from the "
                    "previous version (content hash changed)."
                ),
                dedupe_key=f"document_changed:{ctx.workspace_id}:{fund_id}:"
                f"{doc_type}:{snap.content_hash}",
                related_entity_type="document_snapshot",
                related_entity_id=str(snap.id),
                related_fund_id=fund_id,
                related_document_snapshot_id=snap.id,
                raw_payload_json={
                    "document_type": doc_type,
                    "content_hash": snap.content_hash,
                    "previous_content_hash": snap.previous_content_hash,
                },
            )
        )
    return out


def rule_document_new(ctx: AlertContext) -> list[AlertCandidate]:
    out: list[AlertCandidate] = []
    for (fund_id, doc_type), snap in ctx.latest_documents.items():
        if snap.change_status != "new":
            continue
        label = _fund_label(ctx, fund_id)
        out.append(
            AlertCandidate(
                workspace_id=ctx.workspace_id,
                severity=INFO,
                category=CAT_DOCUMENT,
                title=f"New {doc_type} for {label}",
                message=f"A {doc_type} for {label} is now tracked.",
                dedupe_key=f"document_new:{ctx.workspace_id}:{fund_id}:"
                f"{doc_type}:{snap.content_hash}",
                related_entity_type="document_snapshot",
                related_entity_id=str(snap.id),
                related_fund_id=fund_id,
                related_document_snapshot_id=snap.id,
            )
        )
    return out


def rule_document_missing(ctx: AlertContext) -> list[AlertCandidate]:
    out: list[AlertCandidate] = []
    key_types = set(KEY_DOCUMENT_TYPES)
    for fund in ctx.funds:
        present = ctx.doc_types_by_fund.get(fund.id, set())
        if present.isdisjoint(key_types):
            label = _fund_label(ctx, fund.id)
            out.append(
                AlertCandidate(
                    workspace_id=ctx.workspace_id,
                    severity=WARNING,
                    category=CAT_DOCUMENT,
                    title=f"Missing key documents for {label}",
                    message=(
                        f"{label} has none of the key document types "
                        f"({', '.join(KEY_DOCUMENT_TYPES)})."
                    ),
                    dedupe_key=f"document_missing:{ctx.workspace_id}:{fund.id}",
                    related_entity_type="fund",
                    related_entity_id=str(fund.id),
                    related_fund_id=fund.id,
                )
            )
    return out


def rule_failed_jobs(ctx: AlertContext) -> list[AlertCandidate]:
    out: list[AlertCandidate] = []
    for run in ctx.failed_job_runs:
        partial = run.status == "partial_success"
        severity = WARNING if partial else ERROR
        kind = "had partial failures" if partial else "failed"
        detail = f": {run.message}" if run.message else "."
        out.append(
            AlertCandidate(
                workspace_id=ctx.workspace_id,
                severity=severity,
                category=CAT_JOB,
                title=f"Job {run.job_type} {kind}",
                message=f"Run #{run.id} of {run.job_type} {kind}{detail}",
                dedupe_key=f"job_failed:{ctx.workspace_id}:{run.id}",
                related_entity_type="job_run",
                related_entity_id=str(run.id),
                related_fund_id=run.fund_id,
                related_fund_listing_id=run.fund_listing_id,
                related_job_run_id=run.id,
                raw_payload_json={"job_type": run.job_type, "status": run.status},
            )
        )
    return out


def rule_stale_prices(ctx: AlertContext) -> list[AlertCandidate]:
    out: list[AlertCandidate] = []
    for listing in ctx.listings:
        price_date = ctx.latest_price_date.get(listing.id)
        if price_date is None:
            continue  # missing handled by rule_missing_prices
        age = _age_days(price_date, ctx.now)
        if age is not None and age > PRICE_STALE_DAYS:
            label = _listing_label(ctx, listing.id)
            out.append(
                AlertCandidate(
                    workspace_id=ctx.workspace_id,
                    severity=WARNING,
                    category=CAT_PRICE,
                    title=f"Stale price for {label}",
                    message=(
                        f"The latest price for {label} is {age} days old "
                        f"(>{PRICE_STALE_DAYS}d); last priced {price_date.isoformat()}."
                    ),
                    dedupe_key=f"price_stale:{ctx.workspace_id}:{listing.id}",
                    related_entity_type="fund_listing",
                    related_entity_id=str(listing.id),
                    related_fund_id=listing.fund_id,
                    related_fund_listing_id=listing.id,
                    raw_payload_json={"age_days": age, "price_date": price_date.isoformat()},
                )
            )
    return out


def rule_missing_prices(ctx: AlertContext) -> list[AlertCandidate]:
    out: list[AlertCandidate] = []
    for listing in ctx.listings:
        if listing.id in ctx.listings_with_price:
            continue
        label = _listing_label(ctx, listing.id)
        out.append(
            AlertCandidate(
                workspace_id=ctx.workspace_id,
                severity=ERROR,
                category=CAT_PRICE,
                title=f"No price for {label}",
                message=f"No price has been ingested for held listing {label}.",
                dedupe_key=f"price_missing:{ctx.workspace_id}:{listing.id}",
                related_entity_type="fund_listing",
                related_entity_id=str(listing.id),
                related_fund_id=listing.fund_id,
                related_fund_listing_id=listing.id,
            )
        )
    return out


def rule_fx(ctx: AlertContext) -> list[AlertCandidate]:
    """Missing / stale FX for non-base-currency holdings (one alert per currency)."""
    from app.services.fx import MISSING, STALE

    out: list[AlertCandidate] = []
    base = ctx.base_currency.upper()
    for currency in sorted(ctx.position_currencies):
        if currency == base:
            continue
        result = ctx.fx_index.get_fx_rate(currency, base)
        if result.status == MISSING:
            out.append(
                AlertCandidate(
                    workspace_id=ctx.workspace_id,
                    severity=WARNING,
                    category=CAT_FX,
                    title=f"Missing FX rate {currency}->{base}",
                    message=(
                        f"No usable FX path from {currency} to {base}; "
                        f"{currency} holdings cannot be valued in {base}."
                    ),
                    dedupe_key=f"fx_missing:{ctx.workspace_id}:{currency}:{base}",
                    related_entity_type="currency",
                    related_entity_id=currency,
                )
            )
        elif result.status == STALE:
            as_of = result.rate_date.isoformat() if result.rate_date else "unknown"
            out.append(
                AlertCandidate(
                    workspace_id=ctx.workspace_id,
                    severity=WARNING,
                    category=CAT_FX,
                    title=f"Stale FX rate {currency}->{base}",
                    message=(f"The FX rate from {currency} to {base} is stale (as of {as_of})."),
                    dedupe_key=f"fx_stale:{ctx.workspace_id}:{currency}:{base}",
                    related_entity_type="currency",
                    related_entity_id=currency,
                    raw_payload_json={
                        "rate_date": result.rate_date.isoformat() if result.rate_date else None,
                        "source": result.source,
                    },
                )
            )
    return out


def rule_holdings(ctx: AlertContext) -> list[AlertCandidate]:
    out: list[AlertCandidate] = []
    for fund in ctx.funds:
        label = _fund_label(ctx, fund.id)
        as_of = ctx.holdings_as_of.get(fund.id)
        if as_of is None:
            out.append(
                AlertCandidate(
                    workspace_id=ctx.workspace_id,
                    severity=WARNING,
                    category=CAT_HOLDINGS,
                    title=f"No holdings for {label}",
                    message=f"No holdings snapshot has been ingested for {label}.",
                    dedupe_key=f"holdings_missing:{ctx.workspace_id}:{fund.id}",
                    related_entity_type="fund",
                    related_entity_id=str(fund.id),
                    related_fund_id=fund.id,
                )
            )
            continue
        age = _age_days(as_of, ctx.now)
        if age is not None and age > HOLDINGS_STALE_DAYS:
            out.append(
                AlertCandidate(
                    workspace_id=ctx.workspace_id,
                    severity=WARNING,
                    category=CAT_HOLDINGS,
                    title=f"Stale holdings for {label}",
                    message=(
                        f"The latest holdings snapshot for {label} is {age} days old "
                        f"(>{HOLDINGS_STALE_DAYS}d); as of {as_of.isoformat()}."
                    ),
                    dedupe_key=f"holdings_stale:{ctx.workspace_id}:{fund.id}",
                    related_entity_type="fund",
                    related_entity_id=str(fund.id),
                    related_fund_id=fund.id,
                    raw_payload_json={"age_days": age, "as_of": as_of.isoformat()},
                )
            )
    return out


def rule_instruments(ctx: AlertContext) -> list[AlertCandidate]:
    out: list[AlertCandidate] = []
    for fund in ctx.pending_funds:
        label = _fund_label(ctx, fund.id)
        out.append(
            AlertCandidate(
                workspace_id=ctx.workspace_id,
                severity=INFO,
                category=CAT_INSTRUMENT,
                title=f"Instrument pending for {label}",
                message=f"{label} is resolved but still awaiting data backfill.",
                dedupe_key=f"instrument_pending:{ctx.workspace_id}:{fund.id}",
                related_entity_type="fund",
                related_entity_id=str(fund.id),
                related_fund_id=fund.id,
            )
        )
    for listing in ctx.pending_listings:
        label = _listing_label(ctx, listing.id)
        out.append(
            AlertCandidate(
                workspace_id=ctx.workspace_id,
                severity=INFO,
                category=CAT_INSTRUMENT,
                title=f"Listing pending for {label}",
                message=f"Listing {label} is resolved but still awaiting data backfill.",
                dedupe_key=f"instrument_pending:{ctx.workspace_id}:listing:{listing.id}",
                related_entity_type="fund_listing",
                related_entity_id=str(listing.id),
                related_fund_id=listing.fund_id,
                related_fund_listing_id=listing.id,
            )
        )
    for ident in ctx.ambiguous_identifiers:
        fund_id = ident.fund_id
        label = _fund_label(ctx, fund_id) if fund_id else ident.value
        out.append(
            AlertCandidate(
                workspace_id=ctx.workspace_id,
                severity=WARNING,
                category=CAT_INSTRUMENT,
                title=f"Ambiguous identifier for {label}",
                message=(
                    f"Identifier {ident.scheme}:{ident.value} for {label} resolved at "
                    f"{ident.confidence} confidence (source {ident.source})."
                ),
                dedupe_key=f"instrument_ambiguous:{ctx.workspace_id}:{ident.id}",
                related_entity_type="identifier",
                related_entity_id=str(ident.id),
                related_fund_id=fund_id,
                related_fund_listing_id=ident.fund_listing_id,
            )
        )
    return out


def rule_constituent_identity(ctx: AlertContext) -> list[AlertCandidate]:
    """Ambiguous constituent identities (a human must disambiguate).

    Deliberately narrow: only *ambiguous* constituents alert. Plain unresolved
    constituents are the normal pre-resolution state (surfaced by diagnostics /
    the market-data plan, not an alert), and resolution *failures* are covered by
    the generic failed-jobs rule — so a clean workspace stays quiet.
    """
    out: list[AlertCandidate] = []
    for holding in ctx.ambiguous_constituents:
        label = _fund_label(ctx, holding.fund_id)
        out.append(
            AlertCandidate(
                workspace_id=ctx.workspace_id,
                severity=WARNING,
                category=CAT_INSTRUMENT,
                title=f"Ambiguous constituent identity in {label}",
                message=(
                    f"Constituent '{holding.security_name}' in {label} resolved "
                    "ambiguously; it needs manual disambiguation before pricing."
                ),
                dedupe_key=f"constituent_ambiguous:{ctx.workspace_id}:{holding.id}",
                related_entity_type="fund_holding",
                related_entity_id=str(holding.id),
                related_fund_id=holding.fund_id,
                raw_payload_json={"holding_key": holding.holding_key},
            )
        )
    return out


def rule_source_conflicts(ctx: AlertContext) -> list[AlertCandidate]:
    out: list[AlertCandidate] = []
    for listing_id in ctx.source_conflict_listings:
        label = _listing_label(ctx, listing_id)
        listing = ctx.listing_by_id.get(listing_id)
        out.append(
            AlertCandidate(
                workspace_id=ctx.workspace_id,
                severity=INFO,
                category=CAT_SOURCE,
                title=f"Conflicting price sources for {label}",
                message=(
                    f"{label} has prices from multiple sources for the same date; "
                    "review which source to trust."
                ),
                dedupe_key=f"source_conflict:{ctx.workspace_id}:{listing_id}",
                related_entity_type="fund_listing",
                related_entity_id=str(listing_id),
                related_fund_id=listing.fund_id if listing else None,
                related_fund_listing_id=listing_id,
            )
        )
    return out


def rule_upcoming_distributions(ctx: AlertContext) -> list[AlertCandidate]:
    out: list[AlertCandidate] = []
    for dist in ctx.upcoming_distributions:
        label = _fund_label(ctx, dist.fund_id)
        out.append(
            AlertCandidate(
                workspace_id=ctx.workspace_id,
                severity=INFO,
                category=CAT_DISTRIBUTION,
                title=f"Upcoming distribution for {label}",
                message=(
                    f"{label} has a declared distribution of {dist.amount} {dist.currency} "
                    f"with ex-date {dist.ex_date.isoformat()}."
                ),
                dedupe_key=f"distribution_new:{ctx.workspace_id}:{dist.fund_id}:"
                f"{dist.ex_date.isoformat()}",
                related_entity_type="distribution",
                related_entity_id=str(dist.id),
                related_fund_id=dist.fund_id,
            )
        )
    return out


def rule_exposure(ctx: AlertContext) -> list[AlertCandidate]:
    """Exposure freshness/quality alerts (only when a snapshot or failure exists).

    Deliberately conservative: a workspace that has simply never run
    ``exposure_recompute`` is surfaced by diagnostics (``missing_exposure_
    snapshots``), not an alert, so a fresh workspace stays quiet.
    """
    out: list[AlertCandidate] = []
    snap = ctx.latest_exposure_snapshot
    if snap is not None:
        age = _age_days(snap.created_at, ctx.now)
        if age is not None and age > EXPOSURE_STALE_DAYS:
            out.append(
                AlertCandidate(
                    workspace_id=ctx.workspace_id,
                    severity=INFO,
                    category=CAT_EXPOSURE,
                    title="Exposure snapshot is stale",
                    message=(
                        f"The latest exposure snapshot is {age} days old "
                        f"(>{EXPOSURE_STALE_DAYS}d); re-run exposure_recompute."
                    ),
                    dedupe_key=f"exposure_stale:{ctx.workspace_id}",
                    related_entity_type="exposure_snapshot",
                    related_entity_id=str(snap.id),
                    raw_payload_json={"age_days": age, "as_of": snap.as_of_date.isoformat()},
                )
            )
        coverage = snap.coverage_weight
        if coverage is not None and coverage < EXPOSURE_MIN_COVERAGE:
            pct = (coverage * Decimal(100)).quantize(Decimal("0.1"))
            out.append(
                AlertCandidate(
                    workspace_id=ctx.workspace_id,
                    severity=INFO,
                    category=CAT_EXPOSURE,
                    title="Low exposure coverage",
                    message=(
                        f"Only {pct}% of portfolio value has look-through holdings "
                        f"(<{(EXPOSURE_MIN_COVERAGE * 100):.0f}%)."
                    ),
                    dedupe_key=f"exposure_low_coverage:{ctx.workspace_id}",
                    related_entity_type="exposure_snapshot",
                    related_entity_id=str(snap.id),
                    raw_payload_json={"coverage_weight": str(coverage)},
                )
            )
    for run in ctx.exposure_recompute_failures:
        out.append(
            AlertCandidate(
                workspace_id=ctx.workspace_id,
                severity=ERROR,
                category=CAT_EXPOSURE,
                title="Exposure recompute failed",
                message=f"Run #{run.id} of exposure_recompute failed.",
                dedupe_key=f"exposure_recompute_failed:{ctx.workspace_id}:{run.id}",
                related_entity_type="job_run",
                related_entity_id=str(run.id),
                related_job_run_id=run.id,
            )
        )
    return out


def rule_constituent_valuation(ctx: AlertContext) -> list[AlertCandidate]:
    """Conservative true look-through valuation alerts (one per workspace each).

    Deliberately quiet about the *normal* pre-resolution state: nothing fires
    until some constituents have actually resolved (``identity_coverage_weight``
    > 0). Then it flags, grouped per workspace (never per small holding):

    * low constituent *identity* coverage — much of the looked-through weight has
      no resolved instrument, so the look-through is still wrapper-level;
    * low constituent *price* coverage — resolved, but missing constituent EOD
      prices, so contribution/performance context is thin;
    * constituent valuation *FX missing* — priced constituents whose price
      currency has no path to base (cannot be valued in base).
    """
    snap = ctx.latest_exposure_snapshot
    if snap is None or snap.status == "empty":
        return []
    out: list[AlertCandidate] = []
    holdings_cov = snap.coverage_weight or Decimal("0")
    identity_cov = snap.identity_coverage_weight or Decimal("0")
    price_cov = snap.price_coverage_weight or Decimal("0")
    if identity_cov > 0:
        if holdings_cov > 0 and identity_cov / holdings_cov < CONSTITUENT_MIN_IDENTITY_COVERAGE:
            pct = ((identity_cov / holdings_cov) * Decimal(100)).quantize(Decimal("0.1"))
            out.append(
                AlertCandidate(
                    workspace_id=ctx.workspace_id,
                    severity=INFO,
                    category=CAT_EXPOSURE,
                    title="Low constituent identity coverage",
                    message=(
                        f"Only {pct}% of looked-through holdings resolve to a constituent "
                        f"instrument (<{(CONSTITUENT_MIN_IDENTITY_COVERAGE * 100):.0f}%); "
                        "run constituent_identity_resolution to deepen look-through."
                    ),
                    dedupe_key=f"constituent_identity_coverage_low:{ctx.workspace_id}",
                    related_entity_type="exposure_snapshot",
                    related_entity_id=str(snap.id),
                    raw_payload_json={"identity_coverage_of_holdings": str(pct)},
                )
            )
        if price_cov / identity_cov < CONSTITUENT_MIN_PRICE_COVERAGE:
            pct = ((price_cov / identity_cov) * Decimal(100)).quantize(Decimal("0.1"))
            out.append(
                AlertCandidate(
                    workspace_id=ctx.workspace_id,
                    severity=INFO,
                    category=CAT_EXPOSURE,
                    title="Low constituent price coverage",
                    message=(
                        f"Only {pct}% of resolved constituent weight has a usable EOD price "
                        f"(<{(CONSTITUENT_MIN_PRICE_COVERAGE * 100):.0f}%); run "
                        "constituent_eod_price_ingestion."
                    ),
                    dedupe_key=f"constituent_price_coverage_low:{ctx.workspace_id}",
                    related_entity_type="exposure_snapshot",
                    related_entity_id=str(snap.id),
                    raw_payload_json={"price_coverage_of_identity": str(pct)},
                )
            )
    if (snap.constituent_fx_missing_count or 0) > 0:
        out.append(
            AlertCandidate(
                workspace_id=ctx.workspace_id,
                severity=INFO,
                category=CAT_FX,
                title="Constituent valuation FX missing",
                message=(
                    f"{snap.constituent_fx_missing_count} priced constituent(s) have no FX path "
                    "to the base currency, so their look-through value is approximate."
                ),
                dedupe_key=f"constituent_valuation_fx_missing:{ctx.workspace_id}",
                related_entity_type="exposure_snapshot",
                related_entity_id=str(snap.id),
                raw_payload_json={"fx_missing_count": snap.constituent_fx_missing_count},
            )
        )
    return out


def rule_exposure_drift(ctx: AlertContext) -> list[AlertCandidate]:
    """Conservative exposure-drift alerts (latest vs previous snapshot).

    Compares snapshots only — never claims a trade or PnL (see AGENTS.md). Stays
    silent unless there is a prior snapshot *and* the move clears the threshold,
    and is grouped per workspace/dimension (never one alert per small holding).
    Auto-resolves when the drift falls back below threshold.
    """
    drift = ctx.exposure_drift
    if drift is None or not drift.has_prior:
        return []
    out: list[AlertCandidate] = []
    payload = {
        "base_snapshot_id": drift.base_snapshot_id,
        "comparison_snapshot_id": drift.comparison_snapshot_id,
    }
    constituent = drift.constituent_weight_delta or Decimal("0")
    if constituent >= EXPOSURE_DRIFT_WEIGHT_THRESHOLD:
        pct = (constituent * Decimal(100)).quantize(Decimal("0.1"))
        out.append(
            AlertCandidate(
                workspace_id=ctx.workspace_id,
                severity=INFO,
                category=CAT_EXPOSURE,
                title="Large constituent exposure drift",
                message=(
                    f"Look-through constituent exposure moved {pct}% (absolute) since the "
                    "previous snapshot. This compares snapshots — it is not a trade or PnL."
                ),
                dedupe_key=f"exposure_drift_constituent:{ctx.workspace_id}",
                related_entity_type="exposure_snapshot",
                related_entity_id=str(drift.comparison_snapshot_id),
                raw_payload_json={**payload, "abs_weight_delta": str(constituent)},
            )
        )
    sector = drift.sector_weight_delta or Decimal("0")
    if sector >= EXPOSURE_DRIFT_WEIGHT_THRESHOLD:
        pct = (sector * Decimal(100)).quantize(Decimal("0.1"))
        out.append(
            AlertCandidate(
                workspace_id=ctx.workspace_id,
                severity=INFO,
                category=CAT_EXPOSURE,
                title="Large sector exposure drift",
                message=(
                    f"Look-through sector exposure moved {pct}% (absolute) since the previous "
                    "snapshot."
                ),
                dedupe_key=f"exposure_drift_sector:{ctx.workspace_id}",
                related_entity_type="exposure_snapshot",
                related_entity_id=str(drift.comparison_snapshot_id),
                raw_payload_json={**payload, "abs_weight_delta": str(sector)},
            )
        )
    price_cov = drift.price_coverage_delta
    if price_cov is not None and price_cov <= -COVERAGE_DETERIORATION:
        out.append(
            AlertCandidate(
                workspace_id=ctx.workspace_id,
                severity=INFO,
                category=CAT_EXPOSURE,
                title="Constituent price coverage deteriorated",
                message=(
                    f"Constituent price coverage fell by {(-price_cov * 100):.1f}% since the "
                    "previous snapshot; some constituents lost a usable EOD price."
                ),
                dedupe_key=f"constituent_price_coverage_deteriorated:{ctx.workspace_id}",
                related_entity_type="exposure_snapshot",
                related_entity_id=str(drift.comparison_snapshot_id),
                raw_payload_json={**payload, "price_coverage_delta": str(price_cov)},
            )
        )
    fx_cov = drift.fx_coverage_delta
    if fx_cov is not None and fx_cov <= -COVERAGE_DETERIORATION:
        out.append(
            AlertCandidate(
                workspace_id=ctx.workspace_id,
                severity=INFO,
                category=CAT_FX,
                title="Constituent FX coverage deteriorated",
                message=(
                    f"Constituent FX coverage fell by {(-fx_cov * 100):.1f}% since the previous "
                    "snapshot; some priced constituents lost an FX path to base."
                ),
                dedupe_key=f"constituent_fx_coverage_deteriorated:{ctx.workspace_id}",
                related_entity_type="exposure_snapshot",
                related_entity_id=str(drift.comparison_snapshot_id),
                raw_payload_json={**payload, "fx_coverage_delta": str(fx_cov)},
            )
        )
    return out


def rule_broker_import(ctx: AlertContext) -> list[AlertCandidate]:
    """Conservative broker-import alerts (grouped — never one per row).

    Fires only when an import had *unparseable* rows (a data-quality warning, one
    per import) or when committed transactions remain ``unresolved_instrument``
    (one INFO per workspace, prompting manual review — never a name-only guess).
    A clean import stays silent.
    """
    out: list[AlertCandidate] = []
    for imp in ctx.broker_imports_with_errors:
        label = imp.source_filename or imp.broker_name
        out.append(
            AlertCandidate(
                workspace_id=ctx.workspace_id,
                severity=WARNING,
                category=CAT_DATA_QUALITY,
                title=f"Broker import had {imp.error_count} unparseable row(s)",
                message=(
                    f"Import #{imp.id} ({label}) had {imp.error_count} row(s) that could not "
                    "be parsed; they were skipped. Review the source CSV and re-import."
                ),
                dedupe_key=f"broker_import_failed_rows:{ctx.workspace_id}:{imp.id}",
                related_entity_type="broker_import",
                related_entity_id=str(imp.id),
                raw_payload_json={"error_count": imp.error_count},
            )
        )
    count = ctx.unresolved_import_transaction_count
    if count > 0:
        out.append(
            AlertCandidate(
                workspace_id=ctx.workspace_id,
                severity=INFO,
                category=CAT_INSTRUMENT,
                title="Imported transactions need instrument review",
                message=(
                    f"{count} imported transaction(s) could not be matched to a known "
                    "instrument and are stored as unresolved (with symbol/ISIN). Run "
                    "imported_instrument_resolution to include them in look-through."
                ),
                dedupe_key=f"broker_import_unresolved:{ctx.workspace_id}",
                related_entity_type="workspace",
                related_entity_id=str(ctx.workspace_id),
                raw_payload_json={"unresolved_count": count},
            )
        )
    ambiguous = ctx.ambiguous_import_transaction_count
    if ambiguous > 0:
        out.append(
            AlertCandidate(
                workspace_id=ctx.workspace_id,
                severity=WARNING,
                category=CAT_INSTRUMENT,
                title="Imported transactions resolved ambiguously",
                message=(
                    f"{ambiguous} imported transaction(s) matched more than one materially "
                    "different instrument and were left ambiguous (not linked). Disambiguate "
                    "them manually before they can be valued / priced."
                ),
                dedupe_key=f"broker_import_ambiguous:{ctx.workspace_id}",
                related_entity_type="workspace",
                related_entity_id=str(ctx.workspace_id),
                raw_payload_json={"ambiguous_count": ambiguous},
            )
        )
    return out


# --- registry ----------------------------------------------------------------


@dataclass(frozen=True)
class AlertRule:
    name: str
    evaluate: Callable[[AlertContext], list[AlertCandidate]]


RULES: tuple[AlertRule, ...] = (
    AlertRule("document_changed", rule_document_changed),
    AlertRule("document_new", rule_document_new),
    AlertRule("document_missing", rule_document_missing),
    AlertRule("failed_jobs", rule_failed_jobs),
    AlertRule("stale_prices", rule_stale_prices),
    AlertRule("missing_prices", rule_missing_prices),
    AlertRule("fx", rule_fx),
    AlertRule("holdings", rule_holdings),
    AlertRule("instruments", rule_instruments),
    AlertRule("constituent_identity", rule_constituent_identity),
    AlertRule("source_conflicts", rule_source_conflicts),
    AlertRule("upcoming_distributions", rule_upcoming_distributions),
    AlertRule("exposure", rule_exposure),
    AlertRule("constituent_valuation", rule_constituent_valuation),
    AlertRule("exposure_drift", rule_exposure_drift),
    AlertRule("broker_import", rule_broker_import),
)


def evaluate(ctx: AlertContext, rules: tuple[AlertRule, ...] = RULES) -> list[AlertCandidate]:
    """Run every rule and return the de-duplicated candidate list.

    If two rules emit the same ``dedupe_key`` the first wins (rules are ordered
    most-specific first), which keeps the persistence layer's upsert unambiguous.
    """
    seen: dict[str, AlertCandidate] = {}
    for rule in rules:
        for candidate in rule.evaluate(ctx):
            seen.setdefault(candidate.dedupe_key, candidate)
    return list(seen.values())
