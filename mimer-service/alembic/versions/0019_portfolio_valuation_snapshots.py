"""portfolio valuation/readiness snapshots

Revision ID: 0019
Revises: 0018
Create Date: 2026-06-25

Adds the bounded, cacheable **portfolio valuation/readiness** read model:

* ``portfolio_valuation_snapshots`` — one derived valuation computation for a
  workspace at a point in time, idempotent on ``(workspace_id, as_of_date,
  input_hash)`` (mirrors ``exposure_snapshots`` / ``portfolio_position_snapshots``);
* ``portfolio_valuation_rows`` — one valued / blocked position (or cash balance)
  per snapshot, with explicit price + FX provenance/freshness and a
  ``valuation_status`` / ``readiness_status``.

This layer joins the existing reconciled positions (net quantity per instrument;
cash per currency) to the *latest already-ingested* fund/instrument price + FX at
or before ``as_of_date`` to answer "what can be valued now, and what is blocking
the rest". It is bounded SQL aggregation over existing rows — it fetches nothing
live and resolves no identity.

NON-GOALS (see AGENTS.md compute boundary): this migration adds **no** PnL,
realised/unrealised gain, tax-lot, total-return or performance-attribution tables.
Those analytics live in the Rust GUI / local pricer. All columns are additive +
new tables, so the change is backwards compatible and needs no data migration.
Apply with ``uv run alembic upgrade head``.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0019"
down_revision: str | None = "0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_MONEY = sa.Numeric(24, 8)
_RATE = sa.Numeric(24, 10)


def upgrade() -> None:
    op.create_table(
        "portfolio_valuation_snapshots",
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
            "broker_account_id",
            sa.Integer(),
            sa.ForeignKey("broker_accounts.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "source", sa.String(length=32), nullable=False, server_default="portfolio_valuation"
        ),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="ok"),
        sa.Column("input_hash", sa.String(length=64), nullable=False),
        sa.Column("positions_selected", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("positions_valued", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("missing_price_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("missing_fx_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("unresolved_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("ambiguous_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("stale_price_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("stale_fx_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cash_row_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("total_market_value_base", _MONEY, nullable=True),
        sa.Column("raw_payload_json", sa.JSON(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "workspace_id", "as_of_date", "input_hash", name="uq_valuation_snapshot_identity"
        ),
    )
    op.create_index(
        "ix_portfolio_valuation_snapshots_workspace_id",
        "portfolio_valuation_snapshots",
        ["workspace_id"],
    )

    op.create_table(
        "portfolio_valuation_rows",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "snapshot_id",
            sa.Integer(),
            sa.ForeignKey("portfolio_valuation_snapshots.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("position_key", sa.String(length=128), nullable=False),
        sa.Column("position_type", sa.String(length=24), nullable=False),
        sa.Column("fund_id", sa.Integer(), sa.ForeignKey("funds.id", ondelete="SET NULL")),
        sa.Column(
            "fund_listing_id",
            sa.Integer(),
            sa.ForeignKey("fund_listings.id", ondelete="SET NULL"),
        ),
        sa.Column(
            "instrument_id", sa.Integer(), sa.ForeignKey("instruments.id", ondelete="SET NULL")
        ),
        sa.Column(
            "instrument_listing_id",
            sa.Integer(),
            sa.ForeignKey("instrument_listings.id", ondelete="SET NULL"),
        ),
        sa.Column("symbol", sa.String(length=64)),
        sa.Column("isin", sa.String(length=12)),
        sa.Column("name", sa.String(length=255)),
        sa.Column("quantity", _MONEY, nullable=False, server_default="0"),
        sa.Column("local_currency", sa.String(length=8)),
        sa.Column("base_currency", sa.String(length=8)),
        sa.Column("latest_price", _MONEY, nullable=True),
        sa.Column("latest_price_date", sa.Date(), nullable=True),
        sa.Column("latest_price_source", sa.String(length=32)),
        sa.Column("latest_price_status", sa.String(length=16)),
        sa.Column("fx_rate_to_base", _RATE, nullable=True),
        sa.Column("fx_rate_date", sa.Date(), nullable=True),
        sa.Column("fx_rate_source", sa.String(length=32)),
        sa.Column("fx_status", sa.String(length=16)),
        sa.Column("market_value_local", _MONEY, nullable=True),
        sa.Column("market_value_base", _MONEY, nullable=True),
        sa.Column("valuation_status", sa.String(length=24), nullable=False),
        sa.Column("readiness_status", sa.String(length=16), nullable=False),
        sa.Column("source", sa.String(length=32)),
        sa.Column("status", sa.String(length=16)),
        sa.Column("raw_payload_json", sa.JSON(), nullable=True),
    )
    op.create_index(
        "ix_portfolio_valuation_rows_snapshot_id",
        "portfolio_valuation_rows",
        ["snapshot_id"],
    )
    op.create_index(
        "ix_portfolio_valuation_rows_snapshot_status",
        "portfolio_valuation_rows",
        ["snapshot_id", "valuation_status"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_portfolio_valuation_rows_snapshot_status", table_name="portfolio_valuation_rows"
    )
    op.drop_index("ix_portfolio_valuation_rows_snapshot_id", table_name="portfolio_valuation_rows")
    op.drop_table("portfolio_valuation_rows")
    op.drop_index(
        "ix_portfolio_valuation_snapshots_workspace_id",
        table_name="portfolio_valuation_snapshots",
    )
    op.drop_table("portfolio_valuation_snapshots")
