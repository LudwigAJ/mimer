"""Imported-instrument resolution bridge.

Turns *already-committed* broker-import transactions that are
``status=unresolved_instrument`` into the canonical ``instruments`` /
``instrument_listings`` / ``instrument_identifiers`` universe, then backfills the
instrument/listing/fund links on the transactions and re-reconciles the position
snapshot. This lets directly-held instruments imported from a broker (TSLA,
AAPL, …) participate in instrument prices, charts, the market-data planner,
exposure / portfolio diagnostics and a future GUI/local-pricer PnL.

It is a *bridge*, not a second identity system (see AGENTS.md):

  * existing identity is checked first (``broker_imports.build_resolution_index``)
    — a symbol that resolved to a fund/listing or to a constituent instrument
    since import is linked with **no** resolver call at all;
  * remaining transactions are resolved through the *same* constituent resolvers
    (offline ``constituent_identity_fixture`` by default; live ``openfigi`` only
    when asked, always behind the source budget + fetch log + request cache);
  * the canonical rows are upserted through the *shared*
    ``constituent_identity.upsert_candidate_instrument`` (deduped on the same
    deterministic identity keys) — never a forked upsert;
  * requests are deduped (two TSLA buys => one resolver request), only *safe*
    requests for the chosen source are attempted (OpenFIGI never gets a
    name-only or bare-ticker query), and an ambiguous / not-found / failed result
    never links a transaction to a guessed instrument;
  * **name-only** imported rows are never auto-resolved or auto-created — they
    stay unresolved for manual handling (a broker name is not identity).

Compute boundary: persistence + bounded SQL reconciliation only. This bridge
resolves *identity* and recomputes the bounded position snapshot; it does NOT
compute PnL, tax lots, total return or corporate actions.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.errors import NotFoundError
from app.db.models import BrokerImport, PortfolioTransaction
from app.services import broker_imports as broker_service
from app.services import constituent_identity as ci
from app.services import source_budget as source_budget_service
from app.services import workspaces as workspaces_service
from app.services.broker_imports import ResolvedLink
from app.sources.constituents import (
    AMBIGUOUS,
    FAILED,
    NOT_FOUND,
    RESOLVED,
    SKIPPED_BUDGET,
    SKIPPED_CACHED,
    ConstituentRequest,
    ConstituentResolver,
    ResolutionCandidate,
    get_constituent_resolver,
)

# Transaction source we operate on (broker CSV import ledger rows).
SOURCE = broker_service.SOURCE

# Identifier priority for a *safe* imported resolver request (strong -> weak).
# Deliberately excludes ``name``: a broker-supplied name is never identity, so a
# name-only row is left unresolved/manual (never auto-created).
_REQUEST_PRIORITY = ("isin", "figi", "ticker")

# A resolved candidate links its transactions only at/above this confidence.
_LINKABLE_CONFIDENCE = {"high", "medium"}

# Transaction statuses this bridge will (re)attempt. ``ambiguous_instrument`` is
# left alone for the bulk path (needs a human); a transaction-id scope may force
# a retry of it (see ``select_unresolved_transactions``).
_RETRYABLE_STATUSES = ("unresolved_instrument",)

RESOLVED_STATUS = "resolved"
AMBIGUOUS_STATUS = "ambiguous_instrument"
UNRESOLVED_STATUS = "unresolved_instrument"


# --- run result --------------------------------------------------------------


@dataclass
class ImportedResolutionResult:
    """Counters for one resolution run (mirrors the constituent resolver shape)."""

    candidates_resolved: int = 0  # distinct resolver candidates that resolved
    linked: int = 0  # transactions newly linked to an instrument/listing/fund
    linked_existing: int = 0  # of ``linked``: linked via existing identity (no resolver)
    ambiguous: int = 0  # transactions marked ambiguous_instrument (not linked)
    not_found: int = 0  # transactions left unresolved (resolver found nothing)
    failed: int = 0  # transactions left unresolved (resolver errored)
    skipped_unsafe: int = 0  # transactions with no safe request for this source
    skipped_budget: int = 0  # transactions skipped this cycle (source budget)
    skipped_cached: int = 0  # transactions skipped this cycle (request cache)
    instruments_created: int = 0
    listings_created: int = 0
    identifiers_created: int = 0
    snapshot_created: bool = False
    transactions_selected: int = 0

    @property
    def attempted(self) -> int:
        return self.linked + self.ambiguous + self.not_found + self.failed

    def message(self) -> str:
        return (
            f"selected={self.transactions_selected} linked={self.linked} "
            f"(existing={self.linked_existing} resolver={self.linked - self.linked_existing}) "
            f"candidates_resolved={self.candidates_resolved} ambiguous={self.ambiguous} "
            f"not_found={self.not_found} failed={self.failed} "
            f"skipped_unsafe={self.skipped_unsafe} skipped_budget={self.skipped_budget} "
            f"skipped_cached={self.skipped_cached} instruments={self.instruments_created} "
            f"listings={self.listings_created} identifiers={self.identifiers_created} "
            f"snapshot_created={self.snapshot_created}"
        )


# --- request construction ----------------------------------------------------


def _transaction_primary(txn: PortfolioTransaction) -> tuple[str, str] | None:
    """The highest-priority (scheme, value) identifier present on a transaction."""
    candidates: dict[str, str | None] = {
        "isin": txn.isin,
        "figi": txn.figi,
        "ticker": txn.symbol,
    }
    for scheme in _REQUEST_PRIORITY:
        value = candidates.get(scheme)
        if value and value.strip():
            return scheme, value.strip()
    return None  # name-only / cash row -> unsafe (never auto-create from a name)


def _request_for_transaction(txn: PortfolioTransaction) -> ConstituentRequest | None:
    primary = _transaction_primary(txn)
    if primary is None:
        return None
    scheme, value = primary
    return ConstituentRequest(
        input_key=f"{scheme}:{value.upper()}",
        scheme=scheme,
        value=value,
        name=txn.name,
        ticker=txn.symbol,
        isin=txn.isin,
        figi=txn.figi,
        currency=txn.currency,
    )


@dataclass
class ImportedRequestPlan:
    requests: list[ConstituentRequest]
    txn_ids_by_key: dict[str, list[int]]
    unsafe_txn_ids: list[int]


def build_requests(
    transactions: list[PortfolioTransaction], *, resolver: ConstituentResolver
) -> ImportedRequestPlan:
    """Build deduped, *source-safe* resolver requests from transactions.

    One request per distinct identifier (two TSLA buys => one request, covering
    both); only requests this resolver can safely attempt are kept — the rest
    (name-only, or a bare ticker for OpenFIGI) are returned as unsafe and left
    unresolved for manual handling.
    """
    by_key: dict[str, ConstituentRequest] = {}
    txn_ids: dict[str, list[int]] = {}
    unsafe: list[int] = []
    for txn in transactions:
        request = _request_for_transaction(txn)
        if request is None or not resolver.is_request_safe(request):
            unsafe.append(txn.id)
            continue
        by_key.setdefault(request.input_key, request)
        txn_ids.setdefault(request.input_key, []).append(txn.id)
    return ImportedRequestPlan(list(by_key.values()), txn_ids, unsafe)


# --- transaction selection ---------------------------------------------------


async def _resolve_scope(
    session: AsyncSession,
    *,
    workspace_id: int | None,
    broker_import_id: int | None,
    transaction_id: int | None,
) -> int:
    """Resolve the effective workspace id from whichever scope was supplied."""
    if transaction_id is not None:
        txn = await session.get(PortfolioTransaction, transaction_id)
        if txn is None:
            raise NotFoundError("Transaction not found", code="transaction_not_found")
        if workspace_id is not None and txn.workspace_id != workspace_id:
            raise NotFoundError("Transaction not found", code="transaction_not_found")
        return txn.workspace_id
    if broker_import_id is not None:
        imp = await session.get(BrokerImport, broker_import_id)
        if imp is None:
            raise NotFoundError("Broker import not found", code="broker_import_not_found")
        if workspace_id is not None and imp.workspace_id != workspace_id:
            raise NotFoundError("Broker import not found", code="broker_import_not_found")
        return imp.workspace_id
    if workspace_id is None:
        raise NotFoundError(
            "A workspace, broker import or transaction scope is required",
            code="resolution_scope_required",
        )
    return workspace_id


async def select_unresolved_transactions(
    session: AsyncSession,
    *,
    workspace_id: int,
    broker_import_id: int | None = None,
    broker_account_id: int | None = None,
    transaction_id: int | None = None,
    limit: int | None = None,
) -> list[PortfolioTransaction]:
    """Unresolved imported transactions in scope (newest first, bounded).

    A ``transaction_id`` scope also re-attempts an ``ambiguous_instrument`` row
    (a forced single-transaction retry); the bulk scopes only pick up
    ``unresolved_instrument`` so ambiguous rows stay parked for manual review.
    """
    statuses: list[str] = list(_RETRYABLE_STATUSES)
    if transaction_id is not None:
        statuses.append(AMBIGUOUS_STATUS)
    stmt = (
        select(PortfolioTransaction)
        .where(
            PortfolioTransaction.workspace_id == workspace_id,
            PortfolioTransaction.source == SOURCE,
            PortfolioTransaction.status.in_(statuses),
        )
        .order_by(PortfolioTransaction.trade_date.desc(), PortfolioTransaction.id.desc())
    )
    if transaction_id is not None:
        stmt = stmt.where(PortfolioTransaction.id == transaction_id)
    if broker_import_id is not None:
        stmt = stmt.where(PortfolioTransaction.broker_import_id == broker_import_id)
    if broker_account_id is not None:
        stmt = stmt.where(PortfolioTransaction.broker_account_id == broker_account_id)
    if limit is not None:
        stmt = stmt.limit(max(1, limit))
    return list((await session.execute(stmt)).scalars().all())


# --- linking helpers (never clobber a non-retryable / manual link) -----------


def _apply_existing_link(txn: PortfolioTransaction, link: ResolvedLink) -> None:
    txn.instrument_id = link.instrument_id
    txn.instrument_listing_id = link.instrument_listing_id
    txn.fund_id = link.fund_id
    txn.fund_listing_id = link.fund_listing_id
    txn.status = RESOLVED_STATUS


def _apply_instrument_link(txn: PortfolioTransaction, instrument, listing) -> None:  # type: ignore[no-untyped-def]
    txn.instrument_id = instrument.id
    if listing is not None:
        txn.instrument_listing_id = listing.id
    txn.status = RESOLVED_STATUS


def _mark_ambiguous(txn: PortfolioTransaction, *, now: datetime, source: str) -> None:
    txn.status = AMBIGUOUS_STATUS
    payload = dict(txn.raw_payload_json or {})
    payload["resolution"] = {"status": "ambiguous", "source": source, "at": now.isoformat()}
    txn.raw_payload_json = payload


def _note_unresolved(txn: PortfolioTransaction, *, status: str, now: datetime, source: str) -> None:
    """Record why a transaction stayed unresolved (no link), keeping its status."""
    payload = dict(txn.raw_payload_json or {})
    payload["resolution"] = {"status": status, "source": source, "at": now.isoformat()}
    txn.raw_payload_json = payload


# --- candidate persistence ---------------------------------------------------


async def _persist_candidate(
    session: AsyncSession,
    candidate: ResolutionCandidate,
    transactions: list[PortfolioTransaction],
    *,
    result: ImportedResolutionResult,
    ci_result: ci.ResolutionRunResult,
    now: datetime,
    source: str,
    dry_run: bool,
) -> None:
    if candidate.status == SKIPPED_BUDGET:
        result.skipped_budget += len(transactions)
        return
    if candidate.status == SKIPPED_CACHED:
        result.skipped_cached += len(transactions)
        return
    if candidate.status == FAILED:
        result.failed += len(transactions)
        if not dry_run:
            for txn in transactions:
                _note_unresolved(txn, status="failed", now=now, source=source)
        return
    if candidate.status == NOT_FOUND:
        result.not_found += len(transactions)
        if not dry_run:
            for txn in transactions:
                _note_unresolved(txn, status="not_found", now=now, source=source)
        return
    if candidate.status == AMBIGUOUS:
        result.ambiguous += len(transactions)
        if not dry_run:
            for txn in transactions:
                _mark_ambiguous(txn, now=now, source=source)
        return
    if candidate.status == RESOLVED and candidate.confidence in _LINKABLE_CONFIDENCE:
        result.candidates_resolved += 1
        if dry_run:
            result.linked += len(transactions)
            return
        instrument, listing = await ci.upsert_candidate_instrument(
            session, candidate, result=ci_result
        )
        for txn in transactions:
            _apply_instrument_link(txn, instrument, listing)
            result.linked += 1
        return
    # A resolved-but-low-confidence result is treated as ambiguous (never linked).
    result.ambiguous += len(transactions)
    if not dry_run:
        for txn in transactions:
            _mark_ambiguous(txn, now=now, source=source)


# --- orchestration -----------------------------------------------------------


async def resolve_imported_instruments(
    session: AsyncSession,
    *,
    workspace_id: int | None = None,
    broker_import_id: int | None = None,
    broker_account_id: int | None = None,
    transaction_id: int | None = None,
    limit: int | None = None,
    source: str | None = None,
    dry_run: bool = False,
    now: datetime | None = None,
) -> ImportedResolutionResult:
    """Resolve unresolved imported transactions to canonical instruments + relink.

    ``dry_run`` builds requests / candidates and counts outcomes but writes
    nothing (no upserts, no link changes, no snapshot). Otherwise links are
    persisted and a fresh position snapshot is written if the ledger materially
    changed. Defaults to the offline fixture resolver; pass ``source="openfigi"``
    for the live, budget-guarded path.
    """
    now = now or datetime.now(UTC)
    resolver = get_constituent_resolver(source)
    result = ImportedResolutionResult()

    effective_ws = await _resolve_scope(
        session,
        workspace_id=workspace_id,
        broker_import_id=broker_import_id,
        transaction_id=transaction_id,
    )
    transactions = await select_unresolved_transactions(
        session,
        workspace_id=effective_ws,
        broker_import_id=broker_import_id,
        broker_account_id=broker_account_id,
        transaction_id=transaction_id,
        limit=limit,
    )
    result.transactions_selected = len(transactions)
    if not transactions:
        return result

    # --- 1) existing identity first (cheap; no resolver call) -----------------
    index = await broker_service.build_resolution_index(session, transactions)
    remaining: list[PortfolioTransaction] = []
    for txn in transactions:
        link = index.resolve(isin=txn.isin, figi=txn.figi, symbol=txn.symbol, currency=txn.currency)
        if link.resolved:
            result.linked += 1
            result.linked_existing += 1
            if not dry_run:
                _apply_existing_link(txn, link)
        else:
            remaining.append(txn)

    # --- 2) resolver (fixture / OpenFIGI), deduped + source-safe --------------
    plan = build_requests(remaining, resolver=resolver)
    result.skipped_unsafe = len(plan.unsafe_txn_ids)
    if plan.requests:
        budget = await source_budget_service.get_budget(session, resolver.name)
        batch_size = budget.batch_size if budget and budget.batch_size else 10
        # Fixtures are offline + deterministic, so they bypass the recent-success
        # cache (TTL 0) — the idempotent upsert is what guarantees no duplicate
        # rows on a rerun. External sources use the configured TTL.
        ttl_seconds = (
            0 if resolver.name.endswith("_fixture") else get_settings().request_cache_ttl_seconds
        )
        candidates = await resolver.resolve_batch(
            session, plan.requests, batch_size=batch_size, ttl_seconds=ttl_seconds
        )
        txns_by_id = {t.id: t for t in remaining}
        ci_result = ci.ResolutionRunResult()
        for candidate in candidates:
            covered = [
                txns_by_id[tid]
                for tid in plan.txn_ids_by_key.get(candidate.input_key, [])
                if tid in txns_by_id
            ]
            await _persist_candidate(
                session,
                candidate,
                covered,
                result=result,
                ci_result=ci_result,
                now=now,
                source=resolver.name,
                dry_run=dry_run,
            )
        result.instruments_created = ci_result.instruments_created
        result.listings_created = ci_result.listings_created
        result.identifiers_created = ci_result.identifiers_created

    # --- 3) re-reconcile the bounded position snapshot ------------------------
    # Only when something actually changed the link set; the input hash now keys
    # on the resolved instrument, so a relinked ledger writes a new snapshot.
    if not dry_run and result.linked:
        await session.flush()
        _, created = await broker_service.write_position_snapshot(session, effective_ws)
        result.snapshot_created = created

    return result


# --- read helpers (API) ------------------------------------------------------


async def list_unresolved_transactions(
    session: AsyncSession, workspace_id: int, *, limit: int = 200
) -> list[PortfolioTransaction]:
    """Imported transactions awaiting (or needing manual) instrument resolution."""
    await workspaces_service.get_workspace(session, workspace_id)
    limit = max(1, min(limit, 1000))
    stmt = (
        select(PortfolioTransaction)
        .where(
            PortfolioTransaction.workspace_id == workspace_id,
            PortfolioTransaction.source == SOURCE,
            PortfolioTransaction.status.in_([UNRESOLVED_STATUS, AMBIGUOUS_STATUS]),
        )
        .order_by(PortfolioTransaction.trade_date.desc(), PortfolioTransaction.id.desc())
        .limit(limit)
    )
    return list((await session.execute(stmt)).scalars().all())
