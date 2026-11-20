"""Database engine and session management."""

from collections.abc import Callable, Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import get_settings


class Base(DeclarativeBase):
    pass


# Force UTC session time zone so timestamps are always returned as UTC.
engine = create_engine(
    get_settings().database_url(),
    pool_pre_ping=True,
    connect_args={"options": "-c timezone=UTC"},
)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def get_db() -> Iterator[Session]:
    """FastAPI dependency yielding a session that is always closed."""
    with SessionLocal() as session:
        yield session


def get_session_factory() -> Callable[[], Session]:
    """FastAPI dependency for code that opens its own short-lived sessions, e.g. around slow LLM calls,
    so no connection or transaction stays open while waiting on an external service."""
    return SessionLocal
