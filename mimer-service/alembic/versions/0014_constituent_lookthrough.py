"""true constituent look-through valuation (exposure_rows + snapshot coverage)

Revision ID: 0014
Revises: 0013
Create Date: 2026-06-22

Feeds ``instrument_prices`` (constituent EOD prices) into the cached look-through
exposure so Mimer can value an ETF/fund's *underlying* constituents, not only the
fund wrapper. See ``app/services/constituent_valuation.py`` and AGENTS.md (the
value is a weight-based *implied* estimate, never an assertion of exact share
ownership; missing price/FX is surfaced, never treated as zero).

This is additive and backward-compatible — existing fund-level exposure rows and
snapshots are untouched:

* ``exposure_rows`` — widen ``dimension``/``status`` (the new ``constituent`` /
  ``constituent_price_status`` dimensions and richer statuses are longer than the
  old 16), and add typed constituent context columns (resolved instrument /
  listing / fund, the constituent EOD price + FX used, and the valuation method).
  All new columns are nullable: legacy rows keep reading fine.
* ``exposure_snapshots`` — add weight-based constituent coverage metrics
  (identity / price / fx coverage, nested under the existing holdings coverage)
  and distinct-resolved-instrument counts. Coverage weights are nullable; counts
  default to 0 so pre-0014 snapshots stay valid.

Apply with ``uv run alembic upgrade head``.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_WEIGHT = sa.Numeric(12, 8)
_RATE = sa.Numeric(24, 10)
_ZERO = sa.text("0")


def upgrade() -> None:
    # --- exposure_rows: widen + constituent context --------------------------
    op.alter_column(
        "exposure_rows",
        "dimension",
        existing_type=sa.String(length=16),
        type_=sa.String(length=32),
        existing_nullable=False,
    )
    op.alter_column(
        "exposure_rows",
        "status",
        existing_type=sa.String(length=16),
        type_=sa.String(length=32),
        existing_nullable=True,
    )
    op.add_column(
        "exposure_rows",
        sa.Column(
            "instrument_id",
            sa.Integer(),
            sa.ForeignKey("instruments.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.add_column(
        "exposure_rows",
        sa.Column(
            "instrument_listing_id",
            sa.Integer(),
            sa.ForeignKey("instrument_listings.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.add_column(
        "exposure_rows",
        sa.Column(
            "fund_id",
            sa.Integer(),
            sa.ForeignKey("funds.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.add_column("exposure_rows", sa.Column("price_date", sa.Date(), nullable=True))
    op.add_column("exposure_rows", sa.Column("price_source", sa.String(length=32), nullable=True))
    op.add_column("exposure_rows", sa.Column("price_status", sa.String(length=16), nullable=True))
    op.add_column("exposure_rows", sa.Column("fx_rate", _RATE, nullable=True))
    op.add_column("exposure_rows", sa.Column("fx_source", sa.String(length=32), nullable=True))
    op.add_column(
        "exposure_rows", sa.Column("valuation_method", sa.String(length=48), nullable=True)
    )

    # --- exposure_snapshots: constituent coverage metrics --------------------
    op.add_column(
        "exposure_snapshots", sa.Column("identity_coverage_weight", _WEIGHT, nullable=True)
    )
    op.add_column("exposure_snapshots", sa.Column("price_coverage_weight", _WEIGHT, nullable=True))
    op.add_column("exposure_snapshots", sa.Column("fx_coverage_weight", _WEIGHT, nullable=True))
    for column in (
        "constituent_count",
        "resolved_constituent_count",
        "priced_constituent_count",
        "stale_constituent_price_count",
        "missing_constituent_price_count",
        "constituent_fx_missing_count",
    ):
        op.add_column(
            "exposure_snapshots",
            sa.Column(column, sa.Integer(), nullable=False, server_default=_ZERO),
        )


def downgrade() -> None:
    for column in (
        "constituent_fx_missing_count",
        "missing_constituent_price_count",
        "stale_constituent_price_count",
        "priced_constituent_count",
        "resolved_constituent_count",
        "constituent_count",
        "fx_coverage_weight",
        "price_coverage_weight",
        "identity_coverage_weight",
    ):
        op.drop_column("exposure_snapshots", column)

    for column in (
        "valuation_method",
        "fx_source",
        "fx_rate",
        "price_status",
        "price_source",
        "price_date",
        "fund_id",
        "instrument_listing_id",
        "instrument_id",
    ):
        op.drop_column("exposure_rows", column)

    op.alter_column(
        "exposure_rows",
        "status",
        existing_type=sa.String(length=32),
        type_=sa.String(length=16),
        existing_nullable=True,
    )
    op.alter_column(
        "exposure_rows",
        "dimension",
        existing_type=sa.String(length=32),
        type_=sa.String(length=16),
        existing_nullable=False,
    )
