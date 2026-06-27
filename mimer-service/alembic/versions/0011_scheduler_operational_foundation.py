"""scheduler operational foundation (leasing, source budgets, fetch logs)

Revision ID: 0011
Revises: 0010
Create Date: 2026-06-21

Adds the operational layer that makes recurring jobs + external data fetching
safe, observable, idempotent and rate-limited BEFORE broad stock/constituent
ingestion. See AGENTS.md (no uncontrolled per-holding source loops).

* ``scheduled_jobs`` gains schedule semantics (``schedule_kind`` /
  ``interval_seconds`` / ``timezone``) and a lease (``locked_by`` /
  ``lock_expires_at`` / ``last_heartbeat_at`` + policy columns) so a single
  scheduler claims a due job atomically and a crashed lease can be reclaimed.
* ``source_rate_limits`` — per-source request budget / backoff state answering
  "may source X fetch now, how long to wait, what batch size".
* ``source_fetch_logs`` — one row per external fetch attempt (request key/hash,
  status, timings, counts, backoff) for observability + a recent-success cache.
  Stores no secrets: no API keys, auth headers or tokenised URLs.

Apply with ``uv run alembic upgrade head``.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_NOW = sa.text("now()")


def upgrade() -> None:
    # --- scheduled_jobs: schedule semantics + lease columns ------------------
    op.add_column(
        "scheduled_jobs",
        sa.Column("schedule_kind", sa.String(length=16), nullable=False, server_default="manual"),
    )
    op.add_column("scheduled_jobs", sa.Column("interval_seconds", sa.Integer(), nullable=True))
    op.add_column(
        "scheduled_jobs",
        sa.Column("timezone", sa.String(length=64), nullable=False, server_default="UTC"),
    )
    op.add_column("scheduled_jobs", sa.Column("last_status", sa.String(length=32), nullable=True))
    op.add_column(
        "scheduled_jobs", sa.Column("locked_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column("scheduled_jobs", sa.Column("locked_by", sa.String(length=128), nullable=True))
    op.add_column(
        "scheduled_jobs", sa.Column("lock_expires_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "scheduled_jobs", sa.Column("last_heartbeat_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column("scheduled_jobs", sa.Column("max_runtime_seconds", sa.Integer(), nullable=True))
    op.add_column(
        "scheduled_jobs",
        sa.Column(
            "misfire_policy",
            sa.String(length=32),
            nullable=False,
            server_default="run_once_then_schedule",
        ),
    )
    op.add_column(
        "scheduled_jobs",
        sa.Column("retry_policy", sa.String(length=32), nullable=False, server_default="none"),
    )

    # --- source_rate_limits --------------------------------------------------
    op.create_table(
        "source_rate_limits",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("source_name", sa.String(length=64), nullable=False),
        sa.Column("is_enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("max_requests_per_minute", sa.Integer(), nullable=True),
        sa.Column("max_requests_per_hour", sa.Integer(), nullable=True),
        sa.Column("max_requests_per_day", sa.Integer(), nullable=True),
        sa.Column("max_concurrency", sa.Integer(), nullable=True),
        sa.Column("min_delay_ms", sa.Integer(), nullable=True),
        sa.Column("batch_size", sa.Integer(), nullable=True),
        sa.Column("backoff_seconds", sa.Integer(), nullable=True),
        sa.Column("backoff_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_request_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.UniqueConstraint("source_name", name="uq_source_rate_limit_name"),
    )

    # --- source_fetch_logs ---------------------------------------------------
    op.create_table(
        "source_fetch_logs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("source_name", sa.String(length=64), nullable=False),
        sa.Column("request_kind", sa.String(length=64), nullable=False),
        sa.Column("request_key", sa.String(length=512), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("endpoint_label", sa.String(length=255), nullable=True),
        sa.Column("method", sa.String(length=8), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="started"),
        sa.Column("http_status", sa.Integer(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("records_inserted", sa.Integer(), nullable=True),
        sa.Column("records_updated", sa.Integer(), nullable=True),
        sa.Column("records_failed", sa.Integer(), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("rate_limited", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("backoff_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cache_hit", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("raw_payload_hash", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
    )
    op.create_index(
        "ix_source_fetch_logs_source_kind", "source_fetch_logs", ["source_name", "request_kind"]
    )
    op.create_index("ix_source_fetch_logs_request_key", "source_fetch_logs", ["request_key"])
    op.create_index("ix_source_fetch_logs_started_at", "source_fetch_logs", ["started_at"])


def downgrade() -> None:
    op.drop_index("ix_source_fetch_logs_started_at", table_name="source_fetch_logs")
    op.drop_index("ix_source_fetch_logs_request_key", table_name="source_fetch_logs")
    op.drop_index("ix_source_fetch_logs_source_kind", table_name="source_fetch_logs")
    op.drop_table("source_fetch_logs")
    op.drop_table("source_rate_limits")

    for column in (
        "retry_policy",
        "misfire_policy",
        "max_runtime_seconds",
        "last_heartbeat_at",
        "lock_expires_at",
        "locked_by",
        "locked_at",
        "last_status",
        "timezone",
        "interval_seconds",
        "schedule_kind",
    ):
        op.drop_column("scheduled_jobs", column)
