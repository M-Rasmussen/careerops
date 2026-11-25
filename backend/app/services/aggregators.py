"""Multi-company job feeds: Himalayas (free, no key) and JSearch (optional, metered, RapidAPI).

Only fixed hosts are contacted. Request budgets for metered providers are enforced by the caller through
HttpClient's `before_request` hook, so retries count too.
"""

import hashlib
import json
import re
import urllib.parse
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from app.services.job_sources import (
    MAX_DESCRIPTION_CHARS,
    HttpClient,
    NormalizedJob,
    SourceError,
    _http_url,
    _int,
    _parse_iso,
    _str,
    ats_job_key,
    canonical_url,
    employment_type,
    html_to_text,
    parse_ats_url,
)

HIMALAYAS_SEARCH_URL = "https://himalayas.app/jobs/api/search"
HIMALAYAS_HOME = "https://himalayas.app"
JSEARCH_URL = "https://jsearch.p.rapidapi.com/search"
JSEARCH_HOST = "jsearch.p.rapidapi.com"

HIMALAYAS_SENIORITY = {
    "entry-level": "entry", "mid-level": "mid", "senior": "senior",
    "manager": "manager", "director": "manager", "executive": "manager",
}
SALARY_PERIODS = {"annual": "year", "year": "year", "yearly": "year", "hourly": "hour", "hour": "hour",
                  "monthly": "month", "month": "month", "weekly": "week", "week": "week"}
DOMAIN_RE = re.compile(r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")


def normalize_domain(value: str | None) -> str | None:
    """Lowercased host without "www."; None unless it is a plain DNS name (no IPs, ports, or credentials)."""
    if not value or not isinstance(value, str) or len(value) > 2048:
        return None
    text = value.strip().lower()
    try:
        parts = urllib.parse.urlsplit(text if "://" in text else f"https://{text}")
        if parts.port is not None or parts.username or parts.password:
            return None
    except ValueError:
        return None
    host = (parts.hostname or "").rstrip(".")
    host = host[4:] if host.startswith("www.") else host
    return host if DOMAIN_RE.fullmatch(host) else None


def _period(value: Any) -> str | None:
    return SALARY_PERIODS.get(str(value).strip().lower()) if value else None


# --- Himalayas ----------------------------------------------------------------------------------


def parse_himalayas(payload: Any) -> list[NormalizedJob]:
    if not isinstance(payload, dict) or not isinstance(payload.get("jobs"), list):
        raise SourceError("Unexpected Himalayas response shape")
    jobs = []
    for item in payload["jobs"]:
        if not isinstance(item, dict) or not _str(item.get("guid"), 500) or not _str(item.get("title"), 300):
            continue
        company = _str(item.get("companyName"), 200)
        if not company:
            continue
        restrictions = item.get("locationRestrictions")
        if isinstance(restrictions, list):
            names = [r.strip() for r in restrictions if isinstance(r, str) and r.strip()]
            # US entries first: long country lists are truncated to the column size and must not lose them.
            names.sort(key=lambda n: n.lower() not in ("united states", "usa", "us", "united states of america"))
            # Documented: an empty list means worldwide.
            location = _str("Remote - " + (", ".join(names) if names else "Worldwide"), 200)
        else:
            location = None
        raw_levels = item.get("seniority")
        levels = {HIMALAYAS_SENIORITY.get(str(x).lower()) for x in (raw_levels if isinstance(raw_levels, list) else [raw_levels]) if x}
        levels.discard(None)
        url = _http_url(item.get("applicationLink"))
        slug = _str(item.get("companySlug"), 200)
        pub = item.get("pubDate")
        jobs.append(
            NormalizedJob(
                provider="himalayas",
                source_posting_id=item["guid"].strip()[:500],
                company=company,
                title=_str(item["title"], 300) or "",
                location=location,
                workplace_type="remote",  # Himalayas lists remote jobs only
                description=html_to_text(item.get("description")) if isinstance(item.get("description"), str) else None,
                # Himalayas' terms: Apply must go through applicationLink.
                posting_url=url,
                canonical_url=canonical_url(url),
                salary_min=_int(item.get("minSalary")),
                salary_max=_int(item.get("maxSalary")),
                salary_currency=_str(item.get("currency"), 3),
                salary_interval=_period(item.get("salaryPeriod")),  # not assumed when absent
                source_updated_at=datetime.fromtimestamp(pub, timezone.utc) if isinstance(pub, (int, float)) and pub > 0 else None,
                seniority=levels.pop() if len(levels) == 1 else None,
                employment_type=employment_type(item.get("employmentType"), item.get("title")),
                # pubDate is documented as the last-updated time, so it is not used as the posting date.
                ats_job_key=ats_job_key(parse_ats_url(url)),
                company_alias=("himalayas", slug.lower()) if slug else None,
                ats_urls=(url,) if url else (),
            )
        )
    return jobs


def fetch_himalayas(
    client: HttpClient, queries: list[str], max_pages: int, country: str = "US"
) -> tuple[list[NormalizedJob], list[str]]:
    """One search per query (role family), up to `max_pages` pages each. Returns jobs and per-query errors;
    raises only if every query failed."""
    jobs: list[NormalizedJob] = []
    errors: list[str] = []
    for q in queries:
        for page in range(1, max_pages + 1):
            params = urllib.parse.urlencode({"q": q, "country": country, "sort": "recent", "page": page})
            try:
                batch = parse_himalayas(client.get_json(f"{HIMALAYAS_SEARCH_URL}?{params}"))
            except SourceError as exc:
                errors.append(f'"{q}": {exc}')
                break
            jobs.extend(batch)
            if not batch:
                break
    if errors and len(errors) == len(queries) and not jobs:
        raise SourceError("; ".join(errors[:3]))
    return jobs, errors


# --- JSearch ------------------------------------------------------------------------------------

JSEARCH_GEOGRAPHY = {
    "nj": ("in New Jersey", {}),
    "nyc": ("in New York, NY", {}),
    "remote_us": ("", {"work_from_home": "true"}),
}


def jsearch_queries(phrases: list[str], geographies: list[str]) -> list[dict[str, str]]:
    """Role phrase x geography, ordered so the first few requests cover every geography."""
    out = []
    for phrase in phrases:
        for geo in geographies:
            suffix, extra = JSEARCH_GEOGRAPHY[geo]
            out.append({"query": f"{phrase} {suffix}".strip(), "page": "1", "num_pages": "1",
                        "country": "us", "date_posted": "week", **extra})
    return out


def query_key(params: dict[str, str]) -> str:
    return hashlib.sha256(json.dumps(params, sort_keys=True).encode()).hexdigest()


def parse_jsearch(payload: Any) -> list[NormalizedJob]:
    data = payload.get("data") if isinstance(payload, dict) else None
    if isinstance(data, dict):  # newer search-v2 shape: {"jobs": [...], "cursor": ...}
        data = data.get("jobs")
    if not isinstance(data, list):
        raise SourceError("Unexpected JSearch response shape")
    jobs = []
    for item in data:
        if not isinstance(item, dict) or not _str(item.get("job_id"), 500) or not _str(item.get("job_title"), 300):
            continue
        company = _str(item.get("employer_name"), 200)
        if not company:
            continue
        place = ", ".join(p for p in (item.get("job_city"), item.get("job_state"), item.get("job_country")) if isinstance(p, str) and p)
        apply_link = _http_url(item.get("job_apply_link"))
        options = [o.get("apply_link") for o in item.get("apply_options") or [] if isinstance(o, dict)]
        candidates = tuple(u for u in (apply_link, *(_http_url(o) for o in options)) if u)
        keys = [k for k in (ats_job_key(parse_ats_url(u)) for u in candidates) if k]
        description = item.get("job_description")
        jobs.append(
            NormalizedJob(
                provider="jsearch",
                source_posting_id=item["job_id"].strip()[:500],
                company=company,
                title=_str(item["job_title"], 300) or "",
                location=_str(place, 200) or _str(item.get("job_location"), 200),
                workplace_type="remote" if item.get("job_is_remote") is True else None,
                description=description.strip()[:MAX_DESCRIPTION_CHARS] or None if isinstance(description, str) else None,
                posting_url=apply_link,
                canonical_url=canonical_url(apply_link),
                salary_min=_int(item.get("job_min_salary")),
                salary_max=_int(item.get("job_max_salary")),
                salary_currency=_str(item.get("job_salary_currency"), 3),
                salary_interval=_period(item.get("job_salary_period")),
                source_updated_at=None,
                posted_at=_parse_iso(item.get("job_posted_at_datetime_utc")),
                employment_type=employment_type(item.get("job_employment_type"), item.get("job_title")),
                ats_job_key=keys[0] if keys else None,
                company_domain=normalize_domain(item.get("employer_website")),
                ats_urls=candidates,
            )
        )
    return jobs


def fetch_jsearch(
    client: HttpClient,
    api_key: str,
    queries: list[dict[str, str]],
    reserve: Callable[[str], None],
) -> tuple[list[NormalizedJob], list[str]]:
    """`reserve(query_key)` is called before every attempt and raises SourceError when the budget is spent."""
    jobs: list[NormalizedJob] = []
    errors: list[str] = []
    headers = {"X-RapidAPI-Key": api_key, "X-RapidAPI-Host": JSEARCH_HOST}
    for params in queries:
        key = query_key(params)
        try:
            payload = client.get_json(
                f"{JSEARCH_URL}?{urllib.parse.urlencode(params)}", headers=headers, before_request=lambda: reserve(key)
            )
            jobs.extend(parse_jsearch(payload))
        except BudgetExceeded as exc:
            errors.append(str(exc))
            break
        except SourceError as exc:
            errors.append(f'"{params["query"]}": {exc}')  # status only; never the key or response body
    if errors and not jobs and len(errors) >= len(queries):
        raise SourceError("; ".join(errors[:3]))
    return jobs, errors


class BudgetExceeded(SourceError):
    """A metered provider's local request budget would be exceeded; no request was sent."""
