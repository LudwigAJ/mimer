"""Source rate-budget / fetch-guard service.

Answers the four questions a safe live adapter needs *before* it calls out:

    Can source X make a request now?
    How long should it wait?
    Is source X in backoff?
    What batch size should be used?

Budgets live in ``source_rate_limits`` (one row per source). Fixture/local
sources are permissive; external sources (openfigi/yfinance/stooq) are
conservative. Request counts in the rolling windows are derived from
``source_fetch_logs`` so the budget and the observability layer agree. This is a
pragmatic guard against uncontrolled per-holding loops, not a perfect
distributed limiter — see AGENTS.md.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import SourceFetchLog, SourceRateLimit
from app.services import source_requests
from app.services.source_requests import BUDGET_CONSUMING_STATUSES, SourceFetchResult

# Conservative defaults for external sources; permissive for offline fixtures.
# Nullable window => unbounded for that window. No secrets are stored here.
_PERMISSIVE: dict[str, object] = {
    "max_requests_per_minute": None,
    "max_requests_per_hour": None,
    "max_requests_per_day": None,
    "max_concurrency": None,
    "min_delay_ms": 0,
    "batch_size": 500,
    "backoff_seconds": 0,
}

_DEFAULT_BUDGETS: dict[str, dict[str, object]] = {
    # --- external (conservative; never spam) ---
    "openfigi": {
        "max_requests_per_minute": 6,
        "max_requests_per_hour": 250,
        "max_requests_per_day": 5000,
        "max_concurrency": 1,
        "min_delay_ms": 300,
        "batch_size": 10,  # OpenFIGI maps up to 10 jobs per request.
        "backoff_seconds": 60,
        "notes": "FIGI mapping. Conservative without an API key; key raises limits.",
    },
    "yfinance": {
        "max_requests_per_minute": 30,
        "max_requests_per_hour": 1000,
        "max_requests_per_day": 10000,
        "max_concurrency": 2,
        "min_delay_ms": 400,
        "batch_size": 1,
        "backoff_seconds": 60,
        "notes": "Unofficial Yahoo endpoint; be gentle, one symbol at a time.",
    },
    "stooq": {
        "max_requests_per_minute": 20,
        "max_requests_per_hour": 600,
        "max_requests_per_day": 5000,
        "max_concurrency": 2,
        "min_delay_ms": 500,
        "batch_size": 1,
        "backoff_seconds": 60,
        "notes": "Free EOD CSV; fragile/non-contractual. Default price source.",
    },
    "us_treasury_rates": {
        "max_requests_per_minute": 10,
        "max_requests_per_hour": 120,
        "max_requests_per_day": 500,
        "max_concurrency": 1,
        "min_delay_ms": 1000,
        "batch_size": 1,
        "backoff_seconds": 60,
        "notes": "Official US Treasury daily par-yield XML feed. One request per "
        "calendar year; explicit-only (rates default stays the offline fixture).",
    },
    "ecb_rates": {
        "max_requests_per_minute": 10,
        "max_requests_per_hour": 120,
        "max_requests_per_day": 500,
        "max_concurrency": 1,
        "min_delay_ms": 1000,
        "batch_size": 1,
        "backoff_seconds": 60,
        "notes": "Official ECB Data Portal SDMX API (key interest rates + €STR). One "
        "request per dataflow (FM/EST), spaced by min_delay; explicit-only (rates "
        "default stays the offline fixture).",
    },
    "blackrock_ishares_holdings": {
        "max_requests_per_minute": 10,
        "max_requests_per_hour": 120,
        "max_requests_per_day": 500,
        "max_concurrency": 1,
        "min_delay_ms": 1000,
        "batch_size": 1,
        "backoff_seconds": 60,
        "notes": "iShares/BlackRock issuer-hosted holdings CSV. One request per fund; "
        "explicit-only (holdings default stays the offline fixture).",
    },
    "jpmorgan_etf_holdings": {
        "max_requests_per_minute": 10,
        "max_requests_per_hour": 120,
        "max_requests_per_day": 500,
        "max_concurrency": 1,
        "min_delay_ms": 1000,
        "batch_size": 1,
        "backoff_seconds": 60,
        "notes": "J.P. Morgan AM daily ETF holdings export. One request per fund; "
        "explicit-only (holdings default stays the offline fixture).",
    },
    "jpmorgan_distributions": {
        "max_requests_per_minute": 10,
        "max_requests_per_hour": 120,
        "max_requests_per_day": 500,
        "max_concurrency": 1,
        "min_delay_ms": 1000,
        "batch_size": 1,
        "backoff_seconds": 60,
        "notes": "J.P. Morgan AM fund distribution export. One request per fund; "
        "explicit-only (distribution default stays the offline fixture).",
    },
    "vanguard_distributions": {
        "max_requests_per_minute": 10,
        "max_requests_per_hour": 120,
        "max_requests_per_day": 500,
        "max_concurrency": 1,
        "min_delay_ms": 1000,
        "batch_size": 1,
        "backoff_seconds": 60,
        "notes": "Vanguard product-data distributionHistory JSON. One request per fund; "
        "explicit-only (distribution default stays the offline fixture).",
    },
    # --- offline fixtures + local sources (permissive) ---
    "issuer_fixture": {**_PERMISSIVE, "notes": "Offline fixture; no network."},
    "distribution_fixture": {**_PERMISSIVE, "notes": "Offline fixture; no network."},
    "holdings_fixture": {**_PERMISSIVE, "notes": "Offline fixture; no network."},
    "vanguard_holdings_export": {
        **_PERMISSIVE,
        "notes": "Offline parser for a manually exported Vanguard holdings file; no network.",
    },
    "vanguard_distributions_export": {
        **_PERMISSIVE,
        "notes": "Offline parser for a manually exported Vanguard distribution file; no network.",
    },
    "fx_fixture": {**_PERMISSIVE, "notes": "Offline fixture; no network."},
    "document_fixture": {**_PERMISSIVE, "notes": "Offline fixture; no network."},
    "constituent_identity_fixture": {**_PERMISSIVE, "notes": "Offline fixture; no network."},
    "instrument_price_fixture": {**_PERMISSIVE, "notes": "Offline fixture; no network."},
    "seed": {**_PERMISSIVE, "notes": "Local seed data."},
    "manual": {**_PERMISSIVE, "notes": "Human overrides."},
}


@dataclass(frozen=True)
class SourceBudgetDecision:
    """Whether a source may fetch now, and if not, how long to wait."""

    allowed: bool
    source_name: str
    wait_seconds: float
    reason: str
    batch_size: int | None
    backoff_until: datetime | None


def default_budget_specs() -> dict[str, dict[str, object]]:
    """The seed defaults (source_name -> field overrides)."""
    return {name: dict(spec) for name, spec in _DEFAULT_BUDGETS.items()}


def build_default_rows() -> list[SourceRateLimit]:
    """Materialise the default budgets as ORM rows (for seeding)."""
    return [SourceRateLimit(source_name=name, **spec) for name, spec in _DEFAULT_BUDGETS.items()]


async def seed_source_rate_limits(session: AsyncSession) -> int:
    """Idempotently insert any missing default budgets. Returns rows inserted."""
    existing = set((await session.execute(select(SourceRateLimit.source_name))).scalars().all())
    inserted = 0
    for name, spec in _DEFAULT_BUDGETS.items():
        if name in existing:
            continue
        session.add(SourceRateLimit(source_name=name, **spec))
        inserted += 1
    return inserted


async def get_budget(session: AsyncSession, source_name: str) -> SourceRateLimit | None:
    return await session.scalar(
        select(SourceRateLimit).where(SourceRateLimit.source_name == source_name)
    )


async def list_budgets(session: AsyncSession) -> list[SourceRateLimit]:
    return list(
        (await session.execute(select(SourceRateLimit).order_by(SourceRateLimit.source_name)))
        .scalars()
        .all()
    )


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


async def _window_count(session: AsyncSession, source_name: str, *, since: datetime) -> int:
    return (
        await session.scalar(
            select(func.count())
            .select_from(SourceFetchLog)
            .where(
                SourceFetchLog.source_name == source_name,
                SourceFetchLog.started_at >= since,
                SourceFetchLog.status.in_(BUDGET_CONSUMING_STATUSES),
            )
        )
    ) or 0


async def check_budget(
    session: AsyncSession, source_name: str, *, now: datetime | None = None
) -> SourceBudgetDecision:
    """Decide if ``source_name`` may fetch now (and the wait if not)."""
    now = now or datetime.now(UTC)
    budget = await get_budget(session, source_name)

    if budget is None:
        # Unknown source: permissive but flagged so callers can seed a budget.
        return SourceBudgetDecision(
            allowed=True,
            source_name=source_name,
            wait_seconds=0.0,
            reason="no_budget_configured",
            batch_size=None,
            backoff_until=None,
        )

    if not budget.is_enabled:
        return SourceBudgetDecision(
            allowed=False,
            source_name=source_name,
            wait_seconds=0.0,
            reason="disabled",
            batch_size=budget.batch_size,
            backoff_until=None,
        )

    # 1) explicit backoff window
    backoff_until = _as_utc(budget.backoff_until)
    if backoff_until is not None and backoff_until > now:
        return SourceBudgetDecision(
            allowed=False,
            source_name=source_name,
            wait_seconds=(backoff_until - now).total_seconds(),
            reason="in_backoff",
            batch_size=budget.batch_size,
            backoff_until=backoff_until,
        )

    # 2) minimum spacing between consecutive requests
    last_request_at = _as_utc(budget.last_request_at)
    if budget.min_delay_ms and last_request_at is not None:
        elapsed_ms = (now - last_request_at).total_seconds() * 1000
        if elapsed_ms < budget.min_delay_ms:
            wait = (budget.min_delay_ms - elapsed_ms) / 1000
            return SourceBudgetDecision(
                allowed=False,
                source_name=source_name,
                wait_seconds=wait,
                reason="min_delay",
                batch_size=budget.batch_size,
                backoff_until=None,
            )

    # 3) rolling window limits (counts taken from the fetch log)
    windows = (
        ("rate_limited_minute", budget.max_requests_per_minute, 60),
        ("rate_limited_hour", budget.max_requests_per_hour, 3600),
        ("rate_limited_day", budget.max_requests_per_day, 86400),
    )
    for reason, limit, seconds in windows:
        if not limit:
            continue
        used = await _window_count(session, source_name, since=now - timedelta(seconds=seconds))
        if used >= limit:
            return SourceBudgetDecision(
                allowed=False,
                source_name=source_name,
                wait_seconds=float(seconds),
                reason=reason,
                batch_size=budget.batch_size,
                backoff_until=None,
            )

    return SourceBudgetDecision(
        allowed=True,
        source_name=source_name,
        wait_seconds=0.0,
        reason="ok",
        batch_size=budget.batch_size,
        backoff_until=None,
    )


async def note_request(
    session: AsyncSession, source_name: str, *, now: datetime | None = None
) -> None:
    """Stamp ``last_request_at`` (for min-delay spacing). No-op if no budget row."""
    now = now or datetime.now(UTC)
    budget = await get_budget(session, source_name)
    if budget is not None:
        budget.last_request_at = now


async def apply_backoff(
    session: AsyncSession,
    source_name: str,
    *,
    seconds: int | None = None,
    now: datetime | None = None,
) -> datetime | None:
    """Put a source into backoff (after a rate-limit/failure). Returns the until."""
    now = now or datetime.now(UTC)
    budget = await get_budget(session, source_name)
    if budget is None:
        return None
    cooldown = seconds if seconds is not None else (budget.backoff_seconds or 0)
    until = now + timedelta(seconds=cooldown)
    budget.backoff_until = until
    return until


async def clear_backoff(session: AsyncSession, source_name: str) -> None:
    budget = await get_budget(session, source_name)
    if budget is not None:
        budget.backoff_until = None


async def sources_in_backoff(session: AsyncSession, *, now: datetime | None = None) -> list[str]:
    now = now or datetime.now(UTC)
    rows = (
        await session.execute(
            select(SourceRateLimit.source_name, SourceRateLimit.backoff_until).where(
                SourceRateLimit.backoff_until.is_not(None)
            )
        )
    ).all()
    result: list[str] = []
    for name, until in rows:
        until_utc = _as_utc(until)
        if until_utc is not None and until_utc > now:
            result.append(name)
    return result


async def guarded_fetch(
    session: AsyncSession,
    *,
    source: str,
    request_kind: str,
    fetch: Callable[[], Awaitable[Any]],
    params: dict[str, Any] | None = None,
    endpoint_label: str | None = None,
    method: str | None = None,
    ttl_seconds: int | None = None,
    now: datetime | None = None,
) -> tuple[SourceFetchResult, Any]:
    """Run an external ``fetch`` under cache + budget + fetch-log protection.

    The one place a live adapter should call out. In order:
      1. recent-success cache — skip identical requests inside ``ttl_seconds``;
      2. budget — if the source may not fetch now, log a ``rate_limited`` attempt
         and return without calling ``fetch`` (no uncontrolled retries);
      3. otherwise record start, call ``fetch``, then record success/failure.

    Returns ``(SourceFetchResult, payload)``; ``payload`` is None on a cache hit
    or a budget block. Never logs secrets — pass only safe ``params``. Callers
    own the surrounding transaction (commit to persist the log rows).
    """
    now = now or datetime.now(UTC)
    key = source_requests.build_request_key(source, request_kind, params)

    if ttl_seconds:
        cached = await source_requests.should_skip_recent_success(
            session,
            source=source,
            request_kind=request_kind,
            request_key=key,
            ttl_seconds=ttl_seconds,
            now=now,
        )
        if cached is not None:
            return (
                SourceFetchResult(
                    status=source_requests.CACHE_HIT,
                    cache_hit=True,
                    request_key=key,
                    fetch_log_id=cached.id,
                ),
                None,
            )

    decision = await check_budget(session, source, now=now)
    if not decision.allowed:
        log = await source_requests.record_fetch_start(
            session,
            source=source,
            request_kind=request_kind,
            request_key=key,
            endpoint_label=endpoint_label,
            method=method,
            now=now,
        )
        await source_requests.record_rate_limited(
            session,
            log,
            backoff_until=decision.backoff_until,
            error_message=decision.reason,
            now=now,
        )
        return (
            SourceFetchResult(
                status=source_requests.RATE_LIMITED,
                cache_hit=False,
                request_key=key,
                fetch_log_id=log.id,
            ),
            None,
        )

    log = await source_requests.record_fetch_start(
        session,
        source=source,
        request_kind=request_kind,
        request_key=key,
        endpoint_label=endpoint_label,
        method=method,
        now=now,
    )
    await note_request(session, source, now=now)
    try:
        payload = await fetch()
    except Exception as exc:  # noqa: BLE001 - record then re-raise for the caller
        await source_requests.record_fetch_failure(
            session, log, error_code=type(exc).__name__, error_message=str(exc), now=now
        )
        raise
    await source_requests.record_fetch_success(
        session, log, raw_payload_hash=source_requests.payload_hash(payload), now=now
    )
    return (
        SourceFetchResult(
            status=source_requests.SUCCESS,
            cache_hit=False,
            request_key=key,
            fetch_log_id=log.id,
        ),
        payload,
    )
