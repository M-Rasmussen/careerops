import copy
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.discovery import get_http_client
from app.config import DiscoverySettingsEnv, get_discovery_env
from app.main import app
from app.models import Application, DiscoveredJob, Job, JobSource
from tests.discovery_fixtures import GH_URL, GREENHOUSE, LEVER_URL, fake_client, http_error, lever_posting

GH_SOURCE = {"provider": "greenhouse", "company_name": "Example Labs", "board_token": "examplelabs"}
LEVER_SOURCE = {"provider": "lever", "company_name": "Example Co", "board_token": "exampleco"}


@pytest.fixture
def env(client: TestClient) -> dict[str, Any]:
    """Mutable discovery settings for the test (aggregators off unless a test turns them on)."""
    settings: dict[str, Any] = {"himalayas_enabled": False, "min_resync_minutes": 0}
    app.dependency_overrides[get_discovery_env] = lambda: DiscoverySettingsEnv(**settings)
    return settings


@pytest.fixture
def routes(client: TestClient, env: dict[str, Any]) -> Iterator[dict[str, list[Any]]]:
    """Mutable fake ATS responses used by every discovery run in the test. No real network."""
    table: dict[str, list[Any]] = {GH_URL: [copy.deepcopy(GREENHOUSE)], LEVER_URL: [[lever_posting(1), lever_posting(2)]]}
    app.dependency_overrides[get_http_client] = lambda: fake_client(table)[0]
    yield table


def add_sources(client: TestClient, *sources: dict) -> list[dict]:
    created = [client.post("/api/discovery/sources", json=s) for s in sources]
    assert all(r.status_code == 201 for r in created)
    return [r.json() for r in created]


def run(client: TestClient) -> dict:
    response = client.post("/api/discovery/runs")
    assert response.status_code == 201, response.text
    return response.json()


def jobs(client: TestClient, **params: Any) -> list[dict]:
    response = client.get("/api/discovery/jobs", params={"limit": 200, **params})
    assert response.status_code == 200
    return response.json()


# --- Sources ---


def test_source_crud_and_validation(client: TestClient) -> None:
    [source] = add_sources(client, GH_SOURCE)
    assert source["enabled"] is True and source["last_success_at"] is None
    assert client.post("/api/discovery/sources", json=GH_SOURCE).status_code == 409
    for bad in (
        {**GH_SOURCE, "provider": "workday"},
        {**GH_SOURCE, "board_token": "https://evil.com/x"},
        {**GH_SOURCE, "board_token": "../x"},
        {**GH_SOURCE, "company_name": " "},
        {**GH_SOURCE, "url": "https://boards-api.greenhouse.io"},
    ):
        assert client.post("/api/discovery/sources", json=bad).status_code == 422
    updated = client.patch(f"/api/discovery/sources/{source['id']}", json={"enabled": False}).json()
    assert updated["enabled"] is False
    assert client.patch(f"/api/discovery/sources/{source['id']}", json={"enabled": None}).status_code == 422
    assert client.patch(f"/api/discovery/sources/{uuid.uuid4()}", json={"enabled": True}).status_code == 404
    assert client.delete(f"/api/discovery/sources/{source['id']}").status_code == 204
    assert client.get("/api/discovery/sources").json() == []


# --- Runs ---


def test_run_persists_jobs_and_metadata(client: TestClient, routes: dict, db_session: Session) -> None:
    add_sources(client, GH_SOURCE, LEVER_SOURCE)
    result = run(client)
    assert result["status"] == "succeeded"
    assert (result["sources_checked"], result["jobs_retrieved"], result["jobs_inserted"], result["jobs_updated"]) == (2, 5, 5, 0)
    assert result["finished_at"] is not None and result["duration_ms"] >= 0
    assert {s["status"] for s in result["sources"] if s["kind"] == "board"} == {"succeeded"}
    assert {s["provider"]: s["status"] for s in result["sources"]}["jsearch"] == "skipped"
    assert db_session.scalar(select(func.count()).select_from(DiscoveredJob)) == 5
    listed = jobs(client)
    assert {j["review_status"] for j in listed} == {"new"}
    gh = next(j for j in listed if j["source_posting_id"] == "4001")
    assert gh["company"] == "Example Labs" and gh["workplace_type"] == "remote"
    sources = client.get("/api/discovery/sources").json()
    assert all(s["last_success_at"] and s["last_error"] is None for s in sources)
    assert client.get(f"/api/discovery/runs/{result['id']}").json()["id"] == result["id"]
    assert client.get("/api/discovery/runs").json()[0]["id"] == result["id"]


def test_repeated_runs_do_not_duplicate(client: TestClient, routes: dict) -> None:
    add_sources(client, GH_SOURCE, LEVER_SOURCE)
    run(client)
    first = {j["id"]: j for j in jobs(client)}
    client.patch(f"/api/discovery/jobs/{next(iter(first))}", json={"review_status": "skipped"})
    routes[LEVER_URL] = [[lever_posting(1, text="Software Engineer 1 (updated)"), lever_posting(2), lever_posting(3)]]
    second = run(client)
    assert (second["jobs_inserted"], second["jobs_updated"]) == (1, 5)
    after = {j["id"]: j for j in jobs(client)}
    assert len(after) == 6
    assert set(first) <= set(after)
    # Review status and first-seen time survive re-discovery; content is refreshed.
    skipped = after[next(iter(first))]
    assert skipped["review_status"] == "skipped"
    assert skipped["first_seen_at"] == first[skipped["id"]]["first_seen_at"]
    assert any(j["title"] == "Software Engineer 1 (updated)" for j in after.values())


def test_failed_source_does_not_stop_others_and_keeps_jobs(client: TestClient, routes: dict) -> None:
    add_sources(client, GH_SOURCE, LEVER_SOURCE)
    run(client)
    routes[GH_URL] = [http_error(GH_URL, 500)]
    result = run(client)
    assert result["status"] == "partial"
    assert result["sources_failed"] == 1
    failed = next(s for s in result["sources"] if s["provider"] == "greenhouse")
    assert failed["error"] == "HTTP 500"
    # Nothing is removed or closed because a fetch failed.
    assert len(jobs(client)) == 5
    gh = next(s for s in client.get("/api/discovery/sources").json() if s["provider"] == "greenhouse")
    assert gh["last_error"] == "HTTP 500" and gh["last_success_at"] is not None


def test_all_sources_failing_marks_run_failed(client: TestClient, routes: dict) -> None:
    add_sources(client, {**GH_SOURCE, "board_token": "unknownboard"})
    result = run(client)
    assert result["status"] == "failed"
    board = next(s for s in result["sources"] if s["kind"] == "board")
    assert "404" in board["error"]


def test_disabled_sources_are_skipped_and_nothing_to_run_is_refused(client: TestClient, routes: dict) -> None:
    add_sources(client, {**GH_SOURCE, "enabled": False})
    response = client.post("/api/discovery/runs")
    # No misleading "successful" zero-source run is recorded.
    assert response.status_code == 409
    assert "No discovery sources are available" in response.json()["detail"]
    assert client.get("/api/discovery/runs").json() == []
    assert jobs(client) == []


def test_in_batch_and_url_duplicates_are_flagged(client: TestClient, routes: dict) -> None:
    # Same posting ID twice in one response, plus a different ID whose URL matches after canonicalization.
    dup_url = {**lever_posting(9), "id": "lever-9b", "hostedUrl": "https://JOBS.lever.co/exampleco/lever-9/?utm_source=x"}
    routes[LEVER_URL] = [[lever_posting(1), lever_posting(1), lever_posting(9), dup_url]]
    add_sources(client, LEVER_SOURCE)
    result = run(client)
    assert result["jobs_inserted"] == 3
    assert (result["jobs_retrieved"], result["jobs_unique"]) == (4, 3)  # exact repeat collapsed
    assert result["duplicates"] == 1  # possible duplicate flagged by canonical URL
    flagged = [j for j in jobs(client) if j["duplicate_of_id"]]
    assert len(flagged) == 1
    assert len(jobs(client, hide_duplicates=True)) == 2


# --- Filters ---


def test_filters_are_applied_and_reevaluated(client: TestClient, routes: dict) -> None:
    add_sources(client, GH_SOURCE)
    run(client)
    saved = client.put(
        "/api/discovery/filters",
        json={"title_keywords": ["engineer"], "excluded_keywords": ["marketing"], "workplace_types": ["remote"]},
    )
    assert saved.status_code == 200
    assert client.get("/api/discovery/filters").json()["title_keywords"] == ["engineer"]
    by_id = {j["source_posting_id"]: j for j in jobs(client)}
    assert by_id["4001"]["filter_result"] == "match"
    assert by_id["4002"]["filter_result"] == "rejected"
    assert 'Title contains excluded keyword "marketing"' in by_id["4002"]["filter_reasons"]
    assert by_id["4003"]["filter_result"] == "unknown"  # no location data: unknown, not rejected
    assert [j["source_posting_id"] for j in jobs(client, filter_result="match")] == ["4001"]
    assert client.put("/api/discovery/filters", json={"seniority": ["wizard"]}).status_code == 422
    assert client.put("/api/discovery/filters", json={"min_salary": -1}).status_code == 422


def test_list_filtering_and_pagination(client: TestClient, routes: dict) -> None:
    add_sources(client, GH_SOURCE, LEVER_SOURCE)
    run(client)
    assert len(jobs(client, provider="lever")) == 2
    assert [j["title"] for j in jobs(client, q="marketing")] == ["Marketing Manager"]
    assert len(client.get("/api/discovery/jobs", params={"limit": 2}).json()) == 2
    page = client.get("/api/discovery/jobs", params={"limit": 2, "offset": 4}).json()
    assert len(page) == 1
    assert client.get("/api/discovery/jobs", params={"limit": 0}).status_code == 422
    assert client.get("/api/discovery/jobs", params={"review_status": "bogus"}).status_code == 422


# --- Review and save ---


def test_review_actions(client: TestClient, routes: dict) -> None:
    add_sources(client, GH_SOURCE)
    run(client)
    job_id = jobs(client)[0]["id"]
    later = client.patch(f"/api/discovery/jobs/{job_id}", json={"review_status": "later"})
    assert later.json()["review_status"] == "later"
    assert [j["id"] for j in jobs(client, review_status="later")] == [job_id]
    # "saved" is only reachable through the save endpoint.
    assert client.patch(f"/api/discovery/jobs/{job_id}", json={"review_status": "saved"}).status_code == 422
    assert client.patch(f"/api/discovery/jobs/{uuid.uuid4()}", json={"review_status": "later"}).status_code == 404


def test_save_creates_saved_job_without_application(client: TestClient, routes: dict, db_session: Session) -> None:
    add_sources(client, GH_SOURCE)
    run(client)
    target = next(j for j in jobs(client) if j["source_posting_id"] == "4001")
    applications_before = db_session.scalar(select(func.count()).select_from(Application))
    response = client.post(f"/api/discovery/jobs/{target['id']}/save")
    assert response.status_code == 200
    body = response.json()
    assert body["review_status"] == "saved" and body["saved_job_id"]
    saved = client.get(f"/api/jobs/{body['saved_job_id']}").json()
    assert saved["title"] == "Senior Backend Engineer"
    assert saved["posting_url"] == target["posting_url"]
    assert saved["description"].startswith("Build Python APIs.")
    # Idempotent: saving again links the same job.
    again = client.post(f"/api/discovery/jobs/{target['id']}/save").json()
    assert again["saved_job_id"] == body["saved_job_id"]
    assert db_session.scalar(select(func.count()).select_from(Job).where(Job.title == "Senior Backend Engineer")) == 1
    # Discovery never records an application.
    assert db_session.scalar(select(func.count()).select_from(Application)) == applications_before
    # A saved job can't be skipped (that would orphan the link).
    assert client.patch(f"/api/discovery/jobs/{target['id']}", json={"review_status": "skipped"}).status_code == 409


def test_save_links_existing_saved_job_with_same_url(client: TestClient, routes: dict, db_session: Session) -> None:
    add_sources(client, GH_SOURCE)
    run(client)
    target = next(j for j in jobs(client) if j["source_posting_id"] == "4002")
    existing = client.post("/api/jobs", json={"company": "Example Labs", "title": "Marketing Manager",
                                              "posting_url": target["posting_url"]}).json()
    body = client.post(f"/api/discovery/jobs/{target['id']}/save").json()
    assert body["saved_job_id"] == existing["id"]
    assert db_session.scalar(select(func.count()).select_from(Job).where(Job.posting_url == target["posting_url"])) == 1


def test_save_without_url_and_missing_job(client: TestClient, routes: dict) -> None:
    add_sources(client, GH_SOURCE)
    run(client)
    sparse = next(j for j in jobs(client) if j["source_posting_id"] == "4003")
    body = client.post(f"/api/discovery/jobs/{sparse['id']}/save").json()
    assert client.get(f"/api/jobs/{body['saved_job_id']}").json()["posting_url"] is None
    assert client.post(f"/api/discovery/jobs/{uuid.uuid4()}/save").status_code == 404


def test_deleting_source_keeps_discovered_jobs(client: TestClient, routes: dict, db_session: Session) -> None:
    [source] = add_sources(client, GH_SOURCE)
    run(client)
    client.delete(f"/api/discovery/sources/{source['id']}")
    remaining = jobs(client)
    assert len(remaining) == 3 and all(j["source_id"] is None for j in remaining)
    assert db_session.get(JobSource, uuid.UUID(source["id"])) is None


def test_concurrent_run_is_rejected(client: TestClient, routes: dict, db_session: Session) -> None:
    from app.models import DiscoveryRun

    db_session.add(DiscoveryRun(status="running", sources_checked=0, sources_failed=0, jobs_retrieved=0,
                                jobs_inserted=0, jobs_updated=0, duplicates=0, requests=0, sources=[]))
    db_session.commit()
    response = client.post("/api/discovery/runs")
    assert response.status_code == 409


def test_bad_posting_data_does_not_break_the_board(client: TestClient, routes: dict) -> None:
    huge = lever_posting(1, salaryRange={"currency": "USD", "interval": "per-year-salary", "min": 1, "max": 5_000_000_000})
    bad_port = lever_posting(2, hostedUrl="https://jobs.lever.co:abc/exampleco/2")
    routes[LEVER_URL] = [[huge, bad_port]]
    add_sources(client, GH_SOURCE, LEVER_SOURCE)
    result = run(client)
    assert result["status"] == "succeeded"
    stored = {j["source_posting_id"]: j for j in jobs(client, provider="lever")}
    assert stored["lever-1"]["salary_max"] is None
    assert stored["lever-2"]["posting_url"] is None


def test_unexpected_error_fails_one_source_only(client: TestClient, routes: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services import sync

    real = sync.persist_jobs

    def flaky(session: Session, found: list, *args: Any) -> Any:
        if found[0].provider == "lever":
            raise RuntimeError("db hiccup")
        return real(session, found, *args)

    monkeypatch.setattr(sync, "persist_jobs", flaky)
    add_sources(client, GH_SOURCE, LEVER_SOURCE)
    result = run(client)
    assert result["status"] == "partial"
    assert next(s for s in result["sources"] if s["provider"] == "lever")["error"] == "Unexpected error (RuntimeError)"
    assert len(jobs(client)) == 3  # Greenhouse jobs still stored


def test_crashed_run_is_recovered_and_finished(client: TestClient, routes: dict, db_session: Session, connection: Any) -> None:
    """A worker that dies mid-run leaves a task "running": the queue hands it out again and the run still finishes."""
    from datetime import timedelta

    from sqlalchemy import update

    from app.models import DiscoveryRun, DiscoveryTask
    from app.services import sync
    from tests.conftest import _session, drain_runs
    from tests.discovery_fixtures import fake_client

    factory = lambda: _session(connection)  # noqa: E731
    env = DiscoverySettingsEnv(min_resync_minutes=0, auto_sync_enabled=False)
    add_sources(client, GH_SOURCE)
    run_id = sync.start_run(factory, env)
    db_session.execute(update(DiscoveryTask).where(DiscoveryTask.run_id == run_id).values(
        status="running", locked_at=func.now() - timedelta(hours=1)))  # the crash
    db_session.commit()
    assert client.post("/api/discovery/runs").status_code == 409  # the unfinished run still blocks a second one
    drain_runs(factory, fake_client(routes)[0], env)
    run = client.get(f"/api/discovery/runs/{run_id}").json()
    assert (run["status"], run["tasks_done"], run["jobs_inserted"]) == ("succeeded", 1, 3)
    # A run left "running" with nothing queued (crash between the last task and closing it) is closed by the scheduler.
    db_session.execute(update(DiscoveryRun).where(DiscoveryRun.id == run_id).values(status="running"))
    db_session.commit()
    sync.scheduler_tick(factory, env)
    assert client.get(f"/api/discovery/runs/{run_id}").json()["status"] == "succeeded"
