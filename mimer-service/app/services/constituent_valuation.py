"""True constituent look-through valuation — the price-aware classification layer.

The fund-level exposure pipeline (``app/services/exposure_recompute.py``) values
each portfolio position in base currency, then distributes that value through the
fund's holdings *weights* to produce country/sector/holding/... exposure. That is
already a look-through, but it stops at the holding *weight* — it never touches
the resolved constituent instrument, its EOD price, or the FX needed to value it.

This module adds the missing layer: for each fund holding it resolves the
canonical instrument (constituent identity resolution), finds that instrument's
primary listing + latest constituent EOD price (``instrument_prices``), and the
FX to convert the price currency to the workspace base. It then *classifies* the
holding so the recompute can emit constituent-aware rows and weight-based coverage
metrics.

Crucial honesty (see AGENTS.md): the implied constituent value is still
``position_market_value_base x holding_weight`` — a **weight-based estimate**, not
a share/price-derived notional. ETFs publish weights, not the exact share counts
inside *your* position, so the constituent EOD price + FX are attached as
*coverage / performance context*, never used to invent a notional. Missing price
or FX is surfaced (``price_missing`` / ``fx_missing``), never silently treated as
zero. This module performs NO network I/O — all inputs are DB rows.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import FundHolding, Instrument, InstrumentListing, InstrumentPrice
from app.services import constituent_identity
from app.services import freshness as freshness_service
from app.services import instrument_prices as instrument_prices_service
from app.services.fx import FxConversionResult, FxIndex

# --- valuation methods -------------------------------------------------------
# How a constituent row's implied value was derived. Today every row is
# weight-based; the price-context variant additionally has a constituent EOD
# price attached. The share/market_value methods are reserved for when holdings
# carry exact shares / market values (not used yet — see AGENTS.md).
METHOD_FUND_WEIGHT = "fund_weight_lookthrough"
METHOD_FUND_WEIGHT_PRICED = "fund_weight_with_constituent_price_context"
METHOD_HOLDING_MARKET_VALUE = "holding_market_value"
METHOD_HOLDING_SHARES_PRICE = "holding_shares_x_price"
METHOD_UNCLASSIFIED = "unclassified"

# --- row statuses (constituent dimensions) -----------------------------------
STATUS_OK = "ok"  # resolved, fresh constituent price, FX to base available
STATUS_STALE_PRICE = "stale_price"  # resolved + priced + FX, but price is stale
STATUS_FX_MISSING = "fx_missing"  # resolved + priced, but no FX path to base
STATUS_MISSING_PRICE = "price_missing"  # resolved + listing, but no EOD price yet
STATUS_MISSING_LISTING = "missing_listing"  # resolved instrument, no tradable listing
STATUS_UNRESOLVED = "unresolved_identity"  # holding not linked to an instrument
STATUS_UNCLASSIFIED = "unclassified"  # remainder / missing-holdings weight

# --- price-status funnel buckets ---------------------------------------------
# Buckets for the ``constituent_price_status`` dimension. Together they partition
# the full looked-through-plus-remainder weight, so the dimension sums to ~1.0 and
# reads as a coverage funnel.
PRICE_BUCKET_PRICED_FRESH = "priced_fresh"
PRICE_BUCKET_PRICED_STALE = "priced_stale"
PRICE_BUCKET_PRICE_MISSING = "price_missing"
PRICE_BUCKET_FX_MISSING = "fx_missing"
PRICE_BUCKET_MISSING_LISTING = "missing_listing"
PRICE_BUCKET_UNRESOLVED = "unresolved_identity"
PRICE_BUCKET_UNCLASSIFIED = "unclassified"

_PRICE_BUCKET_LABELS = {
    PRICE_BUCKET_PRICED_FRESH: "Priced (fresh)",
    PRICE_BUCKET_PRICED_STALE: "Priced (stale)",
    PRICE_BUCKET_PRICE_MISSING: "Price missing",
    PRICE_BUCKET_FX_MISSING: "FX missing",
    PRICE_BUCKET_MISSING_LISTING: "No listing",
    PRICE_BUCKET_UNRESOLVED: "Unresolved identity",
    PRICE_BUCKET_UNCLASSIFIED: "Unclassified",
}

# Synthetic constituent-dimension buckets for non-resolved weight.
BUCKET_UNRESOLVED = "__unresolved__"
BUCKET_UNCLASSIFIED = "__unclassified__"
LABEL_UNRESOLVED = "Unresolved constituents"


def price_bucket_label(bucket: str) -> str:
    return _PRICE_BUCKET_LABELS.get(bucket, bucket)


@dataclass
class ConstituentInfo:
    """The resolved instrument + latest price + FX for one ``instrument_id``."""

    instrument: Instrument | None
    listing: InstrumentListing | None
    price: InstrumentPrice | None
    # fresh | stale | missing (of the latest constituent price).
    price_state: str
    # price currency -> base conversion (None when there is no price).
    fx: FxConversionResult | None


@dataclass
class ConstituentClassification:
    """How one holding contributes to the constituent look-through, classified."""

    is_resolved: bool
    status: str
    price_status_bucket: str
    # Stable bucket key for the ``constituent`` dimension (one per instrument).
    bucket: str
    label: str
    instrument_id: int | None = None
    instrument_listing_id: int | None = None
    currency: str | None = None
    price_date: date | None = None
    price_source: str | None = None
    price_status: str | None = None
    fx_rate: Decimal | None = None
    fx_source: str | None = None
    valuation_method: str = METHOD_FUND_WEIGHT
    # Coverage flags (price present at all / FX usable).
    is_priced: bool = False
    is_fx_ok: bool = False


async def load_constituent_infos(
    session: AsyncSession,
    instrument_ids: list[int],
    *,
    fx_index: FxIndex,
    base: str,
) -> dict[int, ConstituentInfo]:
    """Resolve instrument + primary listing + latest price + FX, per instrument.

    Deduped: one read per distinct resolved instrument even if it is held via
    several funds (so a live look-through never loops per holding). ``fx_index``
    and ``base`` come from the caller's single FX load — no extra DB round trip.
    """
    if not instrument_ids:
        return {}
    instruments = await constituent_identity.instruments_by_id(session, instrument_ids)
    price_map = await instrument_prices_service.latest_constituent_prices(session, instrument_ids)

    infos: dict[int, ConstituentInfo] = {}
    for iid in instrument_ids:
        instrument = instruments.get(iid)
        listing, price = price_map.get(iid, (None, None))
        if price is not None:
            state = freshness_service.freshness_state(price.price_date, kind="price")
            currency = price.currency or (listing.currency if listing else None)
            fx = fx_index.convert_amount(price.close, currency, base) if currency else None
        else:
            state = freshness_service.MISSING
            fx = None
        infos[iid] = ConstituentInfo(
            instrument=instrument, listing=listing, price=price, price_state=state, fx=fx
        )
    return infos


def classify(holding: FundHolding, infos: dict[int, ConstituentInfo]) -> ConstituentClassification:
    """Classify one holding's constituent look-through state for valuation.

    Order of resolution (most-blocking first): unresolved identity -> no tradable
    listing -> no EOD price -> price present but no FX path -> stale price -> ok.
    The implied value stays weight-based regardless; price/FX is context only.
    """
    iid = holding.holding_instrument_id
    if iid is None:
        return ConstituentClassification(
            is_resolved=False,
            status=STATUS_UNRESOLVED,
            price_status_bucket=PRICE_BUCKET_UNRESOLVED,
            bucket=BUCKET_UNRESOLVED,
            label=LABEL_UNRESOLVED,
            valuation_method=METHOD_FUND_WEIGHT,
        )

    info = infos.get(iid)
    instrument = info.instrument if info else None
    listing = info.listing if info else None
    price = info.price if info else None
    label = (instrument.name if instrument else None) or holding.security_name
    bucket = f"instrument:{iid}"

    if listing is None:
        return ConstituentClassification(
            is_resolved=True,
            status=STATUS_MISSING_LISTING,
            price_status_bucket=PRICE_BUCKET_MISSING_LISTING,
            bucket=bucket,
            label=label,
            instrument_id=iid,
            valuation_method=METHOD_FUND_WEIGHT,
        )

    if price is None:
        return ConstituentClassification(
            is_resolved=True,
            status=STATUS_MISSING_PRICE,
            price_status_bucket=PRICE_BUCKET_PRICE_MISSING,
            bucket=bucket,
            label=label,
            instrument_id=iid,
            instrument_listing_id=listing.id,
            currency=listing.currency,
            valuation_method=METHOD_FUND_WEIGHT,
        )

    fx = info.fx if info else None
    fx_ok = fx is not None and fx.converted_amount is not None
    common = {
        "is_resolved": True,
        "bucket": bucket,
        "label": label,
        "instrument_id": iid,
        "instrument_listing_id": listing.id,
        "currency": price.currency or listing.currency,
        "price_date": price.price_date,
        "price_source": price.source,
        "price_status": price.status,
        "fx_rate": fx.rate if fx else None,
        "fx_source": fx.source if fx else None,
        "valuation_method": METHOD_FUND_WEIGHT_PRICED,
        "is_priced": True,
    }
    if not fx_ok:
        return ConstituentClassification(
            status=STATUS_FX_MISSING,
            price_status_bucket=PRICE_BUCKET_FX_MISSING,
            is_fx_ok=False,
            **common,
        )
    if info is not None and info.price_state == freshness_service.STALE:
        return ConstituentClassification(
            status=STATUS_STALE_PRICE,
            price_status_bucket=PRICE_BUCKET_PRICED_STALE,
            is_fx_ok=True,
            **common,
        )
    return ConstituentClassification(
        status=STATUS_OK,
        price_status_bucket=PRICE_BUCKET_PRICED_FRESH,
        is_fx_ok=True,
        **common,
    )
