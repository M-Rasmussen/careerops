"""remove company priority; posting metadata; save/unsave lifecycle

- companies.priority is dropped (it never reflected the user's preferences). Seed notes that only described
  the old priority text are replaced; user-written notes are left alone.
- discovered_jobs gains posted_at, employment_type, unsaved_at, review_history. Ashby's publishedAt, previously
  stored as source_updated_at, is copied to posted_at.
- The saved CHECK is relaxed so a lead keeps its saved_job_id link after being unsaved.
- jobs gains unsaved_at (soft removal from Saved Jobs; rows and applications are never deleted).
Existing rows are otherwise unchanged.

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-24
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "UPDATE companies SET notes = 'Curated suggestion. Not evidence of current hiring or ATS verification.' "
        "WHERE origin = 'curated_seed' AND notes LIKE 'Curated suggestion (P_ — %' "
        "AND notes LIKE '%Priority is a suggested monitoring priority, not evidence of hiring or ATS verification.'"
    )
    op.drop_index("ix_companies_priority", "companies")
    op.drop_constraint("ck_companies_priority", "companies", type_="check")
    op.drop_column("companies", "priority")

    op.add_column("discovered_jobs", sa.Column("employment_type", sa.String(40), nullable=True))
    op.add_column("discovered_jobs", sa.Column("posted_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("discovered_jobs", sa.Column("unsaved_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("discovered_jobs", sa.Column("review_history", JSONB(), nullable=False, server_default="[]"))
    op.alter_column("discovered_jobs", "review_history", server_default=None)
    op.create_index("ix_discovered_jobs_posted_at", "discovered_jobs", ["posted_at"])
    op.execute("UPDATE discovered_jobs SET posted_at = source_updated_at, source_updated_at = NULL WHERE provider = 'ashby'")
    op.drop_constraint("ck_discovered_jobs_saved_has_job", "discovered_jobs", type_="check")
    op.create_check_constraint(
        "ck_discovered_jobs_saved_has_job", "discovered_jobs", "review_status <> 'saved' OR saved_job_id IS NOT NULL"
    )
    op.add_column("jobs", sa.Column("unsaved_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    # Restores the old shape. The strict saved CHECK fails (safely) if unsaved leads still keep a saved_job_id.
    op.drop_column("jobs", "unsaved_at")
    op.drop_constraint("ck_discovered_jobs_saved_has_job", "discovered_jobs", type_="check")
    op.create_check_constraint(
        "ck_discovered_jobs_saved_has_job", "discovered_jobs", "(review_status = 'saved') = (saved_job_id IS NOT NULL)"
    )
    op.execute("UPDATE discovered_jobs SET source_updated_at = posted_at WHERE provider = 'ashby'")
    op.drop_index("ix_discovered_jobs_posted_at", "discovered_jobs")
    for col in ("review_history", "unsaved_at", "posted_at", "employment_type"):
        op.drop_column("discovered_jobs", col)
    op.add_column("companies", sa.Column("priority", sa.String(2), nullable=True))
    op.create_check_constraint("ck_companies_priority", "companies", "priority IS NULL OR priority IN ('P1', 'P2', 'P3')")
    op.create_index("ix_companies_priority", "companies", ["priority"])
