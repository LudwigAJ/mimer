"""Running/leased scheduled-job read model (live timeline counterpart).

Covers the single lease classifier (running / stuck / expired / due / blocked),
the bounded read service, timeline ``include_running`` integration, the
``/jobs/running`` + ``/jobs/leases`` API (global + workspace), and the
scheduler-status / diagnostics / capabilities fields that reuse the same helper.
All offline — no live provider call, no lease mutation, no secret leakage.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import JobRun, ScheduledJob, Workspace
from app.services import job_leases as jl
from app.services import job_timeline as tl


def _now() -> datetime:
    return datetime(2026, 6, 21, 12, 0, 0, tzinfo=UTC)


def _job(**kw) -> ScheduledJob:
    """A detached ScheduledJob for the pure-classifier tests."""
    return ScheduledJob(
        name=kw.pop("name", "j"),
        job_type=kw.pop("job_type", "broker_csv_import"),
        schedule_kind=kw.pop("schedule_kind", "daily"),
        is_active=kw.pop("is_active", True),
        **kw,
    )


async def _add_job(session: AsyncSession, **kw) -> ScheduledJob:
    job = _job(**kw)
    session.add(job)
    await session.flush()
    return job


# --- classification (pure) ---------------------------------------------------


def test_classify_unlocked_due_job() -> None:
    now = _now()
    job = _job(next_run_at=now - timedelta(minutes=1))
    assert jl.classify_lease(job, now=now) == jl.DUE


def test_classify_locked_unexpired_is_running() -> None:
    now = _now()
    job = _job(
        locked_at=now - timedelta(seconds=60),
        locked_by="A",
        lock_expires_at=now + timedelta(seconds=240),
        last_heartbeat_at=now - timedelta(seconds=60),
        next_run_at=now + timedelta(days=1),
    )
    assert jl.classify_lease(job, now=now) == jl.RUNNING


def test_classify_locked_expired_is_expired() -> None:
    now = _now()
    job = _job(
        locked_at=now - timedelta(seconds=600),
        locked_by="A",
        lock_expires_at=now - timedelta(seconds=60),
    )
    assert jl.classify_lease(job, now=now) == jl.EXPIRED


def test_classify_stuck_via_max_runtime() -> None:
    now = _now()
    # Active lease (not expired) held far past its watchdog window.
    job = _job(
        locked_at=now - timedelta(seconds=600),
        locked_by="A",
        lock_expires_at=now + timedelta(seconds=60),
        last_heartbeat_at=now - timedelta(seconds=10),
        max_runtime_seconds=120,
    )
    assert jl.classify_lease(job, now=now) == jl.STUCK


def test_classify_stuck_via_stale_heartbeat() -> None:
    now = _now()
    job = _job(
        locked_at=now - timedelta(seconds=1000),
        locked_by="A",
        lock_expires_at=now + timedelta(seconds=100),  # still active
        last_heartbeat_at=now - timedelta(seconds=1000),  # > 900s stale
    )
    assert jl.classify_lease(job, now=now) == jl.STUCK


def test_manual_job_is_not_due() -> None:
    now = _now()
    job = _job(schedule_kind="manual", next_run_at=now - timedelta(hours=1))
    assert jl.classify_lease(job, now=now) == jl.NOT_LEASED


def test_inactive_job_is_ignored() -> None:
    now = _now()
    job = _job(is_active=False, next_run_at=now - timedelta(hours=1))
    assert jl.classify_lease(job, now=now) == jl.NOT_LEASED


def test_blocked_by_lease_when_due_but_leased() -> None:
    now = _now()
    job = _job(
        locked_at=now - timedelta(seconds=60),
        locked_by="A",
        lock_expires_at=now + timedelta(seconds=240),
        last_heartbeat_at=now - timedelta(seconds=60),
        next_run_at=now - timedelta(seconds=60),  # would be due, but leased
    )
    status = jl.classify_lease(job, now=now)
    assert status == jl.RUNNING
    assert jl.is_blocked_by_lease(job, status, now=now) is True


def test_build_running_item_fields() -> None:
    now = _now()
    job = _job(
        name="x",
        locked_at=now - timedelta(seconds=120),
        locked_by="worker:1",
        lock_expires_at=now + timedelta(seconds=180),
        last_heartbeat_at=now - timedelta(seconds=30),
        next_run_at=now + timedelta(days=1),
    )
    job.id = 7
    item = jl.build_running_item(job, now=now)
    assert item.kind == "running_lease"
    assert item.lease_status == jl.RUNNING
    assert item.severity == "running"
    assert item.scheduled_job_id == 7
    assert item.locked_by == "worker:1"
    assert item.age_seconds == 120
    assert item.seconds_until_expiry == 180
    assert item.started_at_for_timeline == job.locked_at
    assert item.is_expired is False and item.is_stuck is False
    # Recommended actions are labels only — never an unlock/kill action.
    codes = {a.code for a in item.recommended_actions}
    assert codes and not (codes & {"unlock", "kill", "force_unlock", "force_release"})


# --- read service (DB) -------------------------------------------------------


async def _seed_states(session: AsyncSession, now: datetime) -> None:
    await _add_job(
        session,
        name="expired_one",
        locked_at=now - timedelta(seconds=600),
        locked_by="A",
        lock_expires_at=now - timedelta(seconds=60),
    )
    await _add_job(
        session,
        name="stuck_one",
        locked_at=now - timedelta(seconds=1000),
        locked_by="B",
        lock_expires_at=now + timedelta(seconds=100),
        last_heartbeat_at=now - timedelta(seconds=1000),
    )
    await _add_job(
        session,
        name="running_one",
        locked_at=now - timedelta(seconds=30),
        locked_by="C",
        lock_expires_at=now + timedelta(seconds=270),
        last_heartbeat_at=now - timedelta(seconds=30),
    )
    await _add_job(
        session,
        name="due_one",
        next_run_at=now - timedelta(seconds=90),
    )


async def test_list_running_jobs_sorted_urgent_first(session: AsyncSession) -> None:
    now = _now()
    await _seed_states(session, now)
    items = await jl.list_running_jobs(session, now=now)
    statuses = [i.lease_status for i in items]
    # Most urgent first: expired, stuck, running, due.
    assert statuses == [jl.EXPIRED, jl.STUCK, jl.RUNNING, jl.DUE]


async def test_summary_counts_correct(session: AsyncSession) -> None:
    now = _now()
    await _seed_states(session, now)
    summary = await jl.lease_summary_counts(session, now=now)
    assert summary.expired_lease_count == 1
    assert summary.stuck_lease_count == 1
    assert summary.running_count == 1
    assert summary.due_count == 1
    assert summary.blocked_by_lease_count == 0
    assert summary.total == 4


async def test_blocked_by_lease_counted(session: AsyncSession) -> None:
    now = _now()
    await _add_job(
        session,
        name="blocked",
        locked_at=now - timedelta(seconds=30),
        locked_by="C",
        lock_expires_at=now + timedelta(seconds=270),
        last_heartbeat_at=now - timedelta(seconds=30),
        next_run_at=now - timedelta(seconds=30),  # overdue but leased
    )
    summary = await jl.lease_summary_counts(session, now=now)
    assert summary.blocked_by_lease_count == 1
    assert summary.running_count == 1
    assert summary.due_count == 0  # blocked != due


async def test_list_due_only(session: AsyncSession) -> None:
    now = _now()
    await _seed_states(session, now)
    due = await jl.list_due_scheduled_jobs(session, now=now)
    assert [i.lease_status for i in due] == [jl.DUE]


async def test_list_job_leases_filter_and_exclude_expired(session: AsyncSession) -> None:
    now = _now()
    await _seed_states(session, now)
    only_stuck = await jl.list_job_leases(session, status=jl.STUCK, now=now)
    assert [i.lease_status for i in only_stuck] == [jl.STUCK]
    no_expired = await jl.list_job_leases(session, include_expired=False, now=now)
    assert all(i.lease_status != jl.EXPIRED for i in no_expired)


async def test_limit_clamped_and_bounded(session: AsyncSession) -> None:
    assert jl.clamp_limit(10_000) == jl.MAX_LIMIT
    assert jl.clamp_limit(None) == jl.DEFAULT_LIMIT
    assert jl.clamp_limit(0) == jl.DEFAULT_LIMIT
    assert jl.clamp_limit(-3) == 1
    now = _now()
    for i in range(4):
        await _add_job(session, name=f"due_{i}", next_run_at=now - timedelta(minutes=i + 1))
    items = await jl.list_running_jobs(session, limit=2, now=now)
    assert len(items) == 2


async def test_timezone_naive_round_trip(session: AsyncSession) -> None:
    now = _now()
    await _add_job(
        session,
        name="naive_running",
        locked_at=now - timedelta(seconds=30),
        locked_by="C",
        lock_expires_at=now + timedelta(seconds=270),
        last_heartbeat_at=now - timedelta(seconds=30),
    )
    await session.commit()
    session.expire_all()  # force a reload from SQLite (naive datetimes)
    items = await jl.list_running_jobs(session, now=now)
    assert [i.lease_status for i in items] == [jl.RUNNING]


async def test_read_service_does_not_mutate(session: AsyncSession) -> None:
    now = _now()
    job = await _add_job(
        session,
        name="immutable",
        locked_at=now - timedelta(seconds=30),
        locked_by="C",
        lock_expires_at=now + timedelta(seconds=270),
        last_heartbeat_at=now - timedelta(seconds=30),
    )
    # Commit + refresh first so the baseline is the DB round-trip value (SQLite
    # returns naive datetimes), making the comparison about mutation, not tzinfo.
    await session.commit()
    await session.refresh(job)
    before = (job.locked_at, job.locked_by, job.lock_expires_at, job.last_heartbeat_at)
    await jl.list_running_jobs(session, now=now)
    await jl.lease_summary_counts(session, now=now)
    await session.refresh(job)
    assert (job.locked_at, job.locked_by, job.lock_expires_at, job.last_heartbeat_at) == before


# --- timeline integration ----------------------------------------------------


async def test_timeline_without_include_running_unchanged(session: AsyncSession) -> None:
    session.add(JobRun(job_type="price_ingestion", status="success"))
    await session.flush()
    resp = await tl.global_timeline(session)
    assert resp.include_running is False
    assert resp.live_jobs == []
    assert resp.running_summary is None
    assert len(resp.runs) >= 1


async def test_timeline_with_include_running_has_live_rows(session: AsyncSession) -> None:
    now = _now()
    session.add(JobRun(job_type="price_ingestion", status="success"))
    await _add_job(
        session,
        name="due_live",
        next_run_at=now - timedelta(seconds=90),
    )
    await session.flush()
    resp = await tl.global_timeline(session, include_running=True)
    assert resp.include_running is True
    assert resp.running_summary is not None
    assert any(i.name == "due_live" for i in resp.live_jobs)
    # Discriminator present on every live row + completed runs still present.
    assert all(i.kind for i in resp.live_jobs)
    assert len(resp.runs) >= 1


async def test_workspace_timeline_runs_scoped_live_global(session: AsyncSession) -> None:
    now = _now()
    other = Workspace(name="OtherWS", base_currency="GBP")
    session.add(other)
    await session.flush()
    session.add(JobRun(job_type="exposure_recompute", status="success", workspace_id=1))
    session.add(JobRun(job_type="exposure_recompute", status="success", workspace_id=other.id))
    await _add_job(session, name="due_live_ws", next_run_at=now - timedelta(seconds=90))
    await session.flush()
    resp = await tl.workspace_timeline(session, 1, include_running=True)
    # Completed runs are workspace-scoped; live scheduled-job rows are global.
    assert all(r.workspace_id == 1 for r in resp.runs)
    assert any(i.name == "due_live_ws" for i in resp.live_jobs)


# --- API ---------------------------------------------------------------------


async def _add_due(session: AsyncSession, name: str) -> None:
    session.add(
        ScheduledJob(
            name=name,
            job_type="broker_csv_import",
            schedule_kind="daily",
            interval_seconds=86400,
            is_active=True,
            next_run_at=datetime.now(UTC) - timedelta(minutes=1),
        )
    )
    await session.commit()


async def test_api_jobs_running(client: AsyncClient, session: AsyncSession) -> None:
    await _add_due(session, "api_due_running")
    body = (await client.get("/api/v1/jobs/running?limit=50")).json()
    assert body["scope_type"] == "global"
    assert "summary" in body and body["summary"]["due_count"] >= 1
    assert any(j["name"] == "api_due_running" for j in body["jobs"])
    assert all("kind" in j and "lease_status" in j for j in body["jobs"])


async def test_api_workspace_jobs_running(client: AsyncClient, session: AsyncSession) -> None:
    await _add_due(session, "api_due_ws_running")
    body = (await client.get("/api/v1/workspaces/1/jobs/running?limit=50")).json()
    assert body["scope_type"] == "workspace" and body["scope_id"] == 1
    assert any(j["name"] == "api_due_ws_running" for j in body["jobs"])
    # Unknown workspace 404s.
    assert (await client.get("/api/v1/workspaces/999999/jobs/running")).status_code == 404


async def test_api_jobs_leases_filter(client: AsyncClient, session: AsyncSession) -> None:
    await _add_due(session, "api_due_lease")
    body = (await client.get("/api/v1/jobs/leases?status=due&limit=50")).json()
    assert all(j["lease_status"] == "due" for j in body["jobs"])
    assert any(j["name"] == "api_due_lease" for j in body["jobs"])


async def test_api_timeline_include_running(client: AsyncClient, session: AsyncSession) -> None:
    await _add_due(session, "api_timeline_due")
    plain = (await client.get("/api/v1/jobs/timeline?limit=10")).json()
    assert plain["include_running"] is False
    assert plain["live_jobs"] == []
    enriched = (await client.get("/api/v1/jobs/timeline?include_running=true&limit=10")).json()
    assert enriched["include_running"] is True
    assert enriched["running_summary"] is not None
    assert any(j["name"] == "api_timeline_due" for j in enriched["live_jobs"])


async def test_api_workspace_timeline_include_running(
    client: AsyncClient, session: AsyncSession
) -> None:
    await _add_due(session, "api_ws_timeline_due")
    body = (
        await client.get("/api/v1/workspaces/1/jobs/timeline?include_running=true&limit=10")
    ).json()
    assert body["include_running"] is True
    assert any(j["name"] == "api_ws_timeline_due" for j in body["live_jobs"])


async def test_api_running_limit_validation(client: AsyncClient) -> None:
    assert (await client.get("/api/v1/jobs/running?limit=0")).status_code == 422
    assert (await client.get("/api/v1/jobs/running?limit=501")).status_code == 422
    assert (await client.get("/api/v1/jobs/leases?limit=99999")).status_code == 422
    assert (await client.get("/api/v1/workspaces/1/jobs/running?limit=501")).status_code == 422


async def test_api_scheduler_status_lease_fields(
    client: AsyncClient, session: AsyncSession
) -> None:
    await _add_due(session, "api_status_due")
    body = (await client.get("/api/v1/scheduler/status")).json()
    for field in ("running_leases", "stuck_leases", "expired_leases", "blocked_by_lease"):
        assert field in body
    assert "next_due_at" in body
    assert body["due_jobs"] >= 1


async def test_api_diagnostics_lease_fields(client: AsyncClient, session: AsyncSession) -> None:
    body = (await client.get("/api/v1/diagnostics")).json()
    for field in (
        "running_job_leases",
        "stuck_job_leases",
        "expired_job_leases",
        "blocked_scheduled_jobs_by_lease",
        "due_scheduled_jobs",
    ):
        assert field in body
    ws = (await client.get("/api/v1/workspaces/1/diagnostics")).json()
    assert "running_job_leases" in ws and "stuck_job_leases" in ws


async def test_api_capabilities_lease_features(client: AsyncClient) -> None:
    features = (await client.get("/api/v1/capabilities")).json()["features"]
    assert features["running_job_timeline"] == "real"
    assert features["job_lease_observability"] == "real"
    assert features["stuck_lease_read_model"] == "real"


# --- safety ------------------------------------------------------------------


async def test_no_lease_mutation_via_api(client: AsyncClient, session: AsyncSession) -> None:
    session.add(
        ScheduledJob(
            name="leased_immutable",
            job_type="broker_csv_import",
            schedule_kind="daily",
            interval_seconds=86400,
            is_active=True,
            locked_at=datetime.now(UTC) - timedelta(seconds=30),
            locked_by="C",
            lock_expires_at=datetime.now(UTC) + timedelta(seconds=270),
            last_heartbeat_at=datetime.now(UTC) - timedelta(seconds=30),
        )
    )
    await session.commit()
    await client.get("/api/v1/jobs/running")
    await client.get("/api/v1/jobs/timeline?include_running=true")
    await client.get("/api/v1/scheduler/status")
    # The read-only views never touch the lease.
    job = (
        await session.execute(select(ScheduledJob).where(ScheduledJob.name == "leased_immutable"))
    ).scalar_one()
    await session.refresh(job)
    assert job.locked_by == "C"
    assert job.lock_expires_at is not None
