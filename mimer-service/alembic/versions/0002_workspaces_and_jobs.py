"""workspaces, users, watchlists, transactions, workspace alerts, scheduled jobs

Revision ID: 0002
Revises: 0001
Create Date: 2026-06-20

Adds the multi-tenant foundation (users / workspaces) and scoping for private
data, plus scheduled-job design tables. Reference data tables from 0001 are
unchanged. Apply with: `uv run alembic upgrade head`.

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_NOW = sa.text("now()")


def upgrade() -> None:
    # --- users / workspaces --------------------------------------------------
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("email", sa.String(length=320), nullable=True, unique=True),
        sa.Column("display_name", sa.String(length=255), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
    )

    op.create_table(
        "workspaces",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("base_currency", sa.String(length=3), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
    )

    op.create_table(
        "workspace_members",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "workspace_id",
            sa.Integer(),
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("role", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.UniqueConstraint("workspace_id", "user_id", name="uq_workspace_member"),
    )
    op.create_index("ix_workspace_members_workspace_id", "workspace_members", ["workspace_id"])
    op.create_index("ix_workspace_members_user_id", "workspace_members", ["user_id"])

    op.create_table(
        "workspace_settings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "workspace_id",
            sa.Integer(),
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("key", sa.String(length=128), nullable=False),
        sa.Column("value_json", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.UniqueConstraint("workspace_id", "key", name="uq_workspace_setting_key"),
    )
    op.create_index("ix_workspace_settings_workspace_id", "workspace_settings", ["workspace_id"])

    # --- scope portfolio_positions to a workspace ----------------------------
    op.add_column("portfolio_positions", sa.Column("workspace_id", sa.Integer(), nullable=False))
    op.create_foreign_key(
        "fk_portfolio_positions_workspace_id",
        "portfolio_positions",
        "workspaces",
        ["workspace_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index("ix_portfolio_positions_workspace_id", "portfolio_positions", ["workspace_id"])

    # --- portfolio transactions (future-facing) ------------------------------
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
        sa.Column("units", sa.Numeric(precision=24, scale=8), nullable=False),
        sa.Column("price", sa.Numeric(precision=24, scale=8), nullable=False),
        sa.Column("currency", sa.String(length=8), nullable=False),
        sa.Column("fees", sa.Numeric(precision=24, scale=8), nullable=True),
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

    # --- watchlists ----------------------------------------------------------
    op.create_table(
        "watchlists",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "workspace_id",
            sa.Integer(),
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
    )
    op.create_index("ix_watchlists_workspace_id", "watchlists", ["workspace_id"])

    op.create_table(
        "watchlist_items",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "watchlist_id",
            sa.Integer(),
            sa.ForeignKey("watchlists.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "fund_id", sa.Integer(), sa.ForeignKey("funds.id", ondelete="CASCADE"), nullable=True
        ),
        sa.Column(
            "fund_listing_id",
            sa.Integer(),
            sa.ForeignKey("fund_listings.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
    )
    op.create_index("ix_watchlist_items_watchlist_id", "watchlist_items", ["watchlist_id"])

    # --- alerts: move read state into workspace_alerts -----------------------
    op.drop_column("alerts", "is_read")
    op.create_table(
        "workspace_alerts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "workspace_id",
            sa.Integer(),
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "alert_id",
            sa.Integer(),
            sa.ForeignKey("alerts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("is_read", sa.Boolean(), nullable=False),
        sa.Column("is_dismissed", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.UniqueConstraint("workspace_id", "alert_id", name="uq_workspace_alert"),
    )
    op.create_index("ix_workspace_alerts_workspace_id", "workspace_alerts", ["workspace_id"])
    op.create_index("ix_workspace_alerts_alert_id", "workspace_alerts", ["alert_id"])

    # --- scheduled jobs / runs -----------------------------------------------
    op.create_table(
        "scheduled_jobs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(length=128), nullable=False, unique=True),
        sa.Column("job_type", sa.String(length=64), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=True),
        sa.Column("schedule_cron", sa.String(length=64), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
    )

    op.create_table(
        "job_runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "scheduled_job_id",
            sa.Integer(),
            sa.ForeignKey("scheduled_jobs.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("job_type", sa.String(length=64), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("message", sa.Text(), nullable=True),
        sa.Column("records_inserted", sa.Integer(), nullable=True),
        sa.Column("records_updated", sa.Integer(), nullable=True),
        sa.Column("records_failed", sa.Integer(), nullable=True),
    )
    op.create_index("ix_job_runs_scheduled_job_id", "job_runs", ["scheduled_job_id"])


def downgrade() -> None:
    op.drop_index("ix_job_runs_scheduled_job_id", table_name="job_runs")
    op.drop_table("job_runs")
    op.drop_table("scheduled_jobs")

    op.drop_index("ix_workspace_alerts_alert_id", table_name="workspace_alerts")
    op.drop_index("ix_workspace_alerts_workspace_id", table_name="workspace_alerts")
    op.drop_table("workspace_alerts")
    op.add_column(
        "alerts",
        sa.Column("is_read", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )

    op.drop_index("ix_watchlist_items_watchlist_id", table_name="watchlist_items")
    op.drop_table("watchlist_items")
    op.drop_index("ix_watchlists_workspace_id", table_name="watchlists")
    op.drop_table("watchlists")

    op.drop_index("ix_portfolio_transactions_fund_listing_id", table_name="portfolio_transactions")
    op.drop_index("ix_portfolio_transactions_workspace_id", table_name="portfolio_transactions")
    op.drop_table("portfolio_transactions")

    op.drop_index("ix_portfolio_positions_workspace_id", table_name="portfolio_positions")
    op.drop_constraint(
        "fk_portfolio_positions_workspace_id", "portfolio_positions", type_="foreignkey"
    )
    op.drop_column("portfolio_positions", "workspace_id")

    op.drop_index("ix_workspace_settings_workspace_id", table_name="workspace_settings")
    op.drop_table("workspace_settings")
    op.drop_index("ix_workspace_members_user_id", table_name="workspace_members")
    op.drop_index("ix_workspace_members_workspace_id", table_name="workspace_members")
    op.drop_table("workspace_members")
    op.drop_table("workspaces")
    op.drop_table("users")
