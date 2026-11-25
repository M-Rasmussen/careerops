"""Discovery inbox counts, filters, sorting, card data, details, and the Save / Unsave lifecycle."""

import uuid
from collections.abc import Iterator
from datetime import date, datetime, timedelta, timezone
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.api.discovery import get_http_client
from app.config import DiscoverySettingsEnv, get_discovery_env
from app.main import app
from app.models import AgentRun, Application, CandidateProfile, Company, DiscoveredJob, Job, JobMatch
from app.services.aggregators import parse_himalayas, parse_jsearch
from app.services.job_sources import parse_ashby, parse_greenhouse, parse_lever
from tests.discovery_fixtures import LEVER_URL, fake_client, himalayas_job, jsearch_job, lever_posting

NOW = datetime.now(timezone.utc)


def lead(db: Session, **kw: Any) -> DiscoveredJob:
    n = uuid.uuid4().hex[:8]
    values: dict[str, Any] = dict(
        provider="greenhouse", source_posting_id=n, company="Acme", title="Backend Engineer", location=None,
        workplace_type=None, posting_url=f"https://boards.greenhouse.io/acme/jobs/{n}", filter_result="match",
        filter_reasons=["ok"], review_status="new", review_history=[], description=None, generation=1,
    )
    values.update(kw)
    job = DiscoveredJob(**values)
    db.add(job)
    db.commit()
    return job


def get(client: TestClient, path: str, **params: Any) -> Any:
    r = client.get(path, params=params)
    assert r.status_code == 200, r.text
    return r.json()


# --- Counts and pagination ---


def test_counts_cover_the_whole_table_and_exclude_duplicates(client: TestClient, db_session: Session) -> None:
    originals = [lead(db_session, title=f"Backend Engineer {i}", filter_result="match" if i < 3 else "unknown") for i in range(5)]
    lead(db_session, title="Backend Engineer 0 (via feed)", provider="himalayas", duplicate_of_id=originals[0].id)
    summary = get(client, "/api/discovery/jobs/summary", hide_duplicates=True)
    assert summary == {"total": 5, "matching": 5, "by_result": {"match": 3, "unknown": 2, "rejected": 0},
                       "by_source": {"greenhouse": 5}, "archived": 0, "non_vacancies": 0, "closed": 0}
    filtered = get(client, "/api/discovery/jobs/summary", hide_duplicates=True, filter_result="match")
    assert (filtered["total"], filtered["matching"]) == (5, 3)
    assert filtered["by_result"] == {"match": 3, "unknown": 2, "rejected": 0}  # distribution ignores the result filter
    # Pages come from the same filters; counts don't depend on the page.
    page2 = get(client, "/api/discovery/jobs", hide_duplicates=True, limit=2, offset=4)
    assert len(page2) == 1
    # Reviewing changes filtered counts immediately.
    client.patch(f"/api/discovery/jobs/{originals[1].id}", json={"review_status": "skipped"})
    client.post(f"/api/discovery/jobs/{originals[2].id}/save")
    new_only = get(client, "/api/discovery/jobs/summary", hide_duplicates=True, review_status="new")
    assert (new_only["total"], new_only["matching"]) == (5, 3)
    assert get(client, "/api/discovery/jobs/summary", review_status="saved")["matching"] == 1


def test_empty_summary(client: TestClient) -> None:
    assert get(client, "/api/discovery/jobs/summary") == {"total": 0, "matching": 0, "by_result": {"match": 0, "unknown": 0, "rejected": 0},
                                                          "by_source": {}, "archived": 0, "non_vacancies": 0, "closed": 0}


# --- Filters and sorting ---


def test_filters_keep_jobs_with_missing_optional_data(client: TestClient, db_session: Session) -> None:
    usd_low = lead(db_session, title="Backend Engineer Low", salary_max=90_000, salary_currency="USD", salary_interval="per-year-salary", workplace_type="remote")
    usd_high = lead(db_session, title="Backend Engineer High", salary_max=150_000, salary_currency="USD", salary_interval="year", workplace_type="hybrid", location="Jersey City, NJ")
    hourly = lead(db_session, title="Backend Engineer Hourly", salary_max=80, salary_currency="USD", salary_interval="hour")
    eur = lead(db_session, title="Backend Engineer EUR", salary_max=60_000, salary_currency="EUR", salary_interval="1 year")
    unknown = lead(db_session, title="Backend Engineer Unknown")
    ids = lambda **p: {j["id"] for j in get(client, "/api/discovery/jobs", **p)}  # noqa: E731
    # Only a comparable (USD annual) salary below the minimum hides a job.
    assert ids(min_salary=100_000) == {str(j.id) for j in (usd_high, hourly, eur, unknown)}
    assert ids(workplace="unknown") == {str(j.id) for j in (hourly, eur, unknown)}
    assert ids(workplace="remote") == {str(usd_low.id)}
    assert ids(location="jersey") == {str(usd_high.id)}
    old = lead(db_session, title="Old Posting", posted_at=NOW - timedelta(days=60))
    recent = lead(db_session, title="Recent Posting", posted_at=NOW - timedelta(days=2))
    within = ids(posted_within_days=30)
    assert str(recent.id) in within and str(unknown.id) in within and str(old.id) not in within  # no date: kept


def test_role_family_and_seniority_filters(client: TestClient, db_session: Session) -> None:
    sdet = lead(db_session, title="Software Engineer in Test II")
    devops = lead(db_session, title="Senior Site Reliability Engineer")
    stated = lead(db_session, title="Platform Engineer", seniority="mid")
    lead(db_session, title="Account Executive")
    titles = lambda **p: {j["title"] for j in get(client, "/api/discovery/jobs", **p)}  # noqa: E731
    assert titles(role_family="devops") == {devops.title}
    assert sdet.title in titles(role_family="sdet")
    assert titles(seniority="mid") == {sdet.title, stated.title}  # "II" in the title, or stated by the source
    assert titles(seniority="senior") == {devops.title}
    assert client.get("/api/discovery/jobs", params={"role_family": "astronaut"}).status_code == 422


def test_sorting(client: TestClient, db_session: Session) -> None:
    lead(db_session, title="b-title", company="Zeta", posted_at=NOW - timedelta(days=5), salary_max=120_000, salary_currency="USD", salary_interval="year")
    lead(db_session, title="A-title", company="alpha", posted_at=None, salary_max=999, salary_currency="USD", salary_interval="hour")
    lead(db_session, title="c-title", company="Mid", posted_at=NOW - timedelta(days=1), salary_max=200_000, salary_currency="USD", salary_interval="per-year-salary")
    order = lambda s: [j["title"] for j in get(client, "/api/discovery/jobs", sort=s)]  # noqa: E731
    assert order("posted") == ["c-title", "b-title", "A-title"]  # missing posting date last
    assert order("salary") == ["c-title", "b-title", "A-title"]  # hourly isn't compared with annual
    assert order("title") == ["A-title", "b-title", "c-title"]
    assert order("company") == ["A-title", "c-title", "b-title"]


def test_saved_state_filter(client: TestClient, db_session: Session) -> None:
    a, b, c = (lead(db_session, title=t) for t in ("Saved one", "Unsaved one", "Never saved"))
    client.post(f"/api/discovery/jobs/{a.id}/save")
    saved_b = client.post(f"/api/discovery/jobs/{b.id}/save").json()
    client.post(f"/api/jobs/{saved_b['saved_job_id']}/unsave")
    names = lambda st: [j["title"] for j in get(client, "/api/discovery/jobs", saved_state=st)]  # noqa: E731
    assert names("saved") == ["Saved one"]
    assert names("unsaved") == ["Unsaved one"]
    assert names("never") == ["Never saved"]


# --- Card data ---


def test_posting_dates_are_distinct_from_update_times() -> None:
    gh = parse_greenhouse({"jobs": [{"id": 1, "title": "T", "updated_at": "2026-11-20T00:00:00Z", "first_published": "2026-10-01T00:00:00Z"}]}, "X")[0]
    assert (gh.posted_at.date(), gh.source_updated_at.date()) == (date(2026, 10, 1), date(2026, 11, 20))
    no_first = parse_greenhouse({"jobs": [{"id": 2, "title": "T", "updated_at": "2026-11-20T00:00:00Z"}]}, "X")[0]
    assert no_first.posted_at is None  # updated_at is never used as the posting date
    hima = parse_himalayas({"jobs": [himalayas_job(1)]})[0]
    assert hima.posted_at is None and hima.source_updated_at is not None  # pubDate = last updated
    assert hima.employment_type == "Full-time"
    lever = parse_lever([lever_posting(1)], "X")[0]
    assert lever.posted_at is not None and lever.employment_type == "Full-time"
    js = parse_jsearch({"data": [jsearch_job(1, job_employment_type="FULLTIME")]})[0]
    assert js.posted_at is not None and js.source_updated_at is None and js.employment_type == "Full-time"
    ashby = parse_ashby({"jobs": [{"title": "T", "employmentType": "Contract", "publishedAt": "2026-11-01T00:00:00Z",
                                   "jobUrl": "https://jobs.ashbyhq.com/acme/11111111-2222-3333-4444-555555555555"}]}, "A", "acme")[0]
    assert ashby.posted_at is not None and ashby.employment_type == "Contract"


def test_card_links_technologies_and_company_website(client: TestClient, db_session: Session) -> None:
    verified = Company(name="Verified Co", domain="verified.example", website="https://verified.example", origin="curated_seed",
                       watchlist=False, verification_status="verified")
    reported = Company(name="Reported Co", website="https://unverified.example", origin="discovered", watchlist=False,
                       verification_status="provider_reported")
    db_session.add_all([verified, reported])
    db_session.commit()
    board_job = lead(db_session, company_id=verified.id, ats_job_key="lever:abc", provider="lever",
                     posting_url="https://jobs.lever.co/verified/abc", description="Python, Kafka and Kubernetes on AWS.")
    feed = lead(db_session, company_id=reported.id, provider="jsearch", ats_job_key="lever:abc",
                posting_url="https://www.linkedin.com/jobs/view/1")
    hima = lead(db_session, provider="himalayas", posting_url="https://himalayas.app/companies/x/jobs/y/apply")
    cards = {j["id"]: j for j in get(client, "/api/discovery/jobs")}
    b = cards[str(board_job.id)]
    assert b["posting_link"] == {"url": "https://jobs.lever.co/verified/abc", "kind": "employer", "via": "lever"}
    assert b["technologies"] == ["Python", "Kafka", "AWS", "Kubernetes"]
    assert b["company_info"]["website"] == "https://verified.example"
    assert "description" not in b  # compact list; details carry the description
    f = cards[str(feed.id)]
    assert f["posting_link"]["kind"] == "employer" and f["posting_link"]["url"] == "https://jobs.lever.co/verified/abc"
    assert f["company_info"]["website"] is None  # identity not verified: no website shown
    h = cards[str(hima.id)]
    assert h["posting_link"]["kind"] == "aggregator" and h["posting_link"]["url"].startswith("https://himalayas.app/")
    assert h["experience_match"] == {"status": "not_evaluated", "match_id": None, "overall_match": None,
                                     "evaluated_at": None, "recommendation": None}


def test_details_requirements_and_missing_data(client: TestClient, db_session: Session) -> None:
    text = "About us\nWe build.\nRequirements:\n- 3+ years with Python\n- SQL\nNice to have\n- Kafka\nBenefits\n- Gym"
    job = lead(db_session, description=text)
    detail = get(client, f"/api/discovery/jobs/{job.id}")
    assert detail["description"] == text
    assert detail["requirements"] == {"sections_found": True, "required": ["3+ years with Python", "SQL"],
                                      "preferred": ["Kafka"], "experience": ["3+ years with Python"],
                                      "technologies": ["Python", "SQL", "Kafka"]}
    bare = lead(db_session, description=None, location=None, posted_at=None, salary_min=None)
    d2 = get(client, f"/api/discovery/jobs/{bare.id}")
    assert d2["requirements"]["sections_found"] is False and d2["requirements"]["required"] == []
    assert (d2["posted_at"], d2["employment_type"], d2["salary_min"], d2["location"]) == (None, None, None, None)
    assert client.get(f"/api/discovery/jobs/{uuid.uuid4()}").status_code == 404


def test_experience_match_states_read_existing_assessments(client: TestClient, db_session: Session) -> None:
    job = lead(db_session)
    saved = client.post(f"/api/discovery/jobs/{job.id}/save").json()
    assert saved["experience_match"]["status"] == "not_evaluated"
    profile = db_session.get(CandidateProfile, 1) or CandidateProfile(id=1, resume_text="Python dev")
    db_session.add(profile)
    db_session.commit()
    run = AgentRun(kind="job_match", job_id=uuid.UUID(saved["saved_job_id"]), status="succeeded", provider="openai", model="m", steps=1, tool_calls=[])
    db_session.add(run)
    db_session.flush()
    db_session.add(JobMatch(run_id=run.id, job_id=run.job_id, overall_match="moderate", job_description="d", input_hash="h" * 64,
                            assessment={"overall_match": "moderate", "recommendation": "Worth applying.", "supported": [], "partial": [], "missing": [], "uncertainties": []}))
    db_session.commit()
    # Same transaction => created_at equals the profile's updated_at => current.
    em = get(client, f"/api/discovery/jobs/{job.id}")["experience_match"]
    assert (em["status"], em["overall_match"], em["recommendation"]) == ("evaluation_available", "moderate", "Worth applying.")
    db_session.execute(update(CandidateProfile).values(updated_at=func.now() + timedelta(minutes=5)))
    db_session.commit()
    assert get(client, f"/api/discovery/jobs/{job.id}")["experience_match"]["status"] == "previously_evaluated"


# --- Save / Unsave lifecycle ---


def test_save_unsave_resave_lifecycle(client: TestClient, db_session: Session) -> None:
    job = lead(db_session, title="Lifecycle Engineer", description="Go and Python")
    first = client.post(f"/api/discovery/jobs/{job.id}/save").json()
    assert client.post(f"/api/discovery/jobs/{job.id}/save").json()["saved_job_id"] == first["saved_job_id"]  # idempotent
    saved_id = first["saved_job_id"]
    assert saved_id in {j["id"] for j in get(client, "/api/jobs")}
    gone = client.post(f"/api/jobs/{saved_id}/unsave")
    assert gone.status_code == 200 and gone.json()["unsaved_at"]
    assert client.post(f"/api/jobs/{saved_id}/unsave").status_code == 200  # idempotent
    assert saved_id not in {j["id"] for j in get(client, "/api/jobs")}  # hidden from Saved Jobs, not deleted
    assert db_session.get(Job, uuid.UUID(saved_id)) is not None
    after = get(client, f"/api/discovery/jobs/{job.id}")
    assert after["review_status"] == "new"  # back in the review queue, not "passed"
    assert after["unsaved_at"] and after["saved_job_id"] == saved_id  # link kept for a duplicate-free re-save
    assert [h["action"] for h in after["review_history"]] == ["saved", "unsaved"]
    again = client.post(f"/api/discovery/jobs/{job.id}/save").json()
    assert again["saved_job_id"] == saved_id and again["review_status"] == "saved"
    assert saved_id in {j["id"] for j in get(client, "/api/jobs")}
    assert db_session.scalar(select(func.count()).select_from(Job).where(Job.title == "Lifecycle Engineer")) == 1
    assert [h["action"] for h in get(client, f"/api/discovery/jobs/{job.id}")["review_history"]] == ["saved", "unsaved", "saved"]
    assert db_session.scalar(select(func.count()).select_from(Application)) == 0
    assert client.post(f"/api/jobs/{uuid.uuid4()}/unsave").status_code == 404


def test_saved_job_cannot_be_passed_until_unsaved(client: TestClient, db_session: Session) -> None:
    job = lead(db_session)
    client.post(f"/api/discovery/jobs/{job.id}/save")
    assert client.patch(f"/api/discovery/jobs/{job.id}", json={"review_status": "skipped"}).status_code == 409


def test_unsave_is_refused_when_an_application_exists(client: TestClient, db_session: Session) -> None:
    job = lead(db_session)
    saved = client.post(f"/api/discovery/jobs/{job.id}/save").json()
    app_body = {"job_id": saved["saved_job_id"], "submission_confirmed": True, "applied_date": date.today().isoformat(),
                "status": "interviewing", "follow_up_date": None, "notes": "Phone screen booked"}
    created = client.post("/api/applications", json=app_body).json()
    response = client.post(f"/api/jobs/{saved['saved_job_id']}/unsave")
    assert response.status_code == 409 and "recorded application" in response.json()["detail"]
    kept = client.get(f"/api/applications/{created['id']}").json()
    assert (kept["status"], kept["notes"], kept["applied_date"]) == ("interviewing", "Phone screen booked", app_body["applied_date"])
    assert saved["saved_job_id"] in {j["id"] for j in get(client, "/api/jobs")}
    assert get(client, f"/api/discovery/jobs/{job.id}")["review_status"] == "saved"


@pytest.fixture
def lever_net(client: TestClient) -> Iterator[dict[str, list[Any]]]:
    routes: dict[str, list[Any]] = {LEVER_URL: [[lever_posting(1)]]}
    app.dependency_overrides[get_discovery_env] = lambda: DiscoverySettingsEnv(himalayas_enabled=False, min_resync_minutes=0)
    app.dependency_overrides[get_http_client] = lambda: fake_client(routes)[0]
    yield routes


def test_rediscovering_an_unsaved_job_updates_it_and_never_resaves(client: TestClient, db_session: Session, lever_net: dict) -> None:
    client.post("/api/discovery/sources", json={"provider": "lever", "company_name": "Example Co", "board_token": "exampleco"})
    assert client.post("/api/discovery/runs").status_code == 201
    [job] = get(client, "/api/discovery/jobs")
    saved = client.post(f"/api/discovery/jobs/{job['id']}/save").json()
    client.post(f"/api/jobs/{saved['saved_job_id']}/unsave")
    db_session.execute(update(DiscoveredJob).values(first_seen_at=func.now() - timedelta(days=3)))
    db_session.commit()
    first_seen = get(client, f"/api/discovery/jobs/{job['id']}")["first_seen_at"]
    lever_net[LEVER_URL] = [[lever_posting(1, text="Software Engineer 1 (retitled)")]]
    run = client.post("/api/discovery/runs").json()
    assert (run["jobs_inserted"], run["jobs_updated"]) == (0, 1)
    [again] = get(client, "/api/discovery/jobs")
    assert again["id"] == job["id"] and again["title"] == "Software Engineer 1 (retitled)"
    assert again["review_status"] == "new" and again["unsaved_at"]  # not resaved automatically
    assert again["first_seen_at"] == first_seen
    assert saved["saved_job_id"] not in {j["id"] for j in get(client, "/api/jobs")}
    # The same posting via another provider is a possible duplicate of the original, not a new opportunity.
    db_session.add(DiscoveredJob(provider="jsearch", source_posting_id="js-x", company="Example Co", title="SE 1",
                                 ats_job_key="lever:lever-1", posting_url="https://jobs.lever.co/exampleco/lever-1",
                                 canonical_url="https://jobs.lever.co/exampleco/lever-1", filter_result="match",
                                 filter_reasons=[], review_status="new", review_history=[]))
    db_session.commit()
    from app.services.discovery import _flag_duplicates

    new_id = db_session.scalar(select(DiscoveredJob.id).where(DiscoveredJob.provider == "jsearch"))
    assert _flag_duplicates(db_session, [new_id]) == 1
    db_session.commit()
    assert db_session.get(DiscoveredJob, new_id).duplicate_of_id == uuid.UUID(job["id"])


def test_requirement_sublists_ending_in_colon_are_kept() -> None:
    from app.services.job_insights import requirements

    text = "Requirements:\nProficiency in one of:\nPython\nGo\nBenefits:\nGym"
    assert requirements(text)["required"] == ["Proficiency in one of:", "Python", "Go"]


def test_save_prefers_an_active_saved_job_with_the_same_url(client: TestClient, db_session: Session) -> None:
    url = "https://boards.greenhouse.io/acme/jobs/777"
    removed = client.post("/api/jobs", json={"company": "Acme", "title": "Old", "posting_url": url}).json()
    client.post(f"/api/jobs/{removed['id']}/unsave")
    active = client.post("/api/jobs", json={"company": "Acme", "title": "Re-added", "posting_url": url}).json()
    job = lead(db_session, posting_url=url)
    assert client.post(f"/api/discovery/jobs/{job.id}/save").json()["saved_job_id"] == active["id"]
    assert [j["id"] for j in get(client, "/api/jobs") if j["posting_url"] == url] == [active["id"]]


# --- Required experience and employment type ---


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Requirements:\n3+ years of Python experience\nBenefits:\nGym", ["3+ years of Python experience"]),
        ("3-5 years in backend development; strong SQL", ["3–5 years in backend development"]),
        ("5+ years in backend development, and 2+ years with Kubernetes", ["5+ years in backend development", "2+ years with Kubernetes"]),
        ("Must have 2 years experience", ["2 years experience"]),
        # Whole paragraphs on one line: the requirement sentence counts, the boast sentence doesn't.
        ("We have 20 years of experience serving banks. We're looking for an engineer with 5+ years of backend experience.",
         ["5+ years of backend experience"]),
        # Not experience requirements: company history, benefits, boilerplate, titles.
        ("We were founded 10 years ago.", []),
        ("We have 20+ years of experience serving clients.", []),
        ("Equity vesting over 4 years.", []),
        ("Senior Software Engineer II", []),
        (None, []),
    ],
)
def test_experience_phrases(text: str | None, expected: list[str]) -> None:
    from app.services.job_insights import experience_phrases

    assert experience_phrases(text) == expected


def test_required_experience_prefers_requirement_section_and_stays_short() -> None:
    from app.services.job_insights import required_experience

    text = ("Nice to have:\n1+ year of Rust experience\nRequirements:\n"
            "7+ years of professional software engineering experience building distributed systems at large scale\n"
            "3+ years with Python")
    got = required_experience(text)
    assert got[0].startswith("7+ years of professional") and got[0].endswith("…") and len(got[0]) <= 71
    assert got[1] == "3+ years with Python" and "1+ year of Rust experience" in got


@pytest.mark.parametrize(
    ("raw", "title", "expected"),
    [
        ("Full Time", "Backend Engineer", "Full-time"),
        ("FULLTIME", "Software Engineer (Contract)", "Contract"),  # full-time hours, but a contract
        ("Full-time", "Contract Software Engineer - 12 months", "Contract"),
        ("Contract", "Backend Engineer - Contract to Hire", "Contract-to-hire"),
        (None, "SDET (Contract-to-Hire)", "Contract-to-hire"),
        ("CONTRACTOR", "x", "Contract"),
        ("Intern", "x", "Internship"),
        ("Temporary", "x", "Temporary"),
        ("PARTTIME", "x", "Part-time"),
        ("Other", "x", None),
        ("Volunteer", "x", None),
        (None, "Internal Tools Engineer", None),  # "internal" isn't "intern"; titles never imply full-time
        ("Full-time", "Contracts Manager", "Full-time"),
        (None, "Contract Manager, Legal", None),  # a job about contracts, not a contract job
        (None, "Government Contracting Officer", None),
        (None, "Candidate Experience Specialist, Contractor (AMER)", "Contract"),
        (None, "Contract Software Engineer", "Contract"),
        (None, "GPU Engineer, Remote- Contract", "Contract"),
        (None, "Freelance Engineer", "Contract"),
    ],
)
def test_employment_type_classification(raw: str | None, title: str, expected: str | None) -> None:
    from app.services.job_sources import employment_type

    assert employment_type(raw, title) == expected


def test_employment_type_multi_filter_counts_and_pagination(client: TestClient, db_session: Session) -> None:
    for i in range(3):
        lead(db_session, title=f"FT {i}", employment_type="Full-time")
    lead(db_session, title="Contract role", employment_type="Contract")
    lead(db_session, title="C2H role", employment_type="Contract-to-hire")
    lead(db_session, title="Unknown type")
    titles = lambda **p: sorted(j["title"] for j in get(client, "/api/discovery/jobs", **p))  # noqa: E731
    assert len(titles()) == 6  # no filter: missing types are not hidden
    assert titles(employment_type=["Contract", "Contract-to-hire"]) == ["C2H role", "Contract role"]
    assert titles(employment_type=["Contract", "unknown"]) == ["Contract role", "Unknown type"]
    assert "Unknown type" not in titles(employment_type=["Full-time"])  # only hidden when explicitly filtered out
    summary = get(client, "/api/discovery/jobs/summary", employment_type=["Full-time", "Contract"])
    assert (summary["total"], summary["matching"]) == (6, 4)
    page = get(client, "/api/discovery/jobs", employment_type=["Full-time", "Contract"], limit=3, offset=3)
    assert len(page) == 1
    assert client.get("/api/discovery/jobs", params={"employment_type": "Gig"}).status_code == 422
    with pytest.raises(Exception):  # the database refuses non-normalized values
        lead(db_session, employment_type="Full Time")
    db_session.rollback()


def test_card_shows_required_experience_or_nothing(client: TestClient, db_session: Session) -> None:
    stated = lead(db_session, title="Senior Backend Engineer", description="Requirements:\n5+ years in backend development")
    silent = lead(db_session, title="Senior Backend Engineer II", description="Build APIs in Python.")
    cards = {j["id"]: j for j in get(client, "/api/discovery/jobs")}
    assert cards[str(stated.id)]["experience_required"] == ["5+ years in backend development"]
    assert cards[str(silent.id)]["experience_required"] == []  # never inferred from "Senior" or "II"
    assert cards[str(silent.id)]["experience_match"]["status"] == "not_evaluated"  # a separate concept
