"""allow Workday board records (ownership only; no job import yet)

A Workday career site is identified by host + tenant + site. Boards with provider 'workday' can be recorded and
ownership-verified, but discovered_jobs.provider is unchanged: no Workday jobs can be stored until an adapter exists.

Revision ID: 0009
Revises: 0008
Create Date: 2026-11-24
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("job_sources", sa.Column("ats_host", sa.String(253), nullable=True))
    op.add_column("job_sources", sa.Column("career_site", sa.String(100), nullable=True))
    op.drop_constraint("ck_job_sources_provider", "job_sources", type_="check")
    op.create_check_constraint("ck_job_sources_provider", "job_sources",
                               "provider IN ('greenhouse', 'lever', 'lever_eu', 'ashby', 'workday')")
    op.create_check_constraint(
        "ck_job_sources_workday_site", "job_sources",
        "provider <> 'workday' OR (ats_host ~ '^[a-z0-9-]+\\.wd[0-9]+\\.myworkdayjobs\\.com$' AND career_site ~ '^[A-Za-z0-9_-]{1,100}$')",
    )


def downgrade() -> None:
    # Refuses if Workday boards exist (the provider check would fail); remove them deliberately first.
    op.drop_constraint("ck_job_sources_workday_site", "job_sources", type_="check")
    op.drop_constraint("ck_job_sources_provider", "job_sources", type_="check")
    op.create_check_constraint("ck_job_sources_provider", "job_sources", "provider IN ('greenhouse', 'lever', 'lever_eu', 'ashby')")
    op.drop_column("job_sources", "career_site")
    op.drop_column("job_sources", "ats_host")
