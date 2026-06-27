"""broker CSV import + canonical transaction / position-reconciliation ledger

Revision ID: 0016
Revises: 0015
Create Date: 2026-06-23

The bridge from the market-data workstation to the *user portfolio* workstation:
ingest a broker CSV export, persist it as canonical, workspace-private
``portfolio_transactions``, and reconcile committed transactions into a bounded
``portfolio_position_snapshots`` read model.

Tables added:

* ``broker_accounts``  — a workspace's broker account (optional grouping).
* ``broker_imports``    — one committed import; unique ``(workspace_id,
  source_hash)`` so re-committing the same file is idempotent (duplicate
  detection), never duplicating rows/transactions. Preview is read-only and
  writes no import row.
* ``broker_import_rows`` — raw rows + per-row parse outcome + the canonical
  transaction each produced (provenance / failure isolation).
* ``portfolio_position_snapshots`` / ``portfolio_position_snapshot_rows`` — a
  derived reconciliation snapshot (buys − sells per instrument; cash per
  currency), idempotent on ``input_hash`` like ``exposure_snapshots``.

This migration also **replaces** the previously unused, future-facing
``portfolio_transactions`` table (one fund_listing per row, NOT NULL units/price)
with the richer canonical ledger (nullable instrument linkage; trade *and*
cash-movement types; idempotency key ``(workspace_id, transaction_key,
source)``). The old table was referenced by no service/API/test, so this is a
clean replacement.

This is **persistence + bounded reconciliation, not PnL** (no realised/unrealised
gain, tax lots or total return) — see AGENTS.md compute boundary. Apply with
``uv run alembic upgrade head``.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_NOW = sa.text("now()")
_MONEY = sa.Numeric(precision=24, scale=8)
_RATE = sa.Numeric(precision=24, scale=10)


def upgrade() -> None:
    # --- broker_accounts -----------------------------------------------------
    op.create_table(
        "broker_accounts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "workspace_id",
            sa.Integer(),
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("broker_name", sa.String(length=64), nullable=False),
        sa.Column("account_label", sa.String(length=128), nullable=True),
        sa.Column("account_currency", sa.String(length=8), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="active"),
        sa.Column("raw_payload_json", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.UniqueConstraint(
            "workspace_id", "broker_name", "account_label", name="uq_broker_account_label"
        ),
    )
    op.create_index("ix_broker_accounts_workspace_id", "broker_accounts", ["workspace_id"])

    # --- broker_imports ------------------------------------------------------
    op.create_table(
        "broker_imports",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "workspace_id",
            sa.Integer(),
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "broker_account_id",
            sa.Integer(),
            sa.ForeignKey("broker_accounts.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("broker_name", sa.String(length=64), nullable=False),
        sa.Column("source_filename", sa.String(length=255), nullable=True),
        sa.Column("source_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="committed"),
        sa.Column("row_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("parsed_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("transaction_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("unresolved_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cash_movement_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("committed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("raw_payload_json", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.UniqueConstraint("workspace_id", "source_hash", name="uq_broker_import_workspace_hash"),
    )
    op.create_index("ix_broker_imports_workspace_id", "broker_imports", ["workspace_id"])
    op.create_index("ix_broker_imports_broker_account_id", "broker_imports", ["broker_account_id"])

    # --- replace the unused future-facing portfolio_transactions -------------
    op.drop_index("ix_portfolio_transactions_fund_listing_id", table_name="portfolio_transactions")
    op.drop_index("ix_portfolio_transactions_workspace_id", table_name="portfolio_transactions")
    op.drop_table("portfolio_transactions")

    op.create_table(
        "portfolio_transactions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "workspace_id",
            sa.Integer(),
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "broker_account_id",
            sa.Integer(),
            sa.ForeignKey("broker_accounts.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "broker_import_id",
            sa.Integer(),
            sa.ForeignKey("broker_imports.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("transaction_key", sa.String(length=128), nullable=False),
        sa.Column("transaction_type", sa.String(length=24), nullable=False),
        sa.Column("trade_date", sa.Date(), nullable=False),
        sa.Column("settle_date", sa.Date(), nullable=True),
        sa.Column(
            "instrument_id",
            sa.Integer(),
            sa.ForeignKey("instruments.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "instrument_listing_id",
            sa.Integer(),
            sa.ForeignKey("instrument_listings.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "fund_id", sa.Integer(), sa.ForeignKey("funds.id", ondelete="SET NULL"), nullable=True
        ),
        sa.Column(
            "fund_listing_id",
            sa.Integer(),
            sa.ForeignKey("fund_listings.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("symbol", sa.String(length=64), nullable=True),
        sa.Column("isin", sa.String(length=12), nullable=True),
        sa.Column("figi", sa.String(length=12), nullable=True),
        sa.Column("name", sa.String(length=255), nullable=True),
        sa.Column("quantity", _MONEY, nullable=True),
        sa.Column("price", _MONEY, nullable=True),
        sa.Column("gross_amount", _MONEY, nullable=True),
        sa.Column("fees", _MONEY, nullable=True),
        sa.Column("taxes", _MONEY, nullable=True),
        sa.Column("net_amount", _MONEY, nullable=True),
        sa.Column("currency", sa.String(length=8), nullable=False),
        sa.Column("cash_currency", sa.String(length=8), nullable=True),
        sa.Column("fx_rate", _RATE, nullable=True),
        sa.Column("source", sa.String(length=32), nullable=False, server_default="broker_csv"),
        sa.Column("status", sa.String(length=24), nullable=False, server_default="committed"),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("raw_payload_json", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.UniqueConstraint(
            "workspace_id", "transaction_key", "source", name="uq_portfolio_transaction_key"
        ),
    )
    op.create_index(
        "ix_portfolio_transactions_workspace_id", "portfolio_transactions", ["workspace_id"]
    )
    op.create_index(
        "ix_portfolio_transactions_broker_import_id",
        "portfolio_transactions",
        ["broker_import_id"],
    )

    # --- broker_import_rows --------------------------------------------------
    op.create_table(
        "broker_import_rows",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "broker_import_id",
            sa.Integer(),
            sa.ForeignKey("broker_imports.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("row_number", sa.Integer(), nullable=False),
        sa.Column("raw_row_json", sa.JSON(), nullable=True),
        sa.Column("parse_status", sa.String(length=16), nullable=False),
        sa.Column("parse_error", sa.Text(), nullable=True),
        sa.Column(
            "canonical_transaction_id",
            sa.Integer(),
            sa.ForeignKey("portfolio_transactions.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
    )
    op.create_index("ix_broker_import_rows_import_id", "broker_import_rows", ["broker_import_id"])

    # --- portfolio_position_snapshots ----------------------------------------
    op.create_table(
        "portfolio_position_snapshots",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "workspace_id",
            sa.Integer(),
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("as_of_date", sa.Date(), nullable=False),
        sa.Column(
            "source", sa.String(length=32), nullable=False, server_default="broker_reconciliation"
        ),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="ok"),
        sa.Column("input_hash", sa.String(length=64), nullable=False),
        sa.Column("transaction_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("unresolved_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("position_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("raw_payload_json", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.UniqueConstraint(
            "workspace_id", "as_of_date", "input_hash", name="uq_position_snapshot_identity"
        ),
    )
    op.create_index(
        "ix_portfolio_position_snapshots_workspace_id",
        "portfolio_position_snapshots",
        ["workspace_id"],
    )

    # --- portfolio_position_snapshot_rows ------------------------------------
    op.create_table(
        "portfolio_position_snapshot_rows",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "snapshot_id",
            sa.Integer(),
            sa.ForeignKey("portfolio_position_snapshots.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(length=16), nullable=False, server_default="position"),
        sa.Column(
            "instrument_id",
            sa.Integer(),
            sa.ForeignKey("instruments.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "instrument_listing_id",
            sa.Integer(),
            sa.ForeignKey("instrument_listings.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "fund_id", sa.Integer(), sa.ForeignKey("funds.id", ondelete="SET NULL"), nullable=True
        ),
        sa.Column(
            "fund_listing_id",
            sa.Integer(),
            sa.ForeignKey("fund_listings.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("symbol", sa.String(length=64), nullable=True),
        sa.Column("isin", sa.String(length=12), nullable=True),
        sa.Column("name", sa.String(length=255), nullable=True),
        sa.Column("currency", sa.String(length=8), nullable=True),
        sa.Column("quantity", _MONEY, nullable=False, server_default="0"),
        sa.Column("fees_total", _MONEY, nullable=True),
        sa.Column("taxes_total", _MONEY, nullable=True),
        sa.Column("status", sa.String(length=24), nullable=True),
        sa.Column("raw_payload_json", sa.JSON(), nullable=True),
    )
    op.create_index(
        "ix_position_snapshot_rows_snapshot_id",
        "portfolio_position_snapshot_rows",
        ["snapshot_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_position_snapshot_rows_snapshot_id",
        table_name="portfolio_position_snapshot_rows",
    )
    op.drop_table("portfolio_position_snapshot_rows")
    op.drop_index(
        "ix_portfolio_position_snapshots_workspace_id",
        table_name="portfolio_position_snapshots",
    )
    op.drop_table("portfolio_position_snapshots")

    op.drop_index("ix_broker_import_rows_import_id", table_name="broker_import_rows")
    op.drop_table("broker_import_rows")

    op.drop_index("ix_portfolio_transactions_broker_import_id", table_name="portfolio_transactions")
    op.drop_index("ix_portfolio_transactions_workspace_id", table_name="portfolio_transactions")
    op.drop_table("portfolio_transactions")

    # Recreate the original future-facing portfolio_transactions table (0002).
    op.create_table(
        "portfolio_transactions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "workspace_id",
            sa.Integer(),
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "fund_listing_id",
            sa.Integer(),
            sa.ForeignKey("fund_listings.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("transaction_date", sa.Date(), nullable=False),
        sa.Column("transaction_type", sa.String(length=16), nullable=False),
        sa.Column("units", _MONEY, nullable=False),
        sa.Column("price", _MONEY, nullable=False),
        sa.Column("currency", sa.String(length=8), nullable=False),
        sa.Column("fees", _MONEY, nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
    )
    op.create_index(
        "ix_portfolio_transactions_workspace_id", "portfolio_transactions", ["workspace_id"]
    )
    op.create_index(
        "ix_portfolio_transactions_fund_listing_id",
        "portfolio_transactions",
        ["fund_listing_id"],
    )

    op.drop_index("ix_broker_imports_broker_account_id", table_name="broker_imports")
    op.drop_index("ix_broker_imports_workspace_id", table_name="broker_imports")
    op.drop_table("broker_imports")

    op.drop_index("ix_broker_accounts_workspace_id", table_name="broker_accounts")
    op.drop_table("broker_accounts")
