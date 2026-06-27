"""holdings ingestion: identifiers, classification, snapshot identity key

Revision ID: 0006
Revises: 0005
Create Date: 2026-06-21

Extends ``fund_holdings`` so the `issuer_holdings_ingestion` worker can ingest
real/fixture look-through holdings idempotently:

* adds identifier columns (SEDOL/CUSIP/FIGI alongside the existing ISIN),
  classification (industry/currency) and economics (market_value/shares),
  a provider ``status`` and a ``raw_payload_json`` provenance blob;
* adds ``holding_key`` — a deterministic identity string the source/ingestion
  layer derives (ISIN > FIGI > CUSIP > SEDOL > normalised name+ticker) —
  backfilled for any existing rows, then made NOT NULL;
* adds the ``uq_fund_holding_identity`` unique constraint on
  (fund_id, as_of_date, source, holding_key) so re-runs/backfills never
  duplicate a holding. Different sources keep their own snapshot rows.

This does not touch ``security_identifiers``: its
``ix_security_identifiers_scheme_value`` index was already created by migration
0003 and is now also declared on the model (purely metadata reconciliation so
Alembic autogenerate stops reporting spurious churn — no DDL change there).

Apply with ``uv run alembic upgrade head``.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_MONEY = sa.Numeric(24, 8)


def upgrade() -> None:
    op.add_column("fund_holdings", sa.Column("security_sedol", sa.String(length=7), nullable=True))
    op.add_column("fund_holdings", sa.Column("security_cusip", sa.String(length=9), nullable=True))
    op.add_column("fund_holdings", sa.Column("security_figi", sa.String(length=12), nullable=True))
    op.add_column("fund_holdings", sa.Column("industry", sa.String(length=64), nullable=True))
    op.add_column("fund_holdings", sa.Column("currency", sa.String(length=8), nullable=True))
    op.add_column("fund_holdings", sa.Column("market_value", _MONEY, nullable=True))
    op.add_column("fund_holdings", sa.Column("shares", _MONEY, nullable=True))
    op.add_column("fund_holdings", sa.Column("status", sa.String(length=32), nullable=True))
    op.add_column("fund_holdings", sa.Column("raw_payload_json", sa.JSON(), nullable=True))

    # Add holding_key nullable, backfill to match holding_identity_key(), then
    # enforce NOT NULL. Existing (seed) rows have no ISIN -> normalised name key.
    op.add_column("fund_holdings", sa.Column("holding_key", sa.String(length=128), nullable=True))
    op.execute(
        """
        UPDATE fund_holdings
        SET holding_key = CASE
            WHEN security_isin IS NOT NULL AND btrim(security_isin) <> ''
                THEN 'isin:' || upper(btrim(security_isin))
            ELSE 'name:' || lower(btrim(security_name))
                 || '|' || lower(coalesce(btrim(security_ticker), ''))
        END
        WHERE holding_key IS NULL
        """
    )
    op.alter_column("fund_holdings", "holding_key", nullable=False)

    op.create_unique_constraint(
        "uq_fund_holding_identity",
        "fund_holdings",
        ["fund_id", "as_of_date", "source", "holding_key"],
    )


def downgrade() -> None:
    op.drop_constraint("uq_fund_holding_identity", "fund_holdings", type_="unique")
    op.drop_column("fund_holdings", "holding_key")
    op.drop_column("fund_holdings", "raw_payload_json")
    op.drop_column("fund_holdings", "status")
    op.drop_column("fund_holdings", "shares")
    op.drop_column("fund_holdings", "market_value")
    op.drop_column("fund_holdings", "currency")
    op.drop_column("fund_holdings", "industry")
    op.drop_column("fund_holdings", "security_figi")
    op.drop_column("fund_holdings", "security_cusip")
    op.drop_column("fund_holdings", "security_sedol")
