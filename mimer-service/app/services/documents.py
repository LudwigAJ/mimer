"""Document read services + content hashing.

Read side of the document subsystem: list/inspect document snapshots and the
deterministic content-hash helper used by both the ingestion service and tests.
The provider-agnostic ingestion/change-detection logic lives in
``app/services/document_ingestion.py``; provider fetch/parse in
``app/sources/documents.py``.
"""

from __future__ import annotations

import hashlib
from datetime import date

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFoundError
from app.db.models import DocumentSnapshot, Fund
from app.sources.documents import DocumentRecord


def compute_document_hash(record: DocumentRecord) -> str:
    """Deterministic SHA-256 hex digest identifying a document's content.

    Prefers the actual content (bytes, then text); when neither is supplied it
    falls back to stable metadata (type + url + date + title), so a URL/date
    change is still detected. No PDF parsing — the bytes/text are hashed as-is.
    """
    digest = hashlib.sha256()
    if record.content_bytes is not None:
        digest.update(record.content_bytes)
    elif record.content_text is not None:
        digest.update(record.content_text.encode("utf-8"))
    else:
        parts = [
            record.document_type,
            record.document_url or "",
            record.document_date.isoformat() if record.document_date else "",
            record.title or "",
        ]
        digest.update("|".join(parts).encode("utf-8"))
    return digest.hexdigest()


def _doc_sort_key(doc: DocumentSnapshot) -> tuple[date, object, int]:
    """Newest-first ordering key: document_date, then created_at, then id.

    The ``id`` tie-breaks rows created within the same clock tick (some backends
    store ``created_at`` at second resolution), keeping "latest" deterministic.
    """
    return (doc.document_date or date.min, doc.created_at, doc.id or 0)


def _latest_per_type(docs: list[DocumentSnapshot]) -> list[DocumentSnapshot]:
    """One snapshot per ``document_type`` — the newest by date/created_at."""
    best: dict[str, DocumentSnapshot] = {}
    for doc in docs:
        current = best.get(doc.document_type)
        if current is None or _doc_sort_key(doc) > _doc_sort_key(current):
            best[doc.document_type] = doc
    return sorted(best.values(), key=lambda d: d.document_type)


async def list_documents(
    session: AsyncSession,
    fund_id: int | None = None,
    document_type: str | None = None,
    limit: int = 200,
) -> list[DocumentSnapshot]:
    stmt = select(DocumentSnapshot).order_by(DocumentSnapshot.created_at.desc()).limit(limit)
    if fund_id is not None:
        stmt = stmt.where(DocumentSnapshot.fund_id == fund_id)
    if document_type is not None:
        stmt = stmt.where(DocumentSnapshot.document_type == document_type)
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def list_fund_documents(
    session: AsyncSession,
    fund_id: int,
    *,
    document_type: str | None = None,
    latest_only: bool = False,
    limit: int = 200,
) -> list[DocumentSnapshot]:
    """Documents for a fund (404 if the fund is unknown).

    ``latest_only`` collapses to one snapshot per document type (newest first);
    otherwise full snapshot history is returned (newest first), bounded.
    """
    fund = await session.get(Fund, fund_id)
    if fund is None:
        raise NotFoundError("Fund not found", code="fund_not_found")
    stmt = select(DocumentSnapshot).where(DocumentSnapshot.fund_id == fund_id)
    if document_type is not None:
        stmt = stmt.where(DocumentSnapshot.document_type == document_type)
    rows = list((await session.execute(stmt)).scalars().all())
    rows.sort(key=_doc_sort_key, reverse=True)
    if latest_only:
        rows = sorted(_latest_per_type(rows), key=_doc_sort_key, reverse=True)
    return rows[:limit]


async def get_document(session: AsyncSession, document_id: int) -> DocumentSnapshot:
    doc = await session.get(DocumentSnapshot, document_id)
    if doc is None:
        raise NotFoundError("Document not found", code="document_not_found")
    return doc
