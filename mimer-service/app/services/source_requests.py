"""Fetch log / request-cache foundation for external data sources.

A generic, secrets-free layer that records every external fetch attempt and lets
identical requests be skipped cheaply. The deterministic *request key* is the
backbone: it dedupes work, ties a fetch to its source budget window, and stays
safe to persist.

SECURITY (see AGENTS.md):
  * the request key is built from *normalised params only* — known credential
    keys (api_key/token/authorization/...) are dropped before hashing;
  * we store an ``endpoint_label`` (a host/path class), never a tokenised URL;
  * we store a ``raw_payload_hash``, never the raw payload or auth headers.

This is observability + a recent-success cache, not a perfect distributed limiter.
It exists so future live adapters (OpenFIGI / yfinance / Stooq / issuer sites)
never spam a source in an uncontrolled per-holding loop.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import SourceFetchLog

# Param keys that must never end up in a persisted request key / log.
_SENSITIVE_PARAM_KEYS = {
    "api_key",
    "apikey",
    "x-openfigi-apikey",
    "authorization",
    "auth",
    "token",
    "access_token",
    "key",
    "secret",
    "password",
}

STARTED = "started"
SUCCESS = "success"
FAILED = "failed"
RATE_LIMITED = "rate_limited"
CACHE_HIT = "cache_hit"

# Statuses that represent a real attempt against the source (consume budget).
BUDGET_CONSUMING_STATUSES = (STARTED, SUCCESS, FAILED, RATE_LIMITED)


@dataclass(frozen=True)
class SourceFetchResult:
    """Outcome handle for a guarded fetch (returned to callers)."""

    status: str
    cache_hit: bool
    request_key: str
    fetch_log_id: int | None


def _normalize_value(value: Any) -> str:
    if isinstance(value, (list, tuple, set)):
        return ",".join(sorted(_normalize_value(v) for v in value))
    return str(value).strip().upper()


def _normalize_params(params: dict[str, Any] | None) -> str:
    if not params:
        return ""
    parts: list[str] = []
    for key in sorted(params):
        if key.lower() in _SENSITIVE_PARAM_KEYS:
            continue
        value = params[key]
        if value is None:
            continue
        parts.append(f"{key.lower()}={_normalize_value(value)}")
    return "&".join(parts)


def build_request_key(source: str, request_kind: str, params: dict[str, Any] | None = None) -> str:
    """Deterministic, secrets-free key: ``source:request_kind:normalised_params``."""
    return f"{source}:{request_kind}:{_normalize_params(params)}"


def request_hash(request_key: str) -> str:
    return hashlib.sha256(request_key.encode("utf-8")).hexdigest()


def payload_hash(payload: Any) -> str:
    """Stable hash of a provider payload (provenance/dedupe — never the payload)."""
    if isinstance(payload, (bytes, bytearray)):
        data = bytes(payload)
    elif isinstance(payload, str):
        data = payload.encode("utf-8")
    else:
        data = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


async def should_skip_recent_success(
    session: AsyncSession,
    *,
    source: str,
    request_kind: str,
    params: dict[str, Any] | None = None,
    request_key: str | None = None,
    ttl_seconds: int,
    now: datetime | None = None,
) -> SourceFetchLog | None:
    """Return the most recent successful log within ``ttl_seconds``, else None.

    A non-None result means an identical request succeeded recently and should be
    served from cache rather than re-fetched.
    """
    now = now or datetime.now(UTC)
    key = request_key or build_request_key(source, request_kind, params)
    cutoff = now - timedelta(seconds=ttl_seconds)
    stmt = (
        select(SourceFetchLog)
        .where(
            SourceFetchLog.request_key == key,
            SourceFetchLog.status == SUCCESS,
            SourceFetchLog.finished_at.is_not(None),
            SourceFetchLog.finished_at >= cutoff,
        )
        .order_by(SourceFetchLog.id.desc())
    )
    return await session.scalar(stmt)


async def record_fetch_start(
    session: AsyncSession,
    *,
    source: str,
    request_kind: str,
    params: dict[str, Any] | None = None,
    request_key: str | None = None,
    endpoint_label: str | None = None,
    method: str | None = None,
    now: datetime | None = None,
) -> SourceFetchLog:
    """Create and persist a ``started`` fetch-log row (no secrets)."""
    now = now or datetime.now(UTC)
    key = request_key or build_request_key(source, request_kind, params)
    log = SourceFetchLog(
        source_name=source,
        request_kind=request_kind,
        request_key=key,
        request_hash=request_hash(key),
        endpoint_label=endpoint_label,
        method=method,
        status=STARTED,
        started_at=now,
    )
    session.add(log)
    await session.flush()
    return log


def _finalize(log: SourceFetchLog, now: datetime) -> None:
    log.finished_at = now
    started = log.started_at
    if started is not None:
        if started.tzinfo is None:
            started = started.replace(tzinfo=UTC)
        log.duration_ms = max(0, int((now - started).total_seconds() * 1000))


async def record_fetch_success(
    session: AsyncSession,
    log: SourceFetchLog,
    *,
    http_status: int | None = None,
    records_inserted: int | None = None,
    records_updated: int | None = None,
    records_failed: int | None = None,
    raw_payload_hash: str | None = None,
    now: datetime | None = None,
) -> SourceFetchLog:
    now = now or datetime.now(UTC)
    log.status = SUCCESS
    log.http_status = http_status
    log.records_inserted = records_inserted
    log.records_updated = records_updated
    log.records_failed = records_failed
    log.raw_payload_hash = raw_payload_hash
    _finalize(log, now)
    await session.flush()
    return log


async def record_fetch_failure(
    session: AsyncSession,
    log: SourceFetchLog,
    *,
    error_code: str | None = None,
    error_message: str | None = None,
    http_status: int | None = None,
    now: datetime | None = None,
) -> SourceFetchLog:
    now = now or datetime.now(UTC)
    log.status = FAILED
    log.error_code = error_code
    log.error_message = error_message
    log.http_status = http_status
    _finalize(log, now)
    await session.flush()
    return log


async def record_rate_limited(
    session: AsyncSession,
    log: SourceFetchLog,
    *,
    backoff_until: datetime | None = None,
    http_status: int | None = None,
    error_message: str | None = None,
    now: datetime | None = None,
) -> SourceFetchLog:
    now = now or datetime.now(UTC)
    log.status = RATE_LIMITED
    log.rate_limited = True
    log.backoff_until = backoff_until
    log.http_status = http_status
    log.error_message = error_message
    _finalize(log, now)
    await session.flush()
    return log


async def list_fetch_logs(
    session: AsyncSession,
    *,
    source: str | None = None,
    status: str | None = None,
    request_kind: str | None = None,
    limit: int = 100,
) -> list[SourceFetchLog]:
    stmt = select(SourceFetchLog).order_by(SourceFetchLog.id.desc())
    if source is not None:
        stmt = stmt.where(SourceFetchLog.source_name == source)
    if status is not None:
        stmt = stmt.where(SourceFetchLog.status == status)
    if request_kind is not None:
        stmt = stmt.where(SourceFetchLog.request_kind == request_kind)
    return list((await session.execute(stmt.limit(limit))).scalars().all())
