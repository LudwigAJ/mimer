"""distribution ingestion: unique (fund, ex_date, source)

Revision ID: 0005
Revises: 0004
Create Date: 2026-06-20

Adds a unique constraint to `distributions` on (fund_id, ex_date, source) so the
`distribution_ingestion` worker can upsert declared distributions idempotently
(re-runs and backfills do not duplicate rows). Different sources may still assert
the same ex-date because provenance differs. Apply with
`uv run alembic upgrade head`.

"""

from collections.abc import Sequence

from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_unique_constraint(
        "uq_distribution_fund_exdate_source",
        "distributions",
        ["fund_id", "ex_date", "source"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_distribution_fund_exdate_source",
        "distributions",
        type_="unique",
    )
