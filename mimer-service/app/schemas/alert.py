"""Alert schemas.

`AlertRead` is the workspace-scoped alert row as the GUI consumes it (content +
lifecycle state + related-entity pointers). `AlertSummary` / `AlertCounts` are
the compact aggregates surfaced on the dashboard and in diagnostics.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel

from app.schemas.common import ORMModel


class AlertRead(ORMModel):
    id: int
    workspace_id: int
    severity: str
    category: str
    title: str
    message: str | None
    # active | read | dismissed | resolved
    status: str
    source: str | None
    related_entity_type: str | None
    related_entity_id: str | None
    related_fund_id: int | None
    related_fund_listing_id: int | None
    related_document_snapshot_id: int | None
    related_job_run_id: int | None
    dedupe_key: str
    first_seen_at: datetime
    last_seen_at: datetime
    read_at: datetime | None
    dismissed_at: datetime | None
    resolved_at: datetime | None
    created_at: datetime
    updated_at: datetime
    raw_payload_json: Any | None = None


class AlertSummary(BaseModel):
    """Compact dashboard summary of a workspace's open alerts."""

    active: int = 0
    unread: int = 0
    # Highest severity among open (active/read) alerts, or None if none.
    highest_severity: str | None = None
    by_severity: dict[str, int] = {}
    by_category: dict[str, int] = {}


class AlertCounts(BaseModel):
    """Alert counts merged into the diagnostics payload."""

    active_alerts: int = 0
    unread_alerts: int = 0
    critical_alerts: int = 0
    error_alerts: int = 0
    warning_alerts: int = 0
    document_alerts: int = 0
    price_alerts: int = 0
    fx_alerts: int = 0
    job_alerts: int = 0


class MarkAllReadResponse(BaseModel):
    marked_read: int
