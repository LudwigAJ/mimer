"""Read schema for the `data_sources` priority/registry table."""

from __future__ import annotations

from datetime import datetime

from app.schemas.common import ORMModel


class DataSourceRead(ORMModel):
    id: int
    name: str
    source_type: str
    base_url: str | None
    priority: int
    notes: str | None
    is_active: bool
    created_at: datetime
    updated_at: datetime
