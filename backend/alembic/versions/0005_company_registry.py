"""company registry, ATS board directory, broad-discovery provenance

Additive: new companies / company_aliases / provider_requests tables and new nullable or defaulted columns.
Existing job sources get a company record (origin "manual", unresolved) so their discovered jobs stay linked.
Provider CHECK constraints are widened (Ashby, Himalayas, JSearch); no rows are changed or removed.

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-23
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OLD_PROVIDERS = "provider IN ('greenhouse', 'lever', 'lever_eu')"
BOARD_PROVIDERS = "provider IN ('greenhouse', 'lever', 'lever_eu', 'ashby')"
JOB_PROVIDERS = "provider IN ('greenhouse', 'lever', 'lever_eu', 'ashby', 'himalayas', 'jsearch')"


def _ts(name: str, nullable: bool = True) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), server_default=None if nullable else sa.func.now(), nullable=nullable)


def upgrade() -> None:
    op.create_table(
        "companies",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("domain", sa.String(253), nullable=True, unique=True),
        sa.Column("website", sa.String(2048), nullable=True),
        sa.Column("careers_url", sa.String(2048), nullable=True),
        sa.Column("industry", sa.String(200), nullable=True),
        sa.Column("engineering_focus", sa.String(300), nullable=True),
        sa.Column("target_geography", sa.String(200), nullable=True),
        sa.Column("priority", sa.String(2), nullable=True),
        sa.Column("origin", sa.String(20), nullable=False),
        sa.Column("watchlist", sa.Boolean(), nullable=False),
        sa.Column("verification_status", sa.String(20), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        _ts("first_seen_at"),
        _ts("last_seen_at"),
        _ts("created_at", nullable=False),
        _ts("updated_at", nullable=False),
        sa.CheckConstraint("priority IS NULL OR priority IN ('P1', 'P2', 'P3')", name="ck_companies_priority"),
        sa.CheckConstraint("origin IN ('curated_seed', 'manual', 'discovered')", name="ck_companies_origin"),
        sa.CheckConstraint(
            "verification_status IN ('verified', 'provider_reported', 'unresolved')",
            name="ck_companies_verification_status",
        ),
    )
    op.create_index("ix_companies_name", "companies", ["name"])
    op.create_index("ix_companies_priority", "companies", ["priority"])
    op.create_table(
        "company_aliases",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("company_id", sa.Uuid(), sa.ForeignKey("companies.id", ondelete="CASCADE"), nullable=False),
        sa.Column("kind", sa.String(30), nullable=False),
        sa.Column("value", sa.String(300), nullable=False),
        sa.UniqueConstraint("kind", "value", name="uq_company_aliases_kind_value"),
    )
    op.create_index("ix_company_aliases_company_id", "company_aliases", ["company_id"])
    op.create_table(
        "provider_requests",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("provider", sa.String(20), nullable=False),
        sa.Column("query_key", sa.String(64), nullable=False),
        _ts("requested_at", nullable=False),
    )
    op.create_index("ix_provider_requests_provider_requested_at", "provider_requests", ["provider", "requested_at"])

    # Board directory columns.
    op.add_column("job_sources", sa.Column("company_id", sa.Uuid(), sa.ForeignKey("companies.id", ondelete="RESTRICT"), nullable=True))
    op.add_column("job_sources", sa.Column("verification_status", sa.String(20), nullable=False, server_default="unverified"))
    op.alter_column("job_sources", "verification_status", server_default=None)
    op.add_column("job_sources", sa.Column("verification_evidence", sa.String(2048), nullable=True))
    op.add_column("job_sources", _ts("verified_at"))
    op.add_column("job_sources", _ts("last_checked_at"))
    op.create_check_constraint(
        "ck_job_sources_verification_status", "job_sources", "verification_status IN ('unverified', 'verified', 'failed')"
    )
    op.create_index("ix_job_sources_company_id", "job_sources", ["company_id"])
    op.drop_constraint("ck_job_sources_provider", "job_sources", type_="check")
    op.create_check_constraint("ck_job_sources_provider", "job_sources", BOARD_PROVIDERS)

    # Discovered job provenance columns.
    op.alter_column("discovered_jobs", "source_posting_id", type_=sa.String(500), existing_type=sa.String(100))
    op.add_column("discovered_jobs", sa.Column("company_id", sa.Uuid(), sa.ForeignKey("companies.id", ondelete="RESTRICT"), nullable=True))
    op.add_column("discovered_jobs", sa.Column("ats_job_key", sa.String(600), nullable=True))
    op.add_column("discovered_jobs", sa.Column("requisition_id", sa.String(100), nullable=True))
    op.add_column("discovered_jobs", sa.Column("seniority", sa.String(20), nullable=True))
    op.create_index("ix_discovered_jobs_company_id", "discovered_jobs", ["company_id"])
    op.create_index("ix_discovered_jobs_ats_job_key", "discovered_jobs", ["ats_job_key"])
    op.drop_constraint("ck_discovered_jobs_provider", "discovered_jobs", type_="check")
    op.create_check_constraint("ck_discovered_jobs_provider", "discovered_jobs", JOB_PROVIDERS)

    # Run metrics.
    for col in ("jobs_unique", "jobs_matched", "jobs_unknown", "companies_created", "boards_verified"):
        op.add_column("discovery_runs", sa.Column(col, sa.Integer(), nullable=False, server_default="0"))
        op.alter_column("discovery_runs", col, server_default=None)
    op.add_column("discovery_runs", sa.Column("warnings", JSONB(), nullable=False, server_default="[]"))
    op.alter_column("discovery_runs", "warnings", server_default=None)

    # Backfill: each existing board gets its own company (no name-based merging), and a board that has
    # answered its official API before counts as verified. Discovered jobs inherit their board's company.
    op.execute(
        """
        INSERT INTO companies (id, name, origin, watchlist, verification_status, notes, first_seen_at, last_seen_at)
        SELECT s.id, s.company_name, 'manual', true, 'unresolved',
               'Created from a job board configured before the company registry existed.',
               s.created_at, s.last_success_at
        FROM job_sources s
        """
    )
    # The company reuses its board's UUID, so the mapping is exactly one-to-one.
    op.execute("UPDATE job_sources SET company_id = id")
    op.execute(
        """
        UPDATE job_sources SET verification_status = 'verified', verified_at = last_success_at,
            last_checked_at = COALESCE(last_success_at, last_error_at),
            verification_evidence = 'Official API answered during an earlier discovery run'
        WHERE last_success_at IS NOT NULL
        """
    )
    op.execute(
        "UPDATE discovered_jobs d SET company_id = s.company_id FROM job_sources s WHERE d.source_id = s.id"
    )
    op.execute(
        "UPDATE discovered_jobs SET ats_job_key = (CASE WHEN provider = 'lever_eu' THEN 'lever' ELSE provider END)"
        " || ':' || source_posting_id"
    )


def downgrade() -> None:
    # Removes only what 0005 added. Fails safely (CHECK violation) if Ashby/Himalayas/JSearch rows exist.
    op.drop_constraint("ck_discovered_jobs_provider", "discovered_jobs", type_="check")
    op.create_check_constraint("ck_discovered_jobs_provider", "discovered_jobs", OLD_PROVIDERS)
    op.drop_constraint("ck_job_sources_provider", "job_sources", type_="check")
    op.create_check_constraint("ck_job_sources_provider", "job_sources", OLD_PROVIDERS)
    for col in ("warnings", "jobs_unique", "jobs_matched", "jobs_unknown", "companies_created", "boards_verified"):
        op.drop_column("discovery_runs", col)
    op.drop_index("ix_discovered_jobs_ats_job_key", "discovered_jobs")
    op.drop_index("ix_discovered_jobs_company_id", "discovered_jobs")
    for col in ("seniority", "requisition_id", "ats_job_key", "company_id"):
        op.drop_column("discovered_jobs", col)
    op.alter_column("discovered_jobs", "source_posting_id", type_=sa.String(100), existing_type=sa.String(500))
    op.drop_index("ix_job_sources_company_id", "job_sources")
    op.drop_constraint("ck_job_sources_verification_status", "job_sources", type_="check")
    for col in ("last_checked_at", "verified_at", "verification_evidence", "verification_status", "company_id"):
        op.drop_column("job_sources", col)
    op.drop_table("provider_requests")
    op.drop_table("company_aliases")
    op.drop_table("companies")
