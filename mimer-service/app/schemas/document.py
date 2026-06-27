"""Document snapshot read schema."""

from __future__ import annotations

from datetime import date, datetime

from app.schemas.common import ORMModel


class DocumentRead(ORMModel):
    id: int
    fund_id: int
    # Populated by aggregate endpoints (dashboard/detail/fund documents) where the
    # fund is known; None on the flat global list endpoint.
    fund_name: str | None = None
    document_type: str
    title: str | None = None
    url: str | None
    document_date: date | None
    language: str | None = None
    country_or_region: str | None = None
    content_type: str | None = None
    content_hash: str | None
    # Change detection vs the previous snapshot of the same (fund, type, source).
    change_status: str | None = None
    previous_content_hash: str | None = None
    previous_snapshot_id: int | None = None
    status: str | None
    source: str
    fetched_at: datetime | None = None
    created_at: datetime
