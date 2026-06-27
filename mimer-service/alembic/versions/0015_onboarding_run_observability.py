"""onboarding run observability (job_runs.workspace_id + payload_json)

Revision ID: 0015
Revises: 0014
Create Date: 2026-06-23

Backs the *onboarding run history / observability* read model. Until now the
instrument-onboarding orchestrator encoded parent/child stage correlation in the
parent ``job_runs.message`` free-text (e.g. ``constituent_prices=success(runs=26)``).
The GUI now needs to ask "which stages ran / were skipped / failed, which child
job_runs belong to each stage, how long did each take" *without* parsing a
string. This adds the minimal, bounded, read-model-friendly storage for that:

* ``job_runs.workspace_id`` — nullable FK to ``workspaces`` (ondelete SET NULL),
  indexed, mirroring the existing ``fund_id`` column. Lets a workspace-scoped
  onboarding parent run be filtered with a bounded, indexed query (rather than
  reading JSON). Fund-scoped runs keep using ``fund_id``; both are nullable.
* ``job_runs.payload_json`` — nullable JSON holding the structured onboarding
  orchestration metadata (typed stage rows: status / reason / timings / child
  run ids / counts, plus scope + source mode + next action). NULL for every
  pre-0015 run and for non-onboarding job types, which the read model surfaces
  as ``legacy_metadata=true`` and falls back to the human-readable ``message``.
* ``ix_job_runs_job_type_id`` — composite index ``(job_type, id)`` so the
  bounded "latest onboarding runs" listing (``WHERE job_type=:t ORDER BY id
  DESC LIMIT :n``) stays index-friendly as history grows.

Purely additive and backward-compatible (all new columns nullable; the index is
new). This is an *observability* slice — no workflow engine, no analytics, no
new compute. Apply with ``uv run alembic upgrade head``.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "job_runs",
        sa.Column(
            "workspace_id",
            sa.Integer(),
            sa.ForeignKey("workspaces.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.create_index("ix_job_runs_workspace_id", "job_runs", ["workspace_id"])
    op.add_column("job_runs", sa.Column("payload_json", sa.JSON(), nullable=True))
    op.create_index("ix_job_runs_job_type_id", "job_runs", ["job_type", "id"])


def downgrade() -> None:
    op.drop_index("ix_job_runs_job_type_id", table_name="job_runs")
    op.drop_column("job_runs", "payload_json")
    op.drop_index("ix_job_runs_workspace_id", table_name="job_runs")
    op.drop_column("job_runs", "workspace_id")
