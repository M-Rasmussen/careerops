"""fair, scheduled board synchronization; missing/closed postings; run progress

- job_sources: next_sync_at (fair, overdue-first scheduling), sync_failures (backoff), last_sync_count.
- discovered_jobs: missing_count (consecutive complete syncs without the posting), closed_at (set after 2; cleared if it
  returns). Closed postings are hidden from Job Discovery, never deleted.
- discovery_runs: trigger (manual | scheduled).
- discovery_tasks: run_id (progress per run), payload, and a 'feeds' kind (aggregator feeds of a run).

Revision ID: 0012
Revises: 0011
Create Date: 2026-11-24
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("job_sources", sa.Column("next_sync_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("job_sources", sa.Column("sync_failures", sa.Integer, nullable=False, server_default="0"))
    op.add_column("job_sources", sa.Column("last_sync_count", sa.Integer, nullable=True))
    # Existing boards: next check one default interval after their last success (no burst right after deploy).
    op.execute("UPDATE job_sources SET next_sync_at = last_success_at + interval '24 hours' WHERE last_success_at IS NOT NULL")
    op.create_index("ix_job_sources_next_sync_at", "job_sources", ["next_sync_at"])

    op.add_column("discovered_jobs", sa.Column("missing_count", sa.Integer, nullable=False, server_default="0"))
    op.add_column("discovered_jobs", sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True))

    op.add_column("discovery_runs", sa.Column("trigger", sa.String(10), nullable=False, server_default="manual"))
    op.create_check_constraint("ck_discovery_runs_trigger", "discovery_runs", "trigger IN ('manual', 'scheduled')")

    op.add_column("discovery_tasks", sa.Column("run_id", sa.Uuid, sa.ForeignKey("discovery_runs.id", ondelete="SET NULL"), nullable=True))
    op.add_column("discovery_tasks", sa.Column("payload", postgresql.JSONB, nullable=True))
    op.create_index("ix_discovery_tasks_run_id", "discovery_tasks", ["run_id"])
    op.drop_constraint("ck_discovery_tasks_kind", "discovery_tasks", type_="check")
    op.create_check_constraint("ck_discovery_tasks_kind", "discovery_tasks", "kind IN ('discover', 'sync', 'feeds')")
    op.drop_constraint("ck_discovery_tasks_target", "discovery_tasks", type_="check")
    op.create_check_constraint(
        "ck_discovery_tasks_target", "discovery_tasks",
        "(kind = 'discover' AND company_id IS NOT NULL) OR (kind = 'sync' AND source_id IS NOT NULL) OR (kind = 'feeds' AND run_id IS NOT NULL)",
    )


def downgrade() -> None:
    op.execute("DELETE FROM discovery_tasks WHERE kind = 'feeds'")
    op.drop_constraint("ck_discovery_tasks_target", "discovery_tasks", type_="check")
    op.create_check_constraint(
        "ck_discovery_tasks_target", "discovery_tasks",
        "(kind = 'discover' AND company_id IS NOT NULL) OR (kind = 'sync' AND source_id IS NOT NULL)",
    )
    op.drop_constraint("ck_discovery_tasks_kind", "discovery_tasks", type_="check")
    op.create_check_constraint("ck_discovery_tasks_kind", "discovery_tasks", "kind IN ('discover', 'sync')")
    op.drop_index("ix_discovery_tasks_run_id", "discovery_tasks")
    op.drop_column("discovery_tasks", "payload")
    op.drop_column("discovery_tasks", "run_id")
    op.drop_constraint("ck_discovery_runs_trigger", "discovery_runs", type_="check")
    op.drop_column("discovery_runs", "trigger")
    op.drop_column("discovered_jobs", "closed_at")
    op.drop_column("discovered_jobs", "missing_count")
    op.drop_index("ix_job_sources_next_sync_at", "job_sources")
    op.drop_column("job_sources", "last_sync_count")
    op.drop_column("job_sources", "sync_failures")
    op.drop_column("job_sources", "next_sync_at")
