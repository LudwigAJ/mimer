"""live issuer distribution ingestion: provenance + extra issuer fields

Revision ID: 0018
Revises: 0017
Create Date: 2026-06-25

Brings ``distributions`` in line with the other ingested data models (holdings /
reference_rates) so real issuer distribution/dividend observations can be stored
with provenance and the few extra issuer-published fields:

* ``distribution_date`` — the issuer's labelled distribution date when distinct
  from ex/record/payment;
* ``distribution_type`` — income / dividend / capital_gain / return_of_capital
  (issuer-asserted, optional; stored verbatim — NEVER used for tax treatment, see
  AGENTS.md compute boundary);
* ``frequency`` / ``share_class`` — optional issuer labels;
* ``raw_payload_json`` — raw provider payload + any issuer field without a
  dedicated canonical column (provenance / debugging);
* ``updated_at`` — mutated-on-change timestamp (matches holdings/reference_rates).

All columns are additive + nullable (``updated_at`` defaults to ``now()``), so the
change is backwards compatible and needs no data migration. The idempotency key is
unchanged — one declared distribution per ``(fund_id, ex_date, source)`` — so
re-runs / backfills still upsert without duplicating, and distinct sources keep
their own rows. Apply with ``uv run alembic upgrade head``.

NON-GOALS (see AGENTS.md): this migration adds **no** dividend-forecast, projected
income, yield or tax-lot tables. The backend collects + persists official
distribution observations only.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_NOW = sa.text("now()")


def upgrade() -> None:
    op.add_column("distributions", sa.Column("distribution_date", sa.Date(), nullable=True))
    op.add_column(
        "distributions", sa.Column("distribution_type", sa.String(length=32), nullable=True)
    )
    op.add_column("distributions", sa.Column("frequency", sa.String(length=32), nullable=True))
    op.add_column("distributions", sa.Column("share_class", sa.String(length=64), nullable=True))
    op.add_column("distributions", sa.Column("raw_payload_json", sa.JSON(), nullable=True))
    op.add_column(
        "distributions",
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=_NOW,
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("distributions", "updated_at")
    op.drop_column("distributions", "raw_payload_json")
    op.drop_column("distributions", "share_class")
    op.drop_column("distributions", "frequency")
    op.drop_column("distributions", "distribution_type")
    op.drop_column("distributions", "distribution_date")
