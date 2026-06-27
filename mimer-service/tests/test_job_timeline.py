"""Bounded job-run timeline / failure drilldown read model.

Covers the generic read service (timeline / failures / detail across all job
types), source-fetch-log correlation (approximate, bounded, labelled), source
budget/backoff context, recommended-action derivation, defensive secret masking,
and the API surface (global + workspace-scoped) + capabilities/diagnostics
fields. All offline — no test makes a live call; nothing leaks a secret.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFoundError
from app.db.models import JobRun, SourceFetchLog, Workspace
from app.services import instrument_onboarding as ob
from app.services import job_timeline as jt
from app.services import secret_masking as mask
from app.services import source_budget as source_budget_service

_SECRET_TOKENS = ("SECRET", "supersecret", "abc.def", "zzztoken", "p4ssw0rd")


# --- helpers -----------------------------------------------------------------


async def _run(session: AsyncSession, **kw) -> JobRun:
    run = JobRun(
        job_type=kw.pop("job_type", "price_ingestion"),
        status=kw.pop("status", "success"),
        **kw,
    )
    session.add(run)
    await session.flush()
    return run


async def _fetch_log(session: AsyncSession, **kw) -> SourceFetchLog:
    log = SourceFetchLog(
        source_name=kw.pop("source_name", "stooq"),
        request_kind=kw.pop("request_kind", "fetch_prices"),
        request_key=kw.pop("request_key", "stooq:fetch_prices:symbol=AAPL"),
        request_hash=kw.pop("request_hash", "h" * 64),
        status=kw.pop("status", "success"),
        **kw,
    )
    session.add(log)
    await session.flush()
    return log


async def _workspace(session: AsyncSession, name: str) -> Workspace:
    ws = Workspace(name=name, base_currency="GBP")
    session.add(ws)
    await session.flush()
    return ws


# --- timeline list -----------------------------------------------------------


async def test_timeline_latest_first(session: AsyncSession) -> None:
    r1 = await _run(session, job_type="price_ingestion")
    r2 = await _run(session, job_type="fx_ingestion")
    r3 = await _run(session, job_type="alert_generation")
    resp = await jt.global_timeline(session)
    ids = [item.run_id for item in resp.runs]
    assert ids[:3] == [r3.id, r2.id, r1.id]  # latest first
    assert resp.scope_type == "global"


async def test_timeline_limit_is_bounded(session: AsyncSession) -> None:
    assert jt.clamp_limit(10_000) == jt.MAX_LIMIT
    assert jt.clamp_limit(None) == jt.DEFAULT_LIMIT
    assert jt.clamp_limit(0) == jt.DEFAULT_LIMIT
    assert jt.clamp_limit(-3) == 1
    for _ in range(4):
        await _run(session)
    resp = await jt.global_timeline(session, limit=2)
    assert resp.limit == 2
    assert len(resp.runs) == 2


async def test_workspace_scoped_timeline(session: AsyncSession) -> None:
    mine = await _run(session, job_type="exposure_recompute", workspace_id=1)
    other_ws = await _workspace(session, "OtherTL")
    await _run(session, job_type="exposure_recompute", workspace_id=other_ws.id)
    resp = await jt.workspace_timeline(session, 1)
    ids = [item.run_id for item in resp.runs]
    assert mine.id in ids
    assert all(item.workspace_id == 1 for item in resp.runs)
    assert resp.scope_type == "workspace" and resp.scope_id == 1


async def test_failures_only_failed_or_partial(session: AsyncSession) -> None:
    ok = await _run(session, status="success")
    failed = await _run(session, status="failed")
    partial = await _run(session, status="partial_success")
    running = await _run(session, status="running")
    resp = await jt.list_job_failures(session)
    ids = {item.run_id for item in resp.failures}
    assert failed.id in ids and partial.id in ids
    assert ok.id not in ids and running.id not in ids
    assert all(item.severity in ("error", "warning") for item in resp.failures)


# --- detail ------------------------------------------------------------------


async def test_detail_by_run_id_counts_and_duration(session: AsyncSession) -> None:
    started = datetime.now(UTC)
    finished = started + timedelta(seconds=3)
    run = await _run(
        session,
        job_type="constituent_eod_price_ingestion",
        source="instrument_price_fixture",
        status="success",
        started_at=started,
        finished_at=finished,
        records_inserted=5,
        records_updated=2,
        records_failed=0,
    )
    detail = await jt.get_job_run_detail(session, run.id)
    assert detail.summary.run_id == run.id
    assert detail.summary.records_inserted == 5
    assert detail.summary.records_updated == 2
    assert detail.summary.duration_ms is not None and detail.summary.duration_ms >= 0
    # A normal (non-orchestration) run has no stages but a full summary.
    assert detail.stages == []
    assert detail.child_runs == []
    assert detail.legacy_metadata is False


async def test_unknown_run_404(session: AsyncSession) -> None:
    with pytest.raises(NotFoundError):
        await jt.get_job_run_detail(session, 987_654)


async def test_foreign_workspace_detail_rejected(session: AsyncSession) -> None:
    run = await _run(session, job_type="exposure_recompute", workspace_id=1)
    other = await _workspace(session, "ForeignWS")
    # Visible through its own workspace, hidden from a foreign one.
    assert (await jt.get_job_run_detail(session, run.id, workspace_id=1)).summary.run_id == run.id
    with pytest.raises(NotFoundError):
        await jt.get_job_run_detail(session, run.id, workspace_id=other.id)


async def test_legacy_orchestration_run_without_payload(session: AsyncSession) -> None:
    legacy = await _run(session, job_type=ob.ONBOARDING_JOB, workspace_id=1, status="success")
    detail = await jt.get_job_run_detail(session, legacy.id)
    assert detail.summary.is_orchestration is True
    assert detail.stages == []
    assert detail.legacy_metadata is True  # orchestration + no payload


async def test_onboarding_run_expands_stages_and_children(session: AsyncSession) -> None:
    run = await ob.execute_onboarding_plan(session, workspace_id=1, source_mode="fixture")
    detail = await jt.get_job_run_detail(session, run.parent_job_run_id)
    assert detail.summary.is_orchestration is True
    assert detail.summary.has_payload is True
    assert len(detail.stages) == 6
    assert detail.summary.has_children is True
    assert detail.child_runs  # identity/prices ran on the seeded workspace
    child_ids = {c.run_id for c in detail.child_runs}
    stage_child_ids = {cid for st in detail.stages for cid in st.child_run_ids}
    assert stage_child_ids <= child_ids
    for c in detail.child_runs:
        assert c.duration_ms is None or c.duration_ms >= 0
        assert c.severity in ("ok", "warning", "error", "running")


# --- fetch-log correlation ---------------------------------------------------


async def test_related_fetch_logs_by_source_time_window(session: AsyncSession) -> None:
    t0 = datetime.now(UTC)
    run = await _run(session, source="stooq", started_at=t0, finished_at=t0 + timedelta(seconds=10))
    in_window = await _fetch_log(session, source_name="stooq", started_at=t0 + timedelta(seconds=1))
    # Different source -> excluded; long before the run -> excluded.
    await _fetch_log(session, source_name="yfinance", started_at=t0 + timedelta(seconds=1))
    await _fetch_log(session, source_name="stooq", started_at=t0 - timedelta(hours=2))
    detail = await jt.get_job_run_detail(session, run.id)
    assert detail.fetch_log_correlation == jt.CORR_TIME_WINDOW
    ids = {log.id for log in detail.related_fetch_logs}
    assert in_window.id in ids
    assert all(log.source_name == "stooq" for log in detail.related_fetch_logs)
    assert detail.summary.has_fetch_logs is True


async def test_related_fetch_logs_are_bounded(session: AsyncSession) -> None:
    t0 = datetime.now(UTC)
    run = await _run(session, source="stooq", started_at=t0, finished_at=t0 + timedelta(seconds=60))
    for i in range(jt.FETCH_LOG_DEFAULT + 12):
        await _fetch_log(session, source_name="stooq", started_at=t0 + timedelta(seconds=i % 50))
    detail = await jt.get_job_run_detail(session, run.id)
    assert len(detail.related_fetch_logs) == jt.FETCH_LOG_DEFAULT


async def test_correlation_unavailable_for_pseudo_source(session: AsyncSession) -> None:
    # exposure_recompute's "source" is a producer name, not a fetch source.
    run = await _run(
        session,
        job_type="exposure_recompute",
        source="exposure_recompute",
        started_at=datetime.now(UTC),
    )
    detail = await jt.get_job_run_detail(session, run.id)
    assert detail.fetch_log_correlation == jt.CORR_UNAVAILABLE
    assert detail.related_fetch_logs == []
    assert detail.source_budget_context is None


async def test_no_fetch_logs_returns_empty_but_labelled(session: AsyncSession) -> None:
    t0 = datetime.now(UTC)
    run = await _run(session, source="stooq", started_at=t0, finished_at=t0 + timedelta(seconds=1))
    detail = await jt.get_job_run_detail(session, run.id)
    assert detail.related_fetch_logs == []
    assert detail.fetch_log_correlation == jt.CORR_TIME_WINDOW  # searched, none found


# --- source budget context ---------------------------------------------------


async def test_source_budget_context_included(session: AsyncSession) -> None:
    run = await _run(session, source="stooq", started_at=datetime.now(UTC))
    detail = await jt.get_job_run_detail(session, run.id)
    ctx = detail.source_budget_context
    assert ctx is not None
    assert ctx.source_name == "stooq"
    assert ctx.enabled is True
    assert ctx.status == "ok" and ctx.allowed is True


async def test_source_budget_context_reports_backoff(session: AsyncSession) -> None:
    # Put stooq into backoff and confirm the context + actions reflect it.
    row = await source_budget_service.get_budget(session, "stooq")
    assert row is not None
    row.backoff_until = datetime.now(UTC) + timedelta(minutes=30)
    await session.flush()
    run = await _run(session, source="stooq", status="failed", started_at=datetime.now(UTC))
    detail = await jt.get_job_run_detail(session, run.id)
    ctx = detail.source_budget_context
    assert ctx is not None and ctx.status == "in_backoff" and ctx.allowed is False
    assert ctx.next_allowed_at is not None
    codes = [a.code for a in detail.recommended_actions]
    assert codes[0] == "wait_for_backoff"


# --- recommended actions -----------------------------------------------------


async def test_actions_failed_source_job() -> None:
    run = JobRun(job_type="constituent_identity_resolution", status="failed")
    codes = jt.recommended_action_codes(run)
    assert "check_source_budget" in codes
    assert "open_fetch_logs" in codes
    assert "rerun_identity_resolution" in codes


async def test_actions_partial_price_ingestion() -> None:
    run = JobRun(job_type="constituent_eod_price_ingestion", status="partial_success")
    codes = jt.recommended_action_codes(run)
    assert "open_missing_prices" in codes
    assert "rerun_price_ingestion" in codes


async def test_actions_rate_limited_first() -> None:
    run = JobRun(job_type="constituent_eod_price_ingestion", status="failed")
    codes = jt.recommended_action_codes(run, source_in_backoff=True)
    assert codes[0] == "wait_for_backoff"
    assert "open_source_budget" in codes


async def test_actions_onboarding_partial() -> None:
    run = JobRun(job_type=ob.ONBOARDING_JOB, status="partial_success")
    codes = jt.recommended_action_codes(run)
    assert "open_onboarding_run" in codes
    assert "run_next_recommended_stage" in codes


async def test_actions_clean_run_has_none() -> None:
    run = JobRun(job_type="price_ingestion", status="success")
    assert jt.recommended_action_codes(run) == []


# --- secret masking ----------------------------------------------------------


def test_mask_text_inline_and_url() -> None:
    assert "SECRET" not in (mask.mask_text("api_key=SECRET") or "")
    assert mask.mask_text("api_key=SECRET") == "api_key=***"
    masked = mask.mask_text("GET https://x/v1?token=zzztoken&page=2") or ""
    assert "zzztoken" not in masked and "page=2" in masked
    assert "abc.def" not in (mask.mask_text("Authorization: Bearer abc.def") or "")
    # Benign suffix keys must NOT be redacted.
    assert mask.mask_text("holding_key=ABC123 request_key=q") == "holding_key=ABC123 request_key=q"
    assert mask.mask_text(None) is None


def test_mask_json_recursive() -> None:
    payload = {
        "kind": "x",
        "api_key": "SECRET",
        "nested": {"token": "supersecret", "items": [{"password": "p4ssw0rd"}]},
        "note": "key=zzztoken",
        "count": 3,
    }
    out = mask.mask_json(payload)
    blob = str(out)
    assert not any(tok in blob for tok in _SECRET_TOKENS)
    assert out["api_key"] == mask.REDACTED
    assert out["nested"]["token"] == mask.REDACTED
    assert out["nested"]["items"][0]["password"] == mask.REDACTED
    assert out["count"] == 3  # non-strings untouched


async def test_detail_masks_payload_message_and_fetch_log(session: AsyncSession) -> None:
    t0 = datetime.now(UTC)
    run = await _run(
        session,
        job_type=ob.ONBOARDING_JOB,
        source="stooq",
        status="failed",
        started_at=t0,
        finished_at=t0 + timedelta(seconds=5),
        message="failed token=zzztoken",
        payload_json={"kind": "x", "api_key": "SECRET", "stages": []},
    )
    await _fetch_log(
        session,
        source_name="stooq",
        started_at=t0 + timedelta(seconds=1),
        request_key="stooq:fetch:api_key=SECRET",
        error_message="auth token=zzztoken failed",
        status="failed",
    )
    detail = await jt.get_job_run_detail(session, run.id)
    blob = (
        str(detail.payload)
        + (detail.summary.message or "")
        + str([log.model_dump() for log in detail.related_fetch_logs])
    )
    assert not any(tok in blob for tok in _SECRET_TOKENS)
    assert detail.payload["api_key"] == mask.REDACTED


# --- API ---------------------------------------------------------------------


async def test_api_global_timeline(client: AsyncClient) -> None:
    await client.post("/api/v1/instruments", json={"symbol": "IE00B3XXRP09", "symbol_type": "isin"})
    body = (await client.get("/api/v1/jobs/timeline?limit=10")).json()
    assert body["scope_type"] == "global"
    assert body["count"] >= 1
    assert body["runs"][0]["severity"] in ("ok", "warning", "error", "running")


async def test_api_run_detail(client: AsyncClient) -> None:
    await client.post("/api/v1/workspaces/1/onboarding/run")
    runs = (await client.get("/api/v1/jobs/timeline?job_type=instrument_onboarding")).json()["runs"]
    run_id = runs[0]["run_id"]
    detail = (await client.get(f"/api/v1/jobs/runs/{run_id}")).json()
    assert detail["summary"]["run_id"] == run_id
    assert {s["stage"] for s in detail["stages"]} >= {"holdings", "constituent_identity"}
    assert "related_fetch_logs" in detail
    assert "recommended_actions" in detail


async def test_api_run_detail_unknown_404(client: AsyncClient) -> None:
    resp = await client.get("/api/v1/jobs/runs/987654")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "job_run_not_found"


async def test_api_failures(client: AsyncClient, session: AsyncSession) -> None:
    await _run(session, status="failed", job_type="fx_ingestion")
    await session.commit()
    body = (await client.get("/api/v1/jobs/failures?limit=10")).json()
    assert body["scope_type"] == "global"
    assert all(f["status"] in ("failed", "partial_success") for f in body["failures"])


async def test_api_workspace_timeline_and_failures(client: AsyncClient) -> None:
    await client.post("/api/v1/workspaces/1/onboarding/run")
    tl = (await client.get("/api/v1/workspaces/1/jobs/timeline?limit=20")).json()
    assert tl["scope_type"] == "workspace" and tl["scope_id"] == 1
    assert all(item["workspace_id"] == 1 for item in tl["runs"])
    fails = (await client.get("/api/v1/workspaces/1/jobs/failures")).json()
    assert fails["scope_type"] == "workspace"


async def test_api_limit_validation(client: AsyncClient) -> None:
    assert (await client.get("/api/v1/jobs/timeline?limit=0")).status_code == 422
    assert (await client.get("/api/v1/jobs/timeline?limit=501")).status_code == 422
    assert (await client.get("/api/v1/workspaces/1/jobs/failures?limit=99999")).status_code == 422


async def test_api_existing_jobs_runs_unchanged(client: AsyncClient) -> None:
    # The simple list endpoint keeps its envelope shape (backward compatible).
    body = (await client.get("/api/v1/jobs/runs?limit=5")).json()
    assert "data" in body and "meta" in body


async def test_api_capabilities_lists_timeline_features(client: AsyncClient) -> None:
    features = (await client.get("/api/v1/capabilities")).json()["features"]
    assert features["job_run_timeline"] == "real"
    assert features["job_run_detail"] == "real"
    assert features["job_failure_drilldown"] == "real"
    assert features["source_fetch_log_correlation"] == "partial"


async def test_api_diagnostics_job_run_fields(client: AsyncClient, session: AsyncSession) -> None:
    await _run(session, status="partial_success", job_type="fx_ingestion")
    await session.commit()
    body = (await client.get("/api/v1/workspaces/1/diagnostics")).json()
    assert "recent_partial_job_runs" in body
    assert body["recent_partial_job_runs"] >= 1
    assert "latest_failed_job_run_id" in body
    assert "latest_failed_job_run_type" in body


# --- safety ------------------------------------------------------------------


async def test_no_secrets_in_api_detail(client: AsyncClient, session: AsyncSession) -> None:
    t0 = datetime.now(UTC)
    run = await _run(
        session,
        source="stooq",
        status="failed",
        started_at=t0,
        finished_at=t0 + timedelta(seconds=2),
        message="boom token=zzztoken",
    )
    await _fetch_log(
        session,
        source_name="stooq",
        started_at=t0 + timedelta(seconds=1),
        request_key="stooq:fetch:api_key=SECRET",
        status="failed",
    )
    await session.commit()
    raw = (await client.get(f"/api/v1/jobs/runs/{run.id}")).text
    assert not any(tok in raw for tok in _SECRET_TOKENS)
