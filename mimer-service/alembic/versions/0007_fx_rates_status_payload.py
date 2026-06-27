"""fx ingestion: rate status + raw payload provenance

Revision ID: 0007
Revises: 0006
Create Date: 2026-06-21

Extends ``fx_rates`` so the `fx_ingestion` worker can record provider-asserted
provenance alongside each rate, mirroring the other ingestion tables:

* adds ``status`` — a provider/derivation marker (e.g. ``fixture`` | ``official``
  | ``estimated`` | ``manual``). Read-side *freshness* (fresh/stale/missing) is
  still derived from ``rate_date`` at request time (`app/services/freshness.py`);
  ``status`` is the stored provenance signal, not the freshness.
* adds ``raw_payload_json`` — reserved for the raw provider payload (debugging /
  future re-parsing), consistent with ``fund_holdings.raw_payload_json``.

The idempotency key is unchanged: ``fx_rates`` is already unique on
(rate_date, base_currency, quote_currency, source) via ``uq_fx_rate`` (migration
0001), so re-running ``fx_ingestion`` never duplicates a rate and distinct
sources coexist. No new constraint is needed here.

Apply with ``uv run alembic upgrade head``.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("fx_rates", sa.Column("status", sa.String(length=16), nullable=True))
    op.add_column("fx_rates", sa.Column("raw_payload_json", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("fx_rates", "raw_payload_json")
    op.drop_column("fx_rates", "status")
