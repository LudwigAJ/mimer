"""Document ingestion service.

Fetches published documents from a `DocumentSource` adapter, hashes their content
and upserts them into ``document_snapshots`` with **content-hash change
detection**, keyed on the
(fund_id, document_type, source, content_hash) unique constraint so re-runs are
idempotent and history is preserved.

Per incoming document, relative to the latest stored snapshot for the same
(fund, document_type, source):

* **new**       — first time we see this document      -> insert (change_status=new)
* **changed**   — content hash differs from the latest -> insert a NEW snapshot
                  (change_status=changed; links the prior hash/id) — old rows kept
* **unchanged** — we already hold this exact content   -> no new row; bump
                  ``fetched_at`` (and update mutable metadata if it drifted)
* **failed**    — one bad document is isolated and counted; the job continues

Old snapshots are never deleted — the history backs the GUI's document Changes
view. Provider-specific fetch/parse lives in ``app/sources/documents.py``; the
hash helper in ``app/services/documents.py``. There is no PDF text extraction.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import DocumentSnapshot, Fund
from app.services.documents import compute_document_hash
from app.sources.documents import DocumentRecord, DocumentSource

NEW = "new"
CHANGED = "changed"
UNCHANGED = "unchanged"

# Metadata that may drift without the content hash changing (e.g. a re-titled
# link). content_hash / identity fields are excluded.
_MUTABLE_FIELDS = (
    "title",
    "url",
    "document_date",
    "language",
    "country_or_region",
    "content_type",
    "status",
    "raw_payload_json",
)


@dataclass
class DocumentCounts:
    inserted: int = 0  # new rows created (new + changed)
    updated: int = 0  # unchanged content, but metadata drifted -> updated in place
    unchanged: int = 0  # exact same content already held
    new: int = 0  # subset of inserted: first version of a document
    changed: int = 0  # subset of inserted: content changed vs the prior snapshot
    failed: int = 0


def _record_metadata(record: DocumentRecord) -> dict[str, object]:
    return {
        "title": record.title,
        "url": record.document_url,
        "document_date": record.document_date,
        "language": record.language,
        "country_or_region": record.country_or_region,
        "content_type": record.content_type,
        "status": record.status,
        "raw_payload_json": record.raw_payload,
    }


def _apply_metadata(existing: DocumentSnapshot, metadata: dict[str, object]) -> bool:
    """Update mutable metadata in place; return True if anything changed."""
    changed = False
    for field in _MUTABLE_FIELDS:
        if getattr(existing, field) != metadata[field]:
            setattr(existing, field, metadata[field])
            changed = True
    return changed


async def _latest_snapshot(
    session: AsyncSession, *, fund_id: int, document_type: str, source: str
) -> DocumentSnapshot | None:
    return await session.scalar(
        select(DocumentSnapshot)
        .where(
            DocumentSnapshot.fund_id == fund_id,
            DocumentSnapshot.document_type == document_type,
            DocumentSnapshot.source == source,
        )
        .order_by(DocumentSnapshot.created_at.desc(), DocumentSnapshot.id.desc())
    )


async def ingest_documents_for_fund(
    session: AsyncSession, fund: Fund, source: DocumentSource
) -> DocumentCounts:
    counts = DocumentCounts()
    records = await source.fetch_documents(isin=fund.isin)
    now = datetime.now(UTC)

    for record in records:
        try:
            content_hash = record.content_hash or compute_document_hash(record)
            metadata = _record_metadata(record)

            # Have we already stored this exact content version?
            existing_same = await session.scalar(
                select(DocumentSnapshot).where(
                    DocumentSnapshot.fund_id == fund.id,
                    DocumentSnapshot.document_type == record.document_type,
                    DocumentSnapshot.source == record.source,
                    DocumentSnapshot.content_hash == content_hash,
                )
            )
            if existing_same is not None:
                meta_changed = _apply_metadata(existing_same, metadata)
                existing_same.fetched_at = now
                if meta_changed:
                    counts.updated += 1
                else:
                    counts.unchanged += 1
                continue

            latest = await _latest_snapshot(
                session,
                fund_id=fund.id,
                document_type=record.document_type,
                source=record.source,
            )
            change_status = NEW if latest is None else CHANGED
            session.add(
                DocumentSnapshot(
                    fund_id=fund.id,
                    document_type=record.document_type,
                    source=record.source,
                    content_hash=content_hash,
                    change_status=change_status,
                    previous_content_hash=latest.content_hash if latest else None,
                    previous_snapshot_id=latest.id if latest else None,
                    fetched_at=now,
                    **metadata,
                )
            )
            counts.inserted += 1
            if change_status == NEW:
                counts.new += 1
            else:
                counts.changed += 1
        except Exception:
            counts.failed += 1

    await session.flush()
    return counts
