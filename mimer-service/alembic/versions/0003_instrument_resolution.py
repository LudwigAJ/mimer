"""instrument resolution: identifiers crosswalk, status/freshness, job targets

Revision ID: 0003
Revises: 0002
Create Date: 2026-06-20

Adds the identity-resolution support: a `security_identifiers` crosswalk table,
lifecycle/freshness columns on funds and fund_listings, and optional target FKs
on job_runs so backfill runs can point at a specific instrument. Apply with
`uv run alembic upgrade head`.

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # --- funds: status + freshness -------------------------------------------
    # server_default "active" backfills any pre-existing rows; new rows created
    # via the ORM use the model default "pending".
    op.add_column(
        "funds",
        sa.Column("status", sa.String(length=16), nullable=False, server_default="active"),
    )
    op.add_column(
        "funds", sa.Column("last_refreshed_at", sa.DateTime(timezone=True), nullable=True)
    )

    # --- fund_listings: status + freshness -----------------------------------
    op.add_column(
        "fund_listings",
        sa.Column("status", sa.String(length=16), nullable=False, server_default="active"),
    )
    op.add_column(
        "fund_listings", sa.Column("last_price_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "fund_listings", sa.Column("last_resolved_at", sa.DateTime(timezone=True), nullable=True)
    )

    # --- job_runs: optional backfill target ----------------------------------
    op.add_column("job_runs", sa.Column("fund_id", sa.Integer(), nullable=True))
    op.add_column("job_runs", sa.Column("fund_listing_id", sa.Integer(), nullable=True))
    op.create_foreign_key(
        "fk_job_runs_fund_id", "job_runs", "funds", ["fund_id"], ["id"], ondelete="SET NULL"
    )
    op.create_foreign_key(
        "fk_job_runs_fund_listing_id",
        "job_runs",
        "fund_listings",
        ["fund_listing_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_job_runs_fund_id", "job_runs", ["fund_id"])
    op.create_index("ix_job_runs_fund_listing_id", "job_runs", ["fund_listing_id"])

    # --- security_identifiers crosswalk --------------------------------------
    op.create_table(
        "security_identifiers",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("scheme", sa.String(length=16), nullable=False),
        sa.Column("value", sa.String(length=64), nullable=False),
        sa.Column(
            "fund_id", sa.Integer(), sa.ForeignKey("funds.id", ondelete="CASCADE"), nullable=True
        ),
        sa.Column(
            "fund_listing_id",
            sa.Integer(),
            sa.ForeignKey("fund_listings.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("exchange", sa.String(length=64), nullable=True),
        sa.Column("currency", sa.String(length=8), nullable=True),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("confidence", sa.String(length=16), nullable=False),
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
            "scheme", "value", "source", "exchange", "currency", name="uq_security_identifier"
        ),
    )
    op.create_index("ix_security_identifiers_fund_id", "security_identifiers", ["fund_id"])
    op.create_index(
        "ix_security_identifiers_fund_listing_id", "security_identifiers", ["fund_listing_id"]
    )
    op.create_index(
        "ix_security_identifiers_scheme_value", "security_identifiers", ["scheme", "value"]
    )


def downgrade() -> None:
    op.drop_index("ix_security_identifiers_scheme_value", table_name="security_identifiers")
    op.drop_index("ix_security_identifiers_fund_listing_id", table_name="security_identifiers")
    op.drop_index("ix_security_identifiers_fund_id", table_name="security_identifiers")
    op.drop_table("security_identifiers")

    op.drop_index("ix_job_runs_fund_listing_id", table_name="job_runs")
    op.drop_index("ix_job_runs_fund_id", table_name="job_runs")
    op.drop_constraint("fk_job_runs_fund_listing_id", "job_runs", type_="foreignkey")
    op.drop_constraint("fk_job_runs_fund_id", "job_runs", type_="foreignkey")
    op.drop_column("job_runs", "fund_listing_id")
    op.drop_column("job_runs", "fund_id")

    op.drop_column("fund_listings", "last_resolved_at")
    op.drop_column("fund_listings", "last_price_at")
    op.drop_column("fund_listings", "status")

    op.drop_column("funds", "last_refreshed_at")
    op.drop_column("funds", "status")
