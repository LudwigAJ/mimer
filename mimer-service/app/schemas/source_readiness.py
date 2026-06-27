"""Schemas for the production data-source readiness matrix."""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel

from app.schemas.common import Meta


class SourceReadinessRead(BaseModel):
    """One source's operational readiness for live VPS ingestion + scheduling."""

    data_type: str
    source_name: str
    provider: str
    # fixture | implemented_live | verified_live | candidate | planned | unsupported
    status: str
    worker_name: str | None
    recommended_cadence: str
    default_for_worker: bool
    safe_for_scheduler: bool
    requires_secret: bool
    requires_url_config: bool
    requires_running_gateway: bool
    last_verified_at: date | None
    known_blockers: str | None
    next_action: str | None
    notes: str | None
    # Public reference identifiers only (e.g. ``ticker:ISIN``) — never secrets/tokenised URLs.
    example_targets: list[str] = []


class SourceReadinessSummary(BaseModel):
    """Compact rollup of the readiness matrix (also embedded in capabilities)."""

    total_sources: int
    status_counts: dict[str, int]
    scheduler_safe_count: int
    verified_live_count: int
    candidate_count: int
    planned_count: int
    fixture_count: int
    required_live_data_types: list[str]
    missing_required_live_sources: list[str]
    scheduler_safe_sources: list[str]


class SourceReadinessMatrix(BaseModel):
    """The readiness matrix list (``{data, meta}`` envelope) plus its summary rollup."""

    data: list[SourceReadinessRead]
    meta: Meta
    summary: SourceReadinessSummary
