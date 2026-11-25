"""Company registry, seed import, identity resolution, ATS verification, and the unified discovery pipeline.
All provider responses are synthetic; no real network."""

import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from datetime import timedelta

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.api.discovery import get_http_client
from app.config import DiscoverySettingsEnv, get_discovery_env
from app.main import app
from app.models import Application, Company, DiscoveredJob, JobSource, ProviderRequest
from app.services.companies import seed_companies
from tests.discovery_fixtures import (
    HIMALAYAS_URL,
    JSEARCH_URL,
    LEVER_URL,
    FakeOpener,
    fake_client,
    http_error,
    himalayas_job,
    jsearch_job,
    lever_posting,
)


@pytest.fixture
def env(client: TestClient) -> dict[str, Any]:
    settings: dict[str, Any] = {"himalayas_enabled": True, "min_resync_minutes": 0}
    app.dependency_overrides[get_discovery_env] = lambda: DiscoverySettingsEnv(**settings)
    return settings


@pytest.fixture
def net(client: TestClient, env: dict[str, Any]) -> Iterator[dict[str, Any]]:
    """routes: URL prefix -> responses. Every run uses a fresh fake client; `openers` records each one."""
    state: dict[str, Any] = {"routes": {HIMALAYAS_URL: [{"jobs": []}]}, "openers": []}

    def make() -> Any:
        c, opener, _ = fake_client(state["routes"])
        state["openers"].append(opener)
        return c

    app.dependency_overrides[get_http_client] = make
    yield state


def urls(net: dict[str, Any]) -> list[str]:
    return [u for o in net["openers"] for u in o.urls]


def run(client: TestClient) -> dict:
    response = client.post("/api/discovery/runs")
    assert response.status_code == 201, response.text
    return response.json()


def company_count(db: Session) -> int:
    return db.scalar(select(func.count()).select_from(Company)) or 0


# --- Seed ---


def test_seed_has_90_unique_equal_employers(db_session: Session) -> None:
    before = company_count(db_session)
    result = seed_companies(db_session)
    assert result["seed_entries"] == 90
    seeded = list(db_session.scalars(select(Company).where(Company.origin == "curated_seed")))
    assert len(seeded) == 90 == company_count(db_session) - before
    assert not hasattr(Company, "priority")  # no ranking: every employer is treated the same
    assert len({c.domain for c in seeded}) == 90
    assert not any(c.watchlist for c in seeded)  # suggestions, not personal favourites
    # No invented ATS identifiers: seeding creates no boards and contacts nothing.
    assert db_session.scalar(select(func.count()).select_from(JobSource)) == 0


def test_seed_is_idempotent_and_preserves_user_edits(client: TestClient, db_session: Session) -> None:
    seed_companies(db_session)
    stripe = db_session.scalar(select(Company).where(Company.domain == "stripe.com"))
    assert stripe is not None
    edited = client.patch(f"/api/companies/{stripe.id}", json={"watchlist": True, "notes": "Dream job"})
    assert edited.status_code == 200
    again = seed_companies(db_session)
    assert again == {"seed_entries": 90, "inserted": 0, "promoted": 0, "already_present": 90}
    db_session.expire_all()
    stripe = db_session.get(Company, stripe.id)
    assert (stripe.watchlist, stripe.notes) == (True, "Dream job")
    assert db_session.scalar(select(func.count()).select_from(Company).where(Company.origin == "curated_seed")) == 90


def test_seed_promotes_auto_discovered_domain_without_overwriting_edits(db_session: Session) -> None:
    db_session.add(Company(name="Stripe, Inc.", domain="stripe.com", origin="discovered", watchlist=True,
                           verification_status="provider_reported", notes="my note"))
    db_session.add(Company(name="My Plaid", domain="plaid.com", origin="manual", watchlist=False,
                           verification_status="verified", notes="mine"))
    db_session.commit()
    result = seed_companies(db_session)
    assert (result["inserted"], result["promoted"], result["already_present"]) == (88, 1, 1)
    db_session.expire_all()
    stripe = db_session.scalar(select(Company).where(Company.domain == "stripe.com"))
    assert (stripe.origin, stripe.notes, stripe.watchlist, stripe.name) == ("curated_seed", "my note", True, "Stripe, Inc.")
    plaid = db_session.scalar(select(Company).where(Company.domain == "plaid.com"))
    assert (plaid.origin, plaid.notes) == ("manual", "mine")  # manual rows are never touched
    assert db_session.scalar(select(func.count()).select_from(Company).where(Company.origin == "curated_seed")) == 89


def test_clearing_domain_unverifies_identity(client: TestClient) -> None:
    created = client.post("/api/companies", json={"name": "Temp", "domain": "temp.example.com"}).json()
    assert created["verification_status"] == "verified"
    assert client.patch(f"/api/companies/{created['id']}", json={"domain": None}).json()["verification_status"] == "unresolved"


# --- Registry API ---


def test_company_crud_filters_and_validation(client: TestClient, db_session: Session) -> None:
    seed_companies(db_session)
    assert len(client.get("/api/companies", params={"origin": "curated_seed", "limit": 200}).json()) == 90
    assert all("priority" not in c for c in client.get("/api/companies").json())
    assert [c["name"] for c in client.get("/api/companies", params={"q": "datadoghq"}).json()] == ["Datadog"]
    by_name = client.get("/api/companies", params={"sort": "name", "limit": 3}).json()
    assert [c["name"] for c in by_name] == sorted((c["name"] for c in by_name), key=str.lower)
    created = client.post("/api/companies", json={"name": "Tiny Startup", "domain": "https://WWW.TinyStartup.io/jobs"})
    assert created.status_code == 201
    body = created.json()
    assert body["domain"] == "tinystartup.io" and body["origin"] == "manual" and body["ats_status"] == "none"
    assert client.post("/api/companies", json={"name": "Dup", "domain": "tinystartup.io"}).status_code == 409
    assert client.post("/api/companies", json={"name": "Bad", "domain": "127.0.0.1"}).status_code == 422
    assert client.post("/api/companies", json={"name": "Bad", "priority": "P1"}).status_code == 422  # field removed
    assert client.patch(f"/api/companies/{uuid.uuid4()}", json={"watchlist": True}).status_code == 404
    assert len(client.get("/api/companies", params={"ats_status": "none", "limit": 200}).json()) == 91


# --- Manual ATS verification ---


def test_manual_verification_of_ats_url(client: TestClient, net: dict, db_session: Session) -> None:
    company = client.post("/api/companies", json={"name": "Example Co", "domain": "example.co"}).json()
    net["routes"]["https://api.lever.co/v0/postings/exampleco"] = [[lever_posting(1)]]
    board = client.post(f"/api/companies/{company['id']}/verify",
                        json={"url": "https://jobs.lever.co/exampleco/lever-1?lever-source=x"})
    assert board.status_code == 200, board.text
    b = board.json()
    assert (b["provider"], b["board_token"], b["verification_status"]) == ("lever", "exampleco", "verified")
    assert b["board_url"] == "https://jobs.lever.co/exampleco" and b["verification_evidence"].startswith("https://jobs.lever.co/")
    assert urls(net) == ["https://api.lever.co/v0/postings/exampleco?mode=json&limit=1"]  # one read-only API call
    detail = client.get(f"/api/companies/{company['id']}").json()
    assert detail["ats_status"] == "verified" and len(detail["boards"]) == 1


@pytest.mark.parametrize(
    "url",
    ["https://example.co/careers", "http://169.254.169.254/latest", "https://jobs.lever.co.evil.com/x",
     "https://user@jobs.lever.co/x/1", "file:///etc/passwd"],
)
def test_untrusted_verification_urls_make_no_request(client: TestClient, net: dict, url: str) -> None:
    company = client.post("/api/companies", json={"name": "Example Co"}).json()
    response = client.post(f"/api/companies/{company['id']}/verify", json={"url": url})
    assert response.status_code == 422
    assert "Not a supported ATS link" in response.json()["detail"]
    assert urls(net) == []


def test_failed_verification_keeps_company_and_is_cached(client: TestClient, net: dict) -> None:
    company = client.post("/api/companies", json={"name": "Ghost Inc"}).json()
    first = client.post(f"/api/companies/{company['id']}/verify", json={"url": "https://boards.greenhouse.io/ghostinc"}).json()
    assert first["verification_status"] == "failed" and "404" in first["last_error"]
    assert client.get(f"/api/companies/{company['id']}").status_code == 200  # company is never removed
    # A board linked to one company can't be claimed by another.
    other = client.post("/api/companies", json={"name": "Other"}).json()
    assert client.post(f"/api/companies/{other['id']}/verify", json={"url": "https://boards.greenhouse.io/ghostinc"}).status_code == 409


# --- Broad discovery pipeline ---


def test_broad_discovery_without_boards_creates_companies(client: TestClient, net: dict, db_session: Session) -> None:
    net["routes"][HIMALAYAS_URL] = [{"jobs": [
        himalayas_job(1), himalayas_job(2), himalayas_job(3, company="Beta Cloud", slug="beta-cloud"),
        himalayas_job(4, company="Gamma Data", slug="gamma-data"),
    ]}]
    assert db_session.scalar(select(func.count()).select_from(JobSource)) == 0
    result = run(client)
    assert result["status"] == "succeeded"
    assert result["companies_created"] == 3
    assert result["jobs_unique"] == 4 and result["jobs_inserted"] == 4  # 7 role queries return the same 4 jobs
    assert result["jobs_retrieved"] == 28
    assert result["requests"] == 7 == len(urls(net))
    skipped = {s["provider"]: s["error"] for s in result["sources"] if s["status"] == "skipped"}
    assert "Not configured" in skipped["jsearch"]
    assert any("Geographic coverage" in w or "JSearch isn't available" in w for w in result["warnings"])
    companies = list(db_session.scalars(select(Company).where(Company.origin == "discovered")))
    assert {c.name for c in companies} == {"Acme Remote", "Beta Cloud", "Gamma Data"}
    assert all(c.verification_status == "provider_reported" and not c.watchlist for c in companies)
    jobs = client.get("/api/discovery/jobs", params={"limit": 200}).json()
    assert all(j["company_id"] and j["company_info"]["name"] for j in jobs)
    # Idempotent re-run: no new jobs or companies.
    again = run(client)
    assert (again["jobs_inserted"], again["companies_created"]) == (0, 0)
    assert company_count(db_session) == 3


def test_coverage_reports_missing_jsearch_without_secrets(client: TestClient, env: dict) -> None:
    env.update(jsearch_enabled=True, jsearch_api_key="")  # enabled but no key = not configured
    body = client.get("/api/discovery/coverage").json()
    js = next(p for p in body["providers"] if p["provider"] == "jsearch")
    assert js["available"] is False and "Not configured" in js["detail"]
    assert any("Geographic coverage is limited" in w for w in body["warnings"])
    env.update(jsearch_api_key="super-secret")
    text = client.get("/api/discovery/coverage").text
    assert "super-secret" not in text and '"available":true' in text.replace(" ", "")


def test_jsearch_budget_counts_retries_and_stops(client: TestClient, net: dict, env: dict, db_session: Session) -> None:
    env.update(himalayas_enabled=False, jsearch_enabled=True, jsearch_api_key="k", jsearch_monthly_limit=2,
               jsearch_max_requests_per_run=5)
    net["routes"][JSEARCH_URL] = [http_error(JSEARCH_URL, 503)]
    first = run(client)
    js = next(s for s in first["sources"] if s["provider"] == "jsearch")
    assert js["requests"] == 2  # third attempt would exceed the monthly budget: never sent
    assert db_session.scalar(select(func.count()).select_from(ProviderRequest)) == 2
    assert "super" not in str(first)
    # Budget used up: JSearch is skipped without any request (and with no other source, nothing runs).
    response = client.post("/api/discovery/runs")
    assert response.status_code == 409 and "Request budget used" in response.json()["detail"]
    assert len([u for u in urls(net) if u.startswith(JSEARCH_URL)]) == 2


def test_jsearch_per_run_limit_and_query_cache(client: TestClient, net: dict, env: dict) -> None:
    env.update(himalayas_enabled=False, jsearch_enabled=True, jsearch_api_key="k", jsearch_monthly_limit=100,
               jsearch_max_requests_per_run=1, max_verifications_per_run=0)
    client.put("/api/discovery/filters", json={"role_families": ["backend"], "geographies": ["nj", "nyc"]})
    net["routes"][JSEARCH_URL] = [{"data": [jsearch_job(1)]}]
    first = run(client)
    assert first["requests"] == 1
    second = run(client)
    assert second["requests"] == 1
    js_urls = [u for u in urls(net) if u.startswith(JSEARCH_URL)]
    assert "New+Jersey" in js_urls[0] and "New+York" in js_urls[1]  # cached query skipped, next one used
    third = client.post("/api/discovery/runs")
    assert third.status_code == 409 and "cached" in third.json()["detail"]


def test_identity_resolution_by_domain_and_ambiguous_names(client: TestClient, net: dict, env: dict, db_session: Session) -> None:
    seed_companies(db_session)
    env.update(himalayas_enabled=False, jsearch_enabled=True, jsearch_api_key="k", jsearch_max_requests_per_run=1)
    client.put("/api/discovery/filters", json={"geographies": ["nj"], "role_families": ["software"]})
    net["routes"][JSEARCH_URL] = [{"data": [
        jsearch_job(1, employer_name="Stripe, Inc.", employer_website="https://www.stripe.com"),
        jsearch_job(2, employer_name="Stripe", employer_website=None, job_apply_link="https://example.org/a"),
        jsearch_job(3, employer_name="Stripe", employer_website=None, job_apply_link="https://example.org/b"),
    ]}]
    env["max_verifications_per_run"] = 0
    result = run(client)
    stripe = db_session.scalar(select(Company).where(Company.domain == "stripe.com"))
    rows = {j.source_posting_id: j for j in db_session.scalars(select(DiscoveredJob))}
    assert rows["js-1"].company_id == stripe.id  # exact domain = high-confidence match to the curated record
    # Same display name without a domain: NOT merged into curated Stripe; grouped as one unresolved record.
    assert rows["js-2"].company_id != stripe.id and rows["js-2"].company_id == rows["js-3"].company_id
    unresolved = db_session.get(Company, rows["js-2"].company_id)
    assert unresolved.verification_status == "unresolved" and unresolved.origin == "discovered"
    assert result["companies_created"] == 1


def test_ats_detection_verification_limit_and_later_monitoring(client: TestClient, net: dict, env: dict, db_session: Session) -> None:
    env.update(himalayas_enabled=False, jsearch_enabled=True, jsearch_api_key="k", jsearch_max_requests_per_run=1,
               max_verifications_per_run=1)
    client.put("/api/discovery/filters", json={"geographies": ["nj", "nyc"], "role_families": ["software"]})
    net["routes"].update({
        JSEARCH_URL: [{"data": [
            jsearch_job(1),  # jobs.lever.co/examplepay/...
            jsearch_job(2, employer_name="Other Corp", employer_website="othercorp.com",
                        job_apply_link="https://boards.greenhouse.io/othercorp/jobs/777"),
        ]}],
        "https://api.lever.co/v0/postings/examplepay": [[lever_posting(1)]],
        "https://boards-api.greenhouse.io/v1/boards/othercorp": [{"name": "Other Corp", "jobs": []}],
    })
    first = run(client)
    assert first["boards_verified"] == 1
    assert any("1 more employer boards" in w for w in first["warnings"])
    boards = list(db_session.scalars(select(JobSource)))
    # The API answered, but only an aggregator link points at the board: ownership is unproven, so it needs review.
    assert len(boards) == 1 and boards[0].verification_status == "needs_review" and boards[0].ownership_verified_at is None
    assert boards[0].company_id is not None and boards[0].verification_evidence.startswith("https://")
    # Next run verifies the second board (bounded); neither is synced into Discovery without ownership evidence.
    second = run(client)
    assert second["boards_verified"] == 1
    assert not any(s["kind"] == "board" for s in second["sources"])
    assert db_session.scalar(select(func.count()).select_from(JobSource).where(JobSource.verification_status == "needs_review")) == 2


def test_cross_provider_duplicate_keeps_review_status(client: TestClient, net: dict, env: dict, db_session: Session) -> None:
    env.update(himalayas_enabled=False)
    company = client.post("/api/companies", json={"name": "Example Co", "domain": "example.co"}).json()
    client.post("/api/discovery/sources", json={"provider": "lever", "company_name": "Example Co",
                                                "board_token": "exampleco", "company_id": company["id"]})
    net["routes"][LEVER_URL] = [[lever_posting(1)]]
    run(client)
    [original] = client.get("/api/discovery/jobs").json()
    client.patch(f"/api/discovery/jobs/{original['id']}", json={"review_status": "later"})
    # Tests share one transaction (constant now()); make the original clearly older, as separate runs would be.
    db_session.execute(update(DiscoveredJob).values(first_seen_at=func.now() - timedelta(hours=1)))
    db_session.commit()
    # The same posting arrives via an aggregator whose apply link is the Lever posting.
    env.update(jsearch_enabled=True, jsearch_api_key="k", jsearch_max_requests_per_run=1)
    net["routes"][JSEARCH_URL] = [{"data": [jsearch_job(9, employer_name="Example Co", employer_website=None,
                                                         job_apply_link="https://jobs.lever.co/exampleco/lever-1")]}]
    result = run(client)
    assert result["duplicates"] >= 1
    jobs = {j["provider"]: j for j in client.get("/api/discovery/jobs").json()}
    assert jobs["jsearch"]["duplicate_of_id"] == original["id"]
    assert jobs["jsearch"]["company_id"] == company["id"]  # identity via the known board
    assert jobs["lever"]["review_status"] == "later"  # user decision preserved


def test_partial_provider_failure_checks_every_board_overdue_first(client: TestClient, net: dict, env: dict, db_session: Session) -> None:
    env.update(min_resync_minutes=0)
    first = client.post("/api/companies", json={"name": "Curated-like", "watchlist": True}).json()
    second = client.post("/api/companies", json={"name": "Discovered-like"}).json()
    for cid, token in ((first["id"], "boarda"), (second["id"], "boardb")):
        client.post("/api/discovery/sources", json={"provider": "lever", "company_name": "x", "board_token": token, "company_id": cid})
    # boardb is overdue (its next sync was due long ago); boarda was never scheduled: never-scheduled first, then overdue.
    db_session.execute(update(JobSource).where(JobSource.board_token == "boardb").values(next_sync_at=func.now() - timedelta(days=30)))
    db_session.commit()
    net["routes"].update({
        HIMALAYAS_URL: [http_error(HIMALAYAS_URL, 500)],
        "https://api.lever.co/v0/postings/": [[lever_posting(5)]],
    })
    result = run(client)
    assert result["status"] == "partial"  # the feed failed; both boards succeeded
    assert [b["board_token"] for b in result["sources"] if b["kind"] == "board"] == ["boarda", "boardb"]
    # 3 tasks (2 boards + feeds) all finished; the failed feed is a failed source, not a lost task.
    assert (result["tasks_total"], result["tasks_done"], result["tasks_remaining"], result["sources_failed"]) == (3, 3, 0, 1)


def test_pipeline_and_save_never_create_applications(client: TestClient, net: dict, db_session: Session) -> None:
    net["routes"][HIMALAYAS_URL] = [{"jobs": [himalayas_job(1)]}]
    run(client)
    [job] = client.get("/api/discovery/jobs").json()
    saved = client.post(f"/api/discovery/jobs/{job['id']}/save").json()
    again = client.post(f"/api/discovery/jobs/{job['id']}/save").json()
    assert saved["saved_job_id"] == again["saved_job_id"]
    assert db_session.scalar(select(func.count()).select_from(Application)) == 0


def test_company_jobs_filter(client: TestClient, net: dict) -> None:
    net["routes"][HIMALAYAS_URL] = [{"jobs": [himalayas_job(1), himalayas_job(2, company="Beta Cloud", slug="beta-cloud")]}]
    run(client)
    beta = client.get("/api/companies", params={"q": "Beta"}).json()[0]
    assert beta["job_count"] == 1
    jobs = client.get("/api/discovery/jobs", params={"company_id": beta["id"]}).json()
    assert [j["company"] for j in jobs] == ["Beta Cloud"]


def test_fake_opener_is_used(net: dict) -> None:
    assert isinstance(FakeOpener({}), FakeOpener)  # guard: fixtures never touch the real network
