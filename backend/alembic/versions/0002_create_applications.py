"""create applications table

Additive only: existing jobs rows are untouched.

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-20
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "applications",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "job_id",
            sa.Uuid(),
            sa.ForeignKey("jobs.id", ondelete="RESTRICT"),
            nullable=False,
            unique=True,
        ),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("applied_date", sa.Date(), nullable=False),
        sa.Column("follow_up_date", sa.Date(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "status IN ('applied', 'interviewing', 'offer', 'rejected', 'withdrawn')",
            name="ck_applications_status",
        ),
        sa.CheckConstraint(
            "follow_up_date IS NULL OR follow_up_date >= applied_date",
            name="ck_applications_follow_up_after_applied",
        ),
    )
    op.create_index("ix_applications_status", "applications", ["status"])
    op.create_index("ix_applications_applied_date", "applications", ["applied_date"])


def downgrade() -> None:
    # Drops only the applications table (and its rows); jobs are untouched.
    op.drop_index("ix_applications_applied_date", table_name="applications")
    op.drop_index("ix_applications_status", table_name="applications")
    op.drop_table("applications")
