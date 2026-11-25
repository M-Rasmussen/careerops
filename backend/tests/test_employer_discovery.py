"""Discovery reset (generations), employer career-source discovery, ownership verification, job sync, the task
queue, and official-site request safety. All websites and ATS responses are synthetic; no real network."""

import socket
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Connection, func, select, update
from sqlalchemy.orm import Session

from app.api.companies import get_site_fetcher
from app.api.discovery import get_http_client
from app.config import DiscoverySettingsEnv, get_discovery_env
from app.main import app
from app.models import Application, Company, DiscoveredJob, DiscoveryTask, Job, JobSource
from app.services import career_discovery as cd
from app.services.discovery import active_generation, archive_active_generation, persist_jobs
from app.services.job_sources import NormalizedJob
from tests.conftest import _session
from tests.discovery_fixtures import GREENHOUSE, fake_client, lever_posting

PUBLIC_IP = "93.184.216.34"
GH_API = "https://boards-api.greenhouse.io/v1/boards/"
LEVER_API = "https://api.lever.co/v0/postings/"
LEVER_EU_API = "https://api.eu.lever.co/v0/postings/"
ASHBY_API = "https://api.ashbyhq.com/posting-api/job-board/"


class FakeSite:
    """url -> (status, headers, body) or an Exception. Unknown URLs answer 404. Records every request."""

    def __init__(self, pages: dict[str, Any], ips: dict[str, list[str]] | None = None) -> None:
        self.pages, self.ips, self.urls = pages, ips or {}, []

    def open(self, url: str, host: str, ip: str) -> tuple[int, dict[str, str], bytes]:
        self.urls.append(url)
        item = self.pages.get(url, (404, {}, b""))
        if isinstance(item, Exception):
            raise item
        if isinstance(item, str):
            return 200, {"content-type": "text/html; charset=utf-8"}, item.encode()
        return item

    def resolve(self, host: str, port: int, **kw: Any) -> list[Any]:
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port)) for ip in self.ips.get(host, [PUBLIC_IP])]

    def fetcher(self) -> cd.SiteFetcher:
        return cd.SiteFetcher(opener=self.open, resolve=self.resolve, sleep=lambda s: None)


@pytest.fixture
def factory(connection: Connection) -> Callable[[], Session]:
    return lambda: _session(connection)


def company(db: Session, name: str = "Example Labs", domain: str | None = "examplelabs.com", **kw: Any) -> Company:
    c = Company(name=name, domain=domain, origin="curated_seed" if domain else "discovered", watchlist=False,
                verification_status="verified" if domain else "unresolved", **kw)
    db.add(c)
    db.commit()
    return c


def legacy_job(db: Session, **kw: Any) -> DiscoveredJob:
    n = uuid.uuid4().hex[:8]
    values: dict[str, Any] = dict(provider="himalayas", source_posting_id=n, company="Example Labs", title="Backend Engineer",
                                  filter_result="match", filter_reasons=["ok"], review_status="new", review_history=[],
                                  generation=1, appearances=[{"generation": 1, "via": "legacy"}],
                                  first_seen_at=datetime.now(timezone.utc) - timedelta(days=10))
    values.update(kw)
    job = DiscoveredJob(**values)
    db.add(job)
    db.commit()
    return job


def summary(client: TestClient, **params: Any) -> dict:
    r = client.get("/api/discovery/jobs/summary", params={"hide_duplicates": True, **params})
    assert r.status_code == 200, r.text
    return r.json()


def nj(provider: str, posting: str, **kw: Any) -> NormalizedJob:
    values: dict[str, Any] = dict(provider=provider, source_posting_id=posting, company="Example Labs", title="Backend Engineer",
                                  location=None, workplace_type=None, description=None, posting_url=None, canonical_url=None,
                                  salary_min=None, salary_max=None, salary_currency=None, salary_interval=None,
                                  source_updated_at=None)
    values.update(kw)
    return NormalizedJob(**values)


# --- Reset / generations ---


def test_archive_empties_discovery_and_preserves_everything(client: TestClient, db_session: Session) -> None:
    saved_job = Job(company="Example Labs", title="Saved Engineer", posting_url="https://example.test/saved")
    db_session.add(saved_job)
    db_session.flush()
    db_session.add(Application(job_id=saved_job.id, status="applied", applied_date=datetime(2026, 11, 2).date()))
    history = [{"action": "saved", "at": "2026-11-01T00:00:00+00:00"}]
    saved = legacy_job(db_session, review_status="saved", saved_job_id=saved_job.id, review_history=history,
                       ats_job_key="greenhouse:77", canonical_url="https://boards.greenhouse.io/examplelabs/jobs/77")
    legacy_job(db_session, review_status="skipped")
    assert summary(client)["total"] == 2

    result = archive_active_generation(db_session, "Employer pilot")
    assert result["archived_jobs"] == 2 and result["active_generation"] > result["archived_generation"]
    assert summary(client) == {"total": 0, "matching": 0, "by_result": {"match": 0, "unknown": 0, "rejected": 0},
                               "by_source": {}, "archived": 2, "non_vacancies": 0, "closed": 0}
    assert client.get("/api/discovery/jobs").json() == []
    # Nothing deleted or changed: identity, review decisions, saved link, Saved Jobs, and applications.
    db_session.refresh(saved)
    assert (saved.review_status, saved.saved_job_id, saved.review_history, saved.ats_job_key) == ("saved", saved_job.id, history, "greenhouse:77")
    assert db_session.scalar(select(func.count()).select_from(DiscoveredJob)) == 2
    assert [j["title"] for j in client.get("/api/jobs").json()] == ["Saved Engineer"]
    assert db_session.scalar(select(func.count()).select_from(Application)) == 1
    # The saved discovered job is still reachable by id (history), just not listed.
    assert client.get(f"/api/discovery/jobs/{saved.id}").status_code == 200


def test_rediscovered_archived_posting_is_reused_with_a_new_appearance(client: TestClient, db_session: Session) -> None:
    old = legacy_job(db_session, provider="greenhouse", source_posting_id="4001", review_status="later")
    first_seen = old.first_seen_at
    archive_active_generation(db_session, "pilot")
    gen = active_generation(db_session)
    stats, _ = persist_jobs(db_session, [nj("greenhouse", "4001", title="Backend Engineer (updated)")], {}, None, None, gen, "employer_ats")
    db_session.commit()
    db_session.refresh(old)
    assert stats["inserted"] == 0 and stats["updated"] == 1  # same identity, no duplicate row
    assert old.generation == gen and old.first_seen_at == first_seen and old.review_status == "later"
    assert [a["via"] for a in old.appearances] == ["legacy", "employer_ats"]  # original appearance kept
    assert summary(client)["total"] == 1
    # Syncing again in the same generation adds no new appearance.
    persist_jobs(db_session, [nj("greenhouse", "4001")], {}, None, None, gen, "employer_ats")
    db_session.commit()
    db_session.refresh(old)
    assert len(old.appearances) == 2


def test_employer_copy_of_an_archived_aggregator_job_is_one_job_shown_as_employer(client: TestClient, db_session: Session) -> None:
    feed = legacy_job(db_session, provider="himalayas", ats_job_key="greenhouse:4001", posting_url="https://himalayas.app/x")
    archive_active_generation(db_session, "pilot")
    persist_jobs(db_session, [nj("greenhouse", "4001", ats_job_key="greenhouse:4001",
                                 posting_url="https://boards.greenhouse.io/examplelabs/jobs/4001")],
                 {}, None, None, active_generation(db_session), "employer_ats")
    db_session.commit()
    [card] = client.get("/api/discovery/jobs", params={"hide_duplicates": True}).json()
    assert card["provider"] == "greenhouse" and card["duplicate_of_id"] == str(feed.id)  # linked to the old identity
    assert card["posting_link"] == {"url": "https://boards.greenhouse.io/examplelabs/jobs/4001", "kind": "employer", "via": "greenhouse"}
    assert summary(client)["by_source"] == {"greenhouse": 1}
    db_session.refresh(feed)
    assert feed.generation == 1 and feed.provider == "himalayas"  # never relabelled as employer-sourced


def test_stale_writer_cannot_repopulate_or_demote(client: TestClient, db_session: Session) -> None:
    old_gen = active_generation(db_session)
    archive_active_generation(db_session, "pilot")
    new_gen = active_generation(db_session)
    # A run that started before the archive writes its (old) generation: invisible in Discovery.
    persist_jobs(db_session, [nj("himalayas", "h1")], {}, None, None, old_gen, "aggregator")
    db_session.commit()
    assert summary(client)["total"] == 0
    # ...and it can't pull an active posting back into the archive.
    persist_jobs(db_session, [nj("greenhouse", "g1")], {}, None, None, new_gen, "employer_ats")
    persist_jobs(db_session, [nj("greenhouse", "g1")], {}, None, None, old_gen, "aggregator")
    db_session.commit()
    assert summary(client)["total"] == 1


def test_himalayas_is_off_by_default_and_legacy_boards_are_not_synced(client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DISCOVERY_HIMALAYAS_ENABLED", raising=False)
    assert get_discovery_env().himalayas_enabled is False and DiscoverySettingsEnv().himalayas_enabled is False
    c = company(db_session)
    # A board that only answered its API (no ownership evidence) is never synced into Discovery.
    db_session.add(JobSource(provider="greenhouse", board_token="examplelabs", company_name=c.name, company_id=c.id,
                             verification_status="needs_review", enabled=True))
    db_session.commit()
    app.dependency_overrides[get_discovery_env] = lambda: DiscoverySettingsEnv()
    r = client.post("/api/discovery/runs")
    assert r.status_code == 409 and "No discovery sources" in r.json()["detail"]


def test_saving_an_employer_copy_reuses_the_saved_job_of_its_archived_twin(client: TestClient, db_session: Session) -> None:
    saved_job = Job(company="Example Labs", title="Backend Engineer", posting_url="https://himalayas.app/x")
    db_session.add(saved_job)
    db_session.flush()
    feed = legacy_job(db_session, ats_job_key="greenhouse:4001", review_status="saved", saved_job_id=saved_job.id)
    archive_active_generation(db_session, "pilot")
    persist_jobs(db_session, [nj("greenhouse", "4001", ats_job_key="greenhouse:4001", posting_url="https://boards.greenhouse.io/x/jobs/4001")],
                 {}, None, None, active_generation(db_session), "employer_ats")
    db_session.commit()
    [card] = client.get("/api/discovery/jobs").json()
    assert client.post(f"/api/discovery/jobs/{card['id']}/save").json()["saved_job_id"] == str(feed.saved_job_id)
    assert db_session.scalar(select(func.count()).select_from(Job)) == 1


# --- Page analysis ---


def page(url: str, body: str) -> cd.Page:
    return cd.Page(url, body)


def test_careers_link_and_ats_detection_for_each_provider() -> None:
    home = page("https://examplelabs.com/", """
        <nav><a href="/about">About</a><a href="/company/careers">Careers</a><a href="https://other.com/jobs">Jobs</a>
        <a href="mailto:jobs@examplelabs.com">Email</a></nav>""")
    found = cd.analyze(home, "examplelabs.com")
    assert found.careers_links == ["https://examplelabs.com/company/careers"] and found.boards == []

    careers = page("https://examplelabs.com/company/careers", """
        <script src="https://boards.greenhouse.io/embed/job_board/js?for=examplelabs"></script>
        <a href="https://jobs.lever.co/examplelabs/abc-123">Role</a>
        <a href="https://jobs.eu.lever.co/examplelabs-eu">EU roles</a>
        <iframe src="https://jobs.ashbyhq.com/example-labs/embed"></iframe>
        <script>window.cfg = {"url":"https:\\/\\/boards.greenhouse.io\\/examplelabs"};</script>""")
    found = cd.analyze(careers, "examplelabs.com")
    assert [(b.provider, b.board) for b in found.boards] == [
        ("greenhouse", "examplelabs"), ("lever", "examplelabs"), ("lever_eu", "examplelabs-eu"), ("ashby", "example-labs")]


def test_unsupported_ats_is_recognised() -> None:
    found = cd.analyze(page("https://examplelabs.com/careers", '<a href="https://examplelabs.wd5.myworkdayjobs.com/External">Open roles</a>'),
                       "examplelabs.com")
    assert found.unsupported == [("Workday", "https://examplelabs.wd5.myworkdayjobs.com/External")] and found.boards == []


# --- Company discovery end to end ---


def gh_routes(board: str = "examplelabs") -> dict[str, list[Any]]:
    return {f"{GH_API}{board}/jobs": [GREENHOUSE], f"{GH_API}{board}": [{"name": "Example Labs"}]}


def test_discovery_verifies_website_careers_and_board_then_imports_jobs(client: TestClient, db_session: Session, factory: Any) -> None:
    c = company(db_session)
    site = FakeSite({
        "https://examplelabs.com/": '<a href="/careers">Join us</a>',
        "https://examplelabs.com/careers": '<a href="https://boards.greenhouse.io/examplelabs">See open roles</a>',
    })
    http, opener, _ = fake_client(gh_routes())
    archive_active_generation(db_session, "pilot")
    assert cd.enqueue_discovery(db_session, [c.id]) == 1
    db_session.commit()
    results = cd.run_tasks(factory, http, site.fetcher(), max_tasks=5)
    assert [r["kind"] for r in results] == ["discover", "sync"] and results[1]["inserted"] == 3
    db_session.expire_all()
    c = db_session.get(Company, c.id)
    assert (c.discovery_status, c.careers_url, c.website_verified_at is not None) == ("verified", "https://examplelabs.com/careers", True)
    [board] = db_session.scalars(select(JobSource))
    assert board.ownership_verified_at and "examplelabs.com/careers" in board.ownership_evidence
    assert board.verification_status == "verified" and board.last_success_at is not None
    # Official ATS requests only; the board identifier came from the page, never from the name.
    assert all(u.startswith(GH_API) for u in opener.urls)
    assert set(site.urls) == {"https://examplelabs.com/robots.txt", "https://examplelabs.com/", "https://examplelabs.com/careers"}
    s = summary(client)
    assert (s["total"], s["by_source"]) == (3, {"greenhouse": 3})
    job = next(j for j in client.get("/api/discovery/jobs").json() if j["source_posting_id"] == "4001")
    # Greenhouse gives a posting page, not an application form: no application URL is claimed.
    assert job["application_url"] is None and job["posting_link"]["kind"] == "employer"
    assert job["posting_link"]["url"].startswith("https://boards.greenhouse.io/examplelabs/jobs/4001")

    # Idempotent: syncing again creates nothing new.
    cd.enqueue_discovery(db_session, [c.id], force=True)
    db_session.commit()
    again = cd.run_tasks(factory, fake_client(gh_routes())[0], site.fetcher(), max_tasks=5)
    assert again[-1]["kind"] == "sync" and again[-1]["inserted"] == 0
    assert summary(client)["total"] == 3


def test_lever_application_url_comes_from_the_provider(client: TestClient, db_session: Session, factory: Any) -> None:
    c = company(db_session)
    site = FakeSite({"https://examplelabs.com/": '<a href="https://jobs.lever.co/exampleco">Careers</a>'})
    http, _, _ = fake_client({f"{LEVER_API}exampleco": [[lever_posting(1, applyUrl="https://jobs.lever.co/exampleco/lever-1/apply"),
                                                         lever_posting(2)]]})
    cd.enqueue_discovery(db_session, [c.id])
    db_session.commit()
    cd.run_tasks(factory, http, site.fetcher(), max_tasks=5)
    jobs = {j["source_posting_id"]: j for j in client.get("/api/discovery/jobs").json()}
    assert jobs["lever-1"]["application_url"] == "https://jobs.lever.co/exampleco/lever-1/apply"
    assert jobs["lever-2"]["application_url"] is None  # missing: never constructed from the id
    assert jobs["lever-2"]["posting_link"]["url"] == "https://jobs.lever.co/exampleco/lever-2"


def test_multiple_boards_per_employer(db_session: Session, factory: Any) -> None:
    c = company(db_session)
    site = FakeSite({"https://examplelabs.com/": '<a href="https://jobs.lever.co/examplelabs">US jobs</a>'
                                                 '<a href="https://jobs.ashbyhq.com/examplelabs-eu">EU jobs</a>'})
    http, _, _ = fake_client({f"{LEVER_API}examplelabs": [[]], f"{ASHBY_API}examplelabs-eu": [{"jobs": []}]})
    cd.enqueue_discovery(db_session, [c.id])
    db_session.commit()
    cd.run_tasks(factory, http, site.fetcher(), max_tasks=1)
    boards = {b.provider: b for b in db_session.scalars(select(JobSource).where(JobSource.company_id == c.id))}
    assert set(boards) == {"lever", "ashby"} and all(b.ownership_verified_at for b in boards.values())
    assert db_session.scalar(select(func.count()).select_from(DiscoveryTask).where(DiscoveryTask.kind == "sync")) == 2


def test_lever_eu_board(db_session: Session, factory: Any) -> None:
    c = company(db_session)
    site = FakeSite({"https://examplelabs.com/": '<a href="https://jobs.eu.lever.co/examplelabs">Careers</a>'})
    http, opener, _ = fake_client({f"{LEVER_EU_API}examplelabs": [[]]})
    cd.enqueue_discovery(db_session, [c.id])
    db_session.commit()
    cd.run_tasks(factory, http, site.fetcher(), max_tasks=1)
    [board] = db_session.scalars(select(JobSource))
    assert board.provider == "lever_eu" and opener.urls[0].startswith(LEVER_EU_API)


def test_company_without_verified_domain_needs_review_without_any_request(db_session: Session, factory: Any) -> None:
    c = company(db_session, name="Name Only Co", domain=None)
    site = FakeSite({})
    http, opener, _ = fake_client({})
    cd.enqueue_discovery(db_session, [c.id])
    db_session.commit()
    [result] = cd.run_tasks(factory, http, site.fetcher(), max_tasks=5)
    assert result["status"] == "needs_review" and "never guessed" in result["error"]
    assert site.urls == [] and opener.urls == []  # no namecompany.com guess, no ATS guess


def test_unsupported_ats_keeps_the_company(db_session: Session, factory: Any) -> None:
    c = company(db_session)
    site = FakeSite({"https://examplelabs.com/": '<a href="https://examplelabs.wd1.myworkdayjobs.com/en-US/careers">Careers</a>'})
    cd.enqueue_discovery(db_session, [c.id])
    db_session.commit()
    [result] = cd.run_tasks(factory, fake_client({})[0], site.fetcher(), max_tasks=5)
    db_session.expire_all()
    c = db_session.get(Company, c.id)
    assert (result["status"], c.unsupported_ats, c.careers_url) == ("unsupported_ats", "Workday", "https://examplelabs.wd1.myworkdayjobs.com/en-US/careers")
    assert db_session.scalar(select(func.count()).select_from(JobSource)) == 0


def test_board_already_owned_by_another_company_is_not_reassigned(db_session: Session, factory: Any) -> None:
    other = company(db_session, name="Other Co", domain="other.com")
    db_session.add(JobSource(provider="greenhouse", board_token="examplelabs", company_name="Other Co", company_id=other.id,
                             verification_status="verified", enabled=True))
    c = company(db_session)
    site = FakeSite({"https://examplelabs.com/": '<a href="https://boards.greenhouse.io/examplelabs">Careers</a>'})
    http, opener, _ = fake_client(gh_routes())
    cd.enqueue_discovery(db_session, [c.id])
    db_session.commit()
    [result] = cd.run_tasks(factory, http, site.fetcher(), max_tasks=5)
    assert result["status"] == "needs_review" and opener.urls == []
    [board] = db_session.scalars(select(JobSource))
    assert board.company_id == other.id and board.ownership_verified_at is None


def test_board_that_does_not_answer_is_not_verified(db_session: Session, factory: Any) -> None:
    c = company(db_session)
    site = FakeSite({"https://examplelabs.com/": '<a href="https://boards.greenhouse.io/examplelabs">Careers</a>'})
    cd.enqueue_discovery(db_session, [c.id])
    db_session.commit()
    [result] = cd.run_tasks(factory, fake_client({})[0], site.fetcher(), max_tasks=5)  # API answers 404
    assert result["status"] == "needs_review"
    [board] = db_session.scalars(select(JobSource))
    assert board.verification_status == "failed" and board.ownership_verified_at is None
    assert db_session.scalar(select(func.count()).select_from(DiscoveryTask).where(DiscoveryTask.kind == "sync")) == 0


def test_offsite_links_and_redirects_are_not_followed() -> None:
    site = FakeSite({
        "https://examplelabs.com/": (301, {"location": "https://evil.example/"}, b""),
    })
    out = cd.explore(site.fetcher(), "examplelabs.com", None)
    assert out.status == "needs_review" and "Left the official domain" in out.error
    assert "https://evil.example/" not in site.urls


def test_guessed_careers_path_is_used_only_when_nothing_is_linked() -> None:
    site = FakeSite({
        "https://examplelabs.com/": "<p>Welcome</p>",
        "https://examplelabs.com/careers": '<a href="https://jobs.ashbyhq.com/examplelabs">Openings</a>',
    })
    out = cd.explore(site.fetcher(), "examplelabs.com", None)
    assert [(b.provider, b.board) for b, _ in out.boards] == [("ashby", "examplelabs")]
    assert any("not linked" in e["detail"] for e in out.evidence)


def test_manual_careers_url_on_ats_and_manual_board_correction(client: TestClient, db_session: Session, net: Any = None) -> None:
    c = company(db_session, name="Manual Co", domain=None)
    out = cd.explore(FakeSite({}).fetcher(), None, "https://jobs.lever.co/manualco")
    assert out.status == "verified" and out.boards[0][0].board == "manualco"
    http, _, _ = fake_client({f"{LEVER_API}manualco": [[]]})
    app.dependency_overrides[get_http_client] = lambda: http
    r = client.post(f"/api/companies/{c.id}/verify", json={"url": "https://jobs.lever.co/manualco/abc"})
    assert r.status_code == 200, r.text
    assert r.json()["ownership_evidence"].startswith("Confirmed by the user") and r.json()["verification_status"] == "verified"
    assert db_session.scalar(select(func.count()).select_from(DiscoveryTask).where(DiscoveryTask.kind == "sync")) == 1


def test_retry_discovery_endpoint(client: TestClient, db_session: Session) -> None:
    c = company(db_session)
    site = FakeSite({"https://examplelabs.com/": '<a href="https://boards.greenhouse.io/examplelabs">Careers</a>'})
    http, _, _ = fake_client(gh_routes())
    app.dependency_overrides[get_http_client] = lambda: http
    app.dependency_overrides[get_site_fetcher] = site.fetcher
    r = client.post(f"/api/companies/{c.id}/discover")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["discovery_status"] == "verified" and body["job_count"] == 3 and body["last_sync_at"]
    assert body["boards"][0]["ownership_verified_at"] and body["boards"][0]["region"] == "Global"
    assert client.post(f"/api/companies/{uuid.uuid4()}/discover").status_code == 404


# --- Queue ---


def test_duplicate_tasks_are_prevented_and_recent_lookups_skipped(db_session: Session) -> None:
    c = company(db_session)
    assert cd.enqueue_discovery(db_session, [c.id, c.id]) == 1
    assert cd.enqueue_discovery(db_session, [c.id], force=True) == 0  # one queued task per company
    db_session.execute(update(DiscoveryTask).values(status="done"))
    c.discovery_checked_at = datetime.now(timezone.utc)
    db_session.commit()
    assert cd.enqueue_discovery(db_session, [c.id]) == 0  # checked recently (failed lookups included)
    assert cd.enqueue_discovery(db_session, [c.id], force=True) == 1  # manual retry


def test_crashed_running_task_is_recovered(db_session: Session, factory: Any) -> None:
    c = company(db_session, domain=None)
    db_session.add(DiscoveryTask(kind="discover", company_id=c.id, status="running", attempts=1,
                                 locked_at=datetime.now(timezone.utc) - timedelta(hours=1)))
    db_session.commit()
    [result] = cd.run_tasks(factory, fake_client({})[0], FakeSite({}).fetcher(), max_tasks=5)
    assert result["kind"] == "discover"
    assert db_session.scalar(select(DiscoveryTask.status)) == "done"


def test_transient_failures_retry_with_backoff_then_stop(db_session: Session, factory: Any) -> None:
    c = company(db_session)
    site = FakeSite({"https://examplelabs.com/": TimeoutError(), "https://examplelabs.com/robots.txt": TimeoutError()})
    cd.enqueue_discovery(db_session, [c.id])
    db_session.commit()
    [r1] = cd.run_tasks(factory, fake_client({})[0], site.fetcher(), max_tasks=5)
    task = db_session.scalar(select(DiscoveryTask))
    db_session.refresh(task)
    assert r1["status"] == "failed" and task.status == "queued" and task.run_after > datetime.now(timezone.utc)
    assert cd.run_tasks(factory, fake_client({})[0], site.fetcher(), max_tasks=5) == []  # not due yet: no re-probe
    for _ in range(cd.MAX_ATTEMPTS - 1):
        db_session.execute(update(DiscoveryTask).values(run_after=func.now() - timedelta(minutes=1)))
        db_session.commit()
        cd.run_tasks(factory, fake_client({})[0], site.fetcher(), max_tasks=5)
    db_session.refresh(task)
    assert (task.status, task.attempts) == ("failed", cd.MAX_ATTEMPTS)


def test_one_failing_company_does_not_stop_the_next(db_session: Session, factory: Any) -> None:
    bad, good = company(db_session, name="Bad", domain="bad.com"), company(db_session)
    site = FakeSite({"https://bad.com/": (403, {"content-type": "text/html"}, b""),
                     "https://examplelabs.com/": '<a href="https://boards.greenhouse.io/examplelabs">Careers</a>'})
    cd.enqueue_discovery(db_session, [bad.id, good.id])
    db_session.commit()
    results = cd.run_tasks(factory, fake_client(gh_routes())[0], site.fetcher(), max_tasks=5)
    assert {r["status"] for r in results} >= {"needs_review", "verified", "synced"}


# --- Official-site request safety (SSRF, robots, size, rate) ---


@pytest.mark.parametrize("url, message", [
    ("ftp://examplelabs.com/", r"Only plain http\(s\)"),
    ("https://user:pw@examplelabs.com/", r"Only plain http\(s\)"),
    ("https://examplelabs.com:8443/", "Only ports 80 and 443"),
    ("https://127.0.0.1/", "IP-address URLs"),
    ("https://[::1]/", "IP-address URLs"),
    ("https://localhost/", "Not a public hostname"),
])
def test_unsafe_urls_are_refused_before_any_request(url: str, message: str) -> None:
    site = FakeSite({})
    with pytest.raises(cd.SiteError, match=message):
        site.fetcher().get(url, "examplelabs.com")
    assert site.urls == []


@pytest.mark.parametrize("ip", ["127.0.0.1", "10.0.0.5", "169.254.169.254", "192.168.1.1", "100.64.0.1", "::1", "fd00::1"])
def test_hosts_resolving_to_private_addresses_are_refused(ip: str) -> None:
    site = FakeSite({}, ips={"examplelabs.com": [PUBLIC_IP, ip]})  # one private answer is enough to refuse
    with pytest.raises(cd.SiteError, match="non-public address"):
        site.fetcher().get("https://examplelabs.com/", "examplelabs.com")
    assert site.urls == []


def test_redirect_to_a_private_address_is_refused() -> None:
    site = FakeSite({"https://examplelabs.com/": (302, {"location": "https://internal.examplelabs.com/"}, b"")},
                    ips={"internal.examplelabs.com": ["10.1.2.3"]})
    with pytest.raises(cd.SiteError, match="non-public"):
        site.fetcher().get("https://examplelabs.com/", "examplelabs.com")


def test_robots_txt_is_honoured() -> None:
    site = FakeSite({"https://examplelabs.com/robots.txt": (200, {"content-type": "text/plain"}, b"User-agent: *\nDisallow: /careers\n"),
                     "https://examplelabs.com/careers": "<p>x</p>"})
    with pytest.raises(cd.SiteError, match="robots.txt disallows"):
        site.fetcher().get("https://examplelabs.com/careers", "examplelabs.com")
    assert "https://examplelabs.com/careers" not in site.urls


def test_large_and_non_html_responses_are_refused() -> None:
    big = FakeSite({"https://examplelabs.com/": (200, {"content-type": "text/html"}, b"x" * (cd.SITE_MAX_BYTES + 1))})
    with pytest.raises(cd.SiteError, match="too large"):
        big.fetcher().get("https://examplelabs.com/", "examplelabs.com")
    pdf = FakeSite({"https://examplelabs.com/": (200, {"content-type": "application/pdf"}, b"%PDF")})
    with pytest.raises(cd.SiteError, match="Not an expected page type"):
        pdf.fetcher().get("https://examplelabs.com/", "examplelabs.com")


def test_requests_to_one_host_are_spaced() -> None:
    sleeps: list[float] = []
    site = FakeSite({"https://examplelabs.com/": "<p>a</p>", "https://examplelabs.com/b": "<p>b</p>"})
    f = cd.SiteFetcher(opener=site.open, resolve=site.resolve, sleep=sleeps.append, clock=lambda: 100.0)
    f.get("https://examplelabs.com/", "examplelabs.com")
    f.get("https://examplelabs.com/b", "examplelabs.com")
    assert sleeps and all(s == cd.SITE_HOST_INTERVAL for s in sleeps)


def test_pinned_connection_uses_the_checked_ip_and_keeps_tls_hostname(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    class Ctx:
        def wrap_socket(self, sock: Any, server_hostname: str) -> Any:
            seen["sni"] = server_hostname
            return sock

    monkeypatch.setattr(cd.socket, "create_connection", lambda addr, timeout=None: seen.setdefault("addr", addr) and object())
    monkeypatch.setattr(cd.ssl, "create_default_context", lambda: Ctx())
    conn = cd._PinnedHTTPS("examplelabs.com", PUBLIC_IP, timeout=1)
    conn.connect()
    assert seen == {"addr": (PUBLIC_IP, 443), "sni": "examplelabs.com"}


def test_rate_limited_sync_backs_off_and_failed_boards_are_not_synced(client: TestClient, db_session: Session, factory: Any) -> None:
    c = company(db_session)
    board = JobSource(provider="greenhouse", board_token="examplelabs", company_name=c.name, company_id=c.id, enabled=True,
                      verification_status="verified", ownership_verified_at=datetime.now(timezone.utc))
    db_session.add(board)
    db_session.commit()
    from tests.discovery_fixtures import http_error
    http, _, _ = fake_client({f"{GH_API}examplelabs/jobs": [http_error(f"{GH_API}examplelabs/jobs", 429)]})
    from app.services.companies import enqueue_sync
    enqueue_sync(db_session, board.id)
    db_session.commit()
    [result] = cd.run_tasks(factory, http, FakeSite({}).fetcher(), max_tasks=1)
    db_session.refresh(board)
    # Not retried in a tight loop: the board waits at least an hour after a 429 (other boards are unaffected).
    assert result["error"] == "HTTP 429" and board.sync_failures == 1
    assert board.next_sync_at >= datetime.now(timezone.utc) + timedelta(minutes=59)
    board.verification_status = "failed"
    db_session.commit()
    app.dependency_overrides[get_discovery_env] = lambda: DiscoverySettingsEnv()
    assert client.post("/api/discovery/runs").status_code == 409  # no syncable board left

def test_careers_page_timeout_is_temporary_not_not_found() -> None:
    site = FakeSite({"https://examplelabs.com/": '<a href="/careers">Careers</a>', "https://examplelabs.com/careers": TimeoutError()})
    out = cd.explore(site.fetcher(), "examplelabs.com", None)
    assert (out.status, out.transient) == ("failed", True) and "will retry" in out.error


def test_postings_sharing_a_requisition_on_one_board_are_separate_jobs(client: TestClient, db_session: Session) -> None:
    c = company(db_session)
    gen = active_generation(db_session)
    persist_jobs(db_session, [nj("greenhouse", "1", title="Software Engineer, Backend", requisition_id="R-9"),
                              nj("greenhouse", "2", title="Software Engineer, Connect", requisition_id="R-9")],
                 {}, None, c.id, gen, "employer_ats")
    db_session.commit()
    assert summary(client)["total"] == 2
    db_session.execute(update(DiscoveredJob).values(first_seen_at=func.now() - timedelta(hours=1)))  # one shared transaction
    db_session.commit()
    # The same requisition through another source is still one job.
    persist_jobs(db_session, [nj("jsearch", "x", title="Software Engineer, Backend", requisition_id="R-9")], {}, None, c.id, gen, "aggregator")
    db_session.commit()
    assert summary(client)["total"] == 2


def test_workday_board_records_ownership_but_is_never_synced(client: TestClient, db_session: Session, factory: Any) -> None:
    c = company(db_session, name="Embark Example", domain="embark.example")
    board = JobSource(provider="workday", board_token="embarkexample", ats_host="embarkexample.wd1.myworkdayjobs.com",
                      career_site="careers", company_name=c.name, company_id=c.id, enabled=False,
                      verification_status="verified", ownership_verified_at=datetime.now(timezone.utc), ownership_evidence="Confirmed by the user")
    db_session.add(board)
    db_session.commit()
    [read] = client.get(f"/api/companies/{c.id}").json()["boards"]
    assert read["board_url"] == "https://embarkexample.wd1.myworkdayjobs.com/careers" and read["ingestion_supported"] is False
    # Even if enabled, no run or task ever requests it.
    board.enabled = True
    db_session.commit()
    app.dependency_overrides[get_discovery_env] = lambda: DiscoverySettingsEnv()
    assert client.post("/api/discovery/runs").status_code == 409
    from app.services.companies import enqueue_sync
    enqueue_sync(db_session, board.id)
    db_session.commit()
    http, opener, _ = fake_client({})
    [result] = cd.run_tasks(factory, http, FakeSite({}).fetcher(), max_tasks=1)
    assert "no workday adapter" in result["error"] and opener.urls == []
    # The database refuses a malformed Workday host.
    bad = JobSource(provider="workday", board_token="x", ats_host="evil.example.com", career_site="s", company_name="x", enabled=False)
    db_session.add(bad)
    with pytest.raises(Exception):
        db_session.commit()
    db_session.rollback()
