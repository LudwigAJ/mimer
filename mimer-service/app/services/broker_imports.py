"""Broker CSV import ingestion + transaction ledger + position reconciliation.

The provider-agnostic ingestion half of broker import (the parser adapter lives
in ``app/sources/broker_imports.py``). It:

* **previews** a CSV (parse + duplicate check + per-row resolution) writing
  nothing;
* **commits** a CSV into canonical ``portfolio_transactions`` (idempotent by
  ``(workspace_id, transaction_key, source)``), recording the ``broker_import`` +
  raw ``broker_import_rows`` for provenance, then writing an idempotent
  ``portfolio_position_snapshots`` reconciliation;
* **reconciles** committed transactions into a bounded position/cash read model
  (buys − sells per instrument; signed cash flow per currency) — NOT PnL.

Compute boundary (see AGENTS.md): persistence + bounded SQL aggregation only.
Instrument resolution is **best-effort against existing identity** — ISIN, then
FIGI, then a unique ticker(+currency) — with **no live resolver calls and no
name-only guessing**. An unresolvable row is stored with
``status=unresolved_instrument`` (never dropped, never a wrong link).
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.errors import NotFoundError
from app.db.models import (
    BrokerAccount,
    BrokerImport,
    BrokerImportRow,
    Fund,
    FundListing,
    InstrumentIdentifier,
    InstrumentListing,
    PortfolioPositionSnapshot,
    PortfolioPositionSnapshotRow,
    PortfolioTransaction,
    SecurityIdentifier,
)
from app.schemas.broker_import import (
    BrokerImportResponse,
    BrokerImportSummary,
    CashBalance,
    PositionSnapshotSummary,
    PositionsResponse,
    ReconciledPosition,
    TransactionPreviewRow,
)
from app.services import workspaces as workspaces_service
from app.sources.broker_imports import (
    BUY,
    CASH_DEPOSIT,
    CASH_WITHDRAWAL,
    DIVIDEND,
    FEE,
    INTEREST,
    SELL,
    TAX,
    BrokerImportParseResult,
    ParsedBrokerRow,
    ParsedTransaction,
    compute_source_hash,
    get_broker_parser,
)

SOURCE = "broker_csv"
# Bounds (compute boundary): cap rows projected to the GUI and the working set.
MAX_PREVIEW_ROWS = 1000
MAX_IMPORT_ROWS = 10000
DEFAULT_TRANSACTION_LIMIT = 200
MAX_TRANSACTION_LIMIT = 1000
_ZERO = Decimal("0")

# Manual-correction statuses (see ``app.services.transaction_corrections``).
#   ``manual_review`` — a human parked the row for later attention (in the ledger,
#                       still unlinked, so its position stays flagged like an
#                       unresolved row, but it is NOT an urgent auto-resolve item).
#   ``ignored``       — a human excluded the row from the portfolio entirely; it
#                       leaves reconciliation/valuation but stays auditable.
MANUAL_REVIEW_STATUS = "manual_review"
IGNORED_STATUS = "ignored"

# Transaction statuses that participate in the ledger reconciliation. ``resolved``
# / ``ready`` are set by the imported-instrument resolution bridge once a once-
# unresolved transaction gains a canonical instrument link (see
# ``app.services.imported_instrument_resolution``); they behave like ``committed``
# for reconciliation but carry the cleaner status for the GUI / status filter.
# ``manual_review`` is a *parked* unresolved row (counted, kept in the ledger);
# ``ignored`` is deliberately absent so an ignored row drops out of reconciliation.
LEDGER_STATUSES = (
    "committed",
    "resolved",
    "ready",
    "unresolved_instrument",
    "ambiguous_instrument",
    MANUAL_REVIEW_STATUS,
)
# Statuses where a transaction is in the ledger but NOT linked to an instrument
# (so its reconciled position stays flagged for manual / resolver attention).
UNLINKED_STATUSES = ("unresolved_instrument", "ambiguous_instrument", MANUAL_REVIEW_STATUS)


# --- instrument resolution (existing identity only; no live calls) -----------


@dataclass
class ResolvedLink:
    kind: str | None = None  # fund_listing | fund | instrument | instrument_listing
    fund_id: int | None = None
    fund_listing_id: int | None = None
    instrument_id: int | None = None
    instrument_listing_id: int | None = None

    @property
    def resolved(self) -> bool:
        return any(
            v is not None
            for v in (
                self.fund_id,
                self.fund_listing_id,
                self.instrument_id,
                self.instrument_listing_id,
            )
        )


_FIGI_SCHEMES = ("figi", "composite_figi", "share_class_figi")


class _ResolutionIndex:
    """Batch-built crosswalk maps so resolution is O(1)/row, not a DB call/row."""

    def __init__(self) -> None:
        self.funds_by_isin: dict[str, Fund] = {}
        self.secid_by_isin: dict[str, SecurityIdentifier] = {}
        self.instrument_id_by_isin: dict[str, int] = {}
        self.instrument_id_by_figi: dict[str, int] = {}
        self.fund_listings_by_ticker: dict[str, list[FundListing]] = {}
        self.fund_listings_by_figi: dict[str, FundListing] = {}
        self.instr_listings_by_ticker: dict[str, list[InstrumentListing]] = {}
        self.instr_listings_by_figi: dict[str, InstrumentListing] = {}
        self.fund_listings_by_fund: dict[int, list[FundListing]] = {}

    def resolve(
        self, *, isin: str | None, figi: str | None, symbol: str | None, currency: str | None
    ) -> ResolvedLink:
        if isin:
            link = self._resolve_isin(isin.upper(), symbol, currency)
            if link is not None:
                return link
        if figi:
            link = self._resolve_figi(figi.upper())
            if link is not None:
                return link
        if symbol:
            link = self._resolve_ticker(symbol.upper(), currency)
            if link is not None:
                return link
        return ResolvedLink()

    def _resolve_isin(
        self, isin: str, symbol: str | None, currency: str | None
    ) -> ResolvedLink | None:
        fund = self.funds_by_isin.get(isin)
        if fund is not None:
            listing = self._pick_fund_listing(fund.id, symbol, currency)
            if listing is not None:
                return ResolvedLink("fund_listing", fund_id=fund.id, fund_listing_id=listing.id)
            return ResolvedLink("fund", fund_id=fund.id)
        sid = self.secid_by_isin.get(isin)
        if sid is not None and (sid.fund_id or sid.fund_listing_id):
            kind = "fund_listing" if sid.fund_listing_id else "fund"
            return ResolvedLink(kind, fund_id=sid.fund_id, fund_listing_id=sid.fund_listing_id)
        instrument_id = self.instrument_id_by_isin.get(isin)
        if instrument_id is not None:
            return ResolvedLink("instrument", instrument_id=instrument_id)
        return None

    def _resolve_figi(self, figi: str) -> ResolvedLink | None:
        fl = self.fund_listings_by_figi.get(figi)
        if fl is not None:
            return ResolvedLink("fund_listing", fund_id=fl.fund_id, fund_listing_id=fl.id)
        il = self.instr_listings_by_figi.get(figi)
        if il is not None:
            return ResolvedLink(
                "instrument_listing", instrument_id=il.instrument_id, instrument_listing_id=il.id
            )
        instrument_id = self.instrument_id_by_figi.get(figi)
        if instrument_id is not None:
            return ResolvedLink("instrument", instrument_id=instrument_id)
        return None

    def _resolve_ticker(self, symbol: str, currency: str | None) -> ResolvedLink | None:
        fund_listing = _unique_listing(
            self.fund_listings_by_ticker.get(symbol, []),
            currency,
            currency_of=lambda fl: fl.trading_currency or fl.currency_unit,
        )
        if fund_listing is not None:
            return ResolvedLink(
                "fund_listing", fund_id=fund_listing.fund_id, fund_listing_id=fund_listing.id
            )
        instr_listing = _unique_listing(
            self.instr_listings_by_ticker.get(symbol, []),
            currency,
            currency_of=lambda il: il.currency,
        )
        if instr_listing is not None:
            return ResolvedLink(
                "instrument_listing",
                instrument_id=instr_listing.instrument_id,
                instrument_listing_id=instr_listing.id,
            )
        return None

    def _pick_fund_listing(
        self, fund_id: int, symbol: str | None, currency: str | None
    ) -> FundListing | None:
        listings = self.fund_listings_by_fund.get(fund_id, [])
        if symbol:
            listings = [fl for fl in listings if (fl.ticker or "").upper() == symbol.upper()]
        return _unique_listing(
            listings, currency, currency_of=lambda fl: fl.trading_currency or fl.currency_unit
        )


def _unique_listing(listings, currency, *, currency_of):  # type: ignore[no-untyped-def]
    """Return the single matching listing, or None when ambiguous (never guess)."""
    if not listings:
        return None
    if len(listings) == 1:
        return listings[0]
    if currency:
        cur = currency.upper()
        matches = [ln for ln in listings if (currency_of(ln) or "").upper() == cur]
        if len(matches) == 1:
            return matches[0]
    return None


async def build_resolution_index(
    session: AsyncSession, transactions: Sequence[ParsedTransaction | PortfolioTransaction]
) -> _ResolutionIndex:
    """Public alias of the batch crosswalk builder.

    Reused by ``app.services.imported_instrument_resolution`` to re-check an
    *already-committed* unresolved transaction against existing identity (a
    constituent resolved since import, a newly added fund/listing) before it ever
    calls a resolver. Works on any object exposing ``isin``/``figi``/``symbol``/
    ``currency`` (ParsedTransaction *and* PortfolioTransaction)."""
    return await _build_resolution_index(session, transactions)


async def _build_resolution_index(
    session: AsyncSession, transactions: Sequence[ParsedTransaction | PortfolioTransaction]
) -> _ResolutionIndex:
    index = _ResolutionIndex()
    isins = {t.isin.upper() for t in transactions if t.isin}
    figis = {t.figi.upper() for t in transactions if t.figi}
    symbols = {t.symbol.upper() for t in transactions if t.symbol}
    if not (isins or figis or symbols):
        return index

    if isins:
        for fund in (
            (await session.execute(select(Fund).where(Fund.isin.in_(isins)))).scalars().all()
        ):
            if fund.isin:
                index.funds_by_isin[fund.isin.upper()] = fund
        for sid in (
            (
                await session.execute(
                    select(SecurityIdentifier).where(
                        SecurityIdentifier.scheme == "isin", SecurityIdentifier.value.in_(isins)
                    )
                )
            )
            .scalars()
            .all()
        ):
            index.secid_by_isin.setdefault(sid.value.upper(), sid)
        for ident in (
            (
                await session.execute(
                    select(InstrumentIdentifier).where(
                        InstrumentIdentifier.scheme == "isin",
                        InstrumentIdentifier.value.in_(isins),
                    )
                )
            )
            .scalars()
            .all()
        ):
            index.instrument_id_by_isin.setdefault(ident.value.upper(), ident.instrument_id)

    if figis:
        for ident in (
            (
                await session.execute(
                    select(InstrumentIdentifier).where(
                        InstrumentIdentifier.scheme.in_(_FIGI_SCHEMES),
                        InstrumentIdentifier.value.in_(figis),
                    )
                )
            )
            .scalars()
            .all()
        ):
            index.instrument_id_by_figi.setdefault(ident.value.upper(), ident.instrument_id)

    # Fund listings (by ticker / figi / fund).
    fund_listing_rows = (
        (await session.execute(select(FundListing))).scalars().all() if symbols or figis else []
    )
    for fl in fund_listing_rows:
        if fl.ticker and fl.ticker.upper() in symbols:
            index.fund_listings_by_ticker.setdefault(fl.ticker.upper(), []).append(fl)
            index.fund_listings_by_fund.setdefault(fl.fund_id, []).append(fl)
        if fl.figi and fl.figi.upper() in figis:
            index.fund_listings_by_figi.setdefault(fl.figi.upper(), fl)
    # Ensure every resolvable fund's listings are available for ISIN+ticker pick.
    if index.funds_by_isin:
        fund_ids = {f.id for f in index.funds_by_isin.values()}
        for fl in (
            (await session.execute(select(FundListing).where(FundListing.fund_id.in_(fund_ids))))
            .scalars()
            .all()
        ):
            bucket = index.fund_listings_by_fund.setdefault(fl.fund_id, [])
            if fl not in bucket:
                bucket.append(fl)

    if symbols or figis:
        for il in (await session.execute(select(InstrumentListing))).scalars().all():
            if il.ticker and il.ticker.upper() in symbols:
                index.instr_listings_by_ticker.setdefault(il.ticker.upper(), []).append(il)
            for value in (il.figi, il.composite_figi, il.share_class_figi):
                if value and value.upper() in figis:
                    index.instr_listings_by_figi.setdefault(value.upper(), il)
    return index


# --- preview / commit --------------------------------------------------------


def _expects_instrument(txn: ParsedTransaction) -> bool:
    """Whether a transaction should carry an instrument (so a miss is 'unresolved')."""
    return txn.is_trade or bool(txn.symbol or txn.isin or txn.figi)


def _resolution_status(txn: ParsedTransaction, link: ResolvedLink) -> str:
    if not _expects_instrument(txn):
        return "cash"
    return "resolved" if link.resolved else "unresolved_instrument"


def _transaction_status(resolution_status: str) -> str:
    return "unresolved_instrument" if resolution_status == "unresolved_instrument" else "committed"


def _content_hash(txn: ParsedTransaction, account_label: str | None) -> str:
    parts = [
        account_label or "",
        txn.trade_date.isoformat(),
        txn.transaction_type,
        txn.symbol or "",
        txn.isin or "",
        _num(txn.quantity),
        _num(txn.price),
        _num(txn.net_amount if txn.net_amount is not None else txn.gross_amount),
        txn.currency,
    ]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def _num(value: Decimal | None) -> str:
    return format(value, "f") if value is not None else ""


def _preview_row(
    parsed: ParsedBrokerRow, link: ResolvedLink, resolution_status: str
) -> TransactionPreviewRow:
    txn = parsed.transaction
    if txn is None:
        return TransactionPreviewRow(
            row_number=parsed.row_number,
            parse_status=parsed.parse_status,
            parse_error=parsed.parse_error,
            warnings=parsed.warnings,
        )
    return TransactionPreviewRow(
        row_number=parsed.row_number,
        parse_status=parsed.parse_status,
        parse_error=parsed.parse_error,
        warnings=parsed.warnings,
        transaction_type=txn.transaction_type,
        trade_date=txn.trade_date,
        settle_date=txn.settle_date,
        symbol=txn.symbol,
        isin=txn.isin,
        name=txn.name,
        quantity=txn.quantity,
        price=txn.price,
        gross_amount=txn.gross_amount,
        fees=txn.fees,
        taxes=txn.taxes,
        net_amount=txn.net_amount,
        currency=txn.currency,
        resolution_status=resolution_status,
        resolved_kind=link.kind,
        fund_id=link.fund_id,
        fund_listing_id=link.fund_listing_id,
        instrument_id=link.instrument_id,
        instrument_listing_id=link.instrument_listing_id,
    )


def _summary(
    parse_result: BrokerImportParseResult,
    *,
    source_hash: str,
    source_filename: str | None,
    unresolved_count: int,
    status: str,
    duplicate: bool,
) -> BrokerImportSummary:
    return BrokerImportSummary(
        parser=parse_result.parser_name,
        source_filename=source_filename,
        source_hash=source_hash,
        row_count=parse_result.row_count,
        parsed_count=parse_result.parsed_count,
        error_count=parse_result.error_count,
        warning_count=parse_result.warning_count,
        transaction_count=parse_result.parsed_count,
        unresolved_count=unresolved_count,
        cash_movement_count=parse_result.cash_movement_count,
        status=status,
        duplicate=duplicate,
    )


async def preview_import(
    session: AsyncSession, workspace_id: int, *, request
) -> BrokerImportResponse:
    """Parse + resolve a CSV and report outcomes — writes nothing."""
    await workspaces_service.get_workspace(session, workspace_id)
    parser = get_broker_parser(request.broker_name)
    parse_result = parser.parse(request.csv_text)
    source_hash = compute_source_hash(parser.name, request.csv_text)

    existing = await _existing_import(session, workspace_id, source_hash)
    duplicate = existing is not None and existing.status == "committed"

    index = await _build_resolution_index(
        session, [r.transaction for r in parse_result.parsed_rows if r.transaction]
    )
    preview_rows: list[TransactionPreviewRow] = []
    unresolved = 0
    errors = [
        f"row {r.row_number}: {r.parse_error}"
        for r in parse_result.rows
        if r.parse_status == "failed"
    ]
    for parsed in parse_result.rows:
        txn = parsed.transaction
        if txn is None:
            preview_rows.append(_preview_row(parsed, ResolvedLink(), "n/a"))
            continue
        link = index.resolve(isin=txn.isin, figi=txn.figi, symbol=txn.symbol, currency=txn.currency)
        resolution_status = _resolution_status(txn, link)
        if resolution_status == "unresolved_instrument":
            unresolved += 1
        preview_rows.append(_preview_row(parsed, link, resolution_status))

    return BrokerImportResponse(
        committed=False,
        duplicate=duplicate,
        import_id=existing.id if (existing is not None and duplicate) else None,
        summary=_summary(
            parse_result,
            source_hash=source_hash,
            source_filename=request.source_filename,
            unresolved_count=unresolved,
            status="duplicate" if duplicate else "previewed",
            duplicate=duplicate,
        ),
        transactions=preview_rows[:MAX_PREVIEW_ROWS],
        errors=errors,
    )


async def commit_import(
    session: AsyncSession, workspace_id: int, *, request
) -> BrokerImportResponse:
    """Idempotently persist a CSV import into the canonical ledger + reconcile."""
    await workspaces_service.get_workspace(session, workspace_id)
    parser = get_broker_parser(request.broker_name)
    parse_result = parser.parse(request.csv_text)
    if parse_result.row_count > MAX_IMPORT_ROWS:
        raise NotFoundError(
            f"Import exceeds the {MAX_IMPORT_ROWS}-row limit", code="import_too_large"
        )
    source_hash = compute_source_hash(parser.name, request.csv_text)

    existing = await _existing_import(session, workspace_id, source_hash)
    if existing is not None and existing.status == "committed":
        # Idempotent: re-committing the same file returns the existing import.
        snapshot = await _latest_snapshot(session, workspace_id)
        return _committed_response(
            existing, parse_result, duplicate=True, snapshot=snapshot, snapshot_created=False
        )

    parsed_txns = [r.transaction for r in parse_result.parsed_rows if r.transaction]
    index = await _build_resolution_index(session, parsed_txns)

    account = await _get_or_create_account(
        session,
        workspace_id,
        broker_name=parser.name,
        label=request.account_label,
        currency=request.account_currency,
    )

    now = datetime.now(UTC)
    broker_import = BrokerImport(
        workspace_id=workspace_id,
        broker_account_id=account.id if account else None,
        broker_name=parser.name,
        source_filename=request.source_filename,
        source_hash=source_hash,
        status="committed",
        row_count=parse_result.row_count,
        parsed_count=parse_result.parsed_count,
        error_count=parse_result.error_count,
        committed_at=now,
    )
    session.add(broker_import)
    await session.flush()

    occurrences: dict[str, int] = {}
    preview_rows: list[TransactionPreviewRow] = []
    transaction_count = unresolved = cash_movements = 0

    for parsed in parse_result.rows:
        txn = parsed.transaction
        if txn is None:
            session.add(
                BrokerImportRow(
                    broker_import_id=broker_import.id,
                    row_number=parsed.row_number,
                    raw_row_json=parsed.raw,
                    parse_status=parsed.parse_status,
                    parse_error=parsed.parse_error,
                )
            )
            preview_rows.append(_preview_row(parsed, ResolvedLink(), "n/a"))
            continue

        link = index.resolve(isin=txn.isin, figi=txn.figi, symbol=txn.symbol, currency=txn.currency)
        resolution_status = _resolution_status(txn, link)
        account_id = await _account_id_for_row(
            session, workspace_id, parser.name, txn.broker_account, account
        )
        record = await _upsert_transaction(
            session,
            workspace_id=workspace_id,
            broker_import_id=broker_import.id,
            broker_account_id=account_id,
            txn=txn,
            link=link,
            resolution_status=resolution_status,
            occurrences=occurrences,
        )
        transaction_count += 1
        if resolution_status == "unresolved_instrument":
            unresolved += 1
        if txn.is_cash_movement:
            cash_movements += 1

        session.add(
            BrokerImportRow(
                broker_import_id=broker_import.id,
                row_number=parsed.row_number,
                raw_row_json=parsed.raw,
                parse_status=parsed.parse_status,
                parse_error=parsed.parse_error,
                canonical_transaction_id=record.id,
            )
        )
        row_view = _preview_row(parsed, link, resolution_status)
        row_view.transaction_id = record.id
        preview_rows.append(row_view)

    broker_import.transaction_count = transaction_count
    broker_import.unresolved_count = unresolved
    broker_import.cash_movement_count = cash_movements
    if parse_result.error_count and transaction_count:
        broker_import.status = "partial"

    await session.flush()
    snapshot, snapshot_created = await _write_position_snapshot(session, workspace_id)
    await session.commit()
    await session.refresh(broker_import)

    return _committed_response(
        broker_import,
        parse_result,
        duplicate=False,
        snapshot=snapshot,
        snapshot_created=snapshot_created,
        transactions=preview_rows,
        unresolved_count=unresolved,
    )


async def _upsert_transaction(
    session: AsyncSession,
    *,
    workspace_id: int,
    broker_import_id: int,
    broker_account_id: int | None,
    txn: ParsedTransaction,
    link: ResolvedLink,
    resolution_status: str,
    occurrences: dict[str, int],
) -> PortfolioTransaction:
    base = _content_hash(txn, txn.broker_account)
    occurrence = occurrences.get(base, 0)
    occurrences[base] = occurrence + 1
    transaction_key = f"{base[:40]}:{occurrence}"

    existing = await session.scalar(
        select(PortfolioTransaction).where(
            PortfolioTransaction.workspace_id == workspace_id,
            PortfolioTransaction.transaction_key == transaction_key,
            PortfolioTransaction.source == SOURCE,
        )
    )
    if existing is not None:
        # Same economic transaction already in the ledger — idempotent no-op.
        return existing

    record = PortfolioTransaction(
        workspace_id=workspace_id,
        broker_account_id=broker_account_id,
        broker_import_id=broker_import_id,
        transaction_key=transaction_key,
        transaction_type=txn.transaction_type,
        trade_date=txn.trade_date,
        settle_date=txn.settle_date,
        instrument_id=link.instrument_id,
        instrument_listing_id=link.instrument_listing_id,
        fund_id=link.fund_id,
        fund_listing_id=link.fund_listing_id,
        symbol=txn.symbol,
        isin=txn.isin,
        figi=txn.figi,
        name=txn.name,
        quantity=txn.quantity,
        price=txn.price,
        gross_amount=txn.gross_amount,
        fees=txn.fees,
        taxes=txn.taxes,
        net_amount=txn.net_amount,
        currency=txn.currency,
        cash_currency=txn.cash_currency,
        fx_rate=txn.fx_rate,
        source=SOURCE,
        status=_transaction_status(resolution_status),
        notes=txn.notes,
        raw_payload_json=None,
    )
    session.add(record)
    await session.flush()
    return record


def _committed_response(
    broker_import: BrokerImport,
    parse_result: BrokerImportParseResult,
    *,
    duplicate: bool,
    snapshot: PortfolioPositionSnapshot | None,
    snapshot_created: bool,
    transactions: list[TransactionPreviewRow] | None = None,
    unresolved_count: int | None = None,
) -> BrokerImportResponse:
    unresolved = (
        unresolved_count if unresolved_count is not None else broker_import.unresolved_count
    )
    summary = _summary(
        parse_result,
        source_hash=broker_import.source_hash,
        source_filename=broker_import.source_filename,
        unresolved_count=unresolved,
        status="duplicate" if duplicate else broker_import.status,
        duplicate=duplicate,
    )
    summary.transaction_count = broker_import.transaction_count
    summary.cash_movement_count = broker_import.cash_movement_count
    snapshot_summary = _snapshot_summary(snapshot, created=snapshot_created)
    return BrokerImportResponse(
        committed=True,
        duplicate=duplicate,
        import_id=broker_import.id,
        committed_at=broker_import.committed_at,
        summary=summary,
        transactions=(transactions or [])[:MAX_PREVIEW_ROWS],
        position_snapshot=snapshot_summary,
    )


# --- broker accounts ---------------------------------------------------------


async def _get_or_create_account(
    session: AsyncSession,
    workspace_id: int,
    *,
    broker_name: str,
    label: str | None,
    currency: str | None,
) -> BrokerAccount | None:
    if not label:
        return None
    existing = await session.scalar(
        select(BrokerAccount).where(
            BrokerAccount.workspace_id == workspace_id,
            BrokerAccount.broker_name == broker_name,
            BrokerAccount.account_label == label,
        )
    )
    if existing is not None:
        return existing
    account = BrokerAccount(
        workspace_id=workspace_id,
        broker_name=broker_name,
        account_label=label,
        account_currency=(currency.upper() if currency else None),
    )
    session.add(account)
    await session.flush()
    return account


async def _account_id_for_row(
    session: AsyncSession,
    workspace_id: int,
    broker_name: str,
    row_label: str | None,
    default_account: BrokerAccount | None,
) -> int | None:
    if not row_label:
        return default_account.id if default_account else None
    account = await _get_or_create_account(
        session, workspace_id, broker_name=broker_name, label=row_label, currency=None
    )
    return account.id if account else None


# --- position reconciliation (bounded SQL aggregation; NOT PnL) --------------


@dataclass
class _PositionAgg:
    key: str
    resolution_status: str
    instrument_id: int | None = None
    instrument_listing_id: int | None = None
    fund_id: int | None = None
    fund_listing_id: int | None = None
    symbol: str | None = None
    isin: str | None = None
    name: str | None = None
    currency: str | None = None
    quantity: Decimal = _ZERO
    buy_quantity: Decimal = _ZERO
    sell_quantity: Decimal = _ZERO
    fees_total: Decimal = _ZERO
    taxes_total: Decimal = _ZERO
    transaction_count: int = 0


@dataclass
class Reconciliation:
    positions: list[_PositionAgg] = field(default_factory=list)
    cash: dict[str, list[Decimal | int]] = field(default_factory=dict)
    transaction_count: int = 0
    unresolved_count: int = 0
    input_hash: str = ""

    @property
    def status(self) -> str:
        if self.transaction_count == 0:
            return "empty"
        return "partial" if self.unresolved_count else "ok"


def _position_key(txn: PortfolioTransaction) -> str:
    if txn.instrument_id is not None:
        return f"instrument:{txn.instrument_id}"
    if txn.instrument_listing_id is not None:
        return f"instrument_listing:{txn.instrument_listing_id}"
    if txn.fund_listing_id is not None:
        return f"fund_listing:{txn.fund_listing_id}"
    if txn.fund_id is not None:
        return f"fund:{txn.fund_id}"
    if txn.isin:
        return f"isin:{txn.isin.upper()}"
    if txn.symbol:
        return f"symbol:{txn.symbol.upper()}"
    return f"txn:{txn.id}"


def _signed_cash(txn: PortfolioTransaction) -> Decimal | None:
    if txn.net_amount is not None:
        return txn.net_amount
    if txn.gross_amount is None:
        return None
    amount = abs(txn.gross_amount)
    if txn.transaction_type in (SELL, DIVIDEND, CASH_DEPOSIT, INTEREST):
        return amount
    if txn.transaction_type in (BUY, FEE, TAX, CASH_WITHDRAWAL):
        return -amount
    return txn.gross_amount


async def _committed_transactions(
    session: AsyncSession, workspace_id: int, *, broker_account_id: int | None = None
) -> list[PortfolioTransaction]:
    stmt = (
        select(PortfolioTransaction)
        .where(
            PortfolioTransaction.workspace_id == workspace_id,
            PortfolioTransaction.status.in_(LEDGER_STATUSES),
        )
        .order_by(PortfolioTransaction.id)
    )
    if broker_account_id is not None:
        stmt = stmt.where(PortfolioTransaction.broker_account_id == broker_account_id)
    return list((await session.execute(stmt)).scalars().all())


async def committed_transactions(
    session: AsyncSession, workspace_id: int, *, broker_account_id: int | None = None
) -> list[PortfolioTransaction]:
    """Public: the in-ledger committed transactions for a workspace.

    Optionally scoped to one broker account. Reused by the portfolio-valuation
    read model so it values the *same* bounded transaction set the reconciliation
    snapshot does, never a duplicate ledger query."""
    return await _committed_transactions(session, workspace_id, broker_account_id=broker_account_id)


def reconcile_transactions(transactions: list[PortfolioTransaction]) -> Reconciliation:
    """Public: bounded in-memory reconciliation (buys − sells per instrument; cash
    per currency) over an already-bounded transaction set. Reused by portfolio
    valuation so it never re-implements the position/cash aggregation."""
    return _reconcile(transactions)


def transaction_position_key(txn: PortfolioTransaction) -> str:
    """Public alias of the deterministic per-transaction position grouping key."""
    return _position_key(txn)


def _reconcile(transactions: list[PortfolioTransaction]) -> Reconciliation:
    """Bounded in-memory aggregation over an already-bounded transaction set."""
    rec = Reconciliation()
    positions: dict[str, _PositionAgg] = {}
    cash: dict[str, list[Decimal | int]] = {}
    hash_parts: list[str] = []

    for txn in transactions:
        rec.transaction_count += 1
        if txn.status in UNLINKED_STATUSES:
            rec.unresolved_count += 1
        hash_parts.append(
            "|".join(
                [
                    txn.transaction_key,
                    txn.transaction_type,
                    _num(txn.quantity),
                    _num(txn.net_amount),
                    txn.currency,
                    _position_key(txn),
                ]
            )
        )

        # Position effect (trades only).
        if txn.transaction_type in (BUY, SELL) and txn.quantity is not None:
            key = _position_key(txn)
            agg = positions.get(key)
            if agg is None:
                agg = _PositionAgg(
                    key=key,
                    resolution_status=(
                        "unresolved_instrument" if txn.status in UNLINKED_STATUSES else "resolved"
                    ),
                    instrument_id=txn.instrument_id,
                    instrument_listing_id=txn.instrument_listing_id,
                    fund_id=txn.fund_id,
                    fund_listing_id=txn.fund_listing_id,
                    symbol=txn.symbol,
                    isin=txn.isin,
                    name=txn.name,
                    currency=txn.currency,
                )
                positions[key] = agg
            qty = abs(txn.quantity)
            if txn.transaction_type == BUY:
                agg.quantity += qty
                agg.buy_quantity += qty
            else:
                agg.quantity -= qty
                agg.sell_quantity += qty
            agg.fees_total += txn.fees or _ZERO
            agg.taxes_total += txn.taxes or _ZERO
            agg.transaction_count += 1

        # Cash effect (any transaction with a derivable signed amount).
        signed = _signed_cash(txn)
        if signed is not None:
            currency = (txn.cash_currency or txn.currency or "").upper()
            if currency:
                bucket = cash.setdefault(currency, [_ZERO, 0])
                bucket[0] = bucket[0] + signed  # type: ignore[operator]
                bucket[1] = int(bucket[1]) + 1

    rec.positions = sorted(positions.values(), key=lambda a: a.key)
    rec.cash = cash
    rec.input_hash = hashlib.sha256("\n".join(hash_parts).encode("utf-8")).hexdigest()
    return rec


async def reconcile_positions(session: AsyncSession, workspace_id: int) -> PositionsResponse:
    """Live, bounded reconciliation of committed transactions (no snapshot write)."""
    workspace = await workspaces_service.get_workspace(session, workspace_id)
    transactions = await _committed_transactions(session, workspace_id)
    rec = _reconcile(transactions)
    snapshot = await _latest_snapshot(session, workspace_id)
    return PositionsResponse(
        workspace_id=workspace_id,
        as_of_date=date.today(),
        base_currency=workspace.base_currency,
        transaction_count=rec.transaction_count,
        unresolved_count=sum(
            1 for p in rec.positions if p.resolution_status == "unresolved_instrument"
        ),
        positions=[_position_read(p) for p in rec.positions],
        cash=[
            CashBalance(currency=cur, amount=bucket[0], transaction_count=int(bucket[1]))
            for cur, bucket in sorted(rec.cash.items())
        ],
        snapshot_id=snapshot.id if snapshot else None,
        snapshot_status=snapshot.status if snapshot else rec.status,
        input_hash=snapshot.input_hash if snapshot else rec.input_hash,
    )


def _position_read(agg: _PositionAgg) -> ReconciledPosition:
    return ReconciledPosition(
        key=agg.key,
        resolution_status=agg.resolution_status,
        instrument_id=agg.instrument_id,
        instrument_listing_id=agg.instrument_listing_id,
        fund_id=agg.fund_id,
        fund_listing_id=agg.fund_listing_id,
        symbol=agg.symbol,
        isin=agg.isin,
        name=agg.name,
        currency=agg.currency,
        quantity=agg.quantity,
        buy_quantity=agg.buy_quantity,
        sell_quantity=agg.sell_quantity,
        fees_total=agg.fees_total,
        taxes_total=agg.taxes_total,
        transaction_count=agg.transaction_count,
    )


async def write_position_snapshot(
    session: AsyncSession, workspace_id: int
) -> tuple[PortfolioPositionSnapshot | None, bool]:
    """Public idempotent reconciliation-snapshot writer. Returns (snapshot, created).

    Reused by the imported-instrument resolution bridge to re-reconcile after it
    backfills instrument links: the input hash now keys on the resolved instrument
    (not the raw symbol), so a *materially changed* ledger writes a new snapshot
    while an unchanged one is a no-op (same idempotency contract as commit)."""
    return await _write_position_snapshot(session, workspace_id)


async def _write_position_snapshot(
    session: AsyncSession, workspace_id: int
) -> tuple[PortfolioPositionSnapshot | None, bool]:
    """Idempotently persist a reconciliation snapshot. Returns (snapshot, created)."""
    transactions = await _committed_transactions(session, workspace_id)
    rec = _reconcile(transactions)
    latest = await _latest_snapshot(session, workspace_id)
    if latest is not None and latest.input_hash == rec.input_hash:
        return latest, False  # unchanged ledger => no duplicate snapshot

    snapshot = PortfolioPositionSnapshot(
        workspace_id=workspace_id,
        as_of_date=date.today(),
        source="broker_reconciliation",
        status=rec.status,
        input_hash=rec.input_hash,
        transaction_count=rec.transaction_count,
        unresolved_count=rec.unresolved_count,
        position_count=len(rec.positions),
    )
    session.add(snapshot)
    await session.flush()
    for agg in rec.positions:
        session.add(
            PortfolioPositionSnapshotRow(
                snapshot_id=snapshot.id,
                kind="position",
                instrument_id=agg.instrument_id,
                instrument_listing_id=agg.instrument_listing_id,
                fund_id=agg.fund_id,
                fund_listing_id=agg.fund_listing_id,
                symbol=agg.symbol,
                isin=agg.isin,
                name=agg.name,
                currency=agg.currency,
                quantity=agg.quantity,
                fees_total=agg.fees_total,
                taxes_total=agg.taxes_total,
                status=agg.resolution_status,
            )
        )
    for currency, bucket in sorted(rec.cash.items()):
        session.add(
            PortfolioPositionSnapshotRow(
                snapshot_id=snapshot.id,
                kind="cash",
                currency=currency,
                quantity=bucket[0],
                status="cash",
            )
        )
    await session.flush()
    return snapshot, True


def _snapshot_summary(
    snapshot: PortfolioPositionSnapshot | None, *, created: bool
) -> PositionSnapshotSummary | None:
    if snapshot is None:
        return None
    return PositionSnapshotSummary(
        snapshot_id=snapshot.id,
        as_of_date=snapshot.as_of_date,
        status=snapshot.status,
        input_hash=snapshot.input_hash,
        transaction_count=snapshot.transaction_count,
        unresolved_count=snapshot.unresolved_count,
        position_count=snapshot.position_count,
        created=created,
    )


async def _latest_snapshot(
    session: AsyncSession, workspace_id: int
) -> PortfolioPositionSnapshot | None:
    return await session.scalar(
        select(PortfolioPositionSnapshot)
        .where(PortfolioPositionSnapshot.workspace_id == workspace_id)
        .order_by(PortfolioPositionSnapshot.as_of_date.desc(), PortfolioPositionSnapshot.id.desc())
        .limit(1)
    )


# --- history / ledger reads --------------------------------------------------


async def _existing_import(
    session: AsyncSession, workspace_id: int, source_hash: str
) -> BrokerImport | None:
    return await session.scalar(
        select(BrokerImport).where(
            BrokerImport.workspace_id == workspace_id,
            BrokerImport.source_hash == source_hash,
        )
    )


async def list_imports(
    session: AsyncSession, workspace_id: int, *, limit: int = 100
) -> list[BrokerImport]:
    await workspaces_service.get_workspace(session, workspace_id)
    limit = max(1, min(limit, 500))
    return list(
        (
            await session.execute(
                select(BrokerImport)
                .where(BrokerImport.workspace_id == workspace_id)
                .order_by(BrokerImport.id.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )


async def get_import(session: AsyncSession, workspace_id: int, import_id: int) -> BrokerImport:
    """Fetch one import with its raw rows eagerly loaded (for the detail view)."""
    broker_import = await session.scalar(
        select(BrokerImport)
        .where(BrokerImport.id == import_id)
        .options(selectinload(BrokerImport.rows))
    )
    if broker_import is None or broker_import.workspace_id != workspace_id:
        raise NotFoundError("Broker import not found", code="broker_import_not_found")
    return broker_import


async def list_transactions(
    session: AsyncSession,
    workspace_id: int,
    *,
    limit: int = DEFAULT_TRANSACTION_LIMIT,
    transaction_type: str | None = None,
    status: str | None = None,
    broker_import_id: int | None = None,
) -> list[PortfolioTransaction]:
    await workspaces_service.get_workspace(session, workspace_id)
    limit = max(1, min(limit, MAX_TRANSACTION_LIMIT))
    stmt = (
        select(PortfolioTransaction)
        .where(PortfolioTransaction.workspace_id == workspace_id)
        .order_by(PortfolioTransaction.trade_date.desc(), PortfolioTransaction.id.desc())
        .limit(limit)
    )
    if transaction_type is not None:
        stmt = stmt.where(PortfolioTransaction.transaction_type == transaction_type)
    if status is not None:
        stmt = stmt.where(PortfolioTransaction.status == status)
    if broker_import_id is not None:
        stmt = stmt.where(PortfolioTransaction.broker_import_id == broker_import_id)
    return list((await session.execute(stmt)).scalars().all())


async def get_transaction(
    session: AsyncSession, workspace_id: int, transaction_id: int
) -> PortfolioTransaction:
    """Fetch one canonical transaction (workspace-scoped)."""
    await workspaces_service.get_workspace(session, workspace_id)
    txn = await session.get(PortfolioTransaction, transaction_id)
    if txn is None or txn.workspace_id != workspace_id:
        raise NotFoundError("Transaction not found", code="transaction_not_found")
    return txn
