"""Broker CSV import + transaction-ledger + position-reconciliation schemas."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from pydantic import BaseModel

from app.schemas.common import DecimalStr, ORMModel

# --- request -----------------------------------------------------------------


class BrokerImportRequest(BaseModel):
    """Preview/commit request. The CSV is supplied inline as text (a real
    multipart upload can be added later without changing this contract)."""

    # Parser/format name (currently only ``generic_csv_v1``).
    broker_name: str = "generic_csv_v1"
    source_filename: str | None = None
    csv_text: str
    account_label: str | None = None
    account_currency: str | None = None
    # Honoured by the preview endpoint (commit if true); the commit endpoint
    # always commits regardless of this flag.
    commit: bool = False


# --- preview / commit response ----------------------------------------------


class TransactionPreviewRow(BaseModel):
    """One parsed CSV row projected for the GUI (preview *and* commit)."""

    row_number: int
    parse_status: str  # parsed | warning | failed | skipped
    parse_error: str | None = None
    warnings: list[str] = []
    transaction_type: str | None = None
    trade_date: date | None = None
    settle_date: date | None = None
    symbol: str | None = None
    isin: str | None = None
    name: str | None = None
    quantity: DecimalStr | None = None
    price: DecimalStr | None = None
    gross_amount: DecimalStr | None = None
    fees: DecimalStr | None = None
    taxes: DecimalStr | None = None
    net_amount: DecimalStr | None = None
    currency: str | None = None
    # resolved | unresolved_instrument | cash | n/a
    resolution_status: str = "n/a"
    # fund_listing | fund | instrument | instrument_listing | None
    resolved_kind: str | None = None
    fund_id: int | None = None
    fund_listing_id: int | None = None
    instrument_id: int | None = None
    instrument_listing_id: int | None = None
    # Set after commit (the persisted canonical transaction).
    transaction_id: int | None = None


class BrokerImportSummary(BaseModel):
    parser: str
    source_filename: str | None = None
    source_hash: str
    row_count: int = 0
    parsed_count: int = 0
    error_count: int = 0
    warning_count: int = 0
    transaction_count: int = 0
    unresolved_count: int = 0
    cash_movement_count: int = 0
    # previewed | committed | duplicate
    status: str = "previewed"
    duplicate: bool = False


class PositionSnapshotSummary(BaseModel):
    snapshot_id: int | None = None
    as_of_date: date
    status: str  # ok | partial | empty
    input_hash: str
    transaction_count: int = 0
    unresolved_count: int = 0
    position_count: int = 0
    # Whether this commit wrote a *new* snapshot (idempotent recompute => False).
    created: bool = False


class BrokerImportResponse(BaseModel):
    """Unified preview/commit response. ``committed=false`` => nothing written."""

    committed: bool
    duplicate: bool = False
    import_id: int | None = None
    committed_at: datetime | None = None
    summary: BrokerImportSummary
    transactions: list[TransactionPreviewRow] = []
    errors: list[str] = []
    position_snapshot: PositionSnapshotSummary | None = None


# --- import history reads -----------------------------------------------------


class BrokerImportRead(ORMModel):
    id: int
    workspace_id: int
    broker_account_id: int | None
    broker_name: str
    source_filename: str | None
    source_hash: str
    status: str
    row_count: int
    parsed_count: int
    error_count: int
    transaction_count: int
    unresolved_count: int
    cash_movement_count: int
    committed_at: datetime | None
    created_at: datetime
    updated_at: datetime


class BrokerImportRowRead(ORMModel):
    id: int
    row_number: int
    parse_status: str
    parse_error: str | None
    canonical_transaction_id: int | None
    created_at: datetime


class BrokerImportDetailRead(BrokerImportRead):
    rows: list[BrokerImportRowRead] = []


# --- transaction ledger reads -------------------------------------------------


class TransactionRead(ORMModel):
    id: int
    workspace_id: int
    broker_account_id: int | None
    broker_import_id: int | None
    transaction_key: str
    transaction_type: str
    trade_date: date
    settle_date: date | None
    instrument_id: int | None
    instrument_listing_id: int | None
    fund_id: int | None
    fund_listing_id: int | None
    symbol: str | None
    isin: str | None
    figi: str | None
    name: str | None
    quantity: DecimalStr | None
    price: DecimalStr | None
    gross_amount: DecimalStr | None
    fees: DecimalStr | None
    taxes: DecimalStr | None
    net_amount: DecimalStr | None
    currency: str
    cash_currency: str | None
    fx_rate: DecimalStr | None
    source: str
    status: str
    notes: str | None
    created_at: datetime
    updated_at: datetime


# --- position reconciliation reads -------------------------------------------


class ReconciledPosition(BaseModel):
    # Stable grouping key (instrument:N | fund_listing:N | symbol:X | isin:X | ...).
    key: str
    # resolved | unresolved_instrument
    resolution_status: str
    instrument_id: int | None = None
    instrument_listing_id: int | None = None
    fund_id: int | None = None
    fund_listing_id: int | None = None
    symbol: str | None = None
    isin: str | None = None
    name: str | None = None
    currency: str | None = None
    quantity: DecimalStr
    buy_quantity: DecimalStr
    sell_quantity: DecimalStr
    fees_total: DecimalStr
    taxes_total: DecimalStr
    transaction_count: int


class CashBalance(BaseModel):
    currency: str
    amount: DecimalStr
    transaction_count: int


class PositionsResponse(BaseModel):
    """Derived (buys − sells per instrument; cash per currency) — NOT PnL.

    Computed by bounded SQL aggregation over committed transactions; the latest
    persisted reconciliation snapshot's identity is echoed for the GUI.
    """

    workspace_id: int
    as_of_date: date
    base_currency: str
    transaction_count: int = 0
    unresolved_count: int = 0
    positions: list[ReconciledPosition] = []
    cash: list[CashBalance] = []
    snapshot_id: int | None = None
    snapshot_status: str = "empty"
    input_hash: str | None = None


# --- imported-instrument resolution ------------------------------------------


class ResolveTransactionsRequest(BaseModel):
    """Resolve unresolved imported transactions to canonical instruments.

    ``source`` selects the resolver (offline ``constituent_identity_fixture`` by
    default; ``openfigi`` for the live, budget-guarded path). ``dry_run=true``
    builds requests/candidates and reports outcomes but writes nothing. The
    optional scopes narrow the unresolved set within the workspace.
    """

    source: str | None = None
    limit: int | None = 200
    dry_run: bool = False
    broker_import_id: int | None = None
    broker_account_id: int | None = None
    transaction_id: int | None = None


class ResolveTransactionsResponse(BaseModel):
    """Outcome counts of a resolution run (or dry-run preview)."""

    workspace_id: int
    source: str
    dry_run: bool
    transactions_selected: int = 0
    linked: int = 0
    linked_existing: int = 0
    candidates_resolved: int = 0
    ambiguous: int = 0
    not_found: int = 0
    failed: int = 0
    skipped_unsafe: int = 0
    skipped_budget: int = 0
    skipped_cached: int = 0
    instruments_created: int = 0
    listings_created: int = 0
    identifiers_created: int = 0
    snapshot_created: bool = False
    message: str = ""


# --- manual correction workflows ---------------------------------------------
#
# Operator-driven cleanup of unresolved / ambiguous / mistakenly-linked imported
# transactions. These never create an instrument, never call OpenFIGI / a live
# provider / a resolver, and never name-only guess a link (see
# app/services/transaction_corrections.py + AGENTS.md).


class TransactionLinks(BaseModel):
    """The four canonical instrument/fund link targets carried by a transaction."""

    instrument_id: int | None = None
    instrument_listing_id: int | None = None
    fund_id: int | None = None
    fund_listing_id: int | None = None


class ManualLinkRequest(BaseModel):
    """Explicitly link a transaction to *existing* canonical identity.

    At least one target id is required. Targets must exist; a supplied listing must
    belong to a supplied instrument/fund; instrument and fund targets cannot be
    mixed. Never creates an instrument, never calls a resolver/OpenFIGI/live source.
    """

    instrument_id: int | None = None
    instrument_listing_id: int | None = None
    fund_id: int | None = None
    fund_listing_id: int | None = None
    correction_reason: str | None = None


class ClearLinkRequest(BaseModel):
    """Clear a manual/automatic link (resets links to NULL).

    The canonical instrument/listing/fund is never deleted — only the transaction's
    FK is cleared. ``reset_status`` chooses the post-clear status:
    ``unresolved_instrument`` (default) or ``manual_review``.
    """

    correction_reason: str | None = None
    reset_status: str | None = None


class CorrectionActionRequest(BaseModel):
    """Body for the ignore / manual-review actions (reason only)."""

    correction_reason: str | None = None


class CorrectionResponse(BaseModel):
    """Outcome of one manual correction (link / clear / ignore / manual_review)."""

    transaction_id: int
    action: str  # manual_link | clear_link | ignore | manual_review
    changed: bool
    old_status: str
    new_status: str
    old_links: TransactionLinks
    new_links: TransactionLinks
    correction_reason: str | None = None
    position_snapshot_id: int | None = None
    position_snapshot_updated: bool = False
    valuation_recompute_needed: bool = False
    market_data_plan_changed: bool = False
    recommended_actions: list[str] = []


class CorrectionCandidate(BaseModel):
    """One bounded candidate target for a manual link (never a name-only guess)."""

    # fund | fund_listing | instrument | instrument_listing
    kind: str
    # isin | figi | ticker
    matched_on: str
    matched_value: str | None = None
    label: str | None = None
    instrument_id: int | None = None
    instrument_listing_id: int | None = None
    fund_id: int | None = None
    fund_listing_id: int | None = None
    currency: str | None = None
    # For ticker matches: whether the candidate's currency equals the txn currency.
    same_currency: bool | None = None


class CorrectionContextResponse(BaseModel):
    """Bounded context to help a human choose a link for one transaction."""

    transaction_id: int
    workspace_id: int
    status: str
    transaction_type: str
    symbol: str | None = None
    isin: str | None = None
    figi: str | None = None
    name: str | None = None
    currency: str | None = None
    # True when the row carries only a broker name (no safe identifier) — such rows
    # are never auto-linked and never name-only guessed.
    name_only: bool = False
    current_links: TransactionLinks
    # The safe auto-resolution against *existing* identity (None if none/ambiguous):
    # what the resolver bridge would link, offered as the recommended target.
    suggested_link: TransactionLinks | None = None
    identifier_candidates: list[CorrectionCandidate] = []
    ticker_candidates: list[CorrectionCandidate] = []
    # The most recent stored resolver outcome (ambiguous/not_found/failed), if any.
    recent_resolver_outcome: dict[str, Any] | None = None
    # The most recent manual correction recorded on this row (provenance), if any.
    last_correction: dict[str, Any] | None = None
    recommended_action: str
