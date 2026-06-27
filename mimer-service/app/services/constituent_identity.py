"""Constituent identity resolution — the provider-agnostic half.

Turns unresolved ETF/fund holding rows into canonical ``instruments`` /
``instrument_listings`` / ``instrument_identifiers`` and links the holdings back,
idempotently and safely. The provider-specific fetch/parse lives in
``app/sources/constituents.py``; this module owns request construction, the
deterministic identity keys, the upsert/link logic and the run counters.

Safety (see AGENTS.md):
  * requests are deduped (one Apple request even if held via several funds) so a
    live resolver never loops per holding;
  * only *safe* requests for the chosen source are attempted (OpenFIGI never gets
    a name-only query);
  * ambiguous / not-found / failed results never link a holding to a guessed
    instrument — it is better to leave a holding unresolved than link it wrong;
  * a ``manual`` instrument / holding link is never clobbered by an automated run.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    FundHolding,
    FundListing,
    Instrument,
    InstrumentIdentifier,
    InstrumentListing,
    PortfolioPosition,
)
from app.services import holdings_ingestion as holdings_service
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
)

# Identifier priority for the *primary* resolver request (strong -> weak).
_REQUEST_PRIORITY = ("isin", "figi", "cusip", "sedol", "ticker", "name")

# A resolved candidate links its holdings only at/above this confidence — never
# on an ambiguous result. "It is better to leave a holding unresolved."
_LINKABLE_CONFIDENCE = {"high", "medium"}

# Holding identity_status values we will (re)attempt. ``manual`` / ``ambiguous`` /
# ``not_found`` are left alone until a human intervenes; ``failed`` is retried.
_RETRYABLE_STATUSES = (None, "failed")


# --- deterministic identity keys --------------------------------------------


def instrument_identity_key(
    *,
    isin: str | None = None,
    share_class_figi: str | None = None,
    composite_figi: str | None = None,
    figi: str | None = None,
    name: str | None = None,
    country: str | None = None,
    currency: str | None = None,
) -> str:
    """Deterministic global identity for an instrument.

    Prefers strong identifiers (ISIN > share-class FIGI > composite FIGI > FIGI);
    only falls back to a normalised name+country+currency for offline/manual
    fixtures (never fuzzy matching). Same constituent => same key => one row.
    """
    for scheme, value in (
        ("isin", isin),
        ("share_class_figi", share_class_figi),
        ("composite_figi", composite_figi),
        ("figi", figi),
    ):
        if value and value.strip():
            return f"{scheme}:{value.strip().upper()}"
    norm_name = " ".join((name or "").lower().split())
    return f"name:{norm_name}|{(country or '').strip().upper()}|{(currency or '').strip().upper()}"


def listing_identity_key(
    *,
    composite_figi: str | None = None,
    figi: str | None = None,
    ticker: str | None = None,
    mic: str | None = None,
    exchange: str | None = None,
) -> str:
    """Deterministic identity for a listing within its instrument."""
    if composite_figi and composite_figi.strip():
        return f"composite_figi:{composite_figi.strip().upper()}"
    if figi and figi.strip():
        return f"figi:{figi.strip().upper()}"
    market = (mic or exchange or "").strip().upper()
    return f"ticker:{(ticker or '').strip().upper()}|{market}"


# --- request construction ----------------------------------------------------


def _holding_primary(holding: FundHolding) -> tuple[str, str] | None:
    """The highest-priority (scheme, value) identifier present on a holding."""
    candidates: dict[str, str | None] = {
        "isin": holding.security_isin,
        "figi": holding.security_figi,
        "cusip": holding.security_cusip,
        "sedol": holding.security_sedol,
        "ticker": holding.security_ticker,
        "name": holding.security_name,
    }
    for scheme in _REQUEST_PRIORITY:
        value = candidates.get(scheme)
        if value and value.strip():
            return scheme, value.strip()
    return None


def _request_for_holding(holding: FundHolding) -> ConstituentRequest | None:
    primary = _holding_primary(holding)
    if primary is None:
        return None
    scheme, value = primary
    input_key = f"{scheme}:{value.upper()}"
    return ConstituentRequest(
        input_key=input_key,
        scheme=scheme,
        value=value,
        name=holding.security_name,
        ticker=holding.security_ticker,
        isin=holding.security_isin,
        figi=holding.security_figi,
        cusip=holding.security_cusip,
        sedol=holding.security_sedol,
        country=holding.country,
        currency=holding.currency,
    )


def build_requests(
    holdings: list[FundHolding], *, resolver: ConstituentResolver
) -> tuple[list[ConstituentRequest], dict[str, list[int]], list[int]]:
    """Build deduped resolver requests from holdings.

    Returns ``(requests, holding_ids_by_input_key, unsafe_holding_ids)``:
      * ``requests`` — one per distinct identifier, only those *safe* for the
        resolver (e.g. OpenFIGI never gets a name-only query);
      * ``holding_ids_by_input_key`` — maps each request back to every holding it
        covers (Apple held via VUSA *and* JPM => one request, two holdings);
      * ``unsafe_holding_ids`` — holdings with no safe request for this resolver
        (left unresolved; surfaced by the planner for manual handling).
    """
    by_key: dict[str, ConstituentRequest] = {}
    holding_ids: dict[str, list[int]] = {}
    unsafe: list[int] = []
    for holding in holdings:
        request = _request_for_holding(holding)
        if request is None or not resolver.is_request_safe(request):
            unsafe.append(holding.id)
            continue
        by_key.setdefault(request.input_key, request)
        holding_ids.setdefault(request.input_key, []).append(holding.id)
    return list(by_key.values()), holding_ids, unsafe


# --- holding selection -------------------------------------------------------


async def _scoped_fund_ids(
    session: AsyncSession, *, fund_id: int | None, workspace_id: int | None
) -> list[int] | None:
    """Fund ids to scope resolution to (None == every fund)."""
    if fund_id is not None:
        return [fund_id]
    if workspace_id is None:
        return None
    rows = (
        await session.execute(
            select(FundListing.fund_id)
            .join(PortfolioPosition, PortfolioPosition.fund_listing_id == FundListing.id)
            .where(PortfolioPosition.workspace_id == workspace_id)
            .distinct()
        )
    ).scalars()
    return sorted(set(rows))


async def unresolved_holdings(
    session: AsyncSession,
    *,
    fund_id: int | None = None,
    workspace_id: int | None = None,
    limit: int | None = None,
) -> list[FundHolding]:
    """Unresolved constituents from each scoped fund's *coherent* snapshot.

    Uses ``latest_holdings_by_fund`` so we never resolve a seed+fixture mix, and
    only returns rows still needing work (no instrument link yet; identity_status
    null or ``failed``). Heaviest weights first, bounded by ``limit``.
    """
    fund_ids = await _scoped_fund_ids(session, fund_id=fund_id, workspace_id=workspace_id)
    if fund_ids is None:
        fund_ids = list((await session.execute(select(FundHolding.fund_id).distinct())).scalars())
    snapshots = await holdings_service.latest_holdings_by_fund(session, fund_ids)

    out: list[FundHolding] = []
    for holding in (h for items in snapshots.values() for h in items):
        if holding.holding_instrument_id is not None:
            continue
        if holding.identity_status not in _RETRYABLE_STATUSES:
            continue
        out.append(holding)
    out.sort(key=lambda h: h.weight, reverse=True)
    if limit is not None:
        out = out[:limit]
    return out


# --- idempotent upsert + linking ---------------------------------------------


@dataclass
class ResolutionRunResult:
    resolved: int = 0
    ambiguous: int = 0
    not_found: int = 0
    failed: int = 0
    skipped_budget: int = 0
    skipped_cached: int = 0
    skipped_unsafe: int = 0
    instruments_created: int = 0
    instruments_updated: int = 0
    listings_created: int = 0
    identifiers_created: int = 0

    @property
    def attempted(self) -> int:
        return self.resolved + self.ambiguous + self.not_found + self.failed

    def message(self) -> str:
        return (
            f"resolved={self.resolved} ambiguous={self.ambiguous} "
            f"not_found={self.not_found} failed={self.failed} "
            f"skipped_budget={self.skipped_budget} skipped_cached={self.skipped_cached} "
            f"skipped_unsafe={self.skipped_unsafe} instruments={self.instruments_created} "
            f"listings={self.listings_created} identifiers={self.identifiers_created}"
        )


_INSTRUMENT_FIELDS = ("name", "legal_name", "country", "currency")


async def _upsert_instrument(
    session: AsyncSession, candidate: ResolutionCandidate, *, result: ResolutionRunResult
) -> Instrument:
    key = instrument_identity_key(
        isin=candidate.isin,
        share_class_figi=candidate.share_class_figi,
        composite_figi=candidate.composite_figi,
        figi=candidate.figi,
        name=candidate.name,
        country=candidate.country,
        currency=candidate.currency,
    )
    instrument = await session.scalar(select(Instrument).where(Instrument.identity_key == key))
    new_values = {
        "name": candidate.name,
        "legal_name": candidate.legal_name,
        "country": candidate.country,
        "currency": candidate.currency,
    }
    if instrument is None:
        instrument = Instrument(
            identity_key=key,
            instrument_type="equity",
            name=candidate.name or candidate.input_key,
            legal_name=candidate.legal_name,
            country=candidate.country,
            currency=candidate.currency,
            status="active",
            source=candidate.source,
            raw_payload_json=candidate.raw_payload_json,
        )
        session.add(instrument)
        await session.flush()
        result.instruments_created += 1
        return instrument

    # Never downgrade a manual record; otherwise fill empties + update changed.
    changed = False
    manual = instrument.source == "manual"
    for field in _INSTRUMENT_FIELDS:
        value = new_values.get(field)
        if not value:
            continue
        current = getattr(instrument, field)
        if current in (None, ""):
            setattr(instrument, field, value)
            changed = True
        elif not manual and current != value:
            setattr(instrument, field, value)
            changed = True
    if changed:
        result.instruments_updated += 1
    return instrument


async def _upsert_listing(
    session: AsyncSession,
    instrument: Instrument,
    candidate: ResolutionCandidate,
    *,
    result: ResolutionRunResult,
) -> InstrumentListing | None:
    """Upsert the candidate's tradable listing; return it (None if untradable)."""
    if not (candidate.ticker or candidate.figi or candidate.composite_figi):
        return None  # nothing tradable to record yet
    key = listing_identity_key(
        composite_figi=candidate.composite_figi,
        figi=candidate.figi,
        ticker=candidate.ticker,
        mic=candidate.mic,
        exchange=candidate.exchange,
    )
    listing = await session.scalar(
        select(InstrumentListing).where(
            InstrumentListing.instrument_id == instrument.id,
            InstrumentListing.listing_key == key,
        )
    )
    fields = {
        "ticker": candidate.ticker,
        "exchange": candidate.exchange,
        "mic": candidate.mic,
        "currency": candidate.currency,
        "country": candidate.country,
        "figi": candidate.figi,
        "composite_figi": candidate.composite_figi,
        "share_class_figi": candidate.share_class_figi,
    }
    if listing is None:
        listing = InstrumentListing(
            instrument_id=instrument.id,
            listing_key=key,
            source=candidate.source,
            status="active",
            raw_payload_json=candidate.raw_payload_json,
            **fields,
        )
        session.add(listing)
        result.listings_created += 1
        return listing
    if listing.source == "manual":
        return listing
    for field, value in fields.items():
        if value and getattr(listing, field) in (None, ""):
            setattr(listing, field, value)
    return listing


async def _upsert_identifiers(
    session: AsyncSession,
    instrument: Instrument,
    candidate: ResolutionCandidate,
    *,
    result: ResolutionRunResult,
) -> None:
    pairs = [
        ("isin", candidate.isin),
        ("figi", candidate.figi),
        ("composite_figi", candidate.composite_figi),
        ("share_class_figi", candidate.share_class_figi),
        ("cusip", candidate.cusip),
        ("sedol", candidate.sedol),
        ("ticker", candidate.ticker),
    ]
    for scheme, value in pairs:
        if not (value and value.strip()):
            continue
        existing = await session.scalar(
            select(InstrumentIdentifier).where(
                InstrumentIdentifier.instrument_id == instrument.id,
                InstrumentIdentifier.scheme == scheme,
                InstrumentIdentifier.value == value,
                InstrumentIdentifier.source == candidate.source,
            )
        )
        if existing is None:
            session.add(
                InstrumentIdentifier(
                    instrument_id=instrument.id,
                    scheme=scheme,
                    value=value,
                    source=candidate.source,
                    status="active",
                )
            )
            result.identifiers_created += 1


async def upsert_candidate_instrument(
    session: AsyncSession,
    candidate: ResolutionCandidate,
    *,
    result: ResolutionRunResult,
) -> tuple[Instrument, InstrumentListing | None]:
    """Idempotently upsert the canonical instrument graph for a resolved candidate.

    The single shared entry point into the instrument master so a *second* caller
    (the imported-instrument resolution bridge) never forks the identity system:
    it upserts the ``instruments`` / ``instrument_listings`` / ``instrument_
    identifiers`` rows (deduped on the deterministic identity keys) and returns the
    instrument + its primary tradable listing for linking. Counts land on
    ``result``. Callers own the surrounding transaction."""
    instrument = await _upsert_instrument(session, candidate, result=result)
    listing = await _upsert_listing(session, instrument, candidate, result=result)
    await _upsert_identifiers(session, instrument, candidate, result=result)
    await session.flush()
    return instrument, listing


def _link_holdings(holdings: list[FundHolding], instrument: Instrument, *, now: datetime) -> int:
    linked = 0
    for holding in holdings:
        if holding.identity_status == "manual":
            continue  # never clobber a manual link
        holding.holding_instrument_id = instrument.id
        holding.identity_status = "resolved"
        holding.identity_resolved_at = now
        linked += 1
    return linked


def _mark_holdings(holdings: list[FundHolding], status: str) -> int:
    marked = 0
    for holding in holdings:
        if holding.identity_status == "manual":
            continue
        holding.identity_status = status
        marked += 1
    return marked


async def persist_candidate(
    session: AsyncSession,
    candidate: ResolutionCandidate,
    holdings: list[FundHolding],
    *,
    now: datetime,
    result: ResolutionRunResult,
) -> None:
    """Apply one resolver candidate: upsert canonical rows + (un)link holdings."""
    if candidate.status == SKIPPED_BUDGET:
        result.skipped_budget += len(holdings)
        return
    if candidate.status == SKIPPED_CACHED:
        result.skipped_cached += len(holdings)
        return
    if candidate.status == FAILED:
        result.failed += _mark_holdings(holdings, "failed")
        return
    if candidate.status == NOT_FOUND:
        result.not_found += _mark_holdings(holdings, "not_found")
        return
    if candidate.status == AMBIGUOUS:
        result.ambiguous += _mark_holdings(holdings, "ambiguous")
        return
    if candidate.status == RESOLVED and candidate.confidence in _LINKABLE_CONFIDENCE:
        instrument, _ = await upsert_candidate_instrument(session, candidate, result=result)
        result.resolved += _link_holdings(holdings, instrument, now=now)
        return
    # A resolved-but-low-confidence result is treated as ambiguous (do not link).
    result.ambiguous += _mark_holdings(holdings, "ambiguous")


async def resolve_and_persist(
    session: AsyncSession,
    resolver: ConstituentResolver,
    requests: list[ConstituentRequest],
    holding_ids_by_key: dict[str, list[int]],
    *,
    batch_size: int,
    ttl_seconds: int,
    now: datetime | None = None,
) -> ResolutionRunResult:
    """Resolve requests via ``resolver`` and persist results idempotently."""
    now = now or datetime.now(UTC)
    result = ResolutionRunResult()
    if not requests:
        return result

    candidates = await resolver.resolve_batch(
        session, requests, batch_size=batch_size, ttl_seconds=ttl_seconds
    )

    # Load all covered holdings once, keyed by id.
    all_ids = {hid for ids in holding_ids_by_key.values() for hid in ids}
    holdings_by_id: dict[int, FundHolding] = {}
    if all_ids:
        rows = (
            await session.execute(select(FundHolding).where(FundHolding.id.in_(all_ids)))
        ).scalars()
        holdings_by_id = {h.id: h for h in rows}

    for candidate in candidates:
        holding_ids = holding_ids_by_key.get(candidate.input_key, [])
        holdings = [holdings_by_id[hid] for hid in holding_ids if hid in holdings_by_id]
        await persist_candidate(session, candidate, holdings, now=now, result=result)

    await session.flush()
    return result


# --- read helpers (API / planner) --------------------------------------------


async def constituents_for_fund(
    session: AsyncSession, fund_id: int, *, status: str | None = None
) -> list[FundHolding]:
    """A fund's coherent holdings snapshot, optionally filtered by identity state.

    ``status`` accepts ``resolved`` / ``ambiguous`` / ``not_found`` / ``failed``
    / ``unresolved`` (the latter == no link and no terminal status yet).
    """
    snapshot = await holdings_service.latest_holdings_for_fund(session, fund_id)
    if status is None:
        return snapshot
    if status == "resolved":
        return [h for h in snapshot if h.holding_instrument_id is not None]
    if status == "unresolved":
        return [
            h
            for h in snapshot
            if h.holding_instrument_id is None and h.identity_status in (None, "failed")
        ]
    return [h for h in snapshot if h.identity_status == status]


async def instruments_by_id(
    session: AsyncSession, instrument_ids: list[int]
) -> dict[int, Instrument]:
    if not instrument_ids:
        return {}
    rows = (
        await session.execute(select(Instrument).where(Instrument.id.in_(set(instrument_ids))))
    ).scalars()
    return {inst.id: inst for inst in rows}


def identity_state(holding: FundHolding) -> str:
    """Derived identity-resolution state for a holding (GUI-friendly)."""
    if holding.holding_instrument_id is not None:
        return "resolved"
    if holding.identity_status in ("ambiguous", "not_found", "failed", "manual"):
        return holding.identity_status
    return "unresolved"


_NEXT_ACTION = {
    "resolved": "ready_for_price_ingestion",
    "unresolved": "resolve_identity",
    "failed": "retry_resolution",
    "ambiguous": "manual_disambiguation",
    "not_found": "manual_identification",
    "manual": "none",
}


def next_action(state: str) -> str:
    return _NEXT_ACTION.get(state, "resolve_identity")


async def hydrate_holdings_with_instruments(
    session: AsyncSession, holdings: list[FundHolding]
) -> dict[int, Instrument]:
    """Load the instruments linked by a holdings list (for include_identity)."""
    ids = [h.holding_instrument_id for h in holdings if h.holding_instrument_id is not None]
    return await instruments_by_id(session, ids)


async def get_instrument(session: AsyncSession, instrument_id: int) -> Instrument | None:
    return await session.get(Instrument, instrument_id)


async def listings_for_instrument(
    session: AsyncSession, instrument_id: int
) -> list[InstrumentListing]:
    return list(
        (
            await session.execute(
                select(InstrumentListing)
                .where(InstrumentListing.instrument_id == instrument_id)
                .order_by(InstrumentListing.id)
            )
        )
        .scalars()
        .all()
    )


async def identifiers_for_instrument(
    session: AsyncSession, instrument_id: int
) -> list[InstrumentIdentifier]:
    return list(
        (
            await session.execute(
                select(InstrumentIdentifier)
                .where(InstrumentIdentifier.instrument_id == instrument_id)
                .order_by(InstrumentIdentifier.scheme, InstrumentIdentifier.id)
            )
        )
        .scalars()
        .all()
    )
