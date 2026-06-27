"""constituent instrument identity (instruments, listings, identifiers)

Revision ID: 0012
Revises: 0011
Create Date: 2026-06-21

Adds the canonical instrument master that ETF/fund *constituents* resolve to,
the prerequisite for constituent EOD price ingestion. See AGENTS.md (no
uncontrolled per-holding source loops) and
``app/services/constituent_identity.py``.

* ``instruments`` — canonical real-world securities/entities (Apple, Shell, ...);
  dedupe on a deterministic ``identity_key`` (ISIN > share-class FIGI > composite
  FIGI > FIGI > normalised name+country+currency).
* ``instrument_listings`` — tradable listings (ticker/mic/currency) so future
  price ingestion knows what to fetch; dedupe on (instrument_id, listing_key).
* ``instrument_identifiers`` — crosswalk + provenance; dedupe on
  (instrument_id, scheme, value, source).
* ``fund_holdings`` gains ``holding_instrument_id`` (nullable FK), plus
  ``identity_status`` / ``identity_resolved_at`` so unresolved / resolved /
  ambiguous / not_found / failed constituent state is visible without re-running
  the resolver. Backward compatible: all new columns are nullable.

Apply with ``uv run alembic upgrade head``.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_NOW = sa.text("now()")


def upgrade() -> None:
    # --- instruments ---------------------------------------------------------
    op.create_table(
        "instruments",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("identity_key", sa.String(length=128), nullable=False),
        sa.Column("instrument_type", sa.String(length=16), nullable=False, server_default="equity"),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("legal_name", sa.String(length=255), nullable=True),
        sa.Column("country", sa.String(length=64), nullable=True),
        sa.Column("currency", sa.String(length=8), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="active"),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("raw_payload_json", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.UniqueConstraint("identity_key", name="uq_instrument_identity_key"),
    )

    # --- instrument_listings -------------------------------------------------
    op.create_table(
        "instrument_listings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "instrument_id",
            sa.Integer(),
            sa.ForeignKey("instruments.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("listing_key", sa.String(length=128), nullable=False),
        sa.Column("ticker", sa.String(length=32), nullable=True),
        sa.Column("exchange", sa.String(length=64), nullable=True),
        sa.Column("mic", sa.String(length=16), nullable=True),
        sa.Column("currency", sa.String(length=8), nullable=True),
        sa.Column("country", sa.String(length=64), nullable=True),
        sa.Column("figi", sa.String(length=12), nullable=True),
        sa.Column("composite_figi", sa.String(length=12), nullable=True),
        sa.Column("share_class_figi", sa.String(length=12), nullable=True),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="active"),
        sa.Column("raw_payload_json", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.UniqueConstraint("instrument_id", "listing_key", name="uq_instrument_listing_key"),
    )
    op.create_index(
        "ix_instrument_listings_instrument_id", "instrument_listings", ["instrument_id"]
    )

    # --- instrument_identifiers ----------------------------------------------
    op.create_table(
        "instrument_identifiers",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "instrument_id",
            sa.Integer(),
            sa.ForeignKey("instruments.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("scheme", sa.String(length=24), nullable=False),
        sa.Column("value", sa.String(length=64), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="active"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.UniqueConstraint(
            "instrument_id", "scheme", "value", "source", name="uq_instrument_identifier"
        ),
    )
    op.create_index(
        "ix_instrument_identifiers_instrument_id", "instrument_identifiers", ["instrument_id"]
    )
    op.create_index(
        "ix_instrument_identifiers_scheme_value",
        "instrument_identifiers",
        ["scheme", "value"],
    )

    # --- fund_holdings: link + resolution state ------------------------------
    op.add_column(
        "fund_holdings",
        sa.Column(
            "holding_instrument_id",
            sa.Integer(),
            sa.ForeignKey("instruments.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.add_column(
        "fund_holdings", sa.Column("identity_status", sa.String(length=16), nullable=True)
    )
    op.add_column(
        "fund_holdings",
        sa.Column("identity_resolved_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_fund_holdings_holding_instrument_id", "fund_holdings", ["holding_instrument_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_fund_holdings_holding_instrument_id", table_name="fund_holdings")
    op.drop_column("fund_holdings", "identity_resolved_at")
    op.drop_column("fund_holdings", "identity_status")
    op.drop_column("fund_holdings", "holding_instrument_id")

    op.drop_index("ix_instrument_identifiers_scheme_value", table_name="instrument_identifiers")
    op.drop_index("ix_instrument_identifiers_instrument_id", table_name="instrument_identifiers")
    op.drop_table("instrument_identifiers")

    op.drop_index("ix_instrument_listings_instrument_id", table_name="instrument_listings")
    op.drop_table("instrument_listings")

    op.drop_table("instruments")
