from __future__ import annotations

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import JobRun, ScheduledJob


async def test_list_scheduled_jobs(client: AsyncClient) -> None:
    response = await client.get("/api/v1/jobs")
    assert response.status_code == 200
    body = response.json()
    assert body["meta"]["count"] == 6
    names = {job["name"] for job in body["data"]}
    assert "daily_price_ingestion" in names
    # Schedule + lease metadata is now exposed on every job.
    price = next(j for j in body["data"] if j["name"] == "daily_price_ingestion")
    assert price["schedule_kind"] == "daily"
    assert price["locked_by"] is None


async def test_trigger_job_creates_stub_run(client: AsyncClient, session: AsyncSession) -> None:
    # rates_curve_ingestion has no worker yet -> triggering it records a stub run.
    session.add(
        ScheduledJob(name="nightly_broker", job_type="rates_curve_ingestion", is_active=True)
    )
    await session.commit()
    job = next(
        j
        for j in (await client.get("/api/v1/jobs")).json()["data"]
        if j["job_type"] == "rates_curve_ingestion"
    )

    run = await client.post(f"/api/v1/jobs/{job['id']}/run")
    assert run.status_code == 201
    body = run.json()
    assert body["status"] == "success_stub"
    assert body["scheduled_job_id"] == job["id"]

    runs = await client.get("/api/v1/jobs/runs")
    assert runs.status_code == 200
    assert runs.json()["meta"]["count"] >= 1


async def test_unknown_job_is_404(client: AsyncClient) -> None:
    response = await client.get("/api/v1/jobs/999999")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "job_not_found"


async def test_job_runs_filtering(client: AsyncClient) -> None:
    # Resolving a known ISIN queues several backfill runs of different types.
    resolved = await client.post(
        "/api/v1/instruments", json={"symbol": "IE00B3XXRP09", "symbol_type": "isin"}
    )
    assert resolved.status_code == 202
    fund_id = resolved.json()["fund_id"]

    by_type = await client.get("/api/v1/jobs/runs?job_type=price_ingestion")
    assert by_type.status_code == 200
    assert all(r["job_type"] == "price_ingestion" for r in by_type.json()["data"])

    queued = await client.get("/api/v1/jobs/runs?status=queued")
    assert all(r["status"] == "queued" for r in queued.json()["data"])
    assert queued.json()["meta"]["count"] >= 1

    by_fund = await client.get(f"/api/v1/jobs/runs?fund_id={fund_id}")
    assert all(r["fund_id"] == fund_id for r in by_fund.json()["data"])


async def test_job_runs_expose_fund_targets(client: AsyncClient) -> None:
    await client.post("/api/v1/instruments", json={"symbol": "IE00B3XXRP09", "symbol_type": "isin"})
    runs = (await client.get("/api/v1/jobs/runs")).json()["data"]
    assert any(r["fund_id"] is not None for r in runs)


async def test_trigger_blocked_when_run_in_progress(
    client: AsyncClient, session: AsyncSession
) -> None:
    job = (await client.get("/api/v1/jobs")).json()["data"][0]
    # Simulate a leftover in-progress run (e.g. a previous crash).
    session.add(JobRun(job_type=job["job_type"], scheduled_job_id=job["id"], status="running"))
    await session.commit()

    response = await client.post(f"/api/v1/jobs/{job['id']}/run")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "job_already_running"
