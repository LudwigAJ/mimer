"""constituent EOD prices (instrument_prices) + listing last_price_at

Revision ID: 0013
Revises: 0012
Create Date: 2026-06-22

Adds the store for constituent end-of-day prices, the next slice after
constituent identity resolution. See AGENTS.md (no uncontrolled per-holding
source loops; all live fetches go through the source budget / fetch log) and
``app/services/instrument_prices.py``.

* ``instrument_prices`` — EOD bars (OHLC + adjusted close + volume) for a
  resolved ``instrument_listing``. Deliberately separate from ``prices`` (which
  is fund-listing oriented and stores a single close), because an ETF
  constituent is a generic security wanting richer data for stock detail pages,
  constituent charts and future true look-through valuation. Dedupe on
  ``(instrument_listing_id, price_date, source)`` so re-runs/backfills never
  duplicate a bar and distinct sources coexist.
* ``instrument_listings.last_price_at`` — bumped on ingestion so read-side
  freshness + the market-data planner can tell fresh / missing / stale apart
  (mirrors ``fund_listings.last_price_at``). Nullable: backward compatible.

Existing ``prices`` (fund listings) is untouched.

Apply with ``uv run alembic upgrade head``.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_NOW = sa.text("now()")
_MONEY = sa.Numeric(24, 8)


def upgrade() -> None:
    # --- instrument_listings: last_price_at ----------------------------------
    op.add_column(
        "instrument_listings",
        sa.Column("last_price_at", sa.DateTime(timezone=True), nullable=True),
    )

    # --- instrument_prices ---------------------------------------------------
    op.create_table(
        "instrument_prices",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "instrument_listing_id",
            sa.Integer(),
            sa.ForeignKey("instrument_listings.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("price_date", sa.Date(), nullable=False),
        sa.Column("open", _MONEY, nullable=True),
        sa.Column("high", _MONEY, nullable=True),
        sa.Column("low", _MONEY, nullable=True),
        sa.Column("close", _MONEY, nullable=False),
        sa.Column("adjusted_close", _MONEY, nullable=True),
        sa.Column("volume", _MONEY, nullable=True),
        sa.Column("currency", sa.String(length=8), nullable=True),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=True),
        sa.Column("raw_payload_json", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.UniqueConstraint(
            "instrument_listing_id",
            "price_date",
            "source",
            name="uq_instrument_price_listing_date_source",
        ),
    )
    op.create_index(
        "ix_instrument_prices_instrument_listing_id",
        "instrument_prices",
        ["instrument_listing_id"],
    )
    op.create_index("ix_instrument_prices_price_date", "instrument_prices", ["price_date"])
    op.create_index("ix_instrument_prices_source", "instrument_prices", ["source"])


def downgrade() -> None:
    op.drop_index("ix_instrument_prices_source", table_name="instrument_prices")
    op.drop_index("ix_instrument_prices_price_date", table_name="instrument_prices")
    op.drop_index("ix_instrument_prices_instrument_listing_id", table_name="instrument_prices")
    op.drop_table("instrument_prices")

    op.drop_column("instrument_listings", "last_price_at")
