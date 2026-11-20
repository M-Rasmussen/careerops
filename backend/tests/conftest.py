"""Test fixtures.

Tests run against a dedicated database (TEST_POSTGRES_DB, default
"careerops_test"), never the development database. Each test runs inside a
transaction that is rolled back, so no data persists between tests.
"""

import os
from collections.abc import Iterator

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import Connection, Engine, create_engine, text
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db, get_session_factory
from app.main import app

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEST_DB_NAME = os.environ.get("TEST_POSTGRES_DB", "careerops_test")


def _ensure_test_database() -> None:
    settings = get_settings()
    if TEST_DB_NAME == settings.db_name:
        raise RuntimeError("TEST_POSTGRES_DB must differ from POSTGRES_DB; refusing to run tests")
    admin = create_engine(settings.database_url(), isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        exists = conn.execute(
            text("SELECT 1 FROM pg_database WHERE datname = :name"), {"name": TEST_DB_NAME}
        ).scalar()
        if not exists:
            # Identifier is from config, quoted; CREATE DATABASE cannot take bind params.
            quoted = conn.dialect.identifier_preparer.quote(TEST_DB_NAME)
            conn.execute(text(f"CREATE DATABASE {quoted}"))
    admin.dispose()


@pytest.fixture(scope="session")
def engine() -> Iterator[Engine]:
    _ensure_test_database()
    cfg = Config(os.path.join(BACKEND_DIR, "alembic.ini"))
    cfg.attributes["database_name"] = TEST_DB_NAME
    command.upgrade(cfg, "head")
    eng = create_engine(
        get_settings().database_url(TEST_DB_NAME),
        connect_args={"options": "-c timezone=UTC"},
    )
    yield eng
    eng.dispose()


@pytest.fixture
def connection(engine: Engine) -> Iterator[Connection]:
    """One connection per test whose outer transaction is always rolled back."""
    conn = engine.connect()
    transaction = conn.begin()
    try:
        yield conn
    finally:
        transaction.rollback()
        conn.close()


def _session(connection: Connection) -> Session:
    # Commits inside the app become savepoints; the outer transaction is rolled back.
    return Session(bind=connection, join_transaction_mode="create_savepoint", expire_on_commit=False)


@pytest.fixture
def db_session(connection: Connection) -> Iterator[Session]:
    session = _session(connection)
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def client(db_session: Session, connection: Connection) -> Iterator[TestClient]:
    app.dependency_overrides[get_db] = lambda: db_session
    # Short-lived sessions (used around LLM calls) share the same rolled-back connection.
    app.dependency_overrides[get_session_factory] = lambda: lambda: _session(connection)
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()
