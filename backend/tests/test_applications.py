import uuid
from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import Application, Job

TODAY = datetime.now(timezone.utc).date()


def make_job(client: TestClient, company: str = "Example Corp", title: str = "Backend Engineer") -> str:
    response = client.post(
        "/api/jobs",
        json={"company": company, "title": title, "posting_url": "https://jobs.example.com/1"},
    )
    assert response.status_code == 201
    return response.json()["id"]


def make_app(client: TestClient, job_id: str, **overrides) -> dict:
    payload = {"job_id": job_id, "submission_confirmed": True, "applied_date": "2026-11-01"}
    payload.update(overrides)
    response = client.post("/api/applications", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


# --- create ---------------------------------------------------------------


def test_create_application_returns_201_with_job(client: TestClient) -> None:
    job_id = make_job(client)
    body = make_app(client, job_id, follow_up_date="2026-11-08", notes="  Referred by a friend  ")
    uuid.UUID(body["id"])
    assert body["job_id"] == job_id
    assert body["status"] == "applied"  # default
    assert body["applied_date"] == "2026-11-01"
    assert body["follow_up_date"] == "2026-11-08"
    assert body["notes"] == "Referred by a friend"
    assert body["job"]["company"] == "Example Corp"
    created = datetime.fromisoformat(body["created_at"])
    assert created.utcoffset() == timedelta(0)


def test_saving_a_job_does_not_create_an_application(client: TestClient) -> None:
    job_id = make_job(client)
    listed = client.get("/api/applications").json()
    assert all(a["job_id"] != job_id for a in listed)


@pytest.mark.parametrize(
    "overrides",
    [
        {"submission_confirmed": False},
        {"submission_confirmed": None},
        {"submission_confirmed": "yes"},
        {"status": "saved"},
        {"status": "Applied"},
        {"applied_date": None},
        {"applied_date": "not-a-date"},
        {"applied_date": (TODAY + timedelta(days=5)).isoformat()},
        {"notes": 123},
        {"unexpected": "field"},
    ],
)
def test_create_application_rejects_invalid_input(client: TestClient, overrides: dict) -> None:
    payload = {"job_id": make_job(client), "submission_confirmed": True, "applied_date": "2026-11-01"}
    payload.update(overrides)
    assert client.post("/api/applications", json=payload).status_code == 422


FOLLOW_UP_ERROR = {"detail": "follow_up_date cannot be before applied_date"}


def test_create_rejects_follow_up_before_applied(client: TestClient) -> None:
    job_id = make_job(client)
    response = client.post(
        "/api/applications",
        json={
            "job_id": job_id,
            "submission_confirmed": True,
            "applied_date": "2026-11-10",
            "follow_up_date": "2026-11-09",
        },
    )
    assert response.status_code == 422
    assert response.json() == FOLLOW_UP_ERROR
    assert all(a["job_id"] != job_id for a in client.get("/api/applications").json())
    # Same-day follow-up is allowed (boundary).
    make_app(client, job_id, applied_date="2026-11-10", follow_up_date="2026-11-10")


def test_create_application_requires_submission_confirmed(client: TestClient) -> None:
    payload = {"job_id": make_job(client), "applied_date": "2026-11-01"}
    assert client.post("/api/applications", json=payload).status_code == 422


def test_create_application_for_missing_job_returns_404(client: TestClient) -> None:
    response = client.post(
        "/api/applications",
        json={"job_id": str(uuid.uuid4()), "submission_confirmed": True, "applied_date": "2026-11-01"},
    )
    assert response.status_code == 404
    assert response.json() == {"detail": "Job not found"}


def test_duplicate_application_for_same_job_returns_409(client: TestClient) -> None:
    job_id = make_job(client)
    make_app(client, job_id)
    response = client.post(
        "/api/applications",
        json={"job_id": job_id, "submission_confirmed": True, "applied_date": "2026-11-02"},
    )
    assert response.status_code == 409
    # The session is still usable after the rolled-back insert.
    assert client.get("/api/applications").status_code == 200


# --- read -----------------------------------------------------------------


def test_get_application_by_id(client: TestClient) -> None:
    created = make_app(client, make_job(client))
    response = client.get(f"/api/applications/{created['id']}")
    assert response.status_code == 200
    assert response.json() == created


def test_get_missing_application_returns_404(client: TestClient) -> None:
    response = client.get(f"/api/applications/{uuid.uuid4()}")
    assert response.status_code == 404
    assert response.json() == {"detail": "Application not found"}


def test_get_application_with_malformed_id_returns_422(client: TestClient) -> None:
    assert client.get("/api/applications/not-a-uuid").status_code == 422


# --- update ---------------------------------------------------------------


def test_update_application_partial(client: TestClient) -> None:
    created = make_app(client, make_job(client), notes="first", follow_up_date="2026-11-05")
    response = client.patch(
        f"/api/applications/{created['id']}", json={"status": "interviewing"}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "interviewing"
    assert body["notes"] == "first"  # untouched
    assert body["follow_up_date"] == "2026-11-05"

    cleared = client.patch(
        f"/api/applications/{created['id']}", json={"follow_up_date": None, "notes": ""}
    ).json()
    assert cleared["follow_up_date"] is None
    assert cleared["notes"] is None


@pytest.mark.parametrize(
    "payload",
    [
        {"status": None},
        {"status": "ghosted"},
        {"applied_date": None},
        {"applied_date": (TODAY + timedelta(days=5)).isoformat()},
        {"job_id": str(uuid.uuid4())},
        {"submission_confirmed": True},
    ],
)
def test_update_application_rejects_invalid_input(client: TestClient, payload: dict) -> None:
    created = make_app(client, make_job(client))
    assert client.patch(f"/api/applications/{created['id']}", json=payload).status_code == 422


def test_update_rejects_follow_up_before_merged_applied_date(client: TestClient) -> None:
    created = make_app(client, make_job(client), applied_date="2026-11-01", follow_up_date="2026-11-05")
    response = client.patch(f"/api/applications/{created['id']}", json={"applied_date": "2026-11-10"})
    assert response.status_code == 422
    assert response.json() == FOLLOW_UP_ERROR
    response = client.patch(f"/api/applications/{created['id']}", json={"follow_up_date": "2026-10-31"})
    assert response.status_code == 422
    assert response.json() == FOLLOW_UP_ERROR
    # Rejected update must not leak into a later commit.
    client.patch(f"/api/applications/{created['id']}", json={"notes": "later"})
    assert client.get(f"/api/applications/{created['id']}").json()["applied_date"] == "2026-11-01"


def test_update_missing_application_returns_404(client: TestClient) -> None:
    assert client.patch(f"/api/applications/{uuid.uuid4()}", json={"notes": "x"}).status_code == 404


# --- delete ---------------------------------------------------------------


def test_delete_application_keeps_job(client: TestClient) -> None:
    job_id = make_job(client)
    created = make_app(client, job_id)
    response = client.delete(f"/api/applications/{created['id']}")
    assert response.status_code == 204
    assert client.get(f"/api/applications/{created['id']}").status_code == 404
    assert client.get(f"/api/jobs/{job_id}").status_code == 200
    assert client.delete(f"/api/applications/{created['id']}").status_code == 404


# --- list / filter / paginate --------------------------------------------


def test_list_filters(client: TestClient) -> None:
    a = make_app(client, make_job(client, "Acme Corp"), applied_date="2026-10-01")
    b = make_app(client, make_job(client, "Globex"), applied_date="2026-10-15", status="rejected")
    c = make_app(client, make_job(client, "100% Remote Co"), applied_date="2026-11-01")

    def ids(**params) -> list[str]:
        response = client.get("/api/applications", params=params)
        assert response.status_code == 200
        return [x["id"] for x in response.json()]

    assert ids()[:3] == [c["id"], b["id"], a["id"]]  # newest applied first
    assert ids(status="rejected") == [b["id"]]
    assert ids(company="acme") == [a["id"]]  # case-insensitive substring
    assert ids(company="100%") == [c["id"]]  # % is literal, not a wildcard
    assert ids(company="0% R") == [c["id"]]
    assert ids(applied_from="2026-10-10", applied_to="2026-10-31") == [b["id"]]
    assert ids(applied_from="2026-10-15", applied_to="2026-10-15") == [b["id"]]  # inclusive
    assert ids(company="Acme", status="rejected") == []


def test_list_pagination_and_bad_params(client: TestClient) -> None:
    for i in range(3):
        make_app(client, make_job(client, f"C{i}"))
    assert len(client.get("/api/applications", params={"limit": 2}).json()) == 2
    for params in [
        {"limit": 0},
        {"limit": 201},
        {"offset": -1},
        {"status": "nope"},
        {"applied_from": "yesterday"},
        {"company": "x" * 201},
    ]:
        assert client.get("/api/applications", params=params).status_code == 422, params


# --- database behaviour ---------------------------------------------------


def test_application_persists_with_status_value(client: TestClient, db_session: Session) -> None:
    created = make_app(client, make_job(client), status="offer")
    stored = db_session.execute(
        text("SELECT status FROM applications WHERE id = :id"), {"id": created["id"]}
    ).scalar_one()
    assert stored == "offer"


@pytest.mark.parametrize(
    "values",
    [
        {"status": "saved", "applied_date": date(2026, 11, 1)},
        {"status": "applied", "applied_date": date(2026, 11, 2), "follow_up_date": date(2026, 11, 1)},
    ],
)
def test_database_check_constraints(client: TestClient, db_session: Session, values: dict) -> None:
    job_id = uuid.UUID(make_job(client))
    db_session.add(Application(job_id=job_id, **values))
    with pytest.raises(IntegrityError):
        db_session.flush()
    db_session.rollback()


def test_job_with_application_cannot_be_deleted(client: TestClient, db_session: Session) -> None:
    job_id = make_job(client)
    make_app(client, job_id)
    with pytest.raises(IntegrityError):
        db_session.execute(text("DELETE FROM jobs WHERE id = :id"), {"id": job_id})
    db_session.rollback()
    assert db_session.get(Job, uuid.UUID(job_id)) is not None
