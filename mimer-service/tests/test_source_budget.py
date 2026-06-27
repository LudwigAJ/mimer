"""Source rate-budget + fetch-log + request-cache + guarded-fetch tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import SourceFetchLog, SourceRateLimit
from app.services import source_budget, source_requests


def _now() -> datetime:
    return datetime(2026, 6, 21, 12, 0, 0, tzinfo=UTC)


# --- budget defaults / seeding ----------------------------------------------


async def test_default_budgets_seeded_and_idempotent(session: AsyncSession) -> None:
    # conftest already seeded defaults; re-seeding inserts nothing.
    inserted = await source_budget.seed_source_rate_limits(session)
    assert inserted == 0
    names = set((await session.execute(select(SourceRateLimit.source_name))).scalars())
    assert {"openfigi", "yfinance", "stooq"} <= names
    # External sources are conservative; fixtures permissive.
    openfigi = await source_budget.get_budget(session, "openfigi")
    assert openfigi is not None and openfigi.max_requests_per_minute == 6
    fixture = await source_budget.get_budget(session, "fx_fixture")
    assert fixture is not None and fixture.max_requests_per_minute is None


async def test_no_secret_fields_on_budget(session: AsyncSession) -> None:
    openfigi = await source_budget.get_budget(session, "openfigi")
    assert openfigi is not None
    # No column holds a credential (notes text may mention "api key"; that's fine).
    column_names = {c.name for c in SourceRateLimit.__table__.columns}
    assert not any(
        sensitive in name
        for name in column_names
        for sensitive in ("api_key", "apikey", "token", "secret", "password")
    )


# --- budget decisions --------------------------------------------------------


async def test_check_budget_allows_and_exposes_batch_size(session: AsyncSession) -> None:
    decision = await source_budget.check_budget(session, "openfigi", now=_now())
    assert decision.allowed is True
    assert decision.reason == "ok"
    assert decision.batch_size == 10  # OpenFIGI batches up to 10 jobs/request


async def test_check_budget_blocks_on_backoff(session: AsyncSession) -> None:
    now = _now()
    until = await source_budget.apply_backoff(session, "stooq", seconds=60, now=now)
    assert until is not None
    decision = await source_budget.check_budget(session, "stooq", now=now)
    assert decision.allowed is False
    assert decision.reason == "in_backoff"
    assert 0 < decision.wait_seconds <= 60
    assert "stooq" in await source_budget.sources_in_backoff(session, now=now)


async def test_check_budget_enforces_min_delay(session: AsyncSession) -> None:
    now = _now()
    await source_budget.note_request(session, "yfinance", now=now)  # min_delay_ms=400
    decision = await source_budget.check_budget(
        session, "yfinance", now=now + timedelta(milliseconds=100)
    )
    assert decision.allowed is False
    assert decision.reason == "min_delay"
    # After the delay elapses it is allowed again.
    later = await source_budget.check_budget(
        session, "yfinance", now=now + timedelta(milliseconds=500)
    )
    assert later.allowed is True


async def test_check_budget_enforces_per_minute_window(session: AsyncSession) -> None:
    now = _now()
    # OpenFIGI allows 6/min; insert 6 consuming attempts within the last minute.
    for i in range(6):
        session.add(
            SourceFetchLog(
                source_name="openfigi",
                request_kind="resolve_identity",
                request_key=f"openfigi:resolve_identity:k{i}",
                request_hash=f"h{i}",
                status="success",
                started_at=now - timedelta(seconds=5),
            )
        )
    await session.commit()
    decision = await source_budget.check_budget(session, "openfigi", now=now)
    assert decision.allowed is False
    assert decision.reason == "rate_limited_minute"


# --- request key / fetch logs ------------------------------------------------


def test_request_key_is_deterministic_and_secret_free() -> None:
    a = source_requests.build_request_key(
        "openfigi", "resolve_identity", {"idType": "ID_ISIN", "idValue": "ie00b3xxrp09"}
    )
    b = source_requests.build_request_key(
        "openfigi", "resolve_identity", {"idValue": "IE00B3XXRP09", "idType": "ID_ISIN"}
    )
    assert a == b  # order-insensitive, case-normalised
    # Credential-like params never appear in the key.
    with_secret = source_requests.build_request_key(
        "openfigi", "resolve_identity", {"idValue": "X", "api_key": "SECRET", "token": "T"}
    )
    assert "SECRET" not in with_secret and "api_key" not in with_secret


async def test_fetch_log_lifecycle_success_failure_rate_limited(session: AsyncSession) -> None:
    now = _now()
    ok = await source_requests.record_fetch_start(
        session, source="stooq", request_kind="fetch_prices", params={"s": "vusa"}, now=now
    )
    await source_requests.record_fetch_success(
        session, ok, http_status=200, records_inserted=3, now=now + timedelta(milliseconds=50)
    )
    assert ok.status == "success" and ok.duration_ms == 50

    bad = await source_requests.record_fetch_start(
        session, source="stooq", request_kind="fetch_prices", params={"s": "bad"}, now=now
    )
    await source_requests.record_fetch_failure(
        session, bad, error_code="500", error_message="boom", now=now
    )
    assert bad.status == "failed"

    limited = await source_requests.record_fetch_start(
        session, source="openfigi", request_kind="resolve_identity", params={"x": 1}, now=now
    )
    await source_requests.record_rate_limited(session, limited, http_status=429, now=now)
    assert limited.status == "rate_limited" and limited.rate_limited is True
    await session.commit()


async def test_recent_success_cache_decision(session: AsyncSession) -> None:
    now = _now()
    log = await source_requests.record_fetch_start(
        session, source="stooq", request_kind="fetch_prices", params={"s": "isf"}, now=now
    )
    await source_requests.record_fetch_success(session, log, now=now)
    await session.commit()

    hit = await source_requests.should_skip_recent_success(
        session,
        source="stooq",
        request_kind="fetch_prices",
        params={"s": "isf"},
        ttl_seconds=3600,
        now=now + timedelta(minutes=5),
    )
    assert hit is not None  # within TTL => cache hit
    miss = await source_requests.should_skip_recent_success(
        session,
        source="stooq",
        request_kind="fetch_prices",
        params={"s": "isf"},
        ttl_seconds=3600,
        now=now + timedelta(hours=2),
    )
    assert miss is None  # past TTL


async def test_fetch_log_serialization_has_no_secrets(session: AsyncSession) -> None:
    now = _now()
    log = await source_requests.record_fetch_start(
        session,
        source="openfigi",
        request_kind="resolve_identity",
        params={"idValue": "IE00B3XXRP09", "api_key": "SUPER_SECRET"},
        endpoint_label="api.openfigi.com/v3/mapping",
        now=now,
    )
    await source_requests.record_fetch_success(session, log, now=now)
    await session.commit()
    from app.schemas.source_ops import SourceFetchLogRead

    blob = SourceFetchLogRead.model_validate(log).model_dump_json()
    assert "SUPER_SECRET" not in blob
    assert "api_key" not in blob


# --- guarded fetch -----------------------------------------------------------


async def test_guarded_fetch_success_then_cache(session: AsyncSession) -> None:
    now = _now()
    calls = {"n": 0}

    async def fetch() -> dict:
        calls["n"] += 1
        return {"data": [1, 2, 3]}

    result, payload = await source_budget.guarded_fetch(
        session,
        source="stooq",
        request_kind="fetch_prices",
        fetch=fetch,
        params={"s": "vusa"},
        ttl_seconds=3600,
        now=now,
    )
    await session.commit()
    assert result.status == "success" and payload == {"data": [1, 2, 3]}
    assert calls["n"] == 1

    # Second identical request inside TTL is served from cache (no fetch call).
    result2, payload2 = await source_budget.guarded_fetch(
        session,
        source="stooq",
        request_kind="fetch_prices",
        fetch=fetch,
        params={"s": "vusa"},
        ttl_seconds=3600,
        now=now + timedelta(minutes=1),
    )
    await session.commit()
    assert result2.cache_hit is True and payload2 is None
    assert calls["n"] == 1  # fetch was NOT called again


async def test_guarded_fetch_blocked_does_not_call_fetch(session: AsyncSession) -> None:
    now = _now()
    await source_budget.apply_backoff(session, "openfigi", seconds=120, now=now)
    called = {"n": 0}

    async def fetch() -> dict:
        called["n"] += 1
        return {}

    result, payload = await source_budget.guarded_fetch(
        session,
        source="openfigi",
        request_kind="resolve_identity",
        fetch=fetch,
        params={"idValue": "X"},
        now=now,
    )
    await session.commit()
    assert result.status == "rate_limited" and payload is None
    assert called["n"] == 0  # budget block prevented the live call
    logged = (
        await session.execute(
            select(SourceFetchLog).where(SourceFetchLog.id == result.fetch_log_id)
        )
    ).scalar_one()
    assert logged.status == "rate_limited"
