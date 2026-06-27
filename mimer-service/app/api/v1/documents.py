"""Global document snapshot endpoint."""

from __future__ import annotations

from fastapi import APIRouter, Query

from app.api.deps import SessionDep
from app.schemas.common import ListResponse
from app.schemas.document import DocumentRead
from app.services import documents as service

router = APIRouter(prefix="/documents", tags=["documents"])


@router.get("", response_model=ListResponse[DocumentRead])
async def list_documents(
    session: SessionDep,
    fund_id: int | None = None,
    document_type: str | None = None,
    limit: int = Query(default=200, ge=1, le=1000),
) -> ListResponse[DocumentRead]:
    items = await service.list_documents(
        session, fund_id=fund_id, document_type=document_type, limit=limit
    )
    return ListResponse.of([DocumentRead.model_validate(i) for i in items])


@router.get("/{document_id}", response_model=DocumentRead)
async def get_document(document_id: int, session: SessionDep) -> DocumentRead:
    """A single document snapshot (with its change-detection provenance)."""
    doc = await service.get_document(session, document_id)
    return DocumentRead.model_validate(doc)
