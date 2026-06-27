"""Broker CSV import + transaction-ledger + position-reconciliation endpoints.

All workspace-scoped under ``/workspaces/{workspace_id}``. Preview is read-only;
commit is idempotent (re-committing the same file is a duplicate no-op). The
positions endpoint is a derived, bounded reconciliation (buys − sells per
instrument; cash per currency) — NOT PnL (see AGENTS.md compute boundary).
"""

from __future__ import annotations

from fastapi import APIRouter, Query

from app.api.deps import PathWorkspaceId, SessionDep
from app.schemas.broker_import import (
    BrokerImportDetailRead,
    BrokerImportRead,
    BrokerImportRequest,
    BrokerImportResponse,
    ClearLinkRequest,
    CorrectionActionRequest,
    CorrectionContextResponse,
    CorrectionResponse,
    ManualLinkRequest,
    PositionsResponse,
    ResolveTransactionsRequest,
    ResolveTransactionsResponse,
    TransactionRead,
)
from app.schemas.common import ListResponse
from app.services import broker_imports as service
from app.services import imported_instrument_resolution as resolution_service
from app.services import transaction_corrections as corrections_service
from app.sources.constituents import get_constituent_resolver

workspace_router = APIRouter(prefix="/workspaces/{workspace_id}", tags=["broker imports"])


def _resolution_response(
    workspace_id: int,
    source: str,
    result: resolution_service.ImportedResolutionResult,
    *,
    dry_run: bool,
) -> ResolveTransactionsResponse:
    return ResolveTransactionsResponse(
        workspace_id=workspace_id,
        source=source,
        dry_run=dry_run,
        transactions_selected=result.transactions_selected,
        linked=result.linked,
        linked_existing=result.linked_existing,
        candidates_resolved=result.candidates_resolved,
        ambiguous=result.ambiguous,
        not_found=result.not_found,
        failed=result.failed,
        skipped_unsafe=result.skipped_unsafe,
        skipped_budget=result.skipped_budget,
        skipped_cached=result.skipped_cached,
        instruments_created=result.instruments_created,
        listings_created=result.listings_created,
        identifiers_created=result.identifiers_created,
        snapshot_created=result.snapshot_created,
        message=result.message(),
    )


@workspace_router.post("/broker-imports/preview", response_model=BrokerImportResponse)
async def preview_broker_import(
    workspace_id: PathWorkspaceId, data: BrokerImportRequest, session: SessionDep
) -> BrokerImportResponse:
    """Parse + resolve a broker CSV and report row/transaction outcomes (no writes)."""
    return await service.preview_import(session, workspace_id, request=data)


@workspace_router.post("/broker-imports/commit", response_model=BrokerImportResponse)
async def commit_broker_import(
    workspace_id: PathWorkspaceId, data: BrokerImportRequest, session: SessionDep
) -> BrokerImportResponse:
    """Idempotently commit a broker CSV into the canonical ledger + reconcile.

    Re-committing the same file content is a duplicate no-op (``duplicate=true``).
    """
    return await service.commit_import(session, workspace_id, request=data)


@workspace_router.get("/broker-imports", response_model=ListResponse[BrokerImportRead])
async def list_broker_imports(
    workspace_id: PathWorkspaceId,
    session: SessionDep,
    limit: int = Query(default=100, ge=1, le=500),
):
    items = await service.list_imports(session, workspace_id, limit=limit)
    return ListResponse.of([BrokerImportRead.model_validate(i) for i in items])


@workspace_router.get("/broker-imports/{import_id}", response_model=BrokerImportDetailRead)
async def get_broker_import(
    workspace_id: PathWorkspaceId, import_id: int, session: SessionDep
) -> BrokerImportDetailRead:
    broker_import = await service.get_import(session, workspace_id, import_id)
    return BrokerImportDetailRead.model_validate(broker_import)


@workspace_router.get("/transactions", response_model=ListResponse[TransactionRead])
async def list_transactions(
    workspace_id: PathWorkspaceId,
    session: SessionDep,
    limit: int = Query(default=200, ge=1, le=1000),
    transaction_type: str | None = Query(default=None),
    status: str | None = Query(default=None),
    broker_import_id: int | None = Query(default=None),
):
    items = await service.list_transactions(
        session,
        workspace_id,
        limit=limit,
        transaction_type=transaction_type,
        status=status,
        broker_import_id=broker_import_id,
    )
    return ListResponse.of([TransactionRead.model_validate(i) for i in items])


@workspace_router.get("/transactions/unresolved", response_model=ListResponse[TransactionRead])
async def list_unresolved_transactions(
    workspace_id: PathWorkspaceId,
    session: SessionDep,
    limit: int = Query(default=200, ge=1, le=1000),
):
    """Imported transactions awaiting (or needing manual) instrument resolution."""
    items = await resolution_service.list_unresolved_transactions(
        session, workspace_id, limit=limit
    )
    return ListResponse.of([TransactionRead.model_validate(i) for i in items])


@workspace_router.get("/transactions/manual-review", response_model=ListResponse[TransactionRead])
async def list_manual_review_transactions(
    workspace_id: PathWorkspaceId,
    session: SessionDep,
    status: str | None = Query(default=None),
    limit: int = Query(default=200, ge=1, le=1000),
):
    """Imported transactions needing a human: unresolved / ambiguous / manual_review.

    Optionally narrow to one status via ``?status=manual_review`` (etc.).
    """
    items = await corrections_service.list_manual_review_transactions(
        session, workspace_id, status=status, limit=limit
    )
    return ListResponse.of([TransactionRead.model_validate(i) for i in items])


@workspace_router.get(
    "/transactions/{transaction_id}/correction-context", response_model=CorrectionContextResponse
)
async def transaction_correction_context(
    workspace_id: PathWorkspaceId, transaction_id: int, session: SessionDep
) -> CorrectionContextResponse:
    """Bounded candidate context (identifier/ticker matches) to choose a link.

    Never calls a resolver/OpenFIGI/live source and never name-only guesses.
    """
    return await corrections_service.get_correction_context(session, workspace_id, transaction_id)


@workspace_router.get("/transactions/{transaction_id}", response_model=TransactionRead)
async def get_transaction(
    workspace_id: PathWorkspaceId, transaction_id: int, session: SessionDep
) -> TransactionRead:
    txn = await service.get_transaction(session, workspace_id, transaction_id)
    return TransactionRead.model_validate(txn)


@workspace_router.post(
    "/transactions/{transaction_id}/manual-link", response_model=CorrectionResponse
)
async def manual_link_transaction(
    workspace_id: PathWorkspaceId,
    transaction_id: int,
    data: ManualLinkRequest,
    session: SessionDep,
) -> CorrectionResponse:
    """Manually link a transaction to existing instrument/listing/fund/listing.

    Existing identity only — never creates an instrument, never calls a resolver.
    """
    response = await corrections_service.manual_link_transaction(
        session,
        workspace_id,
        transaction_id,
        instrument_id=data.instrument_id,
        instrument_listing_id=data.instrument_listing_id,
        fund_id=data.fund_id,
        fund_listing_id=data.fund_listing_id,
        correction_reason=data.correction_reason,
    )
    await session.commit()
    return response


@workspace_router.post(
    "/transactions/{transaction_id}/clear-link", response_model=CorrectionResponse
)
async def clear_transaction_link(
    workspace_id: PathWorkspaceId,
    transaction_id: int,
    data: ClearLinkRequest,
    session: SessionDep,
) -> CorrectionResponse:
    """Clear a mistaken manual/automatic link (canonical instrument untouched)."""
    response = await corrections_service.clear_transaction_link(
        session,
        workspace_id,
        transaction_id,
        correction_reason=data.correction_reason,
        reset_status=data.reset_status,
    )
    await session.commit()
    return response


@workspace_router.post("/transactions/{transaction_id}/ignore", response_model=CorrectionResponse)
async def ignore_transaction(
    workspace_id: PathWorkspaceId,
    transaction_id: int,
    data: CorrectionActionRequest,
    session: SessionDep,
) -> CorrectionResponse:
    """Mark a transaction ignored (excluded from reconciliation, still auditable)."""
    response = await corrections_service.ignore_transaction(
        session, workspace_id, transaction_id, correction_reason=data.correction_reason
    )
    await session.commit()
    return response


@workspace_router.post(
    "/transactions/{transaction_id}/manual-review", response_model=CorrectionResponse
)
async def mark_transaction_manual_review(
    workspace_id: PathWorkspaceId,
    transaction_id: int,
    data: CorrectionActionRequest,
    session: SessionDep,
) -> CorrectionResponse:
    """Park a transaction for manual review (kept in the ledger, flagged)."""
    response = await corrections_service.mark_transaction_manual_review(
        session, workspace_id, transaction_id, correction_reason=data.correction_reason
    )
    await session.commit()
    return response


@workspace_router.post("/transactions/resolve", response_model=ResolveTransactionsResponse)
async def resolve_transactions(
    workspace_id: PathWorkspaceId, data: ResolveTransactionsRequest, session: SessionDep
) -> ResolveTransactionsResponse:
    """Resolve unresolved imported transactions to canonical instruments + relink.

    Offline fixture by default; ``dry_run=true`` writes nothing (preview only).
    """
    result = await resolution_service.resolve_imported_instruments(
        session,
        workspace_id=workspace_id,
        broker_import_id=data.broker_import_id,
        broker_account_id=data.broker_account_id,
        transaction_id=data.transaction_id,
        limit=data.limit,
        source=data.source,
        dry_run=data.dry_run,
    )
    if not data.dry_run:
        await session.commit()
    source_name = get_constituent_resolver(data.source).name
    return _resolution_response(workspace_id, source_name, result, dry_run=data.dry_run)


@workspace_router.post(
    "/broker-imports/{import_id}/resolve", response_model=ResolveTransactionsResponse
)
async def resolve_broker_import_transactions(
    workspace_id: PathWorkspaceId,
    import_id: int,
    data: ResolveTransactionsRequest,
    session: SessionDep,
) -> ResolveTransactionsResponse:
    """Resolve a single import's unresolved transactions (offline fixture default)."""
    result = await resolution_service.resolve_imported_instruments(
        session,
        workspace_id=workspace_id,
        broker_import_id=import_id,
        limit=data.limit,
        source=data.source,
        dry_run=data.dry_run,
    )
    if not data.dry_run:
        await session.commit()
    source_name = get_constituent_resolver(data.source).name
    return _resolution_response(workspace_id, source_name, result, dry_run=data.dry_run)


@workspace_router.get("/positions", response_model=PositionsResponse)
async def list_positions(workspace_id: PathWorkspaceId, session: SessionDep) -> PositionsResponse:
    """Derived positions (buys − sells per instrument) + cash per currency.

    Bounded SQL reconciliation over committed transactions — not PnL / valuation.
    """
    return await service.reconcile_positions(session, workspace_id)
