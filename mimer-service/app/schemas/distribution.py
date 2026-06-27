"""Distribution read schema."""

from __future__ import annotations

from datetime import date, datetime

from app.schemas.common import DecimalStr, ORMModel


class DistributionRead(ORMModel):
    id: int
    fund_id: int
    # Populated by aggregate endpoints (dashboard/detail) where the fund is known;
    # None on the flat global list endpoint.
    fund_name: str | None = None
    ex_date: date
    record_date: date | None
    payment_date: date | None
    # Issuer's labelled distribution date + optional issuer labels (when published).
    distribution_date: date | None = None
    amount: DecimalStr
    currency: str
    distribution_type: str | None = None
    frequency: str | None = None
    share_class: str | None = None
    source: str
    status: str | None
    created_at: datetime
    # Optional workspace-base-currency overlay, populated by aggregate endpoints
    # (dashboard/detail) when an FX rate is available. ``amount`` stays the
    # original declared amount/currency; this is a derived convenience.
    base_currency: str | None = None
    amount_base: DecimalStr | None = None
    fx_rate: DecimalStr | None = None
    fx_source: str | None = None
    # fresh | stale | missing — freshness of the FX rate used (None if unconverted).
    fx_status: str | None = None
