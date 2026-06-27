"""Source rate-budget + fetch-log read schemas.

These never carry secrets: budgets hold only numeric windows/notes, and fetch
logs hold a safe request key, an endpoint *label*, and hashes — never API keys,
auth headers or tokenised URLs.
"""

from __future__ import annotations

from datetime import datetime

from app.schemas.common import ORMModel


class SourceRateLimitRead(ORMModel):
    id: int
    source_name: str
    is_enabled: bool
    max_requests_per_minute: int | None
    max_requests_per_hour: int | None
    max_requests_per_day: int | None
    max_concurrency: int | None
    min_delay_ms: int | None
    batch_size: int | None
    backoff_seconds: int | None
    backoff_until: datetime | None
    last_request_at: datetime | None
    notes: str | None
    created_at: datetime
    updated_at: datetime


class SourceBudgetRead(SourceRateLimitRead):
    """A budget row plus its *current* decision (allowed/why/wait)."""

    allowed: bool = True
    reason: str = "ok"
    wait_seconds: float = 0.0
    in_backoff: bool = False


class SourceFetchLogRead(ORMModel):
    id: int
    source_name: str
    request_kind: str
    request_key: str
    request_hash: str
    endpoint_label: str | None
    method: str | None
    status: str
    http_status: int | None
    started_at: datetime
    finished_at: datetime | None
    duration_ms: int | None
    records_inserted: int | None
    records_updated: int | None
    records_failed: int | None
    error_code: str | None
    error_message: str | None
    rate_limited: bool
    backoff_until: datetime | None
    cache_hit: bool
    raw_payload_hash: str | None
    created_at: datetime
