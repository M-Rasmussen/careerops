import pytest

from app.services.aggregators import (
    BudgetExceeded,
    fetch_himalayas,
    fetch_jsearch,
    jsearch_queries,
    normalize_domain,
    parse_himalayas,
    parse_jsearch,
)
from app.services.job_sources import _NoRedirect, ats_job_key, parse_ashby, parse_ats_url, verify_board
from tests.discovery_fixtures import HIMALAYAS_URL, JSEARCH_URL, fake_client, himalayas_job, http_error, jsearch_job

# --- Himalayas ---


def test_himalayas_parses_multiple_companies() -> None:
    jobs = parse_himalayas({"jobs": [himalayas_job(1), himalayas_job(2, company="Beta Cloud", slug="beta-cloud")]})
    assert {j.company for j in jobs} == {"Acme Remote", "Beta Cloud"}
    first = jobs[0]
    assert first.provider == "himalayas" and first.workplace_type == "remote"
    assert first.location == "Remote - United States"
    assert first.posting_url.endswith("/apply")  # Himalayas requires applying through applicationLink
    assert (first.salary_min, first.salary_max, first.salary_interval) == (120000, 150000, "year")
    assert first.seniority == "mid"
    assert first.company_alias == ("himalayas", "acme-remote")
    assert first.description == "Build APIs."
    assert first.ats_job_key is None  # a Himalayas link is not an ATS link


def test_himalayas_missing_fields_are_not_invented() -> None:
    sparse = {"guid": "g1", "title": "Platform Engineer", "companyName": "NoSlug Co", "minSalary": 100000}
    [job] = parse_himalayas({"jobs": [sparse, {"guid": "g2", "title": "No company"}, {"title": "no guid"}]})
    assert job.location is None  # restrictions absent: not assumed worldwide or US
    assert job.salary_interval is None  # period absent: not assumed annual
    assert job.seniority is None and job.company_alias is None and job.posting_url is None
    worldwide = parse_himalayas({"jobs": [himalayas_job(3, locationRestrictions=[])]})[0]
    assert worldwide.location == "Remote - Worldwide"  # documented: empty list means worldwide
    many = [f"Country {i:02d}" for i in range(40)] + ["United States"]
    long_list = parse_himalayas({"jobs": [himalayas_job(5, locationRestrictions=many)]})[0]
    assert long_list.location.startswith("Remote - United States, Country 00") and len(long_list.location) <= 200
    mixed = parse_himalayas({"jobs": [himalayas_job(4, seniority=["Entry-level", "Senior"])]})[0]
    assert mixed.seniority is None  # ambiguous: not picked


def test_himalayas_bounded_pagination_and_partial_query_failure() -> None:
    client, opener, _ = fake_client({
        f"{HIMALAYAS_URL}?q=backend": [{"jobs": [himalayas_job(1)]}, {"jobs": [himalayas_job(2)]}],
        f"{HIMALAYAS_URL}?q=sdet": [http_error(HIMALAYAS_URL, 400)],
    })
    jobs, errors = fetch_himalayas(client, ["backend engineer", "sdet"], max_pages=2)
    assert len(jobs) == 2
    assert len([u for u in opener.urls if "q=backend" in u]) == 2  # never more than max_pages
    assert "page=2" in opener.urls[1]
    assert errors and "sdet" in errors[0]


def test_himalayas_all_queries_failing_raises() -> None:
    client, _, _ = fake_client({})
    from app.services.job_sources import SourceError

    with pytest.raises(SourceError):
        fetch_himalayas(client, ["backend engineer"], max_pages=1)


# --- JSearch ---


def test_jsearch_parsing_and_identity_evidence() -> None:
    [job] = parse_jsearch({"status": "OK", "data": [jsearch_job(1)]})
    assert job.provider == "jsearch" and job.company == "Example Payments"
    assert job.company_domain == "examplepayments.com"
    assert job.location == "Jersey City, NJ, US"
    assert job.workplace_type is None  # not remote, but on-site isn't assumed
    assert job.salary_interval == "year" and job.salary_currency is None  # currency not stated: not assumed
    assert job.ats_job_key == "lever:00000001-aaaa-bbbb-cccc-000000000000"
    # Newer search-v2 shape.
    assert len(parse_jsearch({"data": {"jobs": [jsearch_job(2)], "cursor": "x"}})) == 1


def test_jsearch_sends_key_only_in_headers_and_reserves_every_attempt() -> None:
    client, opener, _ = fake_client({JSEARCH_URL: [http_error(JSEARCH_URL, 503)]})
    reserved: list[str] = []
    queries = jsearch_queries(["software engineer"], ["nj"])
    from app.services.job_sources import SourceError

    with pytest.raises(SourceError) as failure:
        fetch_jsearch(client, "secret-key", queries, reserved.append)
    assert len(reserved) == 3  # first attempt + 2 retries all count against the budget
    assert all("secret-key" not in u for u in opener.urls)
    assert opener.headers[0]["X-rapidapi-key"] == "secret-key"
    assert "secret-key" not in str(failure.value) and "HTTP 503" in str(failure.value)


def test_jsearch_budget_stop_sends_nothing() -> None:
    client, opener, _ = fake_client({JSEARCH_URL: [{"data": []}]})

    def reserve(key: str) -> None:
        raise BudgetExceeded("Request budget reached")

    jobs, errors = fetch_jsearch(client, "k", jsearch_queries(["sdet"], ["nj", "nyc"]), reserve)
    assert opener.urls == [] and jobs == []
    assert errors == ["Request budget reached"]


def test_jsearch_queries_cover_geographies() -> None:
    qs = jsearch_queries(["backend engineer"], ["nj", "nyc", "remote_us"])
    assert [q["query"] for q in qs] == ["backend engineer in New Jersey", "backend engineer in New York, NY", "backend engineer"]
    assert qs[2]["work_from_home"] == "true" and all(q["num_pages"] == "1" for q in qs)


@pytest.mark.parametrize(
    ("value", "expected"),
    [("https://www.Stripe.com/about", "stripe.com"), ("stripe.com", "stripe.com"), ("http://127.0.0.1", None),
     ("https://user@evil.com", None), ("https://evil.com:8080", None), ("not a domain", None), ("", None)],
)
def test_normalize_domain(value: str, expected: str | None) -> None:
    assert normalize_domain(value) == expected


# --- ATS URL detection ---


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://boards.greenhouse.io/acme/jobs/12345", ("greenhouse", "acme", "12345")),
        ("https://job-boards.greenhouse.io/acme/jobs/12345?gh_src=x", ("greenhouse", "acme", "12345")),
        ("https://boards.greenhouse.io/embed/job_app?for=acme&token=999", ("greenhouse", "acme", "999")),
        ("https://boards.greenhouse.io/acme", ("greenhouse", "acme", None)),
        ("https://jobs.lever.co/acme/1f2e3d4c-aaaa-bbbb-cccc-000000000000/apply", ("lever", "acme", "1f2e3d4c-aaaa-bbbb-cccc-000000000000")),
        ("https://jobs.eu.lever.co/acme-eu/abc-123", ("lever_eu", "acme-eu", "abc-123")),
        ("https://jobs.ashbyhq.com/Acme/7a8b9c0d-0000-1111-2222-333333333333", ("ashby", "Acme", "7a8b9c0d-0000-1111-2222-333333333333")),
    ],
)
def test_ats_url_detection(url: str, expected: tuple) -> None:
    ref = parse_ats_url(url)
    assert ref is not None and (ref.provider, ref.board, ref.posting_id) == expected


@pytest.mark.parametrize(
    "url",
    [
        "https://acme.com/careers?gh_jid=123",  # company site: board unknown, never guessed
        "https://boards.greenhouse.io.evil.com/acme/jobs/1",  # suffix trick
        "https://evil.com/boards.greenhouse.io/acme",
        "https://user:pw@jobs.lever.co/acme/1",  # credentials
        "https://jobs.lever.co:8443/acme/1",  # unusual port
        "http://169.254.169.254/latest/meta-data",  # private/metadata IP
        "javascript:alert(1)",
        "https://jobs.lever.co/",  # no board
        "https://jobs.lever.co/../etc/passwd",
        "https://jobs.lever.co/a%2Fb/1",  # encoded slash
        "https://boards.greenhouse.io/embed/job_app?token=1",  # embed without board
        "https://himalayas.app/companies/acme/jobs/x",  # aggregator page, not an ATS
        "https://jobs.lever.co:abc/acme",  # malformed port
        None,
    ],
)
def test_untrusted_urls_are_rejected(url: str | None) -> None:
    assert parse_ats_url(url) is None


def test_ats_job_key_is_region_agnostic() -> None:
    assert ats_job_key(parse_ats_url("https://jobs.eu.lever.co/acme/abc-1")) == "lever:abc-1"
    assert ats_job_key(parse_ats_url("https://jobs.lever.co/acme")) is None


def test_redirects_are_never_followed() -> None:
    assert _NoRedirect().redirect_request(None, None, 302, "Found", {}, "http://169.254.169.254/") is None


def test_http_and_unlisted_hosts_refused_before_any_request() -> None:
    client, opener, _ = fake_client({})
    from app.services.job_sources import SourceError

    for url in ("http://api.lever.co/v0/postings/acme", "https://10.0.0.1/x", "https://api.lever.co.evil.com/x"):
        with pytest.raises(SourceError, match="allowlisted"):
            client.get_json(url)
    assert opener.urls == []


# --- Ashby and verification ---


def test_ashby_parsing_uses_job_url_identity() -> None:
    payload = {"apiVersion": "1", "jobs": [
        {"title": "Backend Engineer", "location": "New York, NY", "workplaceType": "Hybrid", "isListed": True,
         "descriptionHtml": "<p>Hi</p>", "publishedAt": "2026-11-01T00:00:00.000+00:00",
         "jobUrl": "https://jobs.ashbyhq.com/acme/11111111-2222-3333-4444-555555555555",
         "compensation": {"summaryComponents": [{"compensationType": "Salary", "interval": "1 YEAR", "currencyCode": "USD", "minValue": 150000, "maxValue": 180000}]}},
        {"title": "Unlisted", "isListed": False, "jobUrl": "https://jobs.ashbyhq.com/acme/22222222-2222-3333-4444-555555555555"},
        {"title": "Other board", "jobUrl": "https://jobs.ashbyhq.com/other/33333333-2222-3333-4444-555555555555"},
        {"title": "No url"},
    ]}
    [job] = parse_ashby(payload, "Acme", "acme")
    assert job.source_posting_id == "11111111-2222-3333-4444-555555555555"
    assert job.workplace_type == "hybrid" and job.salary_interval == "1 year" and job.salary_max == 180000
    assert job.ats_job_key == "ashby:11111111-2222-3333-4444-555555555555"


@pytest.mark.parametrize(
    ("provider", "url", "payload"),
    [
        ("greenhouse", "https://boards-api.greenhouse.io/v1/boards/acme", {"name": "Acme", "content": ""}),
        ("lever", "https://api.lever.co/v0/postings/acme?mode=json&limit=1", []),
        ("ashby", "https://api.ashbyhq.com/posting-api/job-board/acme", {"jobs": []}),
    ],
)
def test_verify_board_one_request(provider: str, url: str, payload: object) -> None:
    client, opener, _ = fake_client({url: [payload]})
    verify_board(client, provider, "acme")
    assert opener.urls == [url]


def test_verify_board_rejects_wrong_shape_and_bad_token() -> None:
    from app.services.job_sources import SourceError

    client, opener, _ = fake_client({"https://boards-api.greenhouse.io/v1/boards/acme": [{"unexpected": 1}]})
    with pytest.raises(SourceError, match="did not answer"):
        verify_board(client, "greenhouse", "acme")
    with pytest.raises(SourceError, match="Invalid"):
        verify_board(client, "lever", "../x")
    assert len(opener.urls) == 1
