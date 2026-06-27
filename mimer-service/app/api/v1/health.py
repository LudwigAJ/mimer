"""Health endpoints (mounted at the application root, not under /api/v1).

* ``GET /health``    — liveness: the process is up and serving. No DB touch, so
  a container healthcheck never fails just because Postgres is briefly down.
* ``GET /health/db`` — readiness: the API can reach Postgres (``SELECT 1``).
  Returns 503 if the database is unreachable. Cheap — no migrations, no
  diagnostics, no business queries.
"""

from __future__ import annotations

from fastapi import APIRouter, Response
from sqlalchemy import text

from app.api.deps import SessionDep

router = APIRouter(tags=["health"])


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/health/db")
async def health_db(session: SessionDep, response: Response) -> dict[str, str]:
    try:
        await session.execute(text("SELECT 1"))
    except Exception:  # noqa: BLE001 - any failure means "not ready"
        response.status_code = 503
        return {"status": "error", "database": "unreachable"}
    return {"status": "ok", "database": "connected"}
