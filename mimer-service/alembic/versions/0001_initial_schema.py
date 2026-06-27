"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-06-20

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_NOW = sa.text("now()")


def upgrade() -> None:
    op.create_table(
        "funds",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("isin", sa.String(length=12), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("provider", sa.String(length=128), nullable=True),
        sa.Column("domicile", sa.String(length=2), nullable=True),
        sa.Column("base_currency", sa.String(length=3), nullable=True),
        sa.Column("distribution_policy", sa.String(length=32), nullable=True),
        sa.Column("strategy", sa.String(length=255), nullable=True),
        sa.Column("ocf", sa.Numeric(precision=8, scale=5), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
    )
    op.create_index("ix_funds_isin", "funds", ["isin"], unique=True)

    op.create_table(
        "fund_listings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "fund_id",
            sa.Integer(),
            sa.ForeignKey("funds.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("ticker", sa.String(length=32), nullable=False),
        sa.Column("exchange", sa.String(length=64), nullable=True),
        sa.Column("trading_currency", sa.String(length=3), nullable=True),
        sa.Column("currency_unit", sa.String(length=8), nullable=True),
        sa.Column("figi", sa.String(length=12), nullable=True),
        sa.Column("sedol", sa.String(length=7), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.UniqueConstraint(
            "fund_id", "ticker", "exchange", name="uq_listing_fund_ticker_exchange"
        ),
    )
    op.create_index("ix_fund_listings_fund_id", "fund_listings", ["fund_id"])

    op.create_table(
        "portfolio_positions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "fund_listing_id",
            sa.Integer(),
            sa.ForeignKey("fund_listings.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("account_name", sa.String(length=128), nullable=True),
        sa.Column("units", sa.Numeric(precision=24, scale=8), nullable=False),
        sa.Column("average_cost", sa.Numeric(precision=24, scale=8), nullable=True),
        sa.Column("cost_currency", sa.String(length=8), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
    )
    op.create_index(
        "ix_portfolio_positions_fund_listing_id", "portfolio_positions", ["fund_listing_id"]
    )

    op.create_table(
        "prices",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "fund_listing_id",
            sa.Integer(),
            sa.ForeignKey("fund_listings.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("price_date", sa.Date(), nullable=False),
        sa.Column("price", sa.Numeric(precision=24, scale=8), nullable=False),
        sa.Column("currency", sa.String(length=8), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.UniqueConstraint(
            "fund_listing_id", "price_date", "source", name="uq_price_listing_date_source"
        ),
    )
    op.create_index("ix_prices_fund_listing_id", "prices", ["fund_listing_id"])

    op.create_table(
        "distributions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "fund_id",
            sa.Integer(),
            sa.ForeignKey("funds.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("ex_date", sa.Date(), nullable=False),
        sa.Column("record_date", sa.Date(), nullable=True),
        sa.Column("payment_date", sa.Date(), nullable=True),
        sa.Column("amount", sa.Numeric(precision=24, scale=8), nullable=False),
        sa.Column("currency", sa.String(length=8), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
    )
    op.create_index("ix_distributions_fund_id", "distributions", ["fund_id"])

    op.create_table(
        "fund_holdings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "fund_id",
            sa.Integer(),
            sa.ForeignKey("funds.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("as_of_date", sa.Date(), nullable=False),
        sa.Column("security_name", sa.String(length=255), nullable=False),
        sa.Column("security_ticker", sa.String(length=32), nullable=True),
        sa.Column("security_isin", sa.String(length=12), nullable=True),
        sa.Column("country", sa.String(length=64), nullable=True),
        sa.Column("sector", sa.String(length=64), nullable=True),
        sa.Column("weight", sa.Numeric(precision=12, scale=8), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
    )
    op.create_index("ix_fund_holdings_fund_id", "fund_holdings", ["fund_id"])

    op.create_table(
        "document_snapshots",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "fund_id",
            sa.Integer(),
            sa.ForeignKey("funds.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("document_type", sa.String(length=32), nullable=False),
        sa.Column("url", sa.String(length=1024), nullable=True),
        sa.Column("document_date", sa.Date(), nullable=True),
        sa.Column("content_hash", sa.String(length=128), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=True),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
    )
    op.create_index("ix_document_snapshots_fund_id", "document_snapshots", ["fund_id"])

    op.create_table(
        "alerts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("severity", sa.String(length=16), nullable=False),
        sa.Column("category", sa.String(length=64), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("message", sa.Text(), nullable=True),
        sa.Column(
            "fund_id",
            sa.Integer(),
            sa.ForeignKey("funds.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("is_read", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
    )
    op.create_index("ix_alerts_fund_id", "alerts", ["fund_id"])

    op.create_table(
        "fx_rates",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("rate_date", sa.Date(), nullable=False),
        sa.Column("base_currency", sa.String(length=3), nullable=False),
        sa.Column("quote_currency", sa.String(length=3), nullable=False),
        sa.Column("rate", sa.Numeric(precision=24, scale=10), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.UniqueConstraint(
            "rate_date", "base_currency", "quote_currency", "source", name="uq_fx_rate"
        ),
    )

    op.create_table(
        "ingestion_runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("job_type", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("message", sa.Text(), nullable=True),
        sa.Column("rows_inserted", sa.Integer(), nullable=True),
        sa.Column("rows_updated", sa.Integer(), nullable=True),
    )

    op.create_table(
        "data_sources",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("source_type", sa.String(length=32), nullable=False),
        sa.Column("base_url", sa.String(length=1024), nullable=True),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.UniqueConstraint("name", name="uq_data_sources_name"),
    )


def downgrade() -> None:
    op.drop_table("data_sources")
    op.drop_table("ingestion_runs")
    op.drop_table("fx_rates")
    op.drop_index("ix_alerts_fund_id", table_name="alerts")
    op.drop_table("alerts")
    op.drop_index("ix_document_snapshots_fund_id", table_name="document_snapshots")
    op.drop_table("document_snapshots")
    op.drop_index("ix_fund_holdings_fund_id", table_name="fund_holdings")
    op.drop_table("fund_holdings")
    op.drop_index("ix_distributions_fund_id", table_name="distributions")
    op.drop_table("distributions")
    op.drop_index("ix_prices_fund_listing_id", table_name="prices")
    op.drop_table("prices")
    op.drop_index("ix_portfolio_positions_fund_listing_id", table_name="portfolio_positions")
    op.drop_table("portfolio_positions")
    op.drop_index("ix_fund_listings_fund_id", table_name="fund_listings")
    op.drop_table("fund_listings")
    op.drop_index("ix_funds_isin", table_name="funds")
    op.drop_table("funds")
