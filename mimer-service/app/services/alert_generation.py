"""Alert generation — load a workspace context, run the rules, upsert results.

This is the DB-facing half of alerting (the rules in `app.services.alert_rules`
are pure). It:

1. builds an `AlertContext` for a workspace from existing diagnostics/change
   signals (held funds/listings, latest prices, document change status, holdings
   freshness, FX coverage, recent failed jobs, instrument resolution state, ...);
2. evaluates the rule registry into `AlertCandidate` values;
3. upserts them idempotently keyed by ``(workspace_id, dedupe_key)``.

Idempotency / lifecycle (see Part 5 of the spec):

* candidate seen, no row        -> insert (active)
* candidate seen, row active/read -> refresh ``last_seen_at`` + content; keep
  the read state
* candidate seen, row resolved   -> reactivate (the issue came back)
* candidate seen, row dismissed  -> stay dismissed (same ``dedupe_key`` == same
  material issue); only a *materially* different issue produces a new key/row
* candidate gone, row active/read, key auto-resolvable -> resolve

No scheduler/broker — the `alert_generation` worker drives this synchronously.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    Alert,
    BrokerImport,
    Distribution,
    DocumentSnapshot,
    ExposureSnapshot,
    Fund,
    FundHolding,
    FundListing,
    JobRun,
    PortfolioPosition,
    PortfolioTransaction,
    Price,
    SecurityIdentifier,
    Workspace,
)
from app.services import alert_rules
from app.services import exposure_drift as exposure_drift_service
from app.services import holdings_ingestion as holdings_service
from app.services import workspaces as workspaces_service
from app.services.alert_rules import AlertCandidate, is_auto_resolvable
from app.services.conversion import normalise_currency
from app.services.fx import FxIndex, load_fx_index

# Content fields synced from a candidate onto an existing alert row.
_MUTABLE_FIELDS = (
    "severity",
    "category",
    "title",
    "message",
    "source",
    "related_entity_type",
    "related_entity_id",
    "related_fund_id",
    "related_fund_listing_id",
    "related_document_snapshot_id",
    "related_job_run_id",
    "raw_payload_json",
)


@dataclass
class ExposureDriftSignal:
    """Compact latest-vs-previous drift facts the pure drift rule reads.

    Computed DB-side (so the rule stays pure). ``has_prior`` is False when there
    is no earlier snapshot to compare — the rule then stays silent."""

    has_prior: bool = False
    base_snapshot_id: int | None = None
    comparison_snapshot_id: int | None = None
    constituent_weight_delta: Decimal | None = None
    sector_weight_delta: Decimal | None = None
    price_coverage_delta: Decimal | None = None
    fx_coverage_delta: Decimal | None = None


@dataclass
class AlertContext:
    """Everything the pure rules need, loaded once per workspace."""

    workspace_id: int
    base_currency: str
    now: datetime
    funds: list[Fund] = field(default_factory=list)
    listings: list[FundListing] = field(default_factory=list)
    fund_by_id: dict[int, Fund] = field(default_factory=dict)
    listing_by_id: dict[int, FundListing] = field(default_factory=dict)
    latest_price_date: dict[int, date] = field(default_factory=dict)
    listings_with_price: set[int] = field(default_factory=set)
    latest_documents: dict[tuple[int, str], DocumentSnapshot] = field(default_factory=dict)
    doc_types_by_fund: dict[int, set[str]] = field(default_factory=dict)
    holdings_as_of: dict[int, date | None] = field(default_factory=dict)
    position_currencies: set[str] = field(default_factory=set)
    fx_index: FxIndex | None = None
    failed_job_runs: list[JobRun] = field(default_factory=list)
    ambiguous_identifiers: list[SecurityIdentifier] = field(default_factory=list)
    pending_funds: list[Fund] = field(default_factory=list)
    pending_listings: list[FundListing] = field(default_factory=list)
    source_conflict_listings: list[int] = field(default_factory=list)
    upcoming_distributions: list[Distribution] = field(default_factory=list)
    has_positions: bool = False
    latest_exposure_snapshot: ExposureSnapshot | None = None
    exposure_recompute_failures: list[JobRun] = field(default_factory=list)
    ambiguous_constituents: list[FundHolding] = field(default_factory=list)
    exposure_drift: ExposureDriftSignal = field(default_factory=ExposureDriftSignal)
    broker_imports_with_errors: list[BrokerImport] = field(default_factory=list)
    unresolved_import_transaction_count: int = 0
    ambiguous_import_transaction_count: int = 0


@dataclass
class WorkspaceAlertResult:
    workspace_id: int
    inserted: int = 0
    updated: int = 0
    reactivated: int = 0
    resolved: int = 0
    unchanged: int = 0


@dataclass
class AlertGenerationResult:
    inserted: int = 0
    updated: int = 0
    reactivated: int = 0
    resolved: int = 0
    failed: int = 0
    processed_workspaces: list[int] = field(default_factory=list)
    failures: dict[int, str] = field(default_factory=dict)

    def add(self, res: WorkspaceAlertResult) -> None:
        self.inserted += res.inserted
        self.updated += res.updated
        self.reactivated += res.reactivated
        self.resolved += res.resolved
        self.processed_workspaces.append(res.workspace_id)


# --- context building --------------------------------------------------------


async def _latest_price_dates(
    session: AsyncSession, listing_ids: list[int]
) -> tuple[dict[int, date], set[int]]:
    if not listing_ids:
        return {}, set()
    rows = (
        await session.execute(
            select(Price.fund_listing_id, func.max(Price.price_date))
            .where(Price.fund_listing_id.in_(listing_ids))
            .group_by(Price.fund_listing_id)
        )
    ).all()
    latest = {lid: pdate for lid, pdate in rows}
    return latest, set(latest)


async def _latest_documents(
    session: AsyncSession, fund_ids: list[int]
) -> tuple[dict[tuple[int, str], DocumentSnapshot], dict[int, set[str]]]:
    latest: dict[tuple[int, str], DocumentSnapshot] = {}
    types_by_fund: dict[int, set[str]] = {fid: set() for fid in fund_ids}
    if not fund_ids:
        return latest, types_by_fund
    docs = list(
        (
            await session.execute(
                select(DocumentSnapshot).where(DocumentSnapshot.fund_id.in_(fund_ids))
            )
        )
        .scalars()
        .all()
    )
    for doc in docs:
        doc_type = doc.document_type.lower()
        types_by_fund.setdefault(doc.fund_id, set()).add(doc_type)
        key = (doc.fund_id, doc_type)
        current = latest.get(key)
        # Newest snapshot per (fund, type): prefer created_at, tie-break on id.
        if current is None or (doc.created_at, doc.id) >= (current.created_at, current.id):
            latest[key] = doc
    return latest, types_by_fund


async def _failed_job_runs(
    session: AsyncSession, *, fund_ids: list[int], listing_ids: list[int], now: datetime
) -> list[JobRun]:
    cutoff = now - timedelta(days=alert_rules.FAILED_JOB_LOOKBACK_DAYS)
    # Scope: job runs for held funds/listings, plus global (un-targeted) infra
    # failures (e.g. fx_ingestion) that affect every workspace's data quality.
    scope = [and_(JobRun.fund_id.is_(None), JobRun.fund_listing_id.is_(None))]
    if fund_ids:
        scope.append(JobRun.fund_id.in_(fund_ids))
    if listing_ids:
        scope.append(JobRun.fund_listing_id.in_(listing_ids))
    stmt = (
        select(JobRun)
        .where(
            JobRun.status.in_(["failed", "partial_success"]),
            or_(
                JobRun.finished_at >= cutoff,
                JobRun.started_at >= cutoff,
                and_(JobRun.finished_at.is_(None), JobRun.started_at.is_(None)),
            ),
            or_(*scope),
        )
        .order_by(JobRun.id.desc())
        .limit(50)
    )
    return list((await session.execute(stmt)).scalars().all())


async def _source_conflict_listings(session: AsyncSession, listing_ids: list[int]) -> list[int]:
    if not listing_ids:
        return []
    rows = (
        await session.execute(
            select(Price.fund_listing_id)
            .where(Price.fund_listing_id.in_(listing_ids))
            .group_by(Price.fund_listing_id, Price.price_date)
            .having(func.count(func.distinct(Price.source)) > 1)
        )
    ).all()
    return sorted({lid for (lid,) in rows})


async def _upcoming_distributions(session: AsyncSession, fund_ids: list[int]) -> list[Distribution]:
    if not fund_ids:
        return []
    today = date.today()
    stmt = (
        select(Distribution)
        .where(
            Distribution.fund_id.in_(fund_ids),
            or_(
                Distribution.ex_date >= today,
                func.lower(Distribution.status).in_(["declared", "announced", "pending"]),
            ),
        )
        .order_by(Distribution.ex_date.desc())
        .limit(20)
    )
    return list((await session.execute(stmt)).scalars().all())


async def _latest_exposure_snapshot(
    session: AsyncSession, workspace_id: int
) -> ExposureSnapshot | None:
    return await session.scalar(
        select(ExposureSnapshot)
        .where(ExposureSnapshot.workspace_id == workspace_id)
        .order_by(ExposureSnapshot.as_of_date.desc(), ExposureSnapshot.id.desc())
        .limit(1)
    )


async def _exposure_drift_signal(
    session: AsyncSession, workspace_id: int, latest: ExposureSnapshot | None
) -> ExposureDriftSignal:
    """Latest-vs-previous drift facts for the pure drift rule (DB-side)."""
    if latest is None:
        return ExposureDriftSignal(has_prior=False)
    previous = await exposure_drift_service.previous_snapshot(session, workspace_id, latest)
    if previous is None:
        return ExposureDriftSignal(has_prior=False)
    constituent = await exposure_drift_service.compute_drift(
        session, workspace_id, dimension="constituent", with_price_context=False
    )
    sector = await exposure_drift_service.compute_drift(
        session, workspace_id, dimension="sector", with_price_context=False
    )
    cs = constituent.summary
    ss = sector.summary
    return ExposureDriftSignal(
        has_prior=True,
        base_snapshot_id=previous.id,
        comparison_snapshot_id=latest.id,
        constituent_weight_delta=cs.total_abs_weight_delta if cs else None,
        sector_weight_delta=ss.total_abs_weight_delta if ss else None,
        price_coverage_delta=cs.price_coverage_delta if cs else None,
        fx_coverage_delta=cs.fx_coverage_delta if cs else None,
    )


async def _exposure_recompute_failures(session: AsyncSession, *, now: datetime) -> list[JobRun]:
    cutoff = now - timedelta(days=alert_rules.FAILED_JOB_LOOKBACK_DAYS)
    stmt = (
        select(JobRun)
        .where(
            JobRun.job_type == "exposure_recompute",
            JobRun.status == "failed",
            or_(
                JobRun.finished_at >= cutoff,
                JobRun.started_at >= cutoff,
                and_(JobRun.finished_at.is_(None), JobRun.started_at.is_(None)),
            ),
        )
        .order_by(JobRun.id.desc())
        .limit(20)
    )
    return list((await session.execute(stmt)).scalars().all())


async def build_context(session: AsyncSession, workspace_id: int) -> AlertContext:
    workspace = await workspaces_service.get_workspace(session, workspace_id)
    now = datetime.now(UTC)

    positions = (
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

    funds: list[Fund] = []
    if fund_ids:
        funds = list(
            (await session.execute(select(Fund).where(Fund.id.in_(fund_ids)))).scalars().all()
        )

    latest_price_date, listings_with_price = await _latest_price_dates(session, listing_ids)
    latest_documents, doc_types_by_fund = await _latest_documents(session, fund_ids)

    holdings_snapshots = await holdings_service.latest_holdings_by_fund(session, fund_ids)
    holdings_as_of: dict[int, date | None] = {}
    ambiguous_constituents: list[FundHolding] = []
    for fund_id in fund_ids:
        snapshot = holdings_snapshots.get(fund_id) or []
        holdings_as_of[fund_id] = max((h.as_of_date for h in snapshot), default=None)
        ambiguous_constituents.extend(h for h in snapshot if h.identity_status == "ambiguous")

    base = workspace.base_currency
    position_currencies = {
        normalise_currency(ln.currency_unit or ln.trading_currency, base) for ln in listings
    }

    ambiguous_identifiers: list[SecurityIdentifier] = []
    if fund_ids:
        ambiguous_identifiers = list(
            (
                await session.execute(
                    select(SecurityIdentifier).where(
                        SecurityIdentifier.fund_id.in_(fund_ids),
                        SecurityIdentifier.confidence != "high",
                    )
                )
            )
            .scalars()
            .all()
        )

    latest_exposure_snapshot = await _latest_exposure_snapshot(session, workspace_id)

    broker_imports_with_errors = list(
        (
            await session.execute(
                select(BrokerImport)
                .where(
                    BrokerImport.workspace_id == workspace_id,
                    BrokerImport.error_count > 0,
                )
                .order_by(BrokerImport.id.desc())
                .limit(20)
            )
        )
        .scalars()
        .all()
    )
    unresolved_import_transaction_count = (
        await session.scalar(
            select(func.count())
            .select_from(PortfolioTransaction)
            .where(
                PortfolioTransaction.workspace_id == workspace_id,
                PortfolioTransaction.status == "unresolved_instrument",
            )
        )
    ) or 0
    ambiguous_import_transaction_count = (
        await session.scalar(
            select(func.count())
            .select_from(PortfolioTransaction)
            .where(
                PortfolioTransaction.workspace_id == workspace_id,
                PortfolioTransaction.status == "ambiguous_instrument",
            )
        )
    ) or 0

    return AlertContext(
        workspace_id=workspace_id,
        base_currency=base,
        now=now,
        funds=funds,
        listings=listings,
        fund_by_id={f.id: f for f in funds},
        listing_by_id={ln.id: ln for ln in listings},
        latest_price_date=latest_price_date,
        listings_with_price=listings_with_price,
        latest_documents=latest_documents,
        doc_types_by_fund=doc_types_by_fund,
        holdings_as_of=holdings_as_of,
        position_currencies=position_currencies,
        fx_index=await load_fx_index(session),
        failed_job_runs=await _failed_job_runs(
            session, fund_ids=fund_ids, listing_ids=listing_ids, now=now
        ),
        ambiguous_identifiers=ambiguous_identifiers,
        pending_funds=[f for f in funds if f.status == "pending"],
        pending_listings=[ln for ln in listings if ln.status == "pending"],
        source_conflict_listings=await _source_conflict_listings(session, listing_ids),
        upcoming_distributions=await _upcoming_distributions(session, fund_ids),
        has_positions=bool(positions),
        latest_exposure_snapshot=latest_exposure_snapshot,
        exposure_recompute_failures=await _exposure_recompute_failures(session, now=now),
        ambiguous_constituents=ambiguous_constituents,
        exposure_drift=await _exposure_drift_signal(
            session, workspace_id, latest_exposure_snapshot
        ),
        broker_imports_with_errors=broker_imports_with_errors,
        unresolved_import_transaction_count=unresolved_import_transaction_count,
        ambiguous_import_transaction_count=ambiguous_import_transaction_count,
    )


# --- persistence -------------------------------------------------------------


def _apply_candidate(alert: Alert, candidate: AlertCandidate) -> bool:
    """Sync a candidate's content onto an alert row; return whether it changed."""
    changed = False
    for name in _MUTABLE_FIELDS:
        new_value = getattr(candidate, name)
        if getattr(alert, name) != new_value:
            setattr(alert, name, new_value)
            changed = True
    return changed


async def _persist(
    session: AsyncSession, workspace_id: int, candidates: list[AlertCandidate]
) -> WorkspaceAlertResult:
    res = WorkspaceAlertResult(workspace_id=workspace_id)
    now = datetime.now(UTC)
    existing = {
        a.dedupe_key: a
        for a in (await session.execute(select(Alert).where(Alert.workspace_id == workspace_id)))
        .scalars()
        .all()
    }
    candidate_keys = {c.dedupe_key for c in candidates}

    for candidate in candidates:
        alert = existing.get(candidate.dedupe_key)
        if alert is None:
            alert = Alert(
                workspace_id=workspace_id,
                dedupe_key=candidate.dedupe_key,
                status=alert_rules.STATUS_ACTIVE,
                first_seen_at=now,
                last_seen_at=now,
            )
            _apply_candidate(alert, candidate)
            session.add(alert)
            res.inserted += 1
            continue

        alert.last_seen_at = now
        if alert.status == alert_rules.STATUS_DISMISSED:
            # Same dedupe_key => same material issue: respect the dismissal.
            res.unchanged += 1
            continue
        if alert.status == alert_rules.STATUS_RESOLVED:
            alert.status = alert_rules.STATUS_ACTIVE
            alert.resolved_at = None
            _apply_candidate(alert, candidate)
            res.reactivated += 1
            continue
        # active / read: refresh content, keep read state.
        if _apply_candidate(alert, candidate):
            res.updated += 1
        else:
            res.unchanged += 1

    # Auto-resolve previously-active alerts whose issue is no longer asserted.
    for key, alert in existing.items():
        if key in candidate_keys:
            continue
        if alert.status in (
            alert_rules.STATUS_ACTIVE,
            alert_rules.STATUS_READ,
        ) and is_auto_resolvable(key):
            alert.status = alert_rules.STATUS_RESOLVED
            alert.resolved_at = now
            res.resolved += 1

    return res


async def generate_for_workspace(session: AsyncSession, workspace_id: int) -> WorkspaceAlertResult:
    """Evaluate rules for one workspace and upsert. Does not commit."""
    ctx = await build_context(session, workspace_id)
    candidates = alert_rules.evaluate(ctx)
    return await _persist(session, workspace_id, candidates)


async def active_workspace_ids(session: AsyncSession) -> list[int]:
    return list(
        (await session.execute(select(Workspace.id).order_by(Workspace.id))).scalars().all()
    )
