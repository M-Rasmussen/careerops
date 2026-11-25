"""non-vacancy classification on discovered jobs; provider-reported board names

- discovered_jobs.non_vacancy_reason: set when a posting explicitly isn't a specific opening (interest form, talent
  pool). Such postings stay in the table (history, saved links) but never show in active Job Discovery.
- job_sources.provider_name: the employer name the ATS itself reports for a board (an ownership signal).

Revision ID: 0010
Revises: 0009
Create Date: 2026-11-24
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("discovered_jobs", sa.Column("non_vacancy_reason", sa.Text, nullable=True))
    op.add_column("job_sources", sa.Column("provider_name", sa.String(200), nullable=True))


def downgrade() -> None:
    op.drop_column("job_sources", "provider_name")
    op.drop_column("discovered_jobs", "non_vacancy_reason")
