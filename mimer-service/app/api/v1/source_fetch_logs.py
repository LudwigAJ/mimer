"""External fetch-log endpoints (observability for source requests).

Read-only. Logs carry a safe request key, an endpoint *label* and hashes only —
never API keys, auth headers or tokenised URLs.
"""

from __future__ import annotations

from fastapi import APIRouter, Query

from app.api.deps import SessionDep
from app.schemas.common import ListResponse
from app.schemas.source_ops import SourceFetchLogRead
from app.services import source_requests

router = APIRouter(prefix="/source-fetch-logs", tags=["source-fetch-logs"])


@router.get("", response_model=ListResponse[SourceFetchLogRead])
async def list_source_fetch_logs(
    session: SessionDep,
    source: str | None = Query(default=None),
    status: str | None = Query(default=None),
    request_kind: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=1000),
) -> ListResponse[SourceFetchLogRead]:
    logs = await source_requests.list_fetch_logs(
        session, source=source, status=status, request_kind=request_kind, limit=limit
    )
    return ListResponse.of([SourceFetchLogRead.model_validate(log) for log in logs])
