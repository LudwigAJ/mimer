"""Onboarding run history / observability read model — metadata, read service, API.

All offline (fixture source mode). Exercises the structured ``payload_json`` an
``instrument_onboarding`` parent run records, the bounded read service over that
history, the workspace/fund-scoped endpoints, and the dashboard / diagnostics /
capabilities surfaces. No test makes a live call; readiness is data-quality.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFoundError
from app.db.models import JobRun, Workspace
from app.schemas.onboarding import STAGES
from app.services import instrument_onboarding as ob
from app.services import onboarding_runs as runs_service

_LIVE_SOURCES = {"openfigi", "stooq", "yfinance"}
_SECRET_TOKENS = ("api_key", "apikey", "secret", "password", "token")


async def _workspace(session: AsyncSession, name: str) -> Workspace:
    ws = Workspace(name=name, base_currency="GBP")
    session.add(ws)
    await session.flush()
    return ws


def _stage(detail, name: str):
    return next(st for st in detail.stages if st.stage == name)


# --- metadata writing --------------------------------------------------------


async def test_parent_run_writes_structured_payload(session: AsyncSession) -> None:
    run = await ob.execute_onboarding_plan(session, workspace_id=1, source_mode="fixture")
    parent = await session.get(JobRun, run.parent_job_run_id)
    assert parent is not None
    assert parent.workspace_id == 1
    payload = parent.payload_json
    assert isinstance(payload, dict)
    assert payload["kind"] == "instrument_onboarding"
    assert payload["schema_version"] == ob.ONBOARDING_PAYLOAD_VERSION
    assert payload["scope"] == {"type": "workspace", "id": 1}
    assert payload["source_mode"] == "fixture"
    assert payload["duration_ms"] >= 0
    # All six conceptual stages are recorded with typed status/timings.
    stages = payload["stages"]
    assert [s["stage"] for s in stages] == list(STAGES)
    for st in stages:
        assert st["status"] in {
            "success",
            "partial_success",
            "failed",
            "skipped",
            "blocked",
        }
        assert st["duration_ms"] >= 0
        assert "started_at" in st and "finished_at" in st


async def test_stage_metadata_captures_child_run_ids(session: AsyncSession) -> None:
    run = await ob.execute_onboarding_plan(session, workspace_id=1, source_mode="fixture")
    parent = await session.get(JobRun, run.parent_job_run_id)
    stages = {s["stage"]: s for s in parent.payload_json["stages"]}
    # Identity executed -> at least one child worker run, and it exists in job_runs.
    identity = stages["constituent_identity"]
    assert identity["status"] == "success"
    assert identity["child_run_ids"]
    for cid in identity["child_run_ids"]:
        child = await session.get(JobRun, cid)
        assert child is not None and child.id != parent.id
    # Holdings was already complete on the seeded workspace -> skipped/already_ready.
    holdings = stages["holdings"]
    assert holdings["status"] == "skipped"
    assert holdings["reason"] == "already_ready"


async def test_skip_flags_recorded_as_skipped_by_flag(session: AsyncSession) -> None:
    run = await ob.execute_onboarding_plan(
        session, workspace_id=1, source_mode="fixture", skip_exposure=True, skip_alerts=True
    )
    parent = await session.get(JobRun, run.parent_job_run_id)
    stages = {s["stage"]: s for s in parent.payload_json["stages"]}
    assert stages["exposure_recompute"]["status"] == "skipped"
    assert stages["exposure_recompute"]["reason"] == "skipped_by_flag"
    assert stages["alerts"]["reason"] == "skipped_by_flag"


async def test_blocked_stage_recorded_with_structured_reason(session: AsyncSession) -> None:
    # A fund the holdings fixture doesn't know -> identity blocked by missing holdings.
    ws = await _workspace(session, "BlockedRuns")
    from tests.test_instrument_onboarding import _fund_with_holdings, _only_fund

    await _fund_with_holdings(session, ws, isin="ZZ00BLOCK001", holdings=[])
    await session.commit()
    fund_id = await _only_fund(session, ws.id)
    run = await ob.execute_onboarding_plan(session, fund_id=fund_id, source_mode="fixture")
    parent = await session.get(JobRun, run.parent_job_run_id)
    assert parent.fund_id == fund_id
    stages = {s["stage"]: s for s in parent.payload_json["stages"]}
    assert stages["constituent_identity"]["status"] == "blocked"
    assert stages["constituent_identity"]["reason"] == "blocked_by_missing_holdings"


async def test_idempotent_rerun_still_records_stage_metadata(session: AsyncSession) -> None:
    await ob.execute_onboarding_plan(session, workspace_id=1, source_mode="fixture")
    run2 = await ob.execute_onboarding_plan(session, workspace_id=1, source_mode="fixture")
    parent = await session.get(JobRun, run2.parent_job_run_id)
    stages = {s["stage"]: s for s in parent.payload_json["stages"]}
    # Everything already satisfied -> all skipped, but still fully recorded.
    assert stages["constituent_identity"]["status"] == "skipped"
    assert stages["constituent_prices"]["status"] == "skipped"
    assert len(parent.payload_json["stages"]) == 6


# --- read service ------------------------------------------------------------


async def test_list_latest_first_and_scope(session: AsyncSession) -> None:
    r1 = await ob.execute_onboarding_plan(session, workspace_id=1, source_mode="fixture")
    r2 = await ob.execute_onboarding_plan(session, workspace_id=1, source_mode="fixture")
    summaries = await runs_service.list_onboarding_runs(session, workspace_id=1)
    ids = [s.run_id for s in summaries]
    assert ids[0] == r2.parent_job_run_id  # latest first
    assert r1.parent_job_run_id in ids
    top = summaries[0]
    assert top.scope_type == "workspace" and top.scope_id == 1
    assert top.stage_count == 6
    assert top.duration_ms is not None and top.duration_ms >= 0
    assert top.legacy_metadata is False


async def test_list_limit_is_bounded(session: AsyncSession) -> None:
    assert runs_service.clamp_limit(10_000) == runs_service.MAX_LIMIT
    assert runs_service.clamp_limit(None) == runs_service.DEFAULT_LIMIT
    assert runs_service.clamp_limit(0) == runs_service.DEFAULT_LIMIT  # 0/None -> default
    assert runs_service.clamp_limit(-5) == 1  # negative -> floor
    await ob.execute_onboarding_plan(session, workspace_id=1, source_mode="fixture")
    summaries = await runs_service.list_onboarding_runs(session, workspace_id=1, limit=1)
    assert len(summaries) == 1


async def test_workspace_scope_isolates_runs(session: AsyncSession) -> None:
    run = await ob.execute_onboarding_plan(session, workspace_id=1, source_mode="fixture")
    other = await _workspace(session, "Other")
    await session.commit()
    # Foreign workspace sees nothing and 404s on the run.
    assert await runs_service.list_onboarding_runs(session, workspace_id=other.id) == []
    with pytest.raises(NotFoundError):
        await runs_service.get_onboarding_run_detail(
            session, run.parent_job_run_id, workspace_id=other.id
        )


async def test_detail_includes_stages_and_child_runs(session: AsyncSession) -> None:
    run = await ob.execute_onboarding_plan(session, workspace_id=1, source_mode="fixture")
    detail = await runs_service.get_onboarding_run_detail(
        session, run.parent_job_run_id, workspace_id=1
    )
    assert detail.run_id == run.parent_job_run_id
    assert len(detail.stages) == 6
    identity = _stage(detail, "constituent_identity")
    assert identity.status == "success"
    assert identity.child_run_ids
    # Child runs are hydrated from job_runs with computed durations.
    assert detail.child_runs
    child_ids = {c.run_id for c in detail.child_runs}
    assert set(identity.child_run_ids) <= child_ids
    for c in detail.child_runs:
        assert c.duration_ms is None or c.duration_ms >= 0


async def test_legacy_run_without_payload_is_graceful(session: AsyncSession) -> None:
    now = datetime.now(UTC)
    legacy = JobRun(
        job_type=ob.ONBOARDING_JOB,
        workspace_id=1,
        status="success",
        source="fixture",
        started_at=now,
        finished_at=now,
        message="mode=fixture constituent_identity=success(runs=5)",
    )
    session.add(legacy)
    await session.commit()
    summaries = await runs_service.list_onboarding_runs(session, workspace_id=1)
    summary = next(s for s in summaries if s.run_id == legacy.id)
    assert summary.legacy_metadata is True
    assert summary.stage_count == 0
    assert summary.message and "constituent_identity" in summary.message
    detail = await runs_service.get_onboarding_run_detail(session, legacy.id, workspace_id=1)
    assert detail.stages == []
    assert detail.legacy_metadata is True


async def test_stage_counts_are_correct(session: AsyncSession) -> None:
    run = await ob.execute_onboarding_plan(session, workspace_id=1, source_mode="fixture")
    detail = await runs_service.get_onboarding_run_detail(
        session, run.parent_job_run_id, workspace_id=1
    )
    success = sum(1 for s in detail.stages if s.status in ("success", "partial_success"))
    skipped = sum(1 for s in detail.stages if s.status == "skipped")
    assert detail.success_count == success
    assert detail.skipped_count == skipped
    assert detail.success_count >= 1  # identity/prices/exposure ran on seeded ws


async def test_unknown_run_404(session: AsyncSession) -> None:
    with pytest.raises(NotFoundError):
        await runs_service.get_onboarding_run_detail(session, 999_999, workspace_id=1)


# --- API ---------------------------------------------------------------------


async def test_api_workspace_runs_list_and_detail(client: AsyncClient) -> None:
    await client.post("/api/v1/workspaces/1/onboarding/run")
    listed = (await client.get("/api/v1/workspaces/1/onboarding/runs")).json()
    assert listed["scope_type"] == "workspace" and listed["scope_id"] == 1
    assert listed["count"] >= 1
    run_id = listed["runs"][0]["run_id"]
    detail = (await client.get(f"/api/v1/workspaces/1/onboarding/runs/{run_id}")).json()
    assert detail["run_id"] == run_id
    assert {s["stage"] for s in detail["stages"]} >= {"holdings", "constituent_identity"}
    assert "child_runs" in detail


async def test_api_fund_runs_list_and_detail(client: AsyncClient) -> None:
    await client.post("/api/v1/funds/1/onboarding/run")
    listed = (await client.get("/api/v1/funds/1/onboarding/runs")).json()
    assert listed["scope_type"] == "fund" and listed["scope_id"] == 1
    assert listed["count"] >= 1
    run_id = listed["runs"][0]["run_id"]
    detail = (await client.get(f"/api/v1/funds/1/onboarding/runs/{run_id}")).json()
    assert detail["run_id"] == run_id


async def test_api_limit_validation(client: AsyncClient) -> None:
    assert (await client.get("/api/v1/workspaces/1/onboarding/runs?limit=0")).status_code == 422
    assert (await client.get("/api/v1/workspaces/1/onboarding/runs?limit=500")).status_code == 422


async def test_api_foreign_workspace_run_404(client: AsyncClient, session: AsyncSession) -> None:
    run = await ob.execute_onboarding_plan(session, workspace_id=1, source_mode="fixture")
    ws = await _workspace(session, "ApiOther")
    await session.commit()
    resp = await client.get(f"/api/v1/workspaces/{ws.id}/onboarding/runs/{run.parent_job_run_id}")
    assert resp.status_code == 404


async def test_api_dashboard_latest_run_fields(client: AsyncClient) -> None:
    await client.post("/api/v1/workspaces/1/onboarding/run")
    body = (await client.get("/api/v1/workspaces/1/dashboard")).json()
    ob_block = body["onboarding"]
    assert ob_block["last_run_id"] is not None
    assert ob_block["last_run_status"] in ("success", "partial_success")
    assert ob_block["last_run_duration_ms"] is not None
    assert "last_run_failed_stage" in ob_block


async def test_api_capabilities_lists_run_history(client: AsyncClient) -> None:
    body = (await client.get("/api/v1/capabilities")).json()
    assert body["features"]["onboarding_run_history"] == "real"
    assert body["features"]["onboarding_stage_observability"] == "real"


async def test_api_diagnostics_onboarding_run_health(client: AsyncClient) -> None:
    body = (await client.get("/api/v1/workspaces/1/diagnostics")).json()
    assert "onboarding_recent_failures" in body
    assert "onboarding_legacy_runs_without_stage_metadata" in body


# --- safety ------------------------------------------------------------------


async def test_no_live_sources_and_no_secrets_in_payload(session: AsyncSession) -> None:
    run = await ob.execute_onboarding_plan(session, workspace_id=1, source_mode="fixture")
    parent = await session.get(JobRun, run.parent_job_run_id)
    # No live provider was touched.
    sources = set((await session.execute(select(JobRun.source))).scalars().all())
    assert not (sources & _LIVE_SOURCES)
    # No secret-looking material leaked into the structured payload or message.
    blob = (str(parent.payload_json) + (parent.message or "")).lower()
    assert not any(tok in blob for tok in _SECRET_TOKENS)
