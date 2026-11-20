from collections.abc import Iterator

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db import get_db
from app.main import app


def test_health_reports_database_ok(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "database": "ok"}


def test_health_returns_503_when_database_unreachable() -> None:
    bad_engine = create_engine(
        "postgresql+psycopg://nobody:wrong@127.0.0.1:1/none",
        connect_args={"connect_timeout": 1},
    )

    def broken_db() -> Iterator[Session]:
        with Session(bad_engine) as session:
            yield session

    app.dependency_overrides[get_db] = broken_db
    try:
        response = TestClient(app).get("/health")
    finally:
        app.dependency_overrides.clear()
        bad_engine.dispose()
    assert response.status_code == 503
    assert response.json()["database"] == "unavailable"
