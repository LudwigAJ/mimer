"""Currency conversion helpers.

FX convention (see `FxRate`): `rate` is the number of `quote_currency` units per
1 unit of `base_currency`. Pence-quoted units (GBX) are normalised to GBP by
dividing by 100. When no rate is available the conversion returns ``None`` so
callers can decide how to present missing data.
"""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import FxRate

_PENCE_UNITS = {"GBX", "GBP_PENCE"}
_HUNDRED = Decimal(100)

FxMap = dict[tuple[str, str], Decimal]


def normalise_currency(unit: str | None, default: str = "GBP") -> str:
    """Collapse pence-quoted units to their pound equivalent (GBX -> GBP)."""
    if unit is None:
        return default
    upper = unit.upper()
    if upper in _PENCE_UNITS:
        return "GBP"
    return upper


async def load_fx_map(session: AsyncSession) -> FxMap:
    """Return the latest rate for each (base_currency, quote_currency) pair."""
    rows = (await session.execute(select(FxRate).order_by(FxRate.rate_date.asc()))).scalars().all()
    fx_map: FxMap = {}
    for row in rows:
        # Later rows win because they are ordered by ascending date.
        fx_map[(row.base_currency.upper(), row.quote_currency.upper())] = row.rate
    return fx_map


def convert(
    amount: Decimal | None,
    from_unit: str | None,
    to_currency: str,
    fx_map: FxMap,
) -> Decimal | None:
    """Convert ``amount`` expressed in ``from_unit`` into ``to_currency``."""
    if amount is None:
        return None

    target = to_currency.upper()
    unit = (from_unit or target).upper()
    value = Decimal(amount)

    if unit in _PENCE_UNITS:
        value = value / _HUNDRED
        unit = "GBP"

    if unit == target:
        return value

    # rate keyed (base, quote) = quote per 1 base.
    direct = fx_map.get((target, unit))
    if direct is not None and direct != 0:
        return value / direct

    inverse = fx_map.get((unit, target))
    if inverse is not None:
        return value * inverse

    return None
