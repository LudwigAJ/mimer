"""document ingestion: metadata, content provenance, change detection

Revision ID: 0008
Revises: 0007
Create Date: 2026-06-21

Extends ``document_snapshots`` so the `document_snapshot_ingestion` worker can
ingest fund documents idempotently with content-hash change detection:

* adds descriptive metadata (``title``, ``language``, ``country_or_region``,
  ``content_type``), a ``fetched_at`` timestamp (last time the document was
  fetched/verified) and a ``raw_payload_json`` provenance blob;
* adds change-detection columns: ``change_status`` (new | changed), and links to
  the snapshot a row superseded (``previous_snapshot_id`` self-FK +
  ``previous_content_hash``) so the GUI can show "Factsheet changed since …";
* backfills existing (seed) rows as ``change_status='new'`` with
  ``fetched_at = created_at``;
* adds the ``uq_document_snapshot_identity`` unique constraint on
  (fund_id, document_type, source, content_hash) so a given content version is a
  single row — re-runs never duplicate, a changed hash inserts a *new* snapshot
  (history is preserved), and distinct sources keep their own rows. NULL hashes
  (legacy seed rows) are treated as distinct, which is the desired behaviour.

This stores document *metadata + hashes*, not blobs. PDF text extraction / OCR
and object-storage of the bytes are explicitly future work.

Apply with ``uv run alembic upgrade head``.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("document_snapshots", sa.Column("title", sa.String(length=512), nullable=True))
    op.add_column("document_snapshots", sa.Column("language", sa.String(length=16), nullable=True))
    op.add_column(
        "document_snapshots", sa.Column("country_or_region", sa.String(length=64), nullable=True)
    )
    op.add_column(
        "document_snapshots", sa.Column("content_type", sa.String(length=64), nullable=True)
    )
    op.add_column(
        "document_snapshots", sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "document_snapshots", sa.Column("change_status", sa.String(length=16), nullable=True)
    )
    op.add_column(
        "document_snapshots",
        sa.Column("previous_content_hash", sa.String(length=128), nullable=True),
    )
    op.add_column(
        "document_snapshots",
        sa.Column(
            "previous_snapshot_id",
            sa.Integer(),
            sa.ForeignKey("document_snapshots.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.add_column("document_snapshots", sa.Column("raw_payload_json", sa.JSON(), nullable=True))

    # Existing rows are the first version of their document.
    op.execute(
        "UPDATE document_snapshots "
        "SET change_status = 'new', fetched_at = created_at "
        "WHERE change_status IS NULL"
    )

    op.create_unique_constraint(
        "uq_document_snapshot_identity",
        "document_snapshots",
        ["fund_id", "document_type", "source", "content_hash"],
    )


def downgrade() -> None:
    op.drop_constraint("uq_document_snapshot_identity", "document_snapshots", type_="unique")
    op.drop_column("document_snapshots", "raw_payload_json")
    op.drop_column("document_snapshots", "previous_snapshot_id")
    op.drop_column("document_snapshots", "previous_content_hash")
    op.drop_column("document_snapshots", "change_status")
    op.drop_column("document_snapshots", "fetched_at")
    op.drop_column("document_snapshots", "content_type")
    op.drop_column("document_snapshots", "country_or_region")
    op.drop_column("document_snapshots", "language")
    op.drop_column("document_snapshots", "title")
