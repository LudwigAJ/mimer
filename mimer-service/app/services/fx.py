"""FX lookup + conversion with provenance.

This is the *read-side* counterpart to ``app/services/fx_ingestion.py``. It loads
the stored ``fx_rates`` into an in-memory `FxIndex` once per request and answers
rate / conversion questions against it, carrying full provenance so the GUI can
show where a number came from and how confident it is.

Resolution order for a pair (``from`` -> ``to``), preferring the strongest path:

1. same currency  -> rate 1 (``is_direct``)
2. direct pair    -> stored ``from/to`` rate (``is_direct``)
3. inverse pair   -> stored ``to/from`` rate, inverted (``is_inverse``)
4. triangulation  -> via a pivot (USD, then EUR): ``from->pivot`` * ``pivot->to``
   (``is_triangulated``)
5. otherwise      -> a clear *missing* result (never a silent rate of 1)

Among competing sources for the same pair/date a code source-priority is applied
(``manual`` > official > ``fx_fixture`` > ``derived`` > ``seed``); a caller may
pin a ``source_policy`` and we record ``requested_source`` / ``effective_source``
/ ``fallback_used`` so source-selection UIs have what they need.

FX convention (see `FxRate`): ``rate`` = units of ``quote`` per 1 unit of
``base``. Pence-quoted inputs (GBX) are normalised to GBP (÷100) so listing-level
amounts can be passed straight in.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import FxRate
from app.services import freshness as freshness_service

# Pence-quoted units normalised to GBP (mirrors app/services/conversion.py).
_PENCE_UNITS = {"GBX", "GBP_PENCE"}
_HUNDRED = Decimal(100)
_ONE = Decimal(1)

# Code source-priority for choosing among competing rates (lower = preferred).
# Mirrors the data_sources priority convention and the holdings snapshot rule.
_FX_SOURCE_PRIORITY: dict[str, int] = {
    "manual": 5,
    "ecb": 10,
    "boe": 10,
    "fx_fixture": 20,
    "derived": 30,
    "seed": 100,
}
_DEFAULT_FX_PRIORITY = 50

# Pivot/vehicle currencies tried for triangulation, in order. Includes the
# common reporting base (GBP) because the fixture/seed rates are GBP-anchored, as
# well as the usual EUR/USD vehicles (e.g. an ECB EUR-based feed).
_PIVOTS = ("USD", "EUR", "GBP")

# Statuses (aligned with the freshness vocabulary used across the GUI).
FRESH = freshness_service.FRESH
STALE = freshness_service.STALE
MISSING = freshness_service.MISSING


def fx_source_priority(source: str | None) -> int:
    return _FX_SOURCE_PRIORITY.get(source or "", _DEFAULT_FX_PRIORITY)


def normalise_pence(amount: Decimal | None, currency: str | None) -> tuple[Decimal | None, str]:
    """Collapse a pence-quoted amount to its pound equivalent (GBX -> GBP)."""
    code = (currency or "").upper()
    if code in _PENCE_UNITS:
        return (None if amount is None else amount / _HUNDRED), "GBP"
    return amount, code


@dataclass(frozen=True)
class FxQuote:
    """A single resolved rate observation for a pair."""

    rate: Decimal
    rate_date: date
    source: str
    status: str | None


@dataclass
class FxConversionResult:
    """Outcome of a rate lookup / amount conversion, with full provenance."""

    from_currency: str
    to_currency: str
    converted_amount: Decimal | None
    rate: Decimal | None
    rate_date: date | None
    source: str | None
    # fresh | stale | missing (freshness of the rate used; "fresh" same-currency).
    status: str
    amount: Decimal | None = None
    is_direct: bool = False
    is_inverse: bool = False
    is_triangulated: bool = False
    missing_reason: str | None = None
    # Source-selection metadata (for source-policy aware GUIs).
    requested_source: str | None = None
    effective_source: str | None = None
    fallback_used: bool = False
    available_sources: list[str] = field(default_factory=list)


# Internal resolution carrier (direct/inverse only, before triangulation).
@dataclass
class _Leg:
    rate: Decimal
    rate_date: date
    source: str
    status: str
    is_inverse: bool
    fallback_used: bool


class FxIndex:
    """In-memory view of ``fx_rates`` supporting as-of + source-policy lookups."""

    def __init__(self, observations: dict[tuple[str, str], dict[str, list[FxQuote]]]) -> None:
        # (base, quote) -> source -> [FxQuote sorted by rate_date asc]
        self._obs = observations

    # --- construction --------------------------------------------------------

    @classmethod
    def from_rows(cls, rows: list[FxRate]) -> FxIndex:
        obs: dict[tuple[str, str], dict[str, list[FxQuote]]] = {}
        for row in rows:
            key = (row.base_currency.upper(), row.quote_currency.upper())
            by_source = obs.setdefault(key, {})
            by_source.setdefault(row.source, []).append(
                FxQuote(
                    rate=row.rate,
                    rate_date=row.rate_date,
                    source=row.source,
                    status=row.status,
                )
            )
        for by_source in obs.values():
            for quotes in by_source.values():
                quotes.sort(key=lambda q: q.rate_date)
        return cls(obs)

    # --- low-level pair selection -------------------------------------------

    def _quotes_for(self, base: str, quote: str) -> dict[str, list[FxQuote]]:
        return self._obs.get((base, quote), {})

    def available_sources(self, base: str, quote: str) -> list[str]:
        sources = set(self._quotes_for(base, quote)) | set(self._quotes_for(quote, base))
        return sorted(sources)

    def _best_in_source(self, quotes: list[FxQuote], as_of: date | None) -> FxQuote | None:
        if not quotes:
            return None
        if as_of is None:
            return max(quotes, key=lambda q: q.rate_date)
        on_or_before = [q for q in quotes if q.rate_date <= as_of]
        if on_or_before:
            return max(on_or_before, key=lambda q: q.rate_date)
        # No rate on/before the as-of date (e.g. a historical distribution but we
        # only hold recent rates). Fall back to the earliest available rate so
        # the conversion still resolves; freshness is derived from its date, so a
        # caller can see it post-dates the as-of moment.
        return min(quotes, key=lambda q: q.rate_date)

    def _select(
        self, base: str, quote: str, as_of: date | None, source_policy: str | None
    ) -> tuple[FxQuote | None, bool]:
        """Best stored quote for (base, quote); returns (quote, fallback_used)."""
        by_source = self._quotes_for(base, quote)
        if not by_source:
            return None, False

        def best_for(sources: list[str]) -> FxQuote | None:
            found = [
                q for s in sources if (q := self._best_in_source(by_source[s], as_of)) is not None
            ]
            if not found:
                return None
            # Prefer source priority, then the most recent observation.
            return min(
                found, key=lambda q: (fx_source_priority(q.source), -q.rate_date.toordinal())
            )

        if source_policy is not None:
            pinned = best_for([source_policy]) if source_policy in by_source else None
            if pinned is not None:
                return pinned, False
            return best_for(list(by_source)), True  # fall back to any source

        return best_for(list(by_source)), False

    # --- pair resolution (direct or inverse) --------------------------------

    def _resolve_leg(
        self, base: str, quote: str, as_of: date | None, source_policy: str | None
    ) -> _Leg | None:
        direct, fb_direct = self._select(base, quote, as_of, source_policy)
        if direct is not None:
            return _Leg(
                rate=direct.rate,
                rate_date=direct.rate_date,
                source=direct.source,
                status=freshness_service.freshness_state(direct.rate_date, kind="fx"),
                is_inverse=False,
                fallback_used=fb_direct,
            )
        inverse, fb_inverse = self._select(quote, base, as_of, source_policy)
        if inverse is not None and inverse.rate != 0:
            return _Leg(
                rate=_ONE / inverse.rate,
                rate_date=inverse.rate_date,
                source=inverse.source,
                status=freshness_service.freshness_state(inverse.rate_date, kind="fx"),
                is_inverse=True,
                fallback_used=fb_inverse,
            )
        return None

    # --- public API ----------------------------------------------------------

    def get_fx_rate(
        self,
        base_currency: str,
        quote_currency: str,
        as_of_date: date | None = None,
        source_policy: str | None = None,
    ) -> FxConversionResult:
        """Resolve ``quote per base`` with provenance (no amount applied)."""
        base, quote = base_currency.upper(), quote_currency.upper()
        result = FxConversionResult(
            from_currency=base,
            to_currency=quote,
            converted_amount=None,
            rate=None,
            rate_date=None,
            source=None,
            status=MISSING,
            amount=_ONE,
            requested_source=source_policy,
            available_sources=self.available_sources(base, quote),
        )

        if base == quote:
            result.rate = _ONE
            result.converted_amount = _ONE
            result.rate_date = as_of_date
            result.status = FRESH
            result.is_direct = True
            result.effective_source = None
            return result

        # 1) direct / 2) inverse
        leg = self._resolve_leg(base, quote, as_of_date, source_policy)
        if leg is not None:
            result.rate = leg.rate
            result.converted_amount = leg.rate
            result.rate_date = leg.rate_date
            result.source = leg.source
            result.effective_source = leg.source
            result.status = leg.status
            result.is_direct = not leg.is_inverse
            result.is_inverse = leg.is_inverse
            result.fallback_used = leg.fallback_used
            return result

        # 3) triangulation via a pivot currency
        for pivot in _PIVOTS:
            if pivot in (base, quote):
                continue
            first = self._resolve_leg(base, pivot, as_of_date, source_policy)
            second = self._resolve_leg(pivot, quote, as_of_date, source_policy)
            if first is None or second is None:
                continue
            rate = first.rate * second.rate
            rate_date = min(first.rate_date, second.rate_date)
            legs_source = first.source if first.source == second.source else "derived"
            result.rate = rate
            result.converted_amount = rate
            result.rate_date = rate_date
            result.source = legs_source
            result.effective_source = legs_source
            result.status = freshness_service.freshness_state(rate_date, kind="fx")
            result.is_triangulated = True
            result.fallback_used = first.fallback_used or second.fallback_used
            return result

        result.missing_reason = "no_fx_path"
        return result

    def convert_amount(
        self,
        amount: Decimal | None,
        from_currency: str,
        to_currency: str,
        as_of_date: date | None = None,
        source_policy: str | None = None,
    ) -> FxConversionResult:
        """Convert ``amount`` from one currency to another, with provenance.

        Pence-quoted inputs (GBX) are normalised to GBP first. ``amount`` may be
        ``None`` (e.g. when a price is unknown): the rate is still resolved so the
        caller can show metadata, but ``converted_amount`` stays ``None``.
        """
        norm_amount, from_code = normalise_pence(amount, from_currency)
        to_code = to_currency.upper()

        rate_result = self.get_fx_rate(from_code, to_code, as_of_date, source_policy)
        rate_result.amount = norm_amount
        if rate_result.rate is not None and norm_amount is not None:
            rate_result.converted_amount = norm_amount * rate_result.rate
        else:
            rate_result.converted_amount = None
        return rate_result


async def load_fx_index(session: AsyncSession) -> FxIndex:
    """Build an `FxIndex` from every stored FX rate (bounded reference data)."""
    rows = list((await session.execute(select(FxRate))).scalars().all())
    return FxIndex.from_rows(rows)
