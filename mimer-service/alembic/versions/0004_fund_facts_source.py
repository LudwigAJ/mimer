"""fund facts provenance: add funds.source

Revision ID: 0004
Revises: 0003
Create Date: 2026-06-20

Adds a `source` column to `funds` so the provenance of *fund facts* (official
name, provider, domicile, base currency, distribution policy, strategy, OCF/TER)
can be recorded and ranked. Populated by the `issuer_facts_ingestion` worker and
exposed in fund read schemas. Apply with `uv run alembic upgrade head`.

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Nullable: existing rows have unknown fact provenance until re-ingested.
    op.add_column("funds", sa.Column("source", sa.String(length=32), nullable=True))


def downgrade() -> None:
    op.drop_column("funds", "source")
