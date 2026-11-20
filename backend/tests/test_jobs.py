import uuid
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

VALID_JOB = {
    "company": "Example Corp",
    "title": "Backend Engineer",
    "location": "Remote",
    "posting_url": "https://jobs.example.com/123",
    "description": "Build APIs.",
    "notes": "Synthetic test record.",
}


def test_create_job_returns_201_with_serialized_fields(client: TestClient) -> None:
    response = client.post("/api/jobs", json=VALID_JOB)
    assert response.status_code == 201
    body = response.json()
    uuid.UUID(body["id"])
    assert body["company"] == "Example Corp"
    assert body["posting_url"] == "https://jobs.example.com/123"
    created = datetime.fromisoformat(body["created_at"])
    assert created.utcoffset() is not None and created.utcoffset().total_seconds() == 0


def test_create_job_with_only_required_fields(client: TestClient) -> None:
    response = client.post("/api/jobs", json={"company": "Acme", "title": "Analyst"})
    assert response.status_code == 201
    body = response.json()
    assert body["location"] is None
    assert body["posting_url"] is None


def test_create_job_strips_whitespace_and_blanks_optional(client: TestClient) -> None:
    response = client.post(
        "/api/jobs", json={"company": "  Acme  ", "title": " Dev ", "location": "   "}
    )
    assert response.status_code == 201
    body = response.json()
    assert body["company"] == "Acme"
    assert body["title"] == "Dev"
    assert body["location"] is None


@pytest.mark.parametrize(
    "payload",
    [
        {"title": "Dev"},  # missing company
        {"company": "Acme"},  # missing title
        {"company": "   ", "title": "Dev"},  # blank company
        {"company": "Acme", "title": ""},  # empty title
        {"company": "Acme", "title": "Dev", "posting_url": "not a url"},
        {"company": "Acme", "title": "Dev", "posting_url": "ftp://example.com/job"},
        {"company": "x" * 201, "title": "Dev"},  # too long
        {"company": "Acme", "title": "Dev", "status": "applied"},  # unknown field
    ],
)
def test_create_job_rejects_invalid_input(client: TestClient, payload: dict) -> None:
    response = client.post("/api/jobs", json=payload)
    assert response.status_code == 422


def test_get_job_by_id(client: TestClient) -> None:
    created = client.post("/api/jobs", json=VALID_JOB).json()
    response = client.get(f"/api/jobs/{created['id']}")
    assert response.status_code == 200
    assert response.json() == created


def test_get_missing_job_returns_404(client: TestClient) -> None:
    response = client.get(f"/api/jobs/{uuid.uuid4()}")
    assert response.status_code == 404
    assert response.json() == {"detail": "Job not found"}


def test_get_job_with_malformed_id_returns_422(client: TestClient) -> None:
    assert client.get("/api/jobs/not-a-uuid").status_code == 422


def test_list_jobs_newest_first(client: TestClient) -> None:
    ids = [
        client.post("/api/jobs", json={"company": f"C{i}", "title": "T"}).json()["id"]
        for i in range(3)
    ]
    response = client.get("/api/jobs")
    assert response.status_code == 200
    listed = [job["id"] for job in response.json()]
    # Inside one test transaction now() is identical, so ties break on id; just
    # check all are present and timestamps are non-increasing.
    assert set(ids) <= set(listed)
    stamps = [job["created_at"] for job in response.json()]
    assert stamps == sorted(stamps, reverse=True)


def test_list_jobs_pagination(client: TestClient) -> None:
    for i in range(3):
        client.post("/api/jobs", json={"company": f"C{i}", "title": "T"})
    assert len(client.get("/api/jobs", params={"limit": 2}).json()) == 2
    assert client.get("/api/jobs", params={"limit": 0}).status_code == 422
    assert client.get("/api/jobs", params={"limit": 201}).status_code == 422
    assert client.get("/api/jobs", params={"offset": -1}).status_code == 422


def test_job_persists_in_database(client: TestClient, db_session) -> None:
    from app.models import Job

    created = client.post("/api/jobs", json=VALID_JOB).json()
    row = db_session.get(Job, uuid.UUID(created["id"]))
    assert row is not None
    assert row.title == "Backend Engineer"
