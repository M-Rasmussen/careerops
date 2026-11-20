from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import Engine

from app.db import Base


def test_migrations_match_models(engine: Engine) -> None:
    """The migrated schema must match the SQLAlchemy models (no missing migration)."""
    with engine.connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn), Base.metadata)
    assert diff == []
