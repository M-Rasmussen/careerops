"""restrict discovered_jobs.employment_type to the normalized values

Values outside the normalized set (unrecognized provider wording) become NULL ("unknown"); recognized values are
unchanged. Later discovery runs re-classify each posting from its source.

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-24
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ALLOWED = "('Full-time', 'Part-time', 'Contract', 'Contract-to-hire', 'Temporary', 'Internship')"


def upgrade() -> None:
    op.execute(f"UPDATE discovered_jobs SET employment_type = NULL WHERE employment_type NOT IN {ALLOWED}")
    op.create_check_constraint(
        "ck_discovered_jobs_employment_type", "discovered_jobs", f"employment_type IS NULL OR employment_type IN {ALLOWED}"
    )


def downgrade() -> None:
    op.drop_constraint("ck_discovered_jobs_employment_type", "discovered_jobs", type_="check")
