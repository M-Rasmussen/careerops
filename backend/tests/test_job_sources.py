import copy
from datetime import datetime, timezone

import pytest

from app.services.job_sources import (
    LEVER_PAGE_SIZE,
    MAX_RETRIES,
    HttpClient,
    SourceError,
    canonical_url,
    fetch_jobs,
    parse_greenhouse,
    parse_lever,
)
from tests.discovery_fixtures import GH_URL, GREENHOUSE, LEVER_URL, fake_client, http_error, lever_posting


def test_greenhouse_parsing_normalizes_fields() -> None:
    jobs = parse_greenhouse(copy.deepcopy(GREENHOUSE), "Fallback Co")
    assert [j.source_posting_id for j in jobs] == ["4001", "4002", "4003"]  # untitled record skipped
    first = jobs[0]
    assert first.provider == "greenhouse"
    assert first.company == "Example Labs"
    assert first.title == "Senior Backend Engineer"
    assert first.location == "Remote - US"
    assert first.workplace_type == "remote"
    assert first.description == "Build Python APIs.\nFastAPI"
    assert first.posting_url.endswith("?gh_src=abc")
    assert first.canonical_url == "https://boards.greenhouse.io/examplelabs/jobs/4001"
    assert first.source_updated_at == datetime(2026, 11, 20, 15, 15, tzinfo=timezone.utc)
    assert first.salary_min is None and first.salary_max is None


def test_greenhouse_missing_fields_stay_none() -> None:
    sparse = parse_greenhouse(copy.deepcopy(GREENHOUSE), "Fallback Co")[2]
    assert sparse.company == "Fallback Co"
    assert sparse.location is None
    assert sparse.workplace_type is None  # not assumed on-site
    assert sparse.description is None
    assert sparse.posting_url is None and sparse.canonical_url is None
    assert sparse.source_updated_at is None
    # A plain city is unknown, not on-site.
    assert parse_greenhouse(copy.deepcopy(GREENHOUSE), "x")[1].workplace_type is None


def test_lever_parsing_with_salary_and_workplace() -> None:
    posting = lever_posting(1, salaryRange={"currency": "USD", "interval": "per-year-salary", "min": 120000, "max": 150000})
    job = parse_lever([posting], "Example Co")[0]
    assert job.provider == "lever"
    assert job.source_posting_id == "lever-1"
    assert job.company == "Example Co"
    assert job.workplace_type == "hybrid"
    assert job.location == "Denver, CO"
    assert job.description == "Work on the platform.\n\nRequirements\n\nPython\nSQL\n\nBenefits included."
    assert (job.salary_min, job.salary_max, job.salary_currency, job.salary_interval) == (
        120000, 150000, "USD", "per-year-salary",
    )
    assert job.source_updated_at is None  # Lever exposes no update time


def test_lever_missing_fields() -> None:
    job = parse_lever([{"id": "x1", "text": "Analyst"}], "Example Co")[0]
    assert job.location is None and job.workplace_type is None and job.description is None
    assert job.salary_min is None and job.posting_url is None
    assert parse_lever([{"id": "x2"}, {"text": "No id"}, "junk"], "Example Co") == []


@pytest.mark.parametrize("payload", [[], {"jobs": "nope"}, None])
def test_greenhouse_rejects_unexpected_shape(payload: object) -> None:
    with pytest.raises(SourceError):
        parse_greenhouse(payload, "x")


def test_lever_rejects_unexpected_shape() -> None:
    with pytest.raises(SourceError):
        parse_lever({"ok": False}, "x")


def test_lever_pagination_follows_skip_until_short_page() -> None:
    page1 = [lever_posting(i) for i in range(LEVER_PAGE_SIZE)]
    page2 = [lever_posting(i) for i in range(LEVER_PAGE_SIZE, LEVER_PAGE_SIZE + 3)]
    client, opener, _ = fake_client({LEVER_URL: [page1, page2]})
    jobs = fetch_jobs(client, "lever", "exampleco", "Example Co")
    assert len(jobs) == LEVER_PAGE_SIZE + 3
    assert opener.urls == [
        f"{LEVER_URL}?mode=json&skip=0&limit={LEVER_PAGE_SIZE}",
        f"{LEVER_URL}?mode=json&skip={LEVER_PAGE_SIZE}&limit={LEVER_PAGE_SIZE}",
    ]


def test_lever_eu_uses_regional_host() -> None:
    client, opener, _ = fake_client({"https://api.eu.lever.co/v0/postings/exampleco": [[lever_posting(1)]]})
    jobs = fetch_jobs(client, "lever_eu", "exampleco", "Example Co")
    assert jobs[0].provider == "lever_eu"
    assert opener.urls[0].startswith("https://api.eu.lever.co/")


def test_greenhouse_uses_content_endpoint() -> None:
    client, opener, _ = fake_client({GH_URL: [copy.deepcopy(GREENHOUSE)]})
    assert len(fetch_jobs(client, "greenhouse", "examplelabs", "Example Labs")) == 3
    assert opener.urls == [f"{GH_URL}?content=true"]


@pytest.mark.parametrize("token", ["../admin", "a/b", "evil.com", "", "x" * 101, "a b", "%2e%2e"])
def test_invalid_board_tokens_never_reach_the_network(token: str) -> None:
    client, opener, _ = fake_client({})
    with pytest.raises(SourceError, match="Invalid"):
        fetch_jobs(client, "greenhouse", token, "x")
    assert opener.urls == []


def test_only_allowlisted_hosts() -> None:
    client, opener, _ = fake_client({})
    with pytest.raises(SourceError, match="allowlisted"):
        client.get_json("https://evil.example.com/v1/boards/x/jobs")
    with pytest.raises(SourceError, match="Invalid"):
        fetch_jobs(client, "workday", "acme", "x")
    assert opener.urls == []


def test_404_is_not_retried() -> None:
    client, opener, _ = fake_client({})
    with pytest.raises(SourceError, match="404"):
        fetch_jobs(client, "greenhouse", "missing", "x")
    assert len(opener.urls) == 1


def test_server_errors_retry_with_bounded_attempts() -> None:
    client, opener, sleeps = fake_client({GH_URL: [http_error(GH_URL, 503)]})
    with pytest.raises(SourceError, match="HTTP 503"):
        fetch_jobs(client, "greenhouse", "examplelabs", "x")
    assert len(opener.urls) == MAX_RETRIES + 1
    assert sleeps == [1.0, 2.0]  # exponential backoff


def test_rate_limit_honors_retry_after_then_succeeds() -> None:
    client, opener, sleeps = fake_client({GH_URL: [http_error(GH_URL, 429, "7"), copy.deepcopy(GREENHOUSE)]})
    assert len(fetch_jobs(client, "greenhouse", "examplelabs", "x")) == 3
    assert len(opener.urls) == 2
    assert 7.0 in sleeps


def test_timeouts_retry_then_fail() -> None:
    client, opener, _ = fake_client({GH_URL: [TimeoutError()]})
    with pytest.raises(SourceError, match="Timed out"):
        fetch_jobs(client, "greenhouse", "examplelabs", "x")
    assert len(opener.urls) == MAX_RETRIES + 1


def test_redirects_and_client_errors_are_not_followed() -> None:
    client, opener, _ = fake_client({GH_URL: [http_error(GH_URL, 302)]})
    with pytest.raises(SourceError, match="HTTP 302"):
        fetch_jobs(client, "greenhouse", "examplelabs", "x")
    assert len(opener.urls) == 1


def test_polite_spacing_between_requests() -> None:
    now = [100.0]
    sleeps: list[float] = []
    client = HttpClient(opener=lambda req, timeout: (_ for _ in ()).throw(http_error("u", 404)),
                        sleep=sleeps.append, clock=lambda: now[0], min_interval=1.0)
    for _ in range(2):
        with pytest.raises(SourceError):
            client.get_json(f"{GH_URL}?content=true")
        now[0] += 0.25
    assert sleeps == [0.75]


def test_canonical_url() -> None:
    assert canonical_url("HTTP://Jobs.Example.com/a/b/?utm_source=x&gh_jid=5#apply") == "https://jobs.example.com/a/b?gh_jid=5"
    # Query-identified postings stay distinct.
    assert canonical_url("https://x.com/careers?gh_jid=1") != canonical_url("https://x.com/careers?gh_jid=2")
    assert canonical_url(None) is None
