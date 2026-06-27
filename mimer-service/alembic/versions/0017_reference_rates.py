"""official rates / reference-rate ingestion foundation

Revision ID: 0017
Revises: 0016
Create Date: 2026-06-24

Adds ``reference_rates`` — a store of official / reference rate *observations*
(central-bank policy rates, overnight benchmarks, government par yields). This is
collection + normalisation + persistence only.

NON-GOALS (see AGENTS.md compute boundary): this migration adds **no** curve,
discount-factor or pricing tables. The backend never fits curves, bootstraps,
interpolates, computes forward rates or prices bonds — that belongs in the Rust
GUI / local pricer. Only published official observations are stored here.

Idempotency key is ``(rate_date, currency, country_or_region, rate_family,
rate_name, tenor, source)`` so re-runs / backfills never duplicate an observation
and distinct sources keep their own rows. Apply with ``uv run alembic upgrade
head``.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_NOW = sa.text("now()")
_RATE = sa.Numeric(precision=24, scale=10)


def upgrade() -> None:
    op.create_table(
        "reference_rates",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("rate_date", sa.Date(), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("country_or_region", sa.String(length=32), nullable=False),
        sa.Column("rate_family", sa.String(length=32), nullable=False),
        sa.Column("rate_name", sa.String(length=64), nullable=False),
        sa.Column("tenor", sa.String(length=16), nullable=True),
        sa.Column("tenor_months", sa.Integer(), nullable=True),
        sa.Column("rate_value", _RATE, nullable=False),
        sa.Column("unit", sa.String(length=16), nullable=False, server_default="percent"),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=True),
        sa.Column("source_url", sa.String(length=1024), nullable=True),
        sa.Column("raw_payload_json", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.UniqueConstraint(
            "rate_date",
            "currency",
            "country_or_region",
            "rate_family",
            "rate_name",
            "tenor",
            "source",
            name="uq_reference_rate",
        ),
    )
    op.create_index("ix_reference_rates_rate_name", "reference_rates", ["rate_name"])
    op.create_index("ix_reference_rates_currency", "reference_rates", ["currency"])
    op.create_index("ix_reference_rates_rate_date", "reference_rates", ["rate_date"])


def downgrade() -> None:
    op.drop_index("ix_reference_rates_rate_date", table_name="reference_rates")
    op.drop_index("ix_reference_rates_currency", table_name="reference_rates")
    op.drop_index("ix_reference_rates_rate_name", table_name="reference_rates")
    op.drop_table("reference_rates")
