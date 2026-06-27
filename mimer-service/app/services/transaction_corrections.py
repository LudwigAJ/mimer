"""Manual correction workflows for imported / ambiguous broker transactions.

When a broker CSV import (``app/services/broker_imports.py``) or the automatic
imported-instrument resolution bridge
(``app/services/imported_instrument_resolution.py``) leaves a transaction
``unresolved_instrument`` / ``ambiguous_instrument`` — or links it wrongly — a
human needs to intervene. This module is the operator-driven cleanup layer:

  * **manual link** a transaction to an *existing* instrument/listing/fund/listing;
  * **clear** a mistaken manual or automatic link;
  * **ignore** a row (drop it from the portfolio, keep it auditable);
  * **manual review** a row (park it for later, keep it in the ledger);
  * **list** the rows needing attention;
  * **show correction context** (bounded candidates) to help choose a link.

Hard rules (see AGENTS.md — do not regress):

  * **Never creates an instrument.** A manual link only attaches *existing*
    canonical identity; the target ids must already exist.
  * **Never calls a resolver / OpenFIGI / a live price/FX provider.** All lookups
    are bounded SQL over already-stored rows.
  * **Never name-only guesses.** Candidate context is built from identifiers
    (ISIN/FIGI) and exact-ticker matches only; a broker name is never a link.
  * **Clearing a link never deletes the canonical instrument/listing/fund** — it
    only nulls the transaction's FK.
  * **Ignored / manual-review state stays visible** (provenance in
    ``raw_payload_json``; surfaced by diagnostics + the planner).

Compute boundary: persistence + bounded SQL reconciliation only. A successful
correction updates the bounded position snapshot via the existing reconciliation
helper and *recommends* (never runs) a valuation recompute / price fetch — it
computes no PnL, tax lots or total return.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError, NotFoundError
from app.db.models import (
    Fund,
    FundListing,
    Instrument,
    InstrumentIdentifier,
    InstrumentListing,
    PortfolioTransaction,
    SecurityIdentifier,
)
from app.schemas.broker_import import (
    CorrectionCandidate,
    CorrectionContextResponse,
    CorrectionResponse,
    TransactionLinks,
)
from app.services import broker_imports as broker_service
from app.services import freshness as freshness_service
from app.services import imported_instrument_resolution as iir
from app.services import workspaces as workspaces_service

RESOLVED_STATUS = iir.RESOLVED_STATUS  # "resolved"
UNRESOLVED_STATUS = iir.UNRESOLVED_STATUS  # "unresolved_instrument"
AMBIGUOUS_STATUS = iir.AMBIGUOUS_STATUS  # "ambiguous_instrument"
MANUAL_REVIEW_STATUS = broker_service.MANUAL_REVIEW_STATUS  # "manual_review"
IGNORED_STATUS = broker_service.IGNORED_STATUS  # "ignored"
SOURCE = broker_service.SOURCE

# The "needs a human" working set surfaced by the manual-review list endpoint.
MANUAL_ATTENTION_STATUSES = (UNRESOLVED_STATUS, AMBIGUOUS_STATUS, MANUAL_REVIEW_STATUS)
# Post-clear statuses a caller may choose.
_CLEAR_RESET_STATUSES = (UNRESOLVED_STATUS, MANUAL_REVIEW_STATUS)

DEFAULT_LIMIT = 200
MAX_LIMIT = 1000
# Bound for each candidate list so the context query stays cheap.
MAX_CANDIDATES = 25
# Cap the per-row manual-correction audit trail kept in ``raw_payload_json``.
_MAX_HISTORY = 50

_FIGI_SCHEMES = ("figi", "composite_figi", "share_class_figi")


class CorrectionError(AppError):
    """422 — an invalid manual correction (no/contradictory target, bad relation)."""

    status_code = 422
    code = "invalid_correction"


# --- link snapshots / provenance ---------------------------------------------


def _links_of(txn: PortfolioTransaction) -> TransactionLinks:
    return TransactionLinks(
        instrument_id=txn.instrument_id,
        instrument_listing_id=txn.instrument_listing_id,
        fund_id=txn.fund_id,
        fund_listing_id=txn.fund_listing_id,
    )


def _links_dict(txn: PortfolioTransaction) -> dict[str, int | None]:
    return {
        "instrument_id": txn.instrument_id,
        "instrument_listing_id": txn.instrument_listing_id,
        "fund_id": txn.fund_id,
        "fund_listing_id": txn.fund_listing_id,
    }


def _record_provenance(
    txn: PortfolioTransaction,
    *,
    action: str,
    reason: str | None,
    previous_status: str,
    previous_links: dict[str, int | None],
    new_links: dict[str, int | None],
    now: datetime,
) -> None:
    """Append manual-correction provenance into ``raw_payload_json`` (additive).

    Keeps the latest correction under ``manual_correction`` and a bounded audit
    trail under ``manual_correction_history``. Never clobbers the resolver's
    ``resolution`` key (distinct provenance)."""
    payload = dict(txn.raw_payload_json or {})
    correction = {
        "action": action,
        "reason": reason,
        "corrected_at": now.isoformat(),
        "previous_status": previous_status,
        "new_status": txn.status,
        "previous_links": previous_links,
        "new_links": new_links,
    }
    payload["manual_correction"] = correction
    history = list(payload.get("manual_correction_history") or [])
    history.append(correction)
    payload["manual_correction_history"] = history[-_MAX_HISTORY:]
    txn.raw_payload_json = payload


# --- follow-through (reconciliation / valuation / planner recommendations) ----


async def _follow_through(
    session: AsyncSession,
    txn: PortfolioTransaction,
    *,
    workspace_id: int,
    new_status: str,
    changed: bool,
) -> dict[str, object]:
    """Re-reconcile the bounded position snapshot + build recommended next steps.

    Updates the position snapshot via the existing idempotent reconciliation
    helper (a no-op when the ledger did not materially change). Recommends — never
    runs — a valuation recompute / price fetch. Fetches nothing."""
    snapshot_id: int | None = None
    snapshot_updated = False
    if changed:
        await session.flush()
        snapshot, created = await broker_service.write_position_snapshot(session, workspace_id)
        snapshot_id = snapshot.id if snapshot else None
        snapshot_updated = created

    recommended: list[str] = []
    if changed:
        recommended.append("recompute_portfolio_valuation")
        if new_status == RESOLVED_STATUS:
            if await _listing_needs_price(session, txn.instrument_listing_id):
                recommended.append("fetch_imported_instrument_price")
        elif new_status == UNRESOLVED_STATUS:
            recommended.append("resolve_or_manually_link_transaction")
        elif new_status == MANUAL_REVIEW_STATUS:
            recommended.append("review_and_link_transaction")
        # IGNORED: deliberately no follow-up action (the row is parked out).

    return {
        "position_snapshot_id": snapshot_id,
        "position_snapshot_updated": snapshot_updated,
        # A changed ledger/link means any cached valuation + market-data plan are
        # now stale; we recommend (never trigger) the recompute.
        "valuation_recompute_needed": changed,
        "market_data_plan_changed": changed,
        "recommended_actions": recommended,
    }


async def _listing_needs_price(session: AsyncSession, instrument_listing_id: int | None) -> bool:
    if instrument_listing_id is None:
        return False
    listing = await session.get(InstrumentListing, instrument_listing_id)
    if listing is None:
        return False
    return freshness_service.freshness_state(listing.last_price_at, kind="price") != (
        freshness_service.FRESH
    )


def _response(
    txn: PortfolioTransaction,
    *,
    action: str,
    changed: bool,
    old_status: str,
    old_links: TransactionLinks,
    reason: str | None,
    follow: dict[str, object],
) -> CorrectionResponse:
    return CorrectionResponse(
        transaction_id=txn.id,
        action=action,
        changed=changed,
        old_status=old_status,
        new_status=txn.status,
        old_links=old_links,
        new_links=_links_of(txn),
        correction_reason=reason,
        position_snapshot_id=follow["position_snapshot_id"],  # type: ignore[arg-type]
        position_snapshot_updated=follow["position_snapshot_updated"],  # type: ignore[arg-type]
        valuation_recompute_needed=follow["valuation_recompute_needed"],  # type: ignore[arg-type]
        market_data_plan_changed=follow["market_data_plan_changed"],  # type: ignore[arg-type]
        recommended_actions=follow["recommended_actions"],  # type: ignore[arg-type]
    )


# --- target validation (existing identity only; never creates) ----------------


async def _validate_and_normalise_targets(
    session: AsyncSession,
    *,
    instrument_id: int | None,
    instrument_listing_id: int | None,
    fund_id: int | None,
    fund_listing_id: int | None,
) -> dict[str, int | None]:
    """Validate target ids exist + are consistent; backfill parent ids.

    Rules: at least one target; instrument and fund sides cannot be mixed; a
    supplied listing must belong to its supplied parent; an orphan listing
    backfills its parent. Never creates anything."""
    has_instrument_side = instrument_id is not None or instrument_listing_id is not None
    has_fund_side = fund_id is not None or fund_listing_id is not None
    if not (has_instrument_side or has_fund_side):
        raise CorrectionError(
            "A manual link needs at least one target "
            "(instrument_id / instrument_listing_id / fund_id / fund_listing_id).",
            code="manual_link_no_target",
        )
    if has_instrument_side and has_fund_side:
        raise CorrectionError(
            "A manual link cannot target both an instrument and a fund.",
            code="manual_link_mixed_targets",
        )

    if has_instrument_side:
        if instrument_listing_id is not None:
            listing = await session.get(InstrumentListing, instrument_listing_id)
            if listing is None:
                raise NotFoundError(
                    "Instrument listing not found", code="instrument_listing_not_found"
                )
            if instrument_id is not None and listing.instrument_id != instrument_id:
                raise CorrectionError(
                    "instrument_listing_id does not belong to instrument_id.",
                    code="manual_link_invalid_relation",
                )
            instrument_id = listing.instrument_id
        if instrument_id is not None and await session.get(Instrument, instrument_id) is None:
            raise NotFoundError("Instrument not found", code="instrument_not_found")
        return {
            "instrument_id": instrument_id,
            "instrument_listing_id": instrument_listing_id,
            "fund_id": None,
            "fund_listing_id": None,
        }

    if fund_listing_id is not None:
        listing = await session.get(FundListing, fund_listing_id)
        if listing is None:
            raise NotFoundError("Fund listing not found", code="fund_listing_not_found")
        if fund_id is not None and listing.fund_id != fund_id:
            raise CorrectionError(
                "fund_listing_id does not belong to fund_id.",
                code="manual_link_invalid_relation",
            )
        fund_id = listing.fund_id
    if fund_id is not None and await session.get(Fund, fund_id) is None:
        raise NotFoundError("Fund not found", code="fund_not_found")
    return {
        "instrument_id": None,
        "instrument_listing_id": None,
        "fund_id": fund_id,
        "fund_listing_id": fund_listing_id,
    }


# --- mutations ----------------------------------------------------------------


async def manual_link_transaction(
    session: AsyncSession,
    workspace_id: int,
    transaction_id: int,
    *,
    instrument_id: int | None = None,
    instrument_listing_id: int | None = None,
    fund_id: int | None = None,
    fund_listing_id: int | None = None,
    correction_reason: str | None = None,
) -> CorrectionResponse:
    """Link a transaction to existing canonical identity (status -> resolved)."""
    txn = await broker_service.get_transaction(session, workspace_id, transaction_id)
    old_status = txn.status
    old_links = _links_of(txn)
    previous_links = _links_dict(txn)

    targets = await _validate_and_normalise_targets(
        session,
        instrument_id=instrument_id,
        instrument_listing_id=instrument_listing_id,
        fund_id=fund_id,
        fund_listing_id=fund_listing_id,
    )

    txn.instrument_id = targets["instrument_id"]
    txn.instrument_listing_id = targets["instrument_listing_id"]
    txn.fund_id = targets["fund_id"]
    txn.fund_listing_id = targets["fund_listing_id"]
    txn.status = RESOLVED_STATUS
    changed = old_status != txn.status or previous_links != _links_dict(txn)
    _record_provenance(
        txn,
        action="manual_link",
        reason=correction_reason,
        previous_status=old_status,
        previous_links=previous_links,
        new_links=_links_dict(txn),
        now=datetime.now(UTC),
    )
    follow = await _follow_through(
        session,
        txn,
        workspace_id=workspace_id,
        new_status=txn.status,
        changed=changed,
    )
    return _response(
        txn,
        action="manual_link",
        changed=changed,
        old_status=old_status,
        old_links=old_links,
        reason=correction_reason,
        follow=follow,
    )


async def clear_transaction_link(
    session: AsyncSession,
    workspace_id: int,
    transaction_id: int,
    *,
    correction_reason: str | None = None,
    reset_status: str | None = None,
) -> CorrectionResponse:
    """Clear a transaction's instrument/fund link (canonical rows untouched)."""
    new_status = reset_status or UNRESOLVED_STATUS
    if new_status not in _CLEAR_RESET_STATUSES:
        raise CorrectionError(
            f"reset_status must be one of {_CLEAR_RESET_STATUSES}.",
            code="invalid_reset_status",
        )
    txn = await broker_service.get_transaction(session, workspace_id, transaction_id)
    old_status = txn.status
    old_links = _links_of(txn)
    previous_links = _links_dict(txn)

    txn.instrument_id = None
    txn.instrument_listing_id = None
    txn.fund_id = None
    txn.fund_listing_id = None
    txn.status = new_status
    changed = old_status != txn.status or previous_links != _links_dict(txn)
    _record_provenance(
        txn,
        action="clear_link",
        reason=correction_reason,
        previous_status=old_status,
        previous_links=previous_links,
        new_links=_links_dict(txn),
        now=datetime.now(UTC),
    )
    follow = await _follow_through(
        session,
        txn,
        workspace_id=workspace_id,
        new_status=txn.status,
        changed=changed,
    )
    return _response(
        txn,
        action="clear_link",
        changed=changed,
        old_status=old_status,
        old_links=old_links,
        reason=correction_reason,
        follow=follow,
    )


async def ignore_transaction(
    session: AsyncSession,
    workspace_id: int,
    transaction_id: int,
    *,
    correction_reason: str | None = None,
) -> CorrectionResponse:
    """Mark a transaction ignored — out of reconciliation, still auditable."""
    return await _set_status(
        session,
        workspace_id,
        transaction_id,
        action="ignore",
        new_status=IGNORED_STATUS,
        correction_reason=correction_reason,
    )


async def mark_transaction_manual_review(
    session: AsyncSession,
    workspace_id: int,
    transaction_id: int,
    *,
    correction_reason: str | None = None,
) -> CorrectionResponse:
    """Park a transaction for manual review (kept in the ledger, flagged)."""
    return await _set_status(
        session,
        workspace_id,
        transaction_id,
        action="manual_review",
        new_status=MANUAL_REVIEW_STATUS,
        correction_reason=correction_reason,
    )


async def _set_status(
    session: AsyncSession,
    workspace_id: int,
    transaction_id: int,
    *,
    action: str,
    new_status: str,
    correction_reason: str | None,
) -> CorrectionResponse:
    """Status-only correction (ignore / manual_review): keeps links for audit."""
    txn = await broker_service.get_transaction(session, workspace_id, transaction_id)
    old_status = txn.status
    old_links = _links_of(txn)
    previous_links = _links_dict(txn)

    txn.status = new_status
    changed = old_status != txn.status
    _record_provenance(
        txn,
        action=action,
        reason=correction_reason,
        previous_status=old_status,
        previous_links=previous_links,
        new_links=_links_dict(txn),
        now=datetime.now(UTC),
    )
    follow = await _follow_through(
        session,
        txn,
        workspace_id=workspace_id,
        new_status=txn.status,
        changed=changed,
    )
    return _response(
        txn,
        action=action,
        changed=changed,
        old_status=old_status,
        old_links=old_links,
        reason=correction_reason,
        follow=follow,
    )


# --- reads --------------------------------------------------------------------


async def list_manual_review_transactions(
    session: AsyncSession,
    workspace_id: int,
    *,
    status: str | None = None,
    limit: int = DEFAULT_LIMIT,
) -> list[PortfolioTransaction]:
    """Imported transactions needing a human: unresolved / ambiguous / manual_review.

    Optionally narrowed to one of those statuses via ``status``."""
    await workspaces_service.get_workspace(session, workspace_id)
    limit = max(1, min(limit, MAX_LIMIT))
    statuses = [status] if status else list(MANUAL_ATTENTION_STATUSES)
    stmt = (
        select(PortfolioTransaction)
        .where(
            PortfolioTransaction.workspace_id == workspace_id,
            PortfolioTransaction.source == SOURCE,
            PortfolioTransaction.status.in_(statuses),
        )
        .order_by(PortfolioTransaction.trade_date.desc(), PortfolioTransaction.id.desc())
        .limit(limit)
    )
    return list((await session.execute(stmt)).scalars().all())


# --- correction context (bounded candidates; never name-only, never live) -----


async def get_correction_context(
    session: AsyncSession, workspace_id: int, transaction_id: int
) -> CorrectionContextResponse:
    """Bounded candidate context for choosing a manual link (no live calls)."""
    txn = await broker_service.get_transaction(session, workspace_id, transaction_id)

    # The safe auto-resolution against existing identity (what the resolver bridge
    # would link). Reuses the shared crosswalk index — no resolver/live call.
    index = await broker_service.build_resolution_index(session, [txn])
    link = index.resolve(isin=txn.isin, figi=txn.figi, symbol=txn.symbol, currency=txn.currency)
    suggested = (
        TransactionLinks(
            instrument_id=link.instrument_id,
            instrument_listing_id=link.instrument_listing_id,
            fund_id=link.fund_id,
            fund_listing_id=link.fund_listing_id,
        )
        if link.resolved
        else None
    )

    identifier_candidates = await _identifier_candidates(session, txn)
    ticker_candidates = await _ticker_candidates(session, txn)
    name_only = bool(txn.name) and not (txn.symbol or txn.isin or txn.figi)

    payload = txn.raw_payload_json or {}
    resolver_outcome = payload.get("resolution") if isinstance(payload, dict) else None
    last_correction = payload.get("manual_correction") if isinstance(payload, dict) else None

    return CorrectionContextResponse(
        transaction_id=txn.id,
        workspace_id=workspace_id,
        status=txn.status,
        transaction_type=txn.transaction_type,
        symbol=txn.symbol,
        isin=txn.isin,
        figi=txn.figi,
        name=txn.name,
        currency=txn.currency,
        name_only=name_only,
        current_links=_links_of(txn),
        suggested_link=suggested,
        identifier_candidates=identifier_candidates,
        ticker_candidates=ticker_candidates,
        recent_resolver_outcome=resolver_outcome if isinstance(resolver_outcome, dict) else None,
        last_correction=last_correction if isinstance(last_correction, dict) else None,
        recommended_action=_recommended_action(
            suggested=suggested,
            identifier_candidates=identifier_candidates,
            ticker_candidates=ticker_candidates,
            name_only=name_only,
        ),
    )


def _recommended_action(
    *,
    suggested: TransactionLinks | None,
    identifier_candidates: list[CorrectionCandidate],
    ticker_candidates: list[CorrectionCandidate],
    name_only: bool,
) -> str:
    if suggested is not None:
        return "manual_link to the suggested existing identity"
    if identifier_candidates:
        return "manual_link to one of the identifier candidates"
    if ticker_candidates:
        return "manual_link to a ticker candidate (verify the currency first)"
    if name_only:
        return "no safe identifier (name-only): mark manual_review or ignore — never name-only link"
    return "no candidate identity found: mark manual_review or ignore"


def _dedupe(candidates: list[CorrectionCandidate]) -> list[CorrectionCandidate]:
    seen: set[tuple] = set()
    out: list[CorrectionCandidate] = []
    for c in candidates:
        key = (c.kind, c.instrument_id, c.instrument_listing_id, c.fund_id, c.fund_listing_id)
        if key in seen:
            continue
        seen.add(key)
        out.append(c)
    return out[:MAX_CANDIDATES]


async def _identifier_candidates(
    session: AsyncSession, txn: PortfolioTransaction
) -> list[CorrectionCandidate]:
    """Existing fund/instrument matches by the transaction's ISIN / FIGI (bounded)."""
    candidates: list[CorrectionCandidate] = []
    isin = (txn.isin or "").strip().upper()
    figi = (txn.figi or "").strip().upper()

    if isin:
        for fund in (
            (await session.execute(select(Fund).where(Fund.isin == isin).limit(MAX_CANDIDATES)))
            .scalars()
            .all()
        ):
            candidates.append(
                CorrectionCandidate(
                    kind="fund",
                    matched_on="isin",
                    matched_value=isin,
                    label=fund.name,
                    fund_id=fund.id,
                    currency=fund.base_currency,
                )
            )
        for sid in (
            (
                await session.execute(
                    select(SecurityIdentifier)
                    .where(SecurityIdentifier.scheme == "isin", SecurityIdentifier.value == isin)
                    .limit(MAX_CANDIDATES)
                )
            )
            .scalars()
            .all()
        ):
            if sid.fund_listing_id is not None:
                candidates.append(
                    CorrectionCandidate(
                        kind="fund_listing",
                        matched_on="isin",
                        matched_value=isin,
                        fund_id=sid.fund_id,
                        fund_listing_id=sid.fund_listing_id,
                        currency=sid.currency,
                    )
                )
            elif sid.fund_id is not None:
                candidates.append(
                    CorrectionCandidate(
                        kind="fund",
                        matched_on="isin",
                        matched_value=isin,
                        fund_id=sid.fund_id,
                        currency=sid.currency,
                    )
                )
        candidates += await _instrument_candidates_by_identifier(
            session, scheme_in=("isin",), value=isin
        )

    if figi:
        for fl in (
            (
                await session.execute(
                    select(FundListing).where(FundListing.figi == figi).limit(MAX_CANDIDATES)
                )
            )
            .scalars()
            .all()
        ):
            candidates.append(
                CorrectionCandidate(
                    kind="fund_listing",
                    matched_on="figi",
                    matched_value=figi,
                    label=fl.ticker,
                    fund_id=fl.fund_id,
                    fund_listing_id=fl.id,
                    currency=fl.trading_currency or fl.currency_unit,
                )
            )
        candidates += await _instrument_candidates_by_identifier(
            session, scheme_in=_FIGI_SCHEMES, value=figi
        )
        for il in (
            (
                await session.execute(
                    select(InstrumentListing)
                    .where(
                        (InstrumentListing.figi == figi)
                        | (InstrumentListing.composite_figi == figi)
                        | (InstrumentListing.share_class_figi == figi)
                    )
                    .limit(MAX_CANDIDATES)
                )
            )
            .scalars()
            .all()
        ):
            candidates.append(
                CorrectionCandidate(
                    kind="instrument_listing",
                    matched_on="figi",
                    matched_value=figi,
                    label=il.ticker,
                    instrument_id=il.instrument_id,
                    instrument_listing_id=il.id,
                    currency=il.currency,
                )
            )

    return _dedupe(candidates)


async def _instrument_candidates_by_identifier(
    session: AsyncSession, *, scheme_in: tuple[str, ...], value: str
) -> list[CorrectionCandidate]:
    rows = (
        (
            await session.execute(
                select(InstrumentIdentifier)
                .where(
                    InstrumentIdentifier.scheme.in_(scheme_in),
                    InstrumentIdentifier.value == value,
                )
                .limit(MAX_CANDIDATES)
            )
        )
        .scalars()
        .all()
    )
    out: list[CorrectionCandidate] = []
    for ident in rows:
        instrument = await session.get(Instrument, ident.instrument_id)
        out.append(
            CorrectionCandidate(
                kind="instrument",
                matched_on="isin" if ident.scheme == "isin" else "figi",
                matched_value=value,
                label=instrument.name if instrument else None,
                instrument_id=ident.instrument_id,
                currency=instrument.currency if instrument else None,
            )
        )
    return out


async def _ticker_candidates(
    session: AsyncSession, txn: PortfolioTransaction
) -> list[CorrectionCandidate]:
    """Exact-ticker fund/instrument listings (bounded). Never a name match.

    A ticker is exchange/currency-specific, never global identity — so these are
    *candidates for a human*, with a ``same_currency`` hint, never auto-links."""
    symbol = (txn.symbol or "").strip().upper()
    if not symbol:
        return []
    txn_ccy = (txn.currency or "").strip().upper()
    candidates: list[CorrectionCandidate] = []

    for fl in (
        (
            await session.execute(
                select(FundListing).where(FundListing.ticker == symbol).limit(MAX_CANDIDATES)
            )
        )
        .scalars()
        .all()
    ):
        ccy = (fl.trading_currency or fl.currency_unit or "").upper() or None
        candidates.append(
            CorrectionCandidate(
                kind="fund_listing",
                matched_on="ticker",
                matched_value=symbol,
                label=fl.ticker,
                fund_id=fl.fund_id,
                fund_listing_id=fl.id,
                currency=ccy,
                same_currency=(ccy == txn_ccy) if (ccy and txn_ccy) else None,
            )
        )
    for il in (
        (
            await session.execute(
                select(InstrumentListing)
                .where(InstrumentListing.ticker == symbol)
                .limit(MAX_CANDIDATES)
            )
        )
        .scalars()
        .all()
    ):
        ccy = (il.currency or "").upper() or None
        candidates.append(
            CorrectionCandidate(
                kind="instrument_listing",
                matched_on="ticker",
                matched_value=symbol,
                label=il.ticker,
                instrument_id=il.instrument_id,
                instrument_listing_id=il.id,
                currency=ccy,
                same_currency=(ccy == txn_ccy) if (ccy and txn_ccy) else None,
            )
        )
    return _dedupe(candidates)
