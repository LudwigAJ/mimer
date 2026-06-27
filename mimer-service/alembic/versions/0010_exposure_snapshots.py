"""derived/cached look-through exposure (exposure_recompute)

Revision ID: 0010
Revises: 0009
Create Date: 2026-06-21

Adds the derived exposure store the `exposure_recompute` worker writes, turning
look-through exposure from an ad-hoc read computation into an inspectable,
cacheable, timestamped, provenance/freshness-aware dataset:

* ``exposure_snapshots`` — one workspace-scoped computation at a point in time,
  with an ``input_hash`` (deterministic digest of positions/prices/FX/holdings/
  base currency/as-of/source policy) for idempotency, component digests,
  coverage/unclassified weights and missing-holdings/FX counts. Unique on
  ``(workspace_id, as_of_date, input_hash)`` so identical inputs never duplicate.
* ``exposure_rows`` — the per-bucket breakdown, generic by ``dimension`` /
  ``bucket`` / ``label`` (country/sector/industry/currency/holding/fund/source
  today; asset_class etc. later) so direct equities/bonds/cash slot in without a
  schema change.

Apply with ``uv run alembic upgrade head``.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_NOW = sa.text("now()")
_MONEY = sa.Numeric(24, 8)
_WEIGHT = sa.Numeric(12, 8)


def upgrade() -> None:
    op.create_table(
        "exposure_snapshots",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "workspace_id",
            sa.Integer(),
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("as_of_date", sa.Date(), nullable=False),
        sa.Column("base_currency", sa.String(length=3), nullable=False),
        sa.Column(
            "source", sa.String(length=32), nullable=False, server_default="exposure_recompute"
        ),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="ok"),
        sa.Column("input_hash", sa.String(length=64), nullable=False),
        sa.Column("holdings_snapshot_hash", sa.String(length=64), nullable=True),
        sa.Column("fx_snapshot_hash", sa.String(length=64), nullable=True),
        sa.Column("position_snapshot_hash", sa.String(length=64), nullable=True),
        sa.Column("total_market_value_base", _MONEY, nullable=True),
        sa.Column("coverage_weight", _WEIGHT, nullable=True),
        sa.Column("unclassified_weight", _WEIGHT, nullable=True),
        sa.Column("missing_holdings_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("missing_fx_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("raw_payload_json", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.UniqueConstraint(
            "workspace_id", "as_of_date", "input_hash", name="uq_exposure_snapshot_identity"
        ),
    )
    op.create_index("ix_exposure_snapshots_workspace_id", "exposure_snapshots", ["workspace_id"])

    op.create_table(
        "exposure_rows",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "exposure_snapshot_id",
            sa.Integer(),
            sa.ForeignKey("exposure_snapshots.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("dimension", sa.String(length=16), nullable=False),
        sa.Column("bucket", sa.String(length=128), nullable=False),
        sa.Column("label", sa.String(length=255), nullable=False),
        sa.Column("weight", _WEIGHT, nullable=False),
        sa.Column("market_value_base", _MONEY, nullable=True),
        sa.Column("currency", sa.String(length=8), nullable=True),
        sa.Column("source", sa.String(length=32), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=True),
        sa.Column("raw_payload_json", sa.JSON(), nullable=True),
    )
    op.create_index(
        "ix_exposure_rows_snapshot_dimension",
        "exposure_rows",
        ["exposure_snapshot_id", "dimension"],
    )


def downgrade() -> None:
    op.drop_index("ix_exposure_rows_snapshot_dimension", table_name="exposure_rows")
    op.drop_table("exposure_rows")
    op.drop_index("ix_exposure_snapshots_workspace_id", table_name="exposure_snapshots")
    op.drop_table("exposure_snapshots")
