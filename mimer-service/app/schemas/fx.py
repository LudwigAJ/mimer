"""FX conversion read schema (provenance-rich result of a rate lookup)."""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel

from app.schemas.common import DecimalStr


class FxConversionRead(BaseModel):
    """Serialised `app.services.fx.FxConversionResult`.

    Carries enough provenance for a source-selection aware GUI: which path was
    used (direct/inverse/triangulated), the rate's freshness ``status``, and the
    requested vs effective source plus whether a fallback was needed.
    """

    from_currency: str
    to_currency: str
    amount: DecimalStr | None
    converted_amount: DecimalStr | None
    rate: DecimalStr | None
    rate_date: date | None
    source: str | None
    # fresh | stale | missing
    status: str
    is_direct: bool
    is_inverse: bool
    is_triangulated: bool
    missing_reason: str | None = None
    # Source-policy metadata.
    requested_source: str | None = None
    effective_source: str | None = None
    fallback_used: bool = False
    available_sources: list[str] = []
