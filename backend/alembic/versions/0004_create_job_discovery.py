"""create job_sources, discovered_jobs, discovery_runs, discovery_settings

Additive only: existing jobs, applications, and AI tables are untouched.

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-23
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PROVIDER_CHECK = "provider IN ('greenhouse', 'lever', 'lever_eu')"


def _timestamp(name: str, nullable: bool = False) -> sa.Column:
    default = None if nullable else sa.func.now()
    return sa.Column(name, sa.DateTime(timezone=True), server_default=default, nullable=nullable)


def upgrade() -> None:
    op.create_table(
        "job_sources",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("provider", sa.String(20), nullable=False),
        sa.Column("company_name", sa.String(200), nullable=False),
        sa.Column("board_token", sa.String(100), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        _timestamp("last_success_at", nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        _timestamp("last_error_at", nullable=True),
        _timestamp("created_at"),
        _timestamp("updated_at"),
        sa.CheckConstraint(PROVIDER_CHECK, name="ck_job_sources_provider"),
        sa.UniqueConstraint("provider", "board_token", name="uq_job_sources_provider_board"),
    )
    op.create_table(
        "discovered_jobs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("source_id", sa.Uuid(), sa.ForeignKey("job_sources.id", ondelete="SET NULL"), nullable=True),
        sa.Column("provider", sa.String(20), nullable=False),
        sa.Column("source_posting_id", sa.String(100), nullable=False),
        sa.Column("company", sa.String(200), nullable=False),
        sa.Column("title", sa.String(300), nullable=False),
        sa.Column("location", sa.String(200), nullable=True),
        sa.Column("workplace_type", sa.String(10), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("posting_url", sa.String(2048), nullable=True),
        sa.Column("canonical_url", sa.String(2048), nullable=True),
        sa.Column("salary_min", sa.Integer(), nullable=True),
        sa.Column("salary_max", sa.Integer(), nullable=True),
        sa.Column("salary_currency", sa.String(3), nullable=True),
        sa.Column("salary_interval", sa.String(30), nullable=True),
        _timestamp("source_updated_at", nullable=True),
        _timestamp("first_seen_at"),
        _timestamp("last_seen_at"),
        sa.Column("filter_result", sa.String(10), nullable=False),
        sa.Column("filter_reasons", JSONB(), nullable=False),
        sa.Column("review_status", sa.String(10), nullable=False),
        sa.Column("saved_job_id", sa.Uuid(), sa.ForeignKey("jobs.id", ondelete="RESTRICT"), nullable=True),
        sa.Column(
            "duplicate_of_id", sa.Uuid(), sa.ForeignKey("discovered_jobs.id", ondelete="SET NULL"), nullable=True
        ),
        _timestamp("updated_at"),
        sa.CheckConstraint(PROVIDER_CHECK, name="ck_discovered_jobs_provider"),
        sa.CheckConstraint(
            "filter_result IN ('match', 'unknown', 'rejected')", name="ck_discovered_jobs_filter_result"
        ),
        sa.CheckConstraint(
            "review_status IN ('new', 'saved', 'skipped', 'later')", name="ck_discovered_jobs_review_status"
        ),
        sa.CheckConstraint(
            "workplace_type IS NULL OR workplace_type IN ('remote', 'hybrid', 'onsite')",
            name="ck_discovered_jobs_workplace_type",
        ),
        sa.CheckConstraint(
            "(review_status = 'saved') = (saved_job_id IS NOT NULL)", name="ck_discovered_jobs_saved_has_job"
        ),
        sa.UniqueConstraint("provider", "source_posting_id", name="uq_discovered_jobs_provider_posting"),
    )
    op.create_index("ix_discovered_jobs_canonical_url", "discovered_jobs", ["canonical_url"])
    op.create_index("ix_discovered_jobs_first_seen_at", "discovered_jobs", ["first_seen_at"])
    op.create_index("ix_discovered_jobs_review_filter", "discovered_jobs", ["review_status", "filter_result"])
    op.create_table(
        "discovery_runs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("sources_checked", sa.Integer(), nullable=False),
        sa.Column("sources_failed", sa.Integer(), nullable=False),
        sa.Column("jobs_retrieved", sa.Integer(), nullable=False),
        sa.Column("jobs_inserted", sa.Integer(), nullable=False),
        sa.Column("jobs_updated", sa.Integer(), nullable=False),
        sa.Column("duplicates", sa.Integer(), nullable=False),
        sa.Column("requests", sa.Integer(), nullable=False),
        sa.Column("sources", JSONB(), nullable=False),
        _timestamp("started_at"),
        _timestamp("finished_at", nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.CheckConstraint(
            "status IN ('running', 'succeeded', 'partial', 'failed')", name="ck_discovery_runs_status"
        ),
    )
    op.create_index("ix_discovery_runs_started_at", "discovery_runs", ["started_at"])
    op.create_table(
        "discovery_settings",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=False),
        sa.Column("filters", JSONB(), nullable=False),
        _timestamp("updated_at"),
        sa.CheckConstraint("id = 1", name="ck_discovery_settings_singleton"),
    )


def downgrade() -> None:
    # Drops only the four discovery tables (and their rows); saved jobs and applications are untouched.
    op.drop_table("discovery_settings")
    op.drop_table("discovery_runs")
    op.drop_table("discovered_jobs")
    op.drop_table("job_sources")
