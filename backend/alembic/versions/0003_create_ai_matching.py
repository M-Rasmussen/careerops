"""create candidate_profiles, agent_runs, job_matches

Additive only: existing jobs and applications rows are untouched.

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-20
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _timestamp(name: str, nullable: bool = False) -> sa.Column:
    default = None if nullable else sa.func.now()
    return sa.Column(name, sa.DateTime(timezone=True), server_default=default, nullable=nullable)


def upgrade() -> None:
    op.create_table(
        "candidate_profiles",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=False),
        sa.Column("resume_text", sa.Text(), nullable=False),
        _timestamp("created_at"),
        _timestamp("updated_at"),
        sa.CheckConstraint("id = 1", name="ck_candidate_profiles_singleton"),
    )
    op.create_table(
        "agent_runs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("kind", sa.String(40), nullable=False),
        sa.Column("job_id", sa.Uuid(), sa.ForeignKey("jobs.id", ondelete="RESTRICT"), nullable=True),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("provider", sa.String(50), nullable=False),
        sa.Column("model", sa.String(200), nullable=False),
        sa.Column("steps", sa.Integer(), nullable=False),
        sa.Column("tool_calls", JSONB(), nullable=False),
        sa.Column("error_type", sa.String(40), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        _timestamp("started_at"),
        _timestamp("finished_at", nullable=True),
        sa.CheckConstraint(
            "status IN ('running', 'succeeded', 'failed')", name="ck_agent_runs_status"
        ),
    )
    op.create_index("ix_agent_runs_started_at", "agent_runs", ["started_at"])
    op.create_index("ix_agent_runs_job_id", "agent_runs", ["job_id"])
    op.create_table(
        "job_matches",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "run_id",
            sa.Uuid(),
            sa.ForeignKey("agent_runs.id", ondelete="RESTRICT"),
            nullable=False,
            unique=True,
        ),
        sa.Column("job_id", sa.Uuid(), sa.ForeignKey("jobs.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("overall_match", sa.String(30), nullable=False),
        sa.Column("assessment", JSONB(), nullable=False),
        sa.Column("job_description", sa.Text(), nullable=False),
        sa.Column("input_hash", sa.String(64), nullable=False),
        _timestamp("created_at"),
        sa.CheckConstraint(
            "overall_match IN ('strong', 'moderate', 'weak', 'insufficient_information')",
            name="ck_job_matches_overall_match",
        ),
    )
    op.create_index("ix_job_matches_job_id", "job_matches", ["job_id"])
    op.create_index("ix_job_matches_input_hash", "job_matches", ["input_hash"])
    op.create_index("ix_job_matches_created_at", "job_matches", ["created_at"])


def downgrade() -> None:
    # Drops only the three AI tables (and their rows); jobs and applications are untouched.
    op.drop_table("job_matches")
    op.drop_table("agent_runs")
    op.drop_table("candidate_profiles")
