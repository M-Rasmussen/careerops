"""Quality pass: non-vacancy exclusion, JavaScript/structured-data ATS detection, ownership conflicts, company merges,
and domain-change candidates. All websites and ATS responses are synthetic; no real network."""

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.models import Application, Company, CompanyAlias, DiscoveredJob, DiscoveryTask, Job, JobSource
from app.services import career_discovery as cd
from app.services.companies import ConflictError, merge_companies, names_consistent
from app.services.discovery import active_generation, persist_jobs, reclassify_non_vacancies
from app.services.job_insights import non_vacancy_reason
from tests.discovery_fixtures import fake_client
from tests.test_employer_discovery import GH_API, FakeSite, company, factory, nj, summary  # noqa: F401 (fixture)

JS = {"content-type": "text/javascript"}
EOI = "This is a future opportunity that may not be immediately available, but we're proactively building our pipeline."


# --- Non-vacancy classification ---


@pytest.mark.parametrize("title, description, source, excluded", [
    ("Backend Engineering Expression of Interest Form", EOI, "GitLab Prospects", True),
    ("Leadership at GitLab", "We're excited to stay in touch and build a relationship with you, whether that leads to a "
     "conversation or positions us to move quickly when the perfect opportunity aligns.", "GitLab Prospects", True),
    ("Join our Talent Community", None, "Acme", True),
    ("Data Engineer", "This posting is not a current opening; we review applications for future roles.", "Acme", True),
    # Real jobs that merely resemble prospect listings stay.
    ("Sales Development Representative", EOI, "GitLab", False),  # evergreen role for a specific job
    ("Talent Acquisition Partner", "Build talent pools and a talent community for engineering.", "Acme", False),
    ("Prospect Research Analyst", None, "Acme", False),
    ("General Counsel", None, "Acme", False),
    ("Software Engineering Intern (Summer 2027)", None, "Acme", False),
    ("(Contract) Recruiter", "Source candidates through referrals and talent pools.", "Plaid", False),
    ("Backend Engineer", "Not the right fit? Join our talent community to stay in touch when new opportunities open.", "Acme", False),
    ("Account Executive", "We'd love to stay in touch when the right opportunity comes up.", "Acme", False),  # no source label
])
def test_non_vacancy_rules(title: str, description: str | None, source: str, excluded: bool) -> None:
    reason = non_vacancy_reason(title, description, source)
    assert (reason is not None) == excluded, reason


def test_non_vacancies_are_hidden_from_counts_and_pages_but_kept(client: TestClient, db_session: Session) -> None:
    c = company(db_session)
    gen = active_generation(db_session)
    jobs = [nj("greenhouse", f"real-{i}", title=f"Backend Engineer {i}") for i in range(3)]
    jobs += [nj("greenhouse", f"eoi-{i}", title=f"Team {i} Expression of Interest Form", company="Example Labs Prospects") for i in range(2)]
    persist_jobs(db_session, jobs, {}, None, c.id, gen, "employer_ats")
    db_session.commit()
    s = summary(client)
    assert (s["total"], s["matching"], s["non_vacancies"]) == (3, 3, 2)
    assert len(client.get("/api/discovery/jobs", params={"limit": 2, "offset": 2}).json()) == 1  # pages follow the count
    assert client.get(f"/api/companies/{c.id}").json()["job_count"] == 3
    # Kept, with the reason, and reachable by id.
    eoi = db_session.scalar(select(DiscoveredJob).where(DiscoveredJob.source_posting_id == "eoi-0"))
    detail = client.get(f"/api/discovery/jobs/{eoi.id}").json()
    assert "interest form" in detail["non_vacancy_reason"]


def test_exclusion_persists_across_syncs_and_follows_new_evidence(client: TestClient, db_session: Session) -> None:
    gen = active_generation(db_session)
    eoi = nj("greenhouse", "e1", title="Leadership at Example", company="Example Prospects",
             description="We'd like to stay in touch as the business grows, when the right opportunity aligns.")
    for _ in range(2):
        persist_jobs(db_session, [eoi], {}, None, None, gen, "employer_ats")
        db_session.commit()
        assert summary(client)["total"] == 0
    # The employer turns it into a real opening: new evidence, so it shows.
    persist_jobs(db_session, [nj("greenhouse", "e1", title="VP Engineering", company="Example", description="Lead 40 engineers.")],
                 {}, None, None, gen, "employer_ats")
    db_session.commit()
    assert summary(client)["total"] == 1


def test_saved_and_applied_non_vacancy_stays_in_saved_jobs(client: TestClient, db_session: Session) -> None:
    gen = active_generation(db_session)
    persist_jobs(db_session, [nj("greenhouse", "x1", title="Engineering Manager")], {}, None, None, gen, "employer_ats")
    db_session.commit()
    [card] = client.get("/api/discovery/jobs").json()
    saved = client.post(f"/api/discovery/jobs/{card['id']}/save").json()
    db_session.add(Application(job_id=saved["saved_job_id"], status="applied", applied_date=datetime(2026, 11, 2).date()))
    db_session.commit()
    # Later the posting is reworded into an interest form: hidden from Discovery, untouched elsewhere.
    persist_jobs(db_session, [nj("greenhouse", "x1", title="Engineering Manager Expression of Interest Form")], {}, None, None, gen, "employer_ats")
    db_session.commit()
    assert summary(client)["total"] == 0
    assert [j["title"] for j in client.get("/api/jobs").json()] == ["Engineering Manager"]
    assert db_session.scalar(select(func.count()).select_from(Application)) == 1
    job = db_session.get(DiscoveredJob, card["id"])
    db_session.refresh(job)
    assert job.review_status == "saved" and str(job.saved_job_id) == saved["saved_job_id"]


def test_offline_reclassification_dry_run_and_apply(db_session: Session) -> None:
    gen = active_generation(db_session)
    db_session.add(DiscoveredJob(provider="greenhouse", source_posting_id="old", company="Acme", title="Sales Expression of Interest",
                                 filter_result="match", filter_reasons=[], review_status="new", review_history=[], generation=gen))
    db_session.commit()
    assert len(reclassify_non_vacancies(db_session, apply=False)) == 1
    assert db_session.scalar(select(DiscoveredJob.non_vacancy_reason).where(DiscoveredJob.source_posting_id == "old")) is None
    reclassify_non_vacancies(db_session, apply=True)
    assert db_session.scalar(select(DiscoveredJob.non_vacancy_reason).where(DiscoveredJob.source_posting_id == "old"))


# --- Detection in scripts and structured data ---


def test_scan_text_finds_boards_in_bundled_scripts_and_serialized_data() -> None:
    boards, _ = cd.scan_text('const u="boards-api.greenhouse.io/v1/boards/cloudflare/jobs";')
    assert [(b.provider, b.board) for b in boards] == [("greenhouse", "cloudflare")]
    boards, _ = cd.scan_text('{"props":{"jobsUrl":"https:\\u002F\\u002Fjobs.ashbyhq.com\\u002Fexamplelabs"}}')  # Next.js data
    assert [(b.provider, b.board) for b in boards] == [("ashby", "examplelabs")]
    # A template that builds the board from a variable is not evidence of any board.
    assert cd.scan_text("fetch(`https://boards-api.greenhouse.io/v1/boards/${token}/jobs`)")[0] == []
    # Lookalike hosts never match.
    assert cd.scan_text("https://xboards.greenhouse.io/acme https://boards.greenhouse.io.evil.example/acme")[0] == []
    _, unsupported = cd.scan_text('var site="examplelabs.wd5.myworkdayjobs.com/External";')
    assert unsupported[0][0] == "Workday"


def test_json_ld_job_posting_urls_and_hiring_organization() -> None:
    page = cd.Page("https://examplelabs.com/careers/1", '<script type="application/ld+json">{"@context":"https://schema.org",'
                   '"@type":"JobPosting","title":"SRE","url":"https://job-boards.greenhouse.io/examplelabs/jobs/77",'
                   '"hiringOrganization":{"@type":"Organization","name":"Example Labs"}}</script>')
    found = cd.analyze(page, "examplelabs.com")
    assert [(b.provider, b.board) for b in found.boards] == [("greenhouse", "examplelabs")] and found.hiring_orgs == ["Example Labs"]


def test_same_domain_component_script_names_the_board_and_third_party_scripts_are_never_fetched() -> None:
    site = FakeSite({
        "https://examplelabs.com/": '<a href="/careers/jobs/">Careers</a>',
        "https://examplelabs.com/careers/jobs/": '<astro-island component-url="/_astro/jobs.abc.js"></astro-island>'
                                                 '<script src="https://cdn.thirdparty.example/lib.js"></script>',
        "https://examplelabs.com/_astro/jobs.abc.js": (200, JS, b'fetch("https://boards-api.greenhouse.io/v1/boards/examplelabs/jobs")'),
    })
    out = cd.explore(site.fetcher(), "examplelabs.com", None)
    assert [(b.provider, b.board, where) for b, where in out.boards] == [("greenhouse", "examplelabs", "https://examplelabs.com/_astro/jobs.abc.js")]
    assert not any("thirdparty" in u for u in site.urls)


def test_official_sitemap_job_page_names_the_board() -> None:
    site = FakeSite({
        "https://examplelabs.com/": '<a href="https://careers.examplelabs.com/">Careers</a>',
        "https://careers.examplelabs.com/": "<div x-data='jobs'></div>",
        "https://careers.examplelabs.com/sitemap.xml": (200, {"content-type": "text/xml"},
            b"<urlset><url><loc>https://careers.examplelabs.com/about</loc></url>"
            b"<url><loc>https://careers.examplelabs.com/detail/123/?gh_jid=123</loc></url></urlset>"),
        "https://careers.examplelabs.com/detail/123/?gh_jid=123":
            '<div id="grnhse_app"></div><script src="https://boards.greenhouse.io/embed/job_board/js?for=examplelabs"></script>',
    })
    out = cd.explore(site.fetcher(), "examplelabs.com", None)
    assert [(b.board, where) for b, where in out.boards] == [("examplelabs", "https://careers.examplelabs.com/detail/123/?gh_jid=123")]


def test_platform_named_script_marks_unsupported_without_guessing() -> None:
    site = FakeSite({
        "https://examplelabs.com/": '<a href="/careers/listings">Careers</a>',
        "https://examplelabs.com/careers/listings": '<div id="job-listings-app"></div><script src="/hub/module_workday-job-listings.min.js"></script>',
        "https://examplelabs.com/hub/module_workday-job-listings.min.js": (200, JS, b'fetch("https://proxy.example.workers.dev/")'),
    })
    out = cd.explore(site.fetcher(), "examplelabs.com", None)
    assert (out.status, out.unsupported, out.boards) == ("unsupported_ats", "Workday", [])
    assert "https://proxy.example.workers.dev/" not in site.urls  # a third-party proxy is never contacted


def test_directory_page_with_many_boards_is_not_ownership_evidence() -> None:
    links = "".join(f'<a href="https://jobs.lever.co/portfolio{i}">Jobs</a>' for i in range(5))
    site = FakeSite({"https://examplelabs.com/": f'<a href="/careers">Careers</a>', "https://examplelabs.com/careers": links})
    out = cd.explore(site.fetcher(), "examplelabs.com", None)
    assert out.boards == [] and any("directory" in e["detail"] for e in out.evidence)


def test_offsite_careers_and_domain_redirects_become_candidates_only() -> None:
    site = FakeSite({"https://examplelabs.com/": '<a href="https://www.examplelabscareers.com/">Careers</a>'})
    out = cd.explore(site.fetcher(), "examplelabs.com", None)
    assert out.status == "needs_review" and out.evidence[-1]["candidate"] == "https://www.examplelabscareers.com/"
    assert "https://www.examplelabscareers.com/" not in site.urls
    moved = FakeSite({"https://examplelabs.so/": (301, {"location": "https://www.examplelabs.com/"}, b"")})
    out = cd.explore(moved.fetcher(), "examplelabs.so", None)
    assert out.status == "needs_review" and [e["candidate"] for e in out.evidence if e.get("candidate")] == ["examplelabs.com"]
    assert not any("examplelabs.com" in u for u in moved.urls)


# --- Ownership conflicts ---


@pytest.mark.parametrize("ours, domain, reported, ok", [
    ("GitLab", "gitlab.com", "GitLab Prospects", True),
    ("Capital One", "capitalone.com", "Capital One Services, LLC", True),
    ("JPMorgan Chase", "jpmorganchase.com", "JPMorgan Chase & Co.", True),
    ("Disney", "thewaltdisneycompany.com", "The Walt Disney Company", True),
    ("Block", "block.xyz", "Square", False),
    ("Example Labs", "examplelabs.com", "Globex Corporation", False),
    ("Example Labs", "examplelabs.com", None, True),  # nothing reported: never a conflict
])
def test_names_consistent(ours: str, domain: str, reported: str | None, ok: bool) -> None:
    assert names_consistent(ours, domain, reported) is ok


def test_board_whose_provider_name_conflicts_needs_review_and_is_not_synced(db_session: Session, factory: Any) -> None:
    c = company(db_session)
    site = FakeSite({"https://examplelabs.com/": '<a href="https://boards.greenhouse.io/globex">Careers</a>'})
    http, _, _ = fake_client({f"{GH_API}globex": [{"name": "Globex Corporation"}]})
    cd.enqueue_discovery(db_session, [c.id])
    db_session.commit()
    [result] = cd.run_tasks(factory, http, site.fetcher(), max_tasks=5)
    [board] = db_session.scalars(select(JobSource))
    assert (result["status"], board.verification_status, board.ownership_verified_at, board.provider_name) == (
        "needs_review", "needs_review", None, "Globex Corporation")
    assert db_session.scalar(select(func.count()).select_from(DiscoveryTask).where(DiscoveryTask.kind == "sync")) == 0


def test_matching_provider_name_verifies_and_is_recorded(db_session: Session, factory: Any) -> None:
    c = company(db_session)
    site = FakeSite({"https://examplelabs.com/": '<a href="https://boards.greenhouse.io/examplelabs">Careers</a>'})
    http, _, _ = fake_client({f"{GH_API}examplelabs": [{"name": "Example Labs"}], f"{GH_API}examplelabs/jobs": [{"jobs": []}]})
    cd.enqueue_discovery(db_session, [c.id])
    db_session.commit()
    cd.run_tasks(factory, http, site.fetcher(), max_tasks=1)
    [board] = db_session.scalars(select(JobSource))
    assert board.ownership_verified_at and board.provider_name == "Example Labs"
    db_session.refresh(c)
    assert "matches" in c.discovery_evidence[-1]["detail"]


def test_conflict_revokes_automatic_ownership_but_keeps_user_confirmed(db_session: Session, factory: Any) -> None:
    now = datetime.now(timezone.utc)
    auto, manual = company(db_session), company(db_session, name="Manual Co", domain="manualco.com")
    db_session.add_all([
        JobSource(provider="greenhouse", board_token="globex", company_name=auto.name, company_id=auto.id, enabled=True,
                  verification_status="verified", ownership_verified_at=now, ownership_evidence="Linked from the employer's official page x"),
        JobSource(provider="greenhouse", board_token="initech", company_name=manual.name, company_id=manual.id, enabled=True,
                  verification_status="verified", ownership_verified_at=now, ownership_evidence="Confirmed by the user: https://x"),
    ])
    db_session.commit()
    site = FakeSite({"https://examplelabs.com/": '<a href="https://boards.greenhouse.io/globex">Careers</a>',
                     "https://manualco.com/": '<a href="https://boards.greenhouse.io/initech">Careers</a>'})
    http, _, _ = fake_client({f"{GH_API}globex": [{"name": "Globex Corporation"}], f"{GH_API}initech": [{"name": "Initech"}]})
    cd.enqueue_discovery(db_session, [auto.id, manual.id])
    db_session.commit()
    cd.run_tasks(factory, http, site.fetcher(), max_tasks=2)
    boards = {b.board_token: b for b in db_session.scalars(select(JobSource))}
    for b in boards.values():
        db_session.refresh(b)
    assert boards["globex"].ownership_verified_at is None and boards["globex"].verification_status == "needs_review"
    assert boards["initech"].ownership_verified_at is not None  # user-confirmed ownership is preserved


def test_conflicting_json_ld_hiring_organization_blocks_ownership(db_session: Session, factory: Any) -> None:
    c = company(db_session)
    site = FakeSite({"https://examplelabs.com/": '<script type="application/ld+json">{"@type":"JobPosting","url":'
                     '"https://jobs.lever.co/othercorp/1","hiringOrganization":{"name":"Other Corp"}}</script>'})
    http, _, _ = fake_client({"https://api.lever.co/v0/postings/othercorp": [[]]})
    cd.enqueue_discovery(db_session, [c.id])
    db_session.commit()
    [result] = cd.run_tasks(factory, http, site.fetcher(), max_tasks=5)
    assert result["status"] == "needs_review"
    assert db_session.scalar(select(JobSource.ownership_verified_at)) is None


# --- Company identity ---


def test_merge_moves_everything_and_keeps_history(client: TestClient, db_session: Session) -> None:
    keep = company(db_session, name="GitLab", domain="gitlab.example")
    dup = Company(name="GitLab", origin="manual", watchlist=True, verification_status="unresolved")
    db_session.add(dup)
    db_session.flush()
    saved = Job(company="GitLab", title="SRE", posting_url="https://x.example/1")
    db_session.add(saved)
    db_session.flush()
    db_session.add_all([
        JobSource(provider="greenhouse", board_token="gitlabx", company_name="GitLab", company_id=dup.id, enabled=True, verification_status="needs_review"),
        DiscoveredJob(provider="greenhouse", source_posting_id="1", company="GitLab", company_id=dup.id, title="SRE", filter_result="match",
                      filter_reasons=[], review_status="saved", saved_job_id=saved.id, review_history=[{"action": "saved"}], generation=1),
        Application(job_id=saved.id, status="applied", applied_date=datetime(2026, 11, 2).date()),
    ])
    db_session.commit()
    dry = merge_companies(db_session, dup.id, keep.id, "Board's postings point to gitlab.example", apply=False)
    assert (dry["boards"], dry["discovered_jobs"]) == (1, 1) and db_session.get(Company, dup.id) is not None
    dup_id = dup.id
    merge_companies(db_session, dup_id, keep.id, "Board's postings point to gitlab.example", apply=True)
    assert db_session.get(Company, dup_id) is None
    assert db_session.scalar(select(JobSource.company_id)) == keep.id
    job = db_session.scalar(select(DiscoveredJob))
    assert (job.company_id, job.review_status, job.saved_job_id, job.review_history) == (keep.id, "saved", saved.id, [{"action": "saved"}])
    assert db_session.scalar(select(func.count()).select_from(Application)) == 1
    assert db_session.scalar(select(CompanyAlias.company_id).where(CompanyAlias.kind == "merged_company", CompanyAlias.value == str(dup_id))) == keep.id
    db_session.refresh(keep)
    assert keep.watchlist and "Merged duplicate record" in keep.notes


def test_same_name_different_domains_are_never_merged(db_session: Session) -> None:
    ours = company(db_session, name="Embark", domain="embarkwithus.example")
    other = company(db_session, name="Embark", domain="embark.example")
    with pytest.raises(ConflictError, match="separate identities"):
        merge_companies(db_session, other.id, ours.id, "same name", apply=True)
    assert db_session.get(Company, other.id) is not None


def test_accepting_a_new_domain_records_the_change(client: TestClient, db_session: Session) -> None:
    c = company(db_session, name="Moved Co", domain="moved.example")
    r = client.patch(f"/api/companies/{c.id}", json={"domain": "moved-new.example"})
    assert r.status_code == 200 and r.json()["domain"] == "moved-new.example"
    assert "from moved.example to moved-new.example" in r.json()["discovery_evidence"][-1]["detail"]
    assert db_session.scalar(select(CompanyAlias.value).where(CompanyAlias.company_id == c.id, CompanyAlias.kind == "former_domain")) == "moved.example"


def test_page_specific_scripts_are_read_before_site_wide_ones() -> None:
    shared = "".join(f'<script src="/common/{i}.js"></script>' for i in range(cd.MAX_SCRIPTS_PER_COMPANY))
    pages: dict[str, Any] = {
        "https://examplelabs.com/": f'{shared}<a href="/careers/">Careers</a>',
        "https://examplelabs.com/careers/": f'{shared}<a href="/careers/jobs/">Open roles</a><a href="/careers/life/">Life at Example</a>',
        "https://examplelabs.com/careers/jobs/": f'{shared}<script src="/jobs-list.js"></script>',
        "https://examplelabs.com/careers/life/": shared + "".join(f'<script src="/life/{i}.js"></script>' for i in range(9)),
        "https://examplelabs.com/jobs-list.js": (200, JS, b'"https://boards-api.greenhouse.io/v1/boards/examplelabs/jobs"'),
    }
    pages.update({f"https://examplelabs.com/common/{i}.js": (200, JS, b"track()") for i in range(cd.MAX_SCRIPTS_PER_COMPANY)})
    pages.update({f"https://examplelabs.com/life/{i}.js": (200, JS, b"gallery()") for i in range(9)})
    out = cd.explore(FakeSite(pages).fetcher(), "examplelabs.com", None)
    assert [(b.board, w) for b, w in out.boards] == [("examplelabs", "https://examplelabs.com/jobs-list.js")]


def test_every_unsupported_platform_is_kept_and_noted_once() -> None:
    links = ('<a href="https://acme.wd12.myworkdayjobs.com/Acme/login">Apply</a>'
             '<a href="https://acme.eightfold.ai/careers/join">Join our talent network</a>')
    site = FakeSite({"https://examplelabs.com/": f'<a href="/careers/">Careers</a>{links}', "https://examplelabs.com/careers/": links})
    out = cd.explore(site.fetcher(), "examplelabs.com", None)
    assert (out.status, out.unsupported) == ("unsupported_ats", "Workday, Eightfold")
    assert sum(e["step"] == "unsupported_ats" for e in out.evidence) == 2
