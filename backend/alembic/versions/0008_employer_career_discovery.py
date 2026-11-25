"""discovery generations, employer career-source discovery, ownership-verified boards, and a task queue

- discovery_generations: the active Job Discovery generation is the highest id. Every existing discovered job is
  placed in generation 1 ("legacy"); archiving later inserts a new generation, so nothing is deleted.
- discovered_jobs: generation, appearances (append-only source history), application_url.
- companies: career-source discovery status, evidence, error, and timestamps.
- job_sources: "needs_review" status and employer-ownership evidence (only ownership-verified boards are synced
  into the active generation).
- discovery_tasks: PostgreSQL-backed work queue (no duplicate active task per company/board).

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-24
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

DISCOVERY_STATUSES = "('not_checked', 'discovering', 'verified', 'needs_review', 'unsupported_ats', 'not_found', 'failed')"


def upgrade() -> None:
    op.create_table(
        "discovery_generations",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("label", sa.String(200), nullable=False),
        sa.Column("archived_jobs", sa.Integer, nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.execute("INSERT INTO discovery_generations (id, label) VALUES (1, 'Legacy discovery (before the employer-source pipeline)')")
    op.execute("SELECT setval(pg_get_serial_sequence('discovery_generations', 'id'), 1)")

    op.add_column("discovered_jobs", sa.Column("generation", sa.Integer, sa.ForeignKey("discovery_generations.id", ondelete="RESTRICT"), nullable=True))
    op.add_column("discovered_jobs", sa.Column("appearances", postgresql.JSONB, nullable=False, server_default="[]"))
    op.add_column("discovered_jobs", sa.Column("application_url", sa.String(2048), nullable=True))
    op.execute(
        "UPDATE discovered_jobs SET generation = 1, appearances = jsonb_build_array(jsonb_build_object("
        "'generation', 1, 'via', 'legacy', 'provider', provider, 'source_id', source_id, "
        "'first_seen_at', first_seen_at, 'last_seen_at', last_seen_at))"
    )
    op.create_index("ix_discovered_jobs_generation", "discovered_jobs", ["generation"])

    op.add_column("companies", sa.Column("discovery_status", sa.String(20), nullable=False, server_default="not_checked"))
    op.add_column("companies", sa.Column("discovery_evidence", postgresql.JSONB, nullable=False, server_default="[]"))
    op.add_column("companies", sa.Column("discovery_error", sa.Text, nullable=True))
    op.add_column("companies", sa.Column("discovery_checked_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("companies", sa.Column("website_verified_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("companies", sa.Column("unsupported_ats", sa.String(40), nullable=True))
    op.create_check_constraint("ck_companies_discovery_status", "companies", f"discovery_status IN {DISCOVERY_STATUSES}")

    op.drop_constraint("ck_job_sources_verification_status", "job_sources", type_="check")
    op.create_check_constraint(
        "ck_job_sources_verification_status", "job_sources",
        "verification_status IN ('unverified', 'verified', 'needs_review', 'failed')",
    )
    op.add_column("job_sources", sa.Column("ownership_verified_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("job_sources", sa.Column("ownership_evidence", sa.String(2048), nullable=True))
    # Boards verified only because their API answered (not linked from the employer's site) need an ownership check.
    op.execute("UPDATE job_sources SET verification_status = 'needs_review' WHERE verification_status = 'verified'")

    op.create_table(
        "discovery_tasks",
        sa.Column("id", sa.Uuid, primary_key=True),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("company_id", sa.Uuid, sa.ForeignKey("companies.id", ondelete="CASCADE"), nullable=True),
        sa.Column("source_id", sa.Uuid, sa.ForeignKey("job_sources.id", ondelete="CASCADE"), nullable=True),
        sa.Column("status", sa.String(10), nullable=False, server_default="queued"),
        sa.Column("attempts", sa.Integer, nullable=False, server_default="0"),
        sa.Column("run_after", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("locked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("kind IN ('discover', 'sync')", name="ck_discovery_tasks_kind"),
        sa.CheckConstraint("status IN ('queued', 'running', 'done', 'failed')", name="ck_discovery_tasks_status"),
        sa.CheckConstraint(
            "(kind = 'discover' AND company_id IS NOT NULL) OR (kind = 'sync' AND source_id IS NOT NULL)",
            name="ck_discovery_tasks_target",
        ),
    )
    # At most one queued/running task per target: enqueueing twice is a no-op.
    op.create_index("uq_discovery_tasks_active_company", "discovery_tasks", ["kind", "company_id"], unique=True,
                    postgresql_where=sa.text("status IN ('queued', 'running') AND company_id IS NOT NULL AND kind = 'discover'"))
    op.create_index("uq_discovery_tasks_active_source", "discovery_tasks", ["kind", "source_id"], unique=True,
                    postgresql_where=sa.text("status IN ('queued', 'running') AND kind = 'sync'"))
    op.create_index("ix_discovery_tasks_status_run_after", "discovery_tasks", ["status", "run_after"])


def downgrade() -> None:
    op.drop_table("discovery_tasks")
    op.drop_column("job_sources", "ownership_evidence")
    op.drop_column("job_sources", "ownership_verified_at")
    op.execute("UPDATE job_sources SET verification_status = 'verified' WHERE verification_status = 'needs_review'")
    op.drop_constraint("ck_job_sources_verification_status", "job_sources", type_="check")
    op.create_check_constraint(
        "ck_job_sources_verification_status", "job_sources", "verification_status IN ('unverified', 'verified', 'failed')"
    )
    op.drop_constraint("ck_companies_discovery_status", "companies", type_="check")
    for col in ("unsupported_ats", "website_verified_at", "discovery_checked_at", "discovery_error", "discovery_evidence", "discovery_status"):
        op.drop_column("companies", col)
    op.drop_index("ix_discovered_jobs_generation", "discovered_jobs")
    for col in ("application_url", "appearances", "generation"):
        op.drop_column("discovered_jobs", col)
    op.drop_table("discovery_generations")
