"""board role: general hiring board vs prospect/talent pool

Set only from evidence: link text on the employer's site, the provider's board name, or postings that are all
non-vacancies. NULL = not known yet.

Revision ID: 0011
Revises: 0010
Create Date: 2026-11-24
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("job_sources", sa.Column("board_role", sa.String(20), nullable=True))
    op.create_check_constraint("ck_job_sources_board_role", "job_sources", "board_role IS NULL OR board_role IN ('general', 'talent_pool')")


def downgrade() -> None:
    op.drop_constraint("ck_job_sources_board_role", "job_sources", type_="check")
    op.drop_column("job_sources", "board_role")
