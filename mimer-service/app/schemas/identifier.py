"""Security identifier (crosswalk / provenance) read schema."""

from __future__ import annotations

from datetime import datetime

from app.schemas.common import ORMModel


class SecurityIdentifierRead(ORMModel):
    id: int
    scheme: str
    value: str
    fund_id: int | None
    fund_listing_id: int | None
    exchange: str | None
    currency: str | None
    source: str
    confidence: str
    created_at: datetime
    updated_at: datetime
