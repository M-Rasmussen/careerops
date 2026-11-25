"""Read-only clients for public ATS job-board APIs (Greenhouse, Lever, Ashby) and ATS URL detection.

Only fixed, allowlisted hosts are contacted; boards are named by a validated identifier, never a URL. URLs found
in job data are only *parsed* for an ATS board name; they are never fetched.
Responses are normalized into `NormalizedJob`. Missing fields stay None: nothing is inferred except
workplace type from explicit "remote"/"hybrid" wording in a location string.
"""

import html
import json
import re
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Any

# Fixed endpoints per provider. "lever_eu" is Lever's documented EU-region API host.
PROVIDER_URLS = {
    "greenhouse": "https://boards-api.greenhouse.io/v1/boards/{board}/jobs?content=true",
    "lever": "https://api.lever.co/v0/postings/{board}?mode=json",
    "lever_eu": "https://api.eu.lever.co/v0/postings/{board}?mode=json",
    "ashby": "https://api.ashbyhq.com/posting-api/job-board/{board}?includeCompensation=true",
}
# Cheap single-request existence checks used to verify a board before monitoring it.
VERIFY_URLS = {
    "greenhouse": "https://boards-api.greenhouse.io/v1/boards/{board}",
    "lever": "https://api.lever.co/v0/postings/{board}?mode=json&limit=1",
    "lever_eu": "https://api.eu.lever.co/v0/postings/{board}?mode=json&limit=1",
    "ashby": "https://api.ashbyhq.com/posting-api/job-board/{board}",
}
# Public pages that identify a board: provider -> canonical board URL.
BOARD_PAGE_URLS = {
    "greenhouse": "https://job-boards.greenhouse.io/{board}",
    "lever": "https://jobs.lever.co/{board}",
    "lever_eu": "https://jobs.eu.lever.co/{board}",
    "ashby": "https://jobs.ashbyhq.com/{board}",
}
PROVIDERS = tuple(PROVIDER_URLS)
AGGREGATOR_HOSTS = {"himalayas.app", "jsearch.p.rapidapi.com"}
ALLOWED_HOSTS = {urllib.parse.urlsplit(u).hostname for u in PROVIDER_URLS.values()} | AGGREGATOR_HOSTS
BOARD_TOKEN_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_-]{0,99}$"

USER_AGENT = "CareerOps/0.5 (personal job tracker; read-only)"
TIMEOUT_SECONDS = 15
MAX_RETRIES = 2
MIN_REQUEST_INTERVAL = 1.0  # polite gap between any two requests in one run
MAX_RESPONSE_BYTES = 30_000_000  # large Greenhouse boards with content=true run to ~10 MB
LEVER_PAGE_SIZE = 100
LEVER_MAX_PAGES = 20
MAX_DESCRIPTION_CHARS = 30_000  # matches the AI-matching input limit so saved jobs stay analyzable
TRACKING_PARAMS = {"gh_src", "lever-source", "lever-origin", "source", "ref", "src"}


class SourceError(Exception):
    """A source fetch failed. The message is safe to store and show (no response bodies)."""


@dataclass(frozen=True)
class NormalizedJob:
    provider: str
    source_posting_id: str
    company: str
    title: str
    location: str | None
    workplace_type: str | None  # remote | hybrid | onsite | None (unknown)
    description: str | None
    posting_url: str | None
    canonical_url: str | None
    salary_min: int | None
    salary_max: int | None
    salary_currency: str | None
    salary_interval: str | None
    source_updated_at: datetime | None
    seniority: str | None = None  # only when the provider states it
    employment_type: str | None = None  # only when the provider states it
    posted_at: datetime | None = None  # original publish date; never an "updated" timestamp
    requisition_id: str | None = None
    ats_job_key: str | None = None  # "<ats>:<posting id>" when known
    application_url: str | None = None  # the provider's own apply-form URL field; never constructed
    # Not stored on the job: identity evidence used to resolve the employer.
    company_domain: str | None = None  # provider-reported employer website domain
    company_alias: tuple[str, str] | None = None  # (provider, provider's own company id)
    ats_urls: tuple[str, ...] = ()  # URLs that may name an ATS board (parsed, never fetched)


# Columns of discovered_jobs filled from a NormalizedJob.
JOB_COLUMNS = (
    "provider", "source_posting_id", "company", "title", "location", "workplace_type", "description",
    "posting_url", "canonical_url", "salary_min", "salary_max", "salary_currency", "salary_interval",
    "source_updated_at", "seniority", "requisition_id", "ats_job_key", "employment_type", "posted_at",
    "application_url",
)
EMPLOYMENT_TYPES = ("Full-time", "Part-time", "Contract", "Contract-to-hire", "Temporary", "Internship")
_C2H = re.compile(r"contract[\s-]*(?:to|2)[\s-]*(?:hire|perm(?:anent)?)|\bc2h\b|temp[\s-]*to[\s-]*(?:hire|perm)", re.IGNORECASE)
_CONTRACT = re.compile(r"\bcontract(?:or|ing)?\b|\bfreelance\b|\b1099\b|\bc2c\b|\bcorp[\s-]*to[\s-]*corp\b", re.IGNORECASE)
# Titles need a clear form: "(Contract)", "- Contract", ", Contractor", "Freelance ...", or "Contract" right before
# an engineering word. "Contract Manager" or "Contracting Officer" are jobs about contracts, not contract jobs.
_CONTRACT_TITLE = re.compile(
    r"[(\[,\-–—|/:]\s*(?:\w+\s+)?contract(?:or)?\b|\bfreelance\b|\bcontract(?:or)?\s*[)\]]|"
    r"\bcontract\s+(?:software|backend|back-end|frontend|front-end|full[\s-]?stack|data|devops|cloud|qa|test|sdet|"
    r"platform|site reliability|python|java|engineer|developer|programmer)\b",
    re.IGNORECASE,
)
_INTERN = re.compile(r"\bintern(?:ship)?\b|\bco-?op\b", re.IGNORECASE)
_TEMP = re.compile(r"\btemp(?:orary)?\b|\bseasonal\b", re.IGNORECASE)
_PART = re.compile(r"\bpart[\s_-]*time\b|\bparttime\b", re.IGNORECASE)
_FULL = re.compile(r"\bfull[\s_-]*time\b|\bfulltime\b|\bpermanent\b|\bregular\b", re.IGNORECASE)


def employment_type(value: Any, title: str | None = None) -> str | None:
    """One of EMPLOYMENT_TYPES, or None (unknown). Conservative: explicit contract wording in the provider's type
    or the title wins over "full-time" (a contract can be full-time hours); contract-to-hire needs explicit words;
    unrecognized wording is unknown, never guessed."""
    raw = value.strip() if isinstance(value, str) else ""
    title = title or ""
    if _C2H.search(raw) or _C2H.search(title):
        return "Contract-to-hire"
    if _CONTRACT.search(raw) or _CONTRACT_TITLE.search(title):
        return "Contract"
    for rx, label in ((_INTERN, "Internship"), (_TEMP, "Temporary"), (_PART, "Part-time"), (_FULL, "Full-time")):
        if rx.search(raw):  # the remaining types come only from the provider's own field, not the title
            return label
    return None


def _epoch_ms(value: Any) -> datetime | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
        try:
            return datetime.fromtimestamp(value / 1000, timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    return None


# --- HTTP ---------------------------------------------------------------------------------------


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    # A redirect could leave the allowlisted host; treat it as an error instead of following it.
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


_opener = urllib.request.build_opener(_NoRedirect)
Opener = Callable[..., Any]


class HostLimiter:
    """Minimum gap between requests to one host, shared by every client (thread) that uses it."""

    def __init__(self, min_interval: float = MIN_REQUEST_INTERVAL, sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._min_interval, self._sleep, self._clock = min_interval, sleep, clock
        self._next: dict[str, float] = {}
        self._lock = threading.Lock()

    def wait(self, host: str) -> None:
        with self._lock:  # reserve a slot, then sleep outside the lock
            now = self._clock()
            start = max(now, self._next.get(host, now))
            self._next[host] = start + self._min_interval
        if start > now:
            self._sleep(start - now)


class HttpClient:
    """GET JSON with timeout, polite spacing, bounded retries on transient failures, and a size cap. With a shared
    `limiter`, spacing is per host across all clients using it (concurrent workers); otherwise per client."""

    def __init__(
        self,
        opener: Opener = _opener.open,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        min_interval: float = MIN_REQUEST_INTERVAL,
        limiter: HostLimiter | None = None,
    ) -> None:
        self._open, self._sleep, self._clock, self._min_interval = opener, sleep, clock, min_interval
        self._limiter = limiter
        self._last: float | None = None
        self.requests = 0

    def _wait_turn(self, host: str = "") -> None:
        if self._limiter is not None:
            self._limiter.wait(host)
            return
        if self._last is not None:
            gap = self._min_interval - (self._clock() - self._last)
            if gap > 0:
                self._sleep(gap)
        self._last = self._clock()

    def get_json(
        self, url: str, headers: dict[str, str] | None = None, before_request: Callable[[], None] | None = None
    ) -> Any:
        """`before_request` runs before every attempt (retries included) and may raise SourceError to stop,
        e.g. when a metered provider's request budget is used up."""
        parts = urllib.parse.urlsplit(url)
        if parts.scheme != "https" or parts.hostname not in ALLOWED_HOSTS:
            raise SourceError(f"Host {parts.hostname} is not allowlisted")
        for attempt in range(MAX_RETRIES + 1):
            if before_request:
                before_request()
            self._wait_turn(parts.hostname or "")
            self.requests += 1
            retry_after = 2.0**attempt
            try:
                req = urllib.request.Request(
                    url, headers={"User-Agent": USER_AGENT, "Accept": "application/json", **(headers or {})}
                )
                with self._open(req, timeout=TIMEOUT_SECONDS) as res:
                    raw = res.read(MAX_RESPONSE_BYTES + 1)
                if len(raw) > MAX_RESPONSE_BYTES:
                    raise SourceError("Response too large")
                try:
                    return json.loads(raw)
                except ValueError:
                    raise SourceError("Response was not valid JSON") from None
            except urllib.error.HTTPError as exc:
                if exc.code == 404:
                    raise SourceError("Not found (HTTP 404)") from None
                if exc.code != 429 and exc.code < 500:
                    raise SourceError(f"HTTP {exc.code}") from None
                error = SourceError(f"HTTP {exc.code}")
                header = exc.headers.get("Retry-After") if exc.headers else None
                if header and header.isdigit():
                    retry_after = min(float(header), 30.0)
            except (TimeoutError, socket.timeout):
                error = SourceError(f"Timed out after {TIMEOUT_SECONDS}s")
            except urllib.error.URLError as exc:
                error = SourceError(
                    "Timed out" if isinstance(exc.reason, TimeoutError) else f"Connection failed: {exc.reason}"
                )
            if attempt < MAX_RETRIES:
                self._sleep(retry_after)
        raise error


# --- Normalization helpers ----------------------------------------------------------------------


class _TextExtractor(HTMLParser):
    BLOCK = {"p", "br", "li", "div", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "ul", "ol"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def html_to_text(value: str | None) -> str | None:
    if not value:
        return None
    parser = _TextExtractor()
    parser.feed(value)
    parser.close()
    text = re.sub(r"[ \t\r\f\v]+", " ", "".join(parser.parts))
    text = re.sub(r"\s*\n\s*", "\n", text).strip()
    return text[:MAX_DESCRIPTION_CHARS] or None


def _str(value: Any, max_len: int) -> str | None:
    if not isinstance(value, str):
        return None
    value = " ".join(value.split())
    return value[:max_len] or None


def _http_url(value: Any) -> str | None:
    if not isinstance(value, str) or len(value) > 2048:
        return None
    try:
        parts = urllib.parse.urlsplit(value.strip())
        parts.port  # raises ValueError on a malformed port; such URLs are dropped, not fatal for the board
    except ValueError:
        return None
    return value.strip() if parts.scheme in ("http", "https") and parts.hostname else None


def canonical_url(url: str | None) -> str | None:
    """Lowercased scheme/host, no fragment, tracking params, or trailing slash. Other query params are kept:
    many company career pages identify the posting only by query (e.g. ?gh_jid=123)."""
    if not url:
        return None
    parts = urllib.parse.urlsplit(url)
    query = sorted(
        (k, v)
        for k, v in urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
        if k.lower() not in TRACKING_PARAMS and not k.lower().startswith("utm_")
    )
    host = (parts.hostname or "").lower()
    if parts.port and parts.port not in (80, 443):
        host = f"{host}:{parts.port}"
    path = parts.path.rstrip("/")
    return urllib.parse.urlunsplit(("https" if parts.scheme == "http" else parts.scheme, host, path, urllib.parse.urlencode(query), ""))


def workplace_from_text(location: str | None) -> str | None:
    """Only explicit wording counts; a plain city is unknown, not assumed on-site."""
    if not location:
        return None
    text = location.lower()
    if "hybrid" in text:
        return "hybrid"
    if "remote" in text:
        return "remote"
    return None


def _parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo else None  # naive = ambiguous, so dropped


MAX_SALARY = 100_000_000  # above this is a data error; also keeps values inside the 32-bit DB column


def _int(value: Any) -> int | None:
    ok = isinstance(value, (int, float)) and not isinstance(value, bool) and 0 <= value <= MAX_SALARY
    return int(value) if ok else None


# --- Providers ----------------------------------------------------------------------------------


def parse_greenhouse(payload: Any, company: str) -> list[NormalizedJob]:
    if not isinstance(payload, dict) or not isinstance(payload.get("jobs"), list):
        raise SourceError("Unexpected Greenhouse response shape")
    jobs = []
    for item in payload["jobs"]:
        if not isinstance(item, dict) or item.get("id") is None or not _str(item.get("title"), 300):
            continue  # unusable without an identity and a title
        location = _str((item.get("location") or {}).get("name") if isinstance(item.get("location"), dict) else None, 200)
        url = _http_url(item.get("absolute_url"))
        jobs.append(
            NormalizedJob(
                provider="greenhouse",
                source_posting_id=str(item["id"])[:500],
                company=_str(item.get("company_name"), 200) or company,
                title=_str(item["title"], 300) or "",
                location=location,
                workplace_type=workplace_from_text(location),
                # content is HTML-escaped HTML: unescape once, then strip tags.
                description=html_to_text(html.unescape(item["content"])) if isinstance(item.get("content"), str) else None,
                posting_url=url,
                canonical_url=canonical_url(url),
                # The list endpoint does not include pay ranges; never guessed from description text.
                salary_min=None,
                salary_max=None,
                salary_currency=None,
                salary_interval=None,
                source_updated_at=_parse_iso(item.get("updated_at")),
                posted_at=_parse_iso(item.get("first_published")),
                requisition_id=_str(str(item["requisition_id"]), 100) if item.get("requisition_id") is not None else None,
                # The list API has no employment-type field; only explicit contract wording in the title counts.
                employment_type=employment_type(None, item.get("title")),
                ats_job_key=f"greenhouse:{item['id']}",
            )
        )
    return jobs


LEVER_WORKPLACE = {"remote": "remote", "hybrid": "hybrid", "on-site": "onsite", "onsite": "onsite"}


def _lever_description(item: dict[str, Any]) -> str | None:
    parts = [item.get("descriptionPlain")]
    for section in item.get("lists") or []:
        if isinstance(section, dict):
            parts.append(section.get("text"))
            parts.append(html_to_text(section.get("content")) if isinstance(section.get("content"), str) else None)
    parts.append(item.get("additionalPlain"))
    text = "\n\n".join(p.strip() for p in parts if isinstance(p, str) and p.strip())
    return text[:MAX_DESCRIPTION_CHARS] or None


def parse_lever(payload: Any, company: str, provider: str = "lever") -> list[NormalizedJob]:
    if not isinstance(payload, list):
        raise SourceError("Unexpected Lever response shape")
    jobs = []
    for item in payload:
        if not isinstance(item, dict) or not item.get("id") or not _str(item.get("text"), 300):
            continue
        categories = item.get("categories") if isinstance(item.get("categories"), dict) else {}
        location = _str(categories.get("location"), 200)
        salary = item.get("salaryRange") if isinstance(item.get("salaryRange"), dict) else {}
        url = _http_url(item.get("hostedUrl"))
        jobs.append(
            NormalizedJob(
                provider=provider,
                source_posting_id=str(item["id"])[:500],
                company=company,
                title=_str(item["text"], 300) or "",
                location=location,
                workplace_type=LEVER_WORKPLACE.get(str(item.get("workplaceType")).lower()) or workplace_from_text(location),
                description=_lever_description(item),
                posting_url=url,
                canonical_url=canonical_url(url),
                salary_min=_int(salary.get("min")),
                salary_max=_int(salary.get("max")),
                salary_currency=_str(salary.get("currency"), 3),
                salary_interval=_str(salary.get("interval"), 30),
                # Lever's public postings expose createdAt (when the posting was created), not an update time.
                source_updated_at=None,
                posted_at=_epoch_ms(item.get("createdAt")),
                employment_type=employment_type(categories.get("commitment"), item.get("text")),
                application_url=_http_url(item.get("applyUrl")),  # Lever's documented apply-form URL
                ats_job_key=f"lever:{item['id']}",  # Lever posting ids are global across regions
            )
        )
    return jobs


ASHBY_WORKPLACE = {"remote": "remote", "hybrid": "hybrid", "onsite": "onsite"}


def _ashby_salary(item: dict[str, Any]) -> tuple[int | None, int | None, str | None, str | None]:
    comp = item.get("compensation") if isinstance(item.get("compensation"), dict) else {}
    for c in comp.get("summaryComponents") or []:
        if isinstance(c, dict) and c.get("compensationType") == "Salary":
            interval = _str(c.get("interval"), 30)
            return _int(c.get("minValue")), _int(c.get("maxValue")), _str(c.get("currencyCode"), 3), interval and interval.lower()
    return None, None, None, None


def parse_ashby(payload: Any, company: str, board: str) -> list[NormalizedJob]:
    if not isinstance(payload, dict) or not isinstance(payload.get("jobs"), list):
        raise SourceError("Unexpected Ashby response shape")
    jobs = []
    for item in payload["jobs"]:
        if not isinstance(item, dict) or item.get("isListed") is False or not _str(item.get("title"), 300):
            continue
        url = _http_url(item.get("jobUrl"))
        ref = parse_ats_url(url)
        # The API has no id field; the posting UUID is the last segment of the Ashby-hosted job URL.
        if ref is None or ref.provider != "ashby" or ref.board.lower() != board.lower() or not ref.posting_id:
            continue
        location = _str(item.get("location"), 200)
        lo, hi, currency, interval = _ashby_salary(item)
        jobs.append(
            NormalizedJob(
                provider="ashby",
                source_posting_id=ref.posting_id,
                company=company,
                title=_str(item["title"], 300) or "",
                location=location,
                workplace_type=ASHBY_WORKPLACE.get(str(item.get("workplaceType")).lower()) or workplace_from_text(location),
                description=html_to_text(item.get("descriptionHtml")) if isinstance(item.get("descriptionHtml"), str)
                else _str(item.get("descriptionPlain"), MAX_DESCRIPTION_CHARS),
                posting_url=url,
                canonical_url=canonical_url(url),
                salary_min=lo,
                salary_max=hi,
                salary_currency=currency,
                salary_interval=interval,
                source_updated_at=None,
                posted_at=_parse_iso(item.get("publishedAt")),
                employment_type=employment_type(item.get("employmentType"), item.get("title")),
                application_url=_http_url(item.get("applyUrl")),  # Ashby's documented apply-form URL
                ats_job_key=f"ashby:{ref.posting_id}",
            )
        )
    return jobs


def _board_url(template: dict[str, str], provider: str, board_token: str) -> str:
    if provider not in template or not re.fullmatch(BOARD_TOKEN_PATTERN, board_token):
        raise SourceError("Invalid provider or board identifier")
    return template[provider].format(board=urllib.parse.quote(board_token, safe=""))


def fetch_jobs(client: HttpClient, provider: str, board_token: str, company: str) -> list[NormalizedJob]:
    base = _board_url(PROVIDER_URLS, provider, board_token)
    if provider == "greenhouse":
        return parse_greenhouse(client.get_json(base), company)
    if provider == "ashby":
        return parse_ashby(client.get_json(base), company, board_token)
    jobs: list[NormalizedJob] = []
    for page in range(LEVER_MAX_PAGES):
        batch = client.get_json(f"{base}&skip={page * LEVER_PAGE_SIZE}&limit={LEVER_PAGE_SIZE}")
        jobs.extend(parse_lever(batch, company, provider))
        # A short page is the last one; a long one means the server ignored paging and sent everything.
        if len(batch) != LEVER_PAGE_SIZE:
            return jobs
    raise SourceError(f"More than {LEVER_MAX_PAGES * LEVER_PAGE_SIZE} postings; stopped paging")


def verify_board(client: HttpClient, provider: str, board_token: str) -> str | None:
    """One read-only request to the provider's official API. Returns the employer name the provider reports for the
    board (Greenhouse only; Lever and Ashby don't expose one), or raises SourceError if it isn't a public board."""
    payload = client.get_json(_board_url(VERIFY_URLS, provider, board_token))
    ok = (
        isinstance(payload, dict) and isinstance(payload.get("name"), str) if provider == "greenhouse"
        else isinstance(payload, dict) and isinstance(payload.get("jobs"), list) if provider == "ashby"
        else isinstance(payload, list)
    )
    if not ok:
        raise SourceError("Board did not answer like a public job board")
    name = payload.get("name") if provider == "greenhouse" else None
    return name.strip()[:200] if isinstance(name, str) and name.strip() else None


def board_page_url(provider: str, board_token: str) -> str:
    return BOARD_PAGE_URLS[provider].format(board=urllib.parse.quote(board_token, safe=""))


# --- ATS URL detection --------------------------------------------------------------------------


@dataclass(frozen=True)
class AtsRef:
    provider: str
    board: str
    posting_id: str | None


# Exact hostnames only (no suffix matching), so "boards.greenhouse.io.evil.com" never matches.
ATS_PAGE_HOSTS = {
    "boards.greenhouse.io": "greenhouse",
    "job-boards.greenhouse.io": "greenhouse",
    "jobs.lever.co": "lever",
    "jobs.eu.lever.co": "lever_eu",
    "jobs.ashbyhq.com": "ashby",
}
RESERVED_SEGMENTS = {"embed", "v1", "api", "jobs", "careers", "static", "assets"}
POSTING_ID = re.compile(r"^[A-Za-z0-9-]{1,100}$")


def parse_ats_url(url: str | None) -> AtsRef | None:
    """Extracts (provider, board, posting id) from a public ATS page URL. Never guesses: anything that isn't
    an exact allowlisted host with a well-formed board segment returns None. The URL is not fetched."""
    if not url or len(url) > 2048:
        return None
    try:
        parts = urllib.parse.urlsplit(url.strip())
        port = parts.port
    except ValueError:
        return None
    host = (parts.hostname or "").lower()
    if parts.scheme not in ("http", "https") or parts.username or parts.password or port not in (None, 80, 443):
        return None
    provider = ATS_PAGE_HOSTS.get(host)
    if provider is None:
        return None
    segments = [urllib.parse.unquote(p) for p in parts.path.split("/") if p]
    if provider == "greenhouse" and segments[:1] == ["embed"]:  # embed/job_app, embed/job_board, embed/job_board/js
        query = dict(urllib.parse.parse_qsl(parts.query))
        board, posting = query.get("for"), query.get("token") if segments[1:2] == ["job_app"] else None
    elif segments:
        board = segments[0]
        if provider == "greenhouse":
            posting = segments[2] if len(segments) >= 3 and segments[1] == "jobs" else None
        else:
            posting = segments[1] if len(segments) >= 2 and segments[1] != "embed" else None
    else:
        return None
    if not board or board.lower() in RESERVED_SEGMENTS or not re.fullmatch(BOARD_TOKEN_PATTERN, board):
        return None
    if posting is not None and not POSTING_ID.fullmatch(posting):
        posting = None
    return AtsRef(provider, board, posting)


def ats_job_key(ref: AtsRef | None) -> str | None:
    if ref is None or not ref.posting_id:
        return None
    return f"{'lever' if ref.provider == 'lever_eu' else ref.provider}:{ref.posting_id}"
