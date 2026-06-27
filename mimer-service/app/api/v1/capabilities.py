"""Service capability discovery endpoint.

``GET /api/v1/capabilities`` returns a single introspection payload describing
what the service implements (real/fixture/stub/planned workers + features),
which provider adapters are configured, supported/planned asset classes and data
types, the migration head, and the source-capability catalogue. It never exposes
secrets (no DSNs or API keys — only whether a key is configured).
"""

from __future__ import annotations

from fastapi import APIRouter

from app.schemas.capability import CapabilitiesResponse
from app.services import capabilities as capabilities_service

router = APIRouter(tags=["capabilities"])


@router.get("/capabilities", response_model=CapabilitiesResponse)
async def get_capabilities() -> CapabilitiesResponse:
    return capabilities_service.build_capabilities()
