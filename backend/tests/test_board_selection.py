"""Board selection: every candidate within bounded limits, main vs prospect-pool boards, per-board ownership, and
deep-scan request limits. All websites and ATS responses are synthetic; no real network."""

from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.companies import get_site_fetcher
from app.api.discovery import get_http_client
from app.main import app
from app.models import DiscoveredJob, DiscoveryTask, JobSource
from app.services import career_discovery as cd
from tests.discovery_fixtures import fake_client
from tests.test_employer_discovery import GH_API, FakeSite, company, factory, summary  # noqa: F401 (fixture)

JS = {"content-type": "text/javascript"}


def gh_job(i: int, title: str) -> dict[str, Any]:
    return {"id": i, "title": title, "absolute_url": f"https://job-boards.greenhouse.io/x/jobs/{i}", "content": "&lt;p&gt;Role.&lt;/p&gt;"}


def gitlab_like_site() -> FakeSite:
    """A prospect-pool link on the jobs page; the main board only in the job list's own script."""
    return FakeSite({
        "https://examplelabs.com/": '<a href="/jobs/">Careers</a>',
        "https://examplelabs.com/jobs/": '<a href="https://job-boards.greenhouse.io/examplecrm/jobs/1" data-ga-name="join our sales talent pool">'
                                         'Sales</a><script src="/_nuxt/list.js"></script>',
        "https://examplelabs.com/_nuxt/list.js": (200, JS, b'const u="https://boards-api.greenhouse.io/v1/boards/examplelabs/departments"'),
    })


def test_all_candidates_are_collected_and_the_main_board_comes_first() -> None:
    out = cd.explore(gitlab_like_site().fetcher(), "examplelabs.com", None)
    assert [b.board for b, _ in out.boards] == ["examplelabs", "examplecrm"]  # the early minor board doesn't stop the search
    assert out.roles == {("greenhouse", "examplecrm"): "talent_pool"}
    assert any("prospect pool" in e["detail"] for e in out.evidence)


def test_boards_on_different_pages_are_all_found() -> None:
    site = FakeSite({
        "https://examplelabs.com/": '<a href="/careers">Careers</a><a href="https://jobs.lever.co/examplelabs">Jobs</a>',
        "https://examplelabs.com/careers": '<a href="https://jobs.ashbyhq.com/examplelabs-eng">Engineering roles</a>',
    })
    out = cd.explore(site.fetcher(), "examplelabs.com", None)
    assert {(b.provider, b.board) for b, _ in out.boards} == {("lever", "examplelabs"), ("ashby", "examplelabs-eng")}


def test_each_board_is_verified_independently_and_roles_are_recorded(db_session: Session, factory: Any) -> None:
    c = company(db_session)
    http, _, _ = fake_client({
        f"{GH_API}examplelabs/jobs": [{"jobs": [gh_job(1, "Backend Engineer"), gh_job(2, "SRE")]}],
        f"{GH_API}examplelabs": [{"name": "Example Labs"}],
        f"{GH_API}examplecrm/jobs": [{"jobs": [gh_job(3, "Sales Expression of Interest Form")]}],
        f"{GH_API}examplecrm": [{"name": "Example Labs Prospects"}],
    })
    cd.enqueue_discovery(db_session, [c.id])
    db_session.commit()
    results = cd.run_tasks(factory, http, gitlab_like_site().fetcher(), max_tasks=5)
    assert [r["kind"] for r in results] == ["discover", "sync", "sync"]
    boards = {b.board_token: b for b in db_session.scalars(select(JobSource))}
    for b in boards.values():
        db_session.refresh(b)
    assert all(b.ownership_verified_at for b in boards.values())
    assert (boards["examplelabs"].board_role, boards["examplecrm"].board_role) == ("general", "talent_pool")
    active = db_session.scalars(select(DiscoveredJob).where(DiscoveredJob.non_vacancy_reason.is_(None))).all()
    assert sorted(j.title for j in active) == ["Backend Engineer", "SRE"]


def test_one_conflicting_board_does_not_block_the_other(db_session: Session, factory: Any) -> None:
    c = company(db_session)
    site = FakeSite({"https://examplelabs.com/": '<a href="https://boards.greenhouse.io/examplelabs">Careers</a>'
                                                 '<a href="https://boards.greenhouse.io/globex">Partner jobs</a>'})
    http, _, _ = fake_client({f"{GH_API}examplelabs": [{"name": "Example Labs"}], f"{GH_API}globex": [{"name": "Globex Corporation"}],
                              f"{GH_API}examplelabs/jobs": [{"jobs": []}]})
    cd.enqueue_discovery(db_session, [c.id])
    db_session.commit()
    cd.run_tasks(factory, http, site.fetcher(), max_tasks=1)
    owned = {b.board_token: b.ownership_verified_at is not None for b in db_session.scalars(select(JobSource))}
    assert owned == {"examplelabs": True, "globex": False}


def test_rediscovery_keeps_existing_verified_boards_and_creates_no_duplicates(client: TestClient, db_session: Session, factory: Any) -> None:
    c = company(db_session)
    http_routes = {f"{GH_API}examplelabs/jobs": [{"jobs": [gh_job(1, "Backend Engineer")]}], f"{GH_API}examplelabs": [{"name": "Example Labs"}],
                   f"{GH_API}examplecrm/jobs": [{"jobs": []}], f"{GH_API}examplecrm": [{"name": "Example Labs"}]}
    for _ in range(2):
        cd.enqueue_discovery(db_session, [c.id], force=True)
        db_session.commit()
        cd.run_tasks(factory, fake_client(http_routes)[0], gitlab_like_site().fetcher(), max_tasks=5)
    assert summary(client)["total"] == 1
    assert db_session.scalar(select(func.count()).select_from(JobSource)) == 2
    # The site stops naming the prospect board: the board stays verified (no contradicting evidence).
    plain = FakeSite({"https://examplelabs.com/": '<a href="https://boards.greenhouse.io/examplelabs">Careers</a>'})
    cd.enqueue_discovery(db_session, [c.id], force=True)
    db_session.commit()
    cd.run_tasks(factory, fake_client(http_routes)[0], plain.fetcher(), max_tasks=5)
    crm = db_session.scalar(select(JobSource).where(JobSource.board_token == "examplecrm"))
    db_session.refresh(crm)
    assert crm.ownership_verified_at is not None and crm.verification_status == "verified"


# --- Deep scan limits ---


def many_scripts_site(n: int, board_at: int) -> FakeSite:
    tags = "".join(f'<script src="/s/{i}.js"></script>' for i in range(n))
    pages: dict[str, Any] = {"https://examplelabs.com/": '<a href="/jobs/">Careers</a>', "https://examplelabs.com/jobs/": tags}
    pages.update({f"https://examplelabs.com/s/{i}.js": (200, JS, b"noop()") for i in range(n)})
    pages[f"https://examplelabs.com/s/{board_at}.js"] = (200, JS, b'"boards-api.greenhouse.io/v1/boards/examplelabs/jobs"')
    return FakeSite(pages)


def test_standard_scan_reads_a_few_scripts_deep_scan_reads_more_within_its_cap() -> None:
    site = many_scripts_site(60, board_at=40)
    out = cd.explore(site.fetcher(), "examplelabs.com", None)
    assert out.boards == [] and sum("/s/" in u for u in site.urls) == cd.STANDARD.scripts
    assert len(site.urls) <= cd.STANDARD.max_requests
    deep = many_scripts_site(60, board_at=40)
    out = cd.explore(deep.fetcher(), "examplelabs.com", None, cd.DEEP)
    assert [b.board for b, _ in out.boards] == ["examplelabs"] and len(deep.urls) <= cd.DEEP.max_requests


def test_request_cap_stops_a_deep_scan() -> None:
    site = many_scripts_site(200, board_at=199)
    fetcher = site.fetcher()
    out = cd.explore(fetcher, "examplelabs.com", None, cd.ScanLimits(pages=2, scripts=200, sitemap_pages=0, max_bytes=cd.SITE_MAX_BYTES, boards=1, max_requests=12))
    assert len(site.urls) == 12 and out.boards == []
    assert any("Request limit" in e["detail"] for e in out.evidence)
    assert fetcher.limit_at is None  # the cap applies to this company only


def test_deep_scan_endpoint_is_per_company(client: TestClient, db_session: Session) -> None:
    c = company(db_session)
    site = many_scripts_site(30, board_at=20)
    http, _, _ = fake_client({f"{GH_API}examplelabs/jobs": [{"jobs": [gh_job(1, "SRE")]}], f"{GH_API}examplelabs": [{"name": "Example Labs"}]})
    app.dependency_overrides[get_http_client] = lambda: http
    app.dependency_overrides[get_site_fetcher] = site.fetcher
    assert client.post(f"/api/companies/{c.id}/discover").json()["discovery_status"] == "not_found"  # standard: 8 scripts
    body = client.post(f"/api/companies/{c.id}/discover", params={"deep": True}).json()
    assert body["discovery_status"] == "verified" and body["job_count"] == 1
    assert db_session.scalar(select(func.count()).select_from(DiscoveryTask).where(DiscoveryTask.kind == "discover")) == 2


def test_deep_cli_refuses_more_than_one_company(monkeypatch: pytest.MonkeyPatch) -> None:
    from scripts import career_discovery as cli

    monkeypatch.setattr("sys.argv", ["x", "--deep", "--company", "A", "--company", "B"])
    with pytest.raises(SystemExit, match="exactly one company"):
        cli.main()


def test_asset_files_under_careers_are_not_careers_pages() -> None:
    page = cd.Page("https://examplelabs.com/", '<a href="/careers/">Careers</a><script src="/careers/_astro/Consent.js"></script>'
                   '<link href="/careers/style.css"><a href="/careers/brochure.pdf">Careers brochure</a>')
    assert cd.analyze(page, "examplelabs.com").careers_links == ["https://examplelabs.com/careers/"]


def test_deep_scan_reads_larger_pages_than_the_standard_scan() -> None:
    big = b"<html>" + b" " * (cd.SITE_MAX_BYTES + 10) + b'<a href="https://jobs.lever.co/examplelabs">Jobs</a></html>'
    pages = {"https://examplelabs.com/": (200, {"content-type": "text/html"}, big)}
    assert cd.explore(FakeSite(dict(pages)).fetcher(), "examplelabs.com", None).boards == []  # standard: too large
    fetcher = FakeSite(dict(pages)).fetcher()
    out = cd.explore(fetcher, "examplelabs.com", None, cd.DEEP)
    assert [b.board for b, _ in out.boards] == ["examplelabs"] and fetcher.max_bytes == cd.SITE_MAX_BYTES  # reset after
