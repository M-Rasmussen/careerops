"""Synthetic ATS API payloads (fictional companies, shaped like the real public responses) and a fake opener."""

import io
import json
import urllib.error
from email.message import Message
from typing import Any

from app.services.job_sources import HttpClient

GREENHOUSE = {
    "jobs": [
        {
            "id": 4001,
            "title": "Senior Backend Engineer",
            "company_name": "Example Labs",
            "updated_at": "2026-11-20T10:15:00-05:00",
            "location": {"name": "Remote - US"},
            "absolute_url": "https://boards.greenhouse.io/examplelabs/jobs/4001?gh_src=abc",
            "content": "&lt;p&gt;Build &lt;strong&gt;Python&lt;/strong&gt; APIs.&lt;/p&gt;&lt;ul&gt;&lt;li&gt;FastAPI&lt;/li&gt;&lt;/ul&gt;",
        },
        {
            "id": 4002,
            "title": "Marketing Manager",
            "updated_at": "2026-11-19T08:00:00Z",
            "location": {"name": "Berlin"},
            "absolute_url": "https://boards.greenhouse.io/examplelabs/jobs/4002",
            "content": "&lt;p&gt;Campaigns.&lt;/p&gt;",
        },
        # Sparse record: only identity and title.
        {"id": 4003, "title": "Data Engineer"},
        # Unusable: no title.
        {"id": 4004},
    ]
}


def lever_posting(i: int, **extra: Any) -> dict[str, Any]:
    return {
        "id": f"lever-{i}",
        "text": f"Software Engineer {i}",
        "categories": {"location": "Denver, CO", "commitment": "Full-time"},
        "workplaceType": "hybrid",
        "hostedUrl": f"https://jobs.lever.co/exampleco/lever-{i}",
        "createdAt": 1_763_000_000_000,
        "descriptionPlain": "Work on the platform.",
        "lists": [{"text": "Requirements", "content": "<li>Python</li><li>SQL</li>"}],
        "additionalPlain": "Benefits included.",
        **extra,
    }


class FakeResponse(io.BytesIO):
    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()


def http_error(url: str, code: int, retry_after: str | None = None) -> urllib.error.HTTPError:
    headers = Message()
    if retry_after:
        headers["Retry-After"] = retry_after
    return urllib.error.HTTPError(url, code, "error", headers, None)


class FakeOpener:
    """Maps URL prefix -> list of responses (payload, or an Exception to raise), consumed in order;
    the last entry repeats. Records every requested URL."""

    def __init__(self, routes: dict[str, list[Any]]) -> None:
        self.routes = routes
        self.urls: list[str] = []
        self.headers: list[dict[str, str]] = []

    def __call__(self, req: Any, timeout: float) -> FakeResponse:
        assert timeout > 0
        url = req.full_url
        self.urls.append(url)
        self.headers.append(dict(req.header_items()))
        for prefix, responses in self.routes.items():
            if url.startswith(prefix):
                item = responses.pop(0) if len(responses) > 1 else responses[0]
                if isinstance(item, Exception):
                    raise item
                return FakeResponse(json.dumps(item).encode())
        raise http_error(url, 404)


def fake_client(routes: dict[str, list[Any]]) -> tuple[HttpClient, FakeOpener, list[float]]:
    opener = FakeOpener(routes)
    sleeps: list[float] = []
    # min_interval=0: only retry backoff shows up in `sleeps` (polite spacing is tested on its own).
    return HttpClient(opener=opener, sleep=sleeps.append, min_interval=0), opener, sleeps


GH_URL = "https://boards-api.greenhouse.io/v1/boards/examplelabs/jobs"
LEVER_URL = "https://api.lever.co/v0/postings/exampleco"


HIMALAYAS_URL = "https://himalayas.app/jobs/api/search"
JSEARCH_URL = "https://jsearch.p.rapidapi.com/search"


def himalayas_job(i: int, company: str = "Acme Remote", slug: str | None = "acme-remote", **extra: Any) -> dict[str, Any]:
    return {
        "guid": f"https://himalayas.app/companies/{slug or 'x'}/jobs/backend-{i}",
        "title": f"Backend Engineer {i}",
        "companyName": company,
        "companySlug": slug,
        "employmentType": "Full Time",
        "minSalary": 120000,
        "maxSalary": 150000,
        "currency": "USD",
        "salaryPeriod": "annual",
        "seniority": ["Mid-level"],
        "locationRestrictions": ["United States"],
        "description": "<p>Build APIs.</p>",
        "pubDate": 1_763_000_000,
        "applicationLink": f"https://himalayas.app/companies/{slug or 'x'}/jobs/backend-{i}/apply",
        **extra,
    }


def jsearch_job(i: int, **extra: Any) -> dict[str, Any]:
    return {
        "job_id": f"js-{i}",
        "job_title": "Software Engineer II",
        "employer_name": "Example Payments",
        "employer_website": "https://www.examplepayments.com/",
        "job_apply_link": f"https://jobs.lever.co/examplepay/0000000{i}-aaaa-bbbb-cccc-000000000000",
        "job_apply_is_direct": True,
        "apply_options": [],
        "job_description": "Build payment APIs.",
        "job_is_remote": False,
        "job_city": "Jersey City",
        "job_state": "NJ",
        "job_country": "US",
        "job_posted_at_datetime_utc": "2026-11-20T12:00:00.000Z",
        "job_min_salary": 130000,
        "job_max_salary": 160000,
        "job_salary_period": "YEAR",
        **extra,
    }
