"""Employer career-source discovery: verified official website -> careers page -> ATS board -> job import.

Identity: only a company's verified domain (curated, or entered by the user) or a careers URL the user entered is
ever fetched. No domain is guessed from a company name and no search engine is used; companies without one are
marked "needs_review" for a manual entry.

Ownership: an ATS board counts as the employer's only when a page on the employer's own verified domain links to it
(or the user confirmed it). The ATS's official API must then answer. Board identifiers come only from observed
links; they are never derived from the company name.

Safety: official-site requests are GET-only, http(s) on ports 80/443, to hostnames whose every resolved address is
public (the connection is pinned to the checked address), robots.txt is honoured, redirects are re-checked and
limited, and responses are size-capped. ATS links found in pages are parsed, never fetched; ATS APIs are reached
only through the allowlisted HttpClient.
"""

import html
import http.client
import ipaddress
import json
import logging
import re
import socket
import ssl
import time
import urllib.parse
import urllib.robotparser
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from typing import Any

from sqlalchemy import or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.models import Company, DiscoveryTask, JobSource
from app.services.companies import MANUAL_OWNERSHIP, enqueue_sync, known_board, names_consistent, record_verification
from app.config import DiscoverySettingsEnv
from app.services.job_sources import AtsRef, HttpClient, SourceError, parse_ats_url, verify_board
from app.services.sync import finish_if_done, record_outcome, run_feeds, sync_board

log = logging.getLogger(__name__)

USER_AGENT = "CareerOps/0.6 (personal job tracker; reads public careers pages)"
ROBOTS_AGENT = "CareerOps"
SITE_TIMEOUT = 10
SITE_MAX_BYTES = 5_000_000  # modern careers pages with inline scripts can exceed 2 MB
DEEP_MAX_BYTES = 15_000_000  # deep scan only: big single-page careers apps bundle their job list in one script
SITE_MAX_REDIRECTS = 3
SITE_HOST_INTERVAL = 2.0  # seconds between requests to one host
@dataclass(frozen=True)
class ScanLimits:
    """Per-company bounds. `max_requests` caps every request to the site (robots.txt, pages, scripts, sitemap)."""

    pages: int
    scripts: int
    sitemap_pages: int
    max_bytes: int  # per response
    boards: int  # candidates verified against their official API
    max_requests: int


STANDARD = ScanLimits(pages=5, scripts=8, sitemap_pages=2, max_bytes=SITE_MAX_BYTES, boards=3, max_requests=40)
# One company at a time, on request only: more scripts (large sites split job lists into many bundles).
DEEP = ScanLimits(pages=10, scripts=80, sitemap_pages=5, max_bytes=DEEP_MAX_BYTES, boards=5, max_requests=150)
MAX_PAGES_PER_COMPANY = STANDARD.pages  # page fetches (robots.txt not counted) per company
MAX_BOARDS_PER_COMPANY = STANDARD.boards
MAX_SCRIPTS_PER_COMPANY = STANDARD.scripts  # same-domain scripts read for embedded ATS configuration (never executed)
MAX_SITEMAP_PAGES = STANDARD.sitemap_pages  # job pages taken from the official sitemap
# Evidence that a board is a prospect pool rather than general hiring (link text, data labels, provider board name).
TALENT_POOL_HINT = re.compile(r"talent (?:pool|community|network)|expression of interest|\bprospects\b|general interest|"
                              r"future opportunit", re.IGNORECASE)
RECHECK_AFTER = timedelta(days=7)  # a finished lookup isn't repeated sooner unless the user retries
# A "running" task older than this is from a crashed process. Above the slowest possible task: a 20-page Lever board
# with every request retried (3 attempts x 15 s timeout + backoff) is about 16 minutes.
STALE_TASK = timedelta(minutes=30)
MAX_ATTEMPTS = 3
RETRY_BASE = timedelta(minutes=5)


class SiteError(Exception):
    """An official-site request failed or was refused. The message is safe to store and show."""

    def __init__(self, message: str, transient: bool = False, redirect_to: str | None = None) -> None:
        super().__init__(message)
        self.transient = transient
        self.redirect_to = redirect_to  # set when the site redirected off the official domain (never followed)


@dataclass
class Page:
    url: str
    text: str


Opener = Callable[[str, str, str], tuple[int, dict[str, str], bytes]]  # (url, host, pinned ip) -> response


class _PinnedHTTPS(http.client.HTTPSConnection):
    """Connects to an already-checked IP while keeping SNI and certificate checks on the hostname."""

    def __init__(self, host: str, ip: str, **kw: Any) -> None:
        super().__init__(host, context=ssl.create_default_context(), **kw)
        self._ip = ip

    def connect(self) -> None:
        sock = socket.create_connection((self._ip, self.port), self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)  # type: ignore[attr-defined]


class _PinnedHTTP(http.client.HTTPConnection):
    def __init__(self, host: str, ip: str, **kw: Any) -> None:
        super().__init__(host, **kw)
        self._ip = ip

    def connect(self) -> None:
        self.sock = socket.create_connection((self._ip, self.port), self.timeout)


def _open(url: str, host: str, ip: str) -> tuple[int, dict[str, str], bytes]:
    parts = urllib.parse.urlsplit(url)
    conn_cls = _PinnedHTTPS if parts.scheme == "https" else _PinnedHTTP
    conn = conn_cls(host, ip, port=parts.port, timeout=SITE_TIMEOUT)
    try:
        path = urllib.parse.urlunsplit(("", "", parts.path or "/", parts.query, ""))
        conn.request("GET", path, headers={"User-Agent": USER_AGENT, "Accept": "text/html,text/plain;q=0.9",
                                           "Accept-Encoding": "identity"})
        res = conn.getresponse()
        # shortcut: reads up to the deep-scan cap even for a standard scan (the 5 MB limit is applied after); pass the
        # limit into the opener if bandwidth matters.
        body = res.read(DEEP_MAX_BYTES + 1)
        return res.status, {k.lower(): v for k, v in res.getheaders()}, body
    finally:
        conn.close()


def _host_in(host: str, domain: str) -> bool:
    return host == domain or host.endswith("." + domain)


class SiteFetcher:
    """Fetches pages from one employer's official domain only."""

    def __init__(
        self,
        opener: Opener = _open,
        resolve: Callable[..., Any] = socket.getaddrinfo,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._open, self._resolve, self._sleep, self._clock = opener, resolve, sleep, clock
        self._last: dict[str, float] = {}
        self._robots: dict[str, urllib.robotparser.RobotFileParser | None] = {}
        self.requests = 0
        self.limit_at: int | None = None  # stop once `requests` reaches this (set per company by explore)
        self.max_bytes = SITE_MAX_BYTES  # per response (explore raises it for a deep scan)

    def _check_url(self, url: str) -> tuple[str, str]:
        try:
            parts = urllib.parse.urlsplit(url)
            port = parts.port
        except ValueError:
            raise SiteError("Malformed URL") from None
        host = (parts.hostname or "").lower().rstrip(".")
        if parts.scheme not in ("http", "https") or parts.username or parts.password:
            raise SiteError("Only plain http(s) URLs are fetched")
        if port not in (None, 80, 443):
            raise SiteError("Only ports 80 and 443 are fetched")
        try:
            ipaddress.ip_address(host)
            raise SiteError("IP-address URLs are not fetched")
        except ValueError:
            pass
        if "." not in host or len(host) > 253:
            raise SiteError("Not a public hostname")
        try:
            infos = self._resolve(host, port or (443 if parts.scheme == "https" else 80), type=socket.SOCK_STREAM)
        except OSError:
            raise SiteError(f"DNS lookup failed for {host}", transient=True) from None
        ips = sorted({info[4][0] for info in infos})
        if not ips or not all(ipaddress.ip_address(ip.split("%")[0]).is_global for ip in ips):
            raise SiteError(f"{host} resolves to a non-public address; not fetched")
        return host, ips[0]

    def _raw(self, url: str, host: str, ip: str) -> tuple[int, dict[str, str], bytes]:
        if self.limit_at is not None and self.requests >= self.limit_at:
            raise SiteError("Request limit for this company reached")
        wait = SITE_HOST_INTERVAL - (self._clock() - self._last.get(host, -1e9))
        if wait > 0:
            self._sleep(wait)
        self._last[host] = self._clock()
        self.requests += 1
        try:
            status, headers, body = self._open(url, host, ip)
        except (TimeoutError, socket.timeout):
            raise SiteError(f"Timed out after {SITE_TIMEOUT}s", transient=True) from None
        except (OSError, http.client.HTTPException, ssl.SSLError) as exc:
            raise SiteError(f"Connection failed ({type(exc).__name__})", transient=True) from None
        if len(body) > self.max_bytes:
            raise SiteError("Page too large")
        return status, headers, body

    def _allowed(self, url: str, host: str, ip: str) -> bool:
        scheme = urllib.parse.urlsplit(url).scheme
        key = f"{scheme}://{host}"
        if key not in self._robots:
            parser: urllib.robotparser.RobotFileParser | None = urllib.robotparser.RobotFileParser()
            try:
                status, _, body = self._raw(f"{key}/robots.txt", host, ip)
            except SiteError:
                status, body = 0, b""
            if status in (401, 403):
                parser.disallow_all = True  # type: ignore[union-attr]
            elif status == 200:
                parser.parse(body.decode("utf-8", "replace").splitlines())  # type: ignore[union-attr]
            else:
                parser = None  # no robots.txt: everything is allowed
            self._robots[key] = parser
        rp = self._robots[key]
        return rp is None or rp.can_fetch(ROBOTS_AGENT, url)

    def get(self, url: str, domain: str, accept: tuple[str, ...] = ("html",)) -> Page:
        """GET a page (HTML by default; scripts or sitemaps when `accept` says so) on `domain` or a subdomain.
        Redirects are followed only within that domain. Content is only read as text, never executed."""
        for hop in range(SITE_MAX_REDIRECTS + 1):
            host, ip = self._check_url(url)
            if not _host_in(host, domain):
                raise SiteError(f"Left the official domain ({host}); not followed", redirect_to=url if hop else None)
            if not self._allowed(url, host, ip):
                raise SiteError(f"robots.txt disallows {urllib.parse.urlsplit(url).path or '/'}")
            status, headers, body = self._raw(url, host, ip)
            if status in (301, 302, 303, 307, 308) and headers.get("location"):
                url = urllib.parse.urljoin(url, headers["location"].strip())
                continue
            if status in (429, 500, 502, 503, 504):
                raise SiteError(f"HTTP {status}", transient=True)
            if status != 200:
                raise SiteError(f"HTTP {status}")
            ctype = headers.get("content-type", "")
            if not any(a in ctype for a in accept):
                raise SiteError(f"Not an expected page type ({ctype.split(';')[0] or 'no content type'})")
            charset = re.search(r"charset=([\w-]+)", ctype)
            try:
                text = body.decode(charset.group(1) if charset else "utf-8", "replace")
            except LookupError:
                text = body.decode("utf-8", "replace")
            return Page(url, text)
        raise SiteError("Too many redirects")


# --- Page analysis (all input is untrusted) -----------------------------------------------------


class _Links(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[str, str]] = []  # (href, anchor text)
        self.scripts: list[str] = []  # script and component URLs (read later as text if on the official domain)
        self.json_ld: list[str] = []
        self._href: str | None = None
        self._text: list[str] = []
        self._ld: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k: v for k, v in attrs if v}
        if tag == "a" and a.get("href"):
            # Labels in data-* attributes (e.g. data-ga-name="join our sales talent pool") describe the link too.
            self._href = a["href"]
            self._text = [a.get("aria-label", ""), a.get("title", ""), *(v[:100] for k, v in a.items() if k.startswith("data-"))]
        elif tag in ("iframe", "script", "frame") and a.get("src"):
            self.links.append((a["src"], ""))
            if tag == "script":
                self.scripts.append(a["src"])
        elif tag == "astro-island" and a.get("component-url"):  # Astro loads interactive parts from these modules
            self.scripts.append(a["component-url"])
        elif tag == "link" and a.get("href") and a.get("rel") in ("modulepreload", "preload") and a.get("as", "script") == "script":
            self.scripts.append(a["href"])
        if tag == "script" and a.get("type") == "application/ld+json":
            self._ld = []

    def handle_data(self, data: str) -> None:
        if self._href is not None:
            self._text.append(data)
        if self._ld is not None:
            self._ld.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "script" and self._ld is not None:
            self.json_ld.append("".join(self._ld))
            self._ld = None
        if tag == "a" and self._href is not None:
            self.links.append((self._href, " ".join(" ".join(self._text).split())[:200]))
            self._href = None


CAREERS_WORDS = re.compile(
    r"\b(careers?|jobs|join (?:us|our team|the team)|work (?:with|for|at) us|open (?:positions|roles)|"
    r"we'?re hiring|job openings|current openings|view (?:all )?(?:open )?(?:jobs|roles|positions))\b", re.IGNORECASE)
NOT_A_PAGE = re.compile(r"\.(?:m?js|css|json|xml|png|jpe?g|gif|svg|webp|ico|woff2?|ttf|pdf|map)$", re.IGNORECASE)  # assets under /careers/
LISTING_PATH = re.compile(r"/(?:all-)?(?:jobs|openings|open-roles|open-positions|positions|listings|vacancies)(?:/|$)", re.IGNORECASE)
CAREERS_PATH = re.compile(r"/(careers?|jobs|join(?:-us)?|work-with-us|opportunities|open-positions|openings)(?:[/.?#-]|$)", re.IGNORECASE)
# ATS links in raw HTML or scripts (embed codes are often only in JavaScript). Parsed for a board name, never fetched.
# The scheme is optional: bundled scripts often hold bare hosts ("boards-api.greenhouse.io/v1/boards/acme/jobs").
_PRE = r"(?:(?:https?:)?//|(?<![\w.-]))"
ATS_IN_TEXT = re.compile(
    _PRE + r"(?:boards|job-boards)\.greenhouse\.io/[^\s\"'<>()\\`$]+"
    r"|" + _PRE + r"jobs(?:\.eu)?\.lever\.co/[^\s\"'<>()\\`$]+"
    r"|" + _PRE + r"jobs\.ashbyhq\.com/[^\s\"'<>()\\`$]+"
    r"|" + _PRE + r"boards-api\.greenhouse\.io/v1/boards/[A-Za-z0-9_-]+(?![\w$`{])"
    r"|" + _PRE + r"api\.ashbyhq\.com/posting-api/job-board/[A-Za-z0-9_-]+(?![\w$`{])",
    re.IGNORECASE,
)
# Hosts of unsupported platforms mentioned in page or script text (e.g. "acme.wd5.myworkdayjobs.com").
UNSUPPORTED_IN_TEXT = re.compile(r"(?<![\w.-])(?:[a-z0-9-]+\.)*(?:myworkdayjobs\.com|myworkdaysite\.com|icims\.com|"
                                 r"smartrecruiters\.com|jobvite\.com|taleo\.net|phenompeople\.com|eightfold\.ai)\b", re.IGNORECASE)
# A same-domain script named after a recruiting platform (e.g. "module_workday-job-listings.js"): a hint only.
PLATFORM_IN_SCRIPT_NAME = {"workday": "Workday", "icims": "iCIMS", "phenom": "Phenom", "smartrecruiters": "SmartRecruiters",
                           "jobvite": "Jobvite", "taleo": "Taleo", "successfactors": "SAP SuccessFactors", "eightfold": "Eightfold"}
API_REF = re.compile(r"^https://(boards-api\.greenhouse\.io/v1/boards|api\.ashbyhq\.com/posting-api/job-board)/([A-Za-z0-9_-]+)", re.IGNORECASE)
# Recognised but not yet supported platforms (host suffix -> name). Kept on the company for future integrations.
UNSUPPORTED_ATS = {
    "myworkdayjobs.com": "Workday", "myworkdaysite.com": "Workday", "smartrecruiters.com": "SmartRecruiters",
    "icims.com": "iCIMS", "jobvite.com": "Jobvite", "taleo.net": "Taleo", "workable.com": "Workable",
    "bamboohr.com": "BambooHR", "successfactors.com": "SAP SuccessFactors", "oraclecloud.com": "Oracle Recruiting",
    "rippling.com": "Rippling", "recruitee.com": "Recruitee", "breezy.hr": "Breezy HR", "applytojob.com": "JazzHR",
    "teamtailor.com": "Teamtailor", "pinpointhq.com": "Pinpoint", "eightfold.ai": "Eightfold", "phenompeople.com": "Phenom",
}


def _ats_ref(url: str) -> AtsRef | None:
    url = "https:" + url if url.startswith("//") else url if "://" in url[:8] else "https://" + url
    if m := API_REF.match(url):
        return AtsRef("greenhouse" if "greenhouse" in m.group(1).lower() else "ashby", m.group(2), None)
    return parse_ats_url(url)


def _unsupported(host: str) -> str | None:
    return next((name for suffix, name in UNSUPPORTED_ATS.items() if _host_in(host, suffix)), None)


@dataclass
class PageFindings:
    boards: list[AtsRef] = field(default_factory=list)
    unsupported: list[tuple[str, str]] = field(default_factory=list)  # (platform, url)
    careers_links: list[str] = field(default_factory=list)  # same-domain careers-looking links, best first
    scripts: list[str] = field(default_factory=list)  # same-domain script URLs, page order
    offsite_careers: list[str] = field(default_factory=list)  # careers links to another domain (candidates only)
    hiring_orgs: list[str] = field(default_factory=list)  # JSON-LD JobPosting hiringOrganization names
    board_context: dict[tuple[str, str], str] = field(default_factory=dict)  # board -> link text/labels pointing to it


def _normalized(text: str) -> str:
    """Page or script text with HTML entities and JSON/JS escapes undone, so embedded URLs are readable."""
    return html.unescape(text).replace("\\/", "/").replace("\\u002F", "/").replace("\\u002f", "/")


def _json_ld(blocks: list[str]) -> tuple[list[str], list[str]]:
    """URLs and hiring-organization names from JSON-LD JobPosting data (structured data; only read)."""
    urls: list[str] = []
    orgs: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, list):
            for n in node:
                walk(n)
        elif isinstance(node, dict):
            if node.get("@type") == "JobPosting" or "JobPosting" in (node.get("@type") or []):
                for k in ("url", "sameAs"):
                    if isinstance(node.get(k), str):
                        urls.append(node[k])
                org = node.get("hiringOrganization")
                if isinstance(org, dict) and isinstance(org.get("name"), str):
                    orgs.append(org["name"][:200])
            for v in node.values():
                if isinstance(v, (list, dict)):
                    walk(v)

    for block in blocks[:20]:
        try:
            walk(json.loads(block))
        except ValueError:
            continue
    return urls, orgs


def scan_text(text: str) -> tuple[list[AtsRef], list[tuple[str, str]]]:
    """ATS boards and unsupported platforms named anywhere in page or script text."""
    norm = _normalized(text)
    boards: list[AtsRef] = []
    for m in ATS_IN_TEXT.finditer(norm):
        if (ref := _ats_ref(m.group(0))) and (ref.provider, ref.board.lower()) not in {(b.provider, b.board.lower()) for b in boards}:
            boards.append(AtsRef(ref.provider, ref.board, None))
    unsupported: list[tuple[str, str]] = []
    for m in UNSUPPORTED_IN_TEXT.finditer(norm):
        name = _unsupported(m.group(0).lower())
        if name and name not in {n for n, _ in unsupported}:
            unsupported.append((name, m.group(0)[:200]))
    return boards, unsupported


def analyze(page: Page, domain: str) -> PageFindings:
    """Finds ATS board links, unsupported-ATS links, and careers links on one official page."""
    out = PageFindings()
    parser = _Links()
    try:
        parser.feed(page.text)
        parser.close()
    except Exception:  # malformed markup: keep whatever was collected
        log.debug("HTML parse error on %s", page.url)
    seen_boards: set[tuple[str, str]] = set()
    candidates: list[tuple[int, str]] = []
    raw_boards, raw_unsupported = scan_text(page.text)  # inline scripts, serialized (e.g. Next.js) data, embeds
    ld_urls, out.hiring_orgs = _json_ld(parser.json_ld)
    for ref in raw_boards:
        seen_boards.add((ref.provider, ref.board.lower()))
        out.boards.append(ref)
    for src in parser.scripts:
        url = urllib.parse.urljoin(page.url, src.strip())
        try:
            parts = urllib.parse.urlsplit(url)
        except ValueError:
            continue
        if parts.scheme in ("http", "https") and _host_in((parts.hostname or "").lower(), domain) and url not in out.scripts:
            out.scripts.append(url)
    for href, text in [*parser.links, *((u, "") for u in ld_urls)]:
        href = href.strip()
        if href.lower().startswith(("mailto:", "tel:", "javascript:", "data:", "#")):
            continue
        url = urllib.parse.urljoin(page.url, href)
        if ref := _ats_ref(url):
            key = (ref.provider, ref.board.lower())
            if text:
                out.board_context[key] = (out.board_context.get(key, "") + " | " + text)[-400:]
            if key not in seen_boards:
                seen_boards.add(key)
                out.boards.append(AtsRef(ref.provider, ref.board, None))
            continue
        try:
            host = (urllib.parse.urlsplit(url).hostname or "").lower()
        except ValueError:
            continue
        if name := _unsupported(host):
            if name not in {n for n, _ in out.unsupported}:
                out.unsupported.append((name, url[:500]))
            continue
        if urllib.parse.urlsplit(url).scheme not in ("http", "https"):
            continue
        score = (2 if CAREERS_WORDS.search(text) else 0) + (1 if CAREERS_PATH.search(urllib.parse.urlsplit(url).path) else 0)
        if not _host_in(host, domain):
            # e.g. capitalone.com -> capitalonecareers.com: possibly official, but never trusted automatically.
            if score >= 2 and "." in host and url not in out.offsite_careers:
                out.offsite_careers.append(urllib.parse.urldefrag(url)[0][:500])
            continue
        if score and not NOT_A_PAGE.search(urllib.parse.urlsplit(url).path):
            candidates.append((-score, urllib.parse.urldefrag(url)[0]))
    # Platforms only mentioned in text (scripts, data) after real links, which carry a usable URL.
    out.unsupported += [(n, w) for n, w in raw_unsupported if n not in {x for x, _ in out.unsupported}]
    for _, url in sorted(candidates, key=lambda c: c[0]):
        if url not in out.careers_links and url.rstrip("/") != page.url.rstrip("/"):
            out.careers_links.append(url)
    return out


# --- Company discovery --------------------------------------------------------------------------


@dataclass
class DiscoveryOutcome:
    status: str
    careers_url: str | None = None
    website: str | None = None
    evidence: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None
    boards: list[tuple[AtsRef, str]] = field(default_factory=list)  # (board, official page or script naming it)
    unsupported: str | None = None
    transient: bool = False
    hiring_orgs: dict[str, list[str]] = field(default_factory=dict)  # page URL -> JSON-LD hiring organizations
    roles: dict[tuple[str, str], str] = field(default_factory=dict)  # board -> "talent_pool" when evidence says so


def _note(out: DiscoveryOutcome, step: str, detail: str, url: str | None = None, candidate: str | None = None) -> None:
    """`candidate`: a value the user may accept (a new official domain or careers URL); never applied automatically."""
    entry = {"step": step, "detail": detail[:500], "url": url[:500] if url else None, "at": datetime.now(timezone.utc).isoformat()}
    if candidate:
        entry["candidate"] = candidate[:500]
    out.evidence.append(entry)


def _add_unsupported(out: DiscoveryOutcome, name: str) -> None:
    """Every platform seen is kept (e.g. "Workday, Eightfold": applications and a talent-network form)."""
    names = [n for n in (out.unsupported or "").split(", ") if n]
    if name not in names:
        out.unsupported = ", ".join([*names, name])[:40]


def _add_boards(out: DiscoveryOutcome, refs: list[AtsRef], where: str, how: str,
                context: dict[tuple[str, str], str] | None = None) -> None:
    for key, text in (context or {}).items():
        if (m := TALENT_POOL_HINT.search(text)) and key not in out.roles:
            out.roles[key] = "talent_pool"
            _note(out, "ats", f"{key[0]} board '{key[1]}' is labelled as a prospect pool (\"{m.group(0)}\" in the link text)", where)
    if len(refs) > MAX_BOARDS_PER_COMPANY:
        # A page naming many boards is a directory (partners, portfolio), not one employer's careers page.
        _note(out, "ats", f"{how} {len(refs)} different job boards; treated as a directory, not used as ownership evidence", where)
        return
    for ref in refs:
        if (ref.provider, ref.board.lower()) not in {(b.provider, b.board.lower()) for b, _ in out.boards}:
            out.boards.append((ref, where))
            _note(out, "ats", f"{how} {ref.provider} board '{ref.board}'", where)


def _sitemap_pages(fetcher: "SiteFetcher", host: str, domain: str, visited: set[str], limit: int = MAX_SITEMAP_PAGES) -> list[str]:
    """Up to `limit` job-detail URLs from the official sitemap (one sitemap request, read as text)."""
    sm = fetcher.get(f"https://{host}/sitemap.xml", domain, accept=("xml",))
    locs = [html.unescape(u).strip() for u in re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", sm.text)[:5000]]
    jobs = [u for u in locs if re.search(r"/(?:jobs?|detail|positions?|openings?|careers?/[^/]+/[^/]+)", urllib.parse.urlsplit(u).path, re.I)
            and _host_in((urllib.parse.urlsplit(u).hostname or "").lower(), domain) and u.rstrip("/") not in visited]
    return jobs[:limit]


def explore(fetcher: SiteFetcher, domain: str | None, careers_url: str | None, limits: ScanLimits = STANDARD) -> DiscoveryOutcome:
    """Walks an employer's official site (bounded by `limits`) and reports every board candidate it found, so a
    minor board found early never hides the main one. No database access. Order: pages (homepage, careers links),
    then same-domain scripts, then the careers sitemap (only if nothing named a board, unless deep)."""
    fetcher.limit_at, fetcher.max_bytes = fetcher.requests + limits.max_requests, limits.max_bytes
    start = careers_url
    if start:  # a careers URL entered by the user defines the official domain when none is verified
        host = (urllib.parse.urlsplit(start).hostname or "").lower()
        if name := _unsupported(host):
            out = DiscoveryOutcome("unsupported_ats", careers_url=start, unsupported=name,
                                   error=f"Careers site uses {name}, which isn't supported yet.")
            _note(out, "unsupported_ats", f"Careers URL is on {name} (not supported yet)", start)
            return out
        if not domain or not _host_in(host, domain):
            if (ref := _ats_ref(start)) is not None:
                out = DiscoveryOutcome("verified", careers_url=start)
                out.boards.append((AtsRef(ref.provider, ref.board, None), start))
                _note(out, "careers_page", "Careers URL entered by the user is an ATS board", start)
                return out
            domain = host.removeprefix("www.")
    if not domain:
        out = DiscoveryOutcome("needs_review", error="No verified official website. Add the company's domain or careers URL; "
                               "websites are never guessed from the name.")
        _note(out, "website", "No verified domain on record; no automated search provider is configured")
        return out

    out = DiscoveryOutcome("not_found")
    queue: list[tuple[str, str]] = [(start, "careers_page")] if start else [(f"https://{domain}/", "website")]
    visited: set[str] = set()
    guessed = False
    pages = 0
    careers_found: str | None = None
    temporary: str | None = None  # a careers page that failed for a reason that may pass (timeout, 5xx)
    script_pages: dict[str, int] = {}  # same-domain script -> number of visited pages using it (insertion = first seen)
    listing_scripts: set[str] = set()  # used on a job-listing page (/jobs, /openings, ...)
    offsite: list[str] = []
    while queue and pages < limits.pages:
        url, kind = queue.pop(0)
        if url.rstrip("/") in visited:
            continue
        visited.add(url.rstrip("/"))
        pages += 1
        try:
            page = fetcher.get(url, domain)
        except SiteError as exc:
            if exc.redirect_to:
                new_host = (urllib.parse.urlsplit(exc.redirect_to).hostname or "").lower().removeprefix("www.")
                _note(out, "domain_candidate", f"Redirects to another domain ({new_host}); not followed. If it's the "
                      "company's new official domain, accept it in the Company directory.", url, candidate=new_host)
            else:
                _note(out, kind, f"Not fetched: {exc}", url)
            if kind == "website":
                out.status, out.error, out.transient = ("failed" if exc.transient else "needs_review"), f"Website: {exc}", exc.transient
                if not exc.transient:
                    return out
            elif exc.transient:
                temporary = f"{url}: {exc}"
            continue
        if kind == "website":
            out.website = f"https://{urllib.parse.urlsplit(page.url).hostname}"
            out.status, out.error, out.transient = "not_found", None, False
            _note(out, "website", "Official website answered", page.url)
        elif careers_found is None:
            careers_found = page.url
            _note(out, "careers_page", "Careers page " + ("(common path, not linked)" if kind == "guessed" else "found"), page.url)
        found = analyze(page, domain)
        _add_boards(out, found.boards, page.url, "Page names", found.board_context)
        if found.hiring_orgs:
            out.hiring_orgs[page.url] = found.hiring_orgs
        for name, link in found.unsupported:
            _add_unsupported(out, name)
            if not any(e["step"] == "unsupported_ats" and e["url"] == link[:500] for e in out.evidence):  # once per link
                _note(out, "unsupported_ats", f"Links to {name} (not supported yet)", link)
            careers_found = careers_found or (link if link.startswith("http") else None)
        listing = bool(LISTING_PATH.search(urllib.parse.urlsplit(page.url).path))
        for src in found.scripts:  # the homepage counts double: its scripts are site-wide, not the job list
            script_pages[src] = script_pages.get(src, 0) + (2 if kind == "website" else 1)
            if listing:
                listing_scripts.add(src)
        offsite += [u for u in found.offsite_careers if u not in offsite]
        queue.extend((u, "careers_page") for u in found.careers_links[:2] if u.rstrip("/") not in visited)
        if kind == "website" and not found.careers_links and not found.unsupported and not guessed:
            guessed = True  # nothing linked: try the two most common paths on the verified domain
            queue.extend([(f"https://{domain}/careers", "guessed"), (f"https://{domain}/jobs", "guessed")])

    # Job lists rendered by JavaScript: read (never run) the official site's own scripts for embedded ATS config.
    if out.website or careers_found:
        # Page-specific scripts first: code used on every page is layout, analytics, or consent, not the job list.
        # Job-listing pages' own scripts come first.
        scripts = sorted(script_pages, key=lambda src: (src not in listing_scripts, script_pages[src]))[:limits.scripts]
        for script in scripts:
            name = urllib.parse.urlsplit(script).path.rsplit("/", 1)[-1].lower()
            hint = next((v for k, v in PLATFORM_IN_SCRIPT_NAME.items() if k in name), None)
            try:
                text = fetcher.get(script, domain, accept=("javascript", "ecmascript")).text
            except SiteError as exc:
                _note(out, "script", f"Not fetched: {exc}", script)
                continue
            boards, unsupported = scan_text(text)
            _add_boards(out, boards, script, "Official site script names")
            for platform, where in unsupported or ([(hint, "script name")] if hint else []):
                _add_unsupported(out, platform)
                _note(out, "unsupported_ats", f"Official site script refers to {platform} ({where}; not supported yet)", script)
        if scripts and not out.boards and not out.unsupported:
            _note(out, "script", f"Read {len(scripts)} of {len(script_pages)} official site scripts; none names a supported board")

    # Still nothing: the careers sitemap can list job pages that embed the board.
    if careers_found and not out.unsupported and (not out.boards or limits is DEEP):
        host = (urllib.parse.urlsplit(careers_found).hostname or "").lower()
        try:
            job_pages = _sitemap_pages(fetcher, host, domain, visited, limits.sitemap_pages)
            if not job_pages:
                _note(out, "sitemap", "Official sitemap lists no job pages", f"https://{host}/sitemap.xml")
            for job_page in job_pages:
                page = fetcher.get(job_page, domain)
                found = analyze(page, domain)
                _add_boards(out, found.boards, page.url, "Job page from the official sitemap names", found.board_context)
                if found.hiring_orgs:
                    out.hiring_orgs[page.url] = found.hiring_orgs
        except SiteError as exc:
            _note(out, "sitemap", f"Sitemap not used: {exc}", f"https://{host}/sitemap.xml")

    fetcher.limit_at, fetcher.max_bytes = None, SITE_MAX_BYTES
    # General boards before prospect pools; otherwise in the order found (page links, then scripts, then sitemap).
    out.boards.sort(key=lambda b: out.roles.get((b[0].provider, b[0].board.lower())) == "talent_pool")
    out.careers_url = careers_found or (out.boards[0][1] if out.boards else None)
    if out.boards:
        out.status = "verified"  # provisional: each board is still checked against its official API
    elif out.unsupported:
        out.status = "unsupported_ats"
    elif out.status == "not_found" and temporary:
        out.status, out.error, out.transient = "failed", f"Temporary error, will retry: {temporary}", True
    elif out.status == "not_found" and offsite and not careers_found:
        out.status = "needs_review"
        out.error = f"Careers appear to be on another domain ({offsite[0]}). If it's official, use it as the careers page."
        _note(out, "careers_candidate", "Careers link points to another domain; not followed automatically", offsite[0], candidate=offsite[0])
    elif out.status == "not_found":
        out.error = ("Careers page found, but no Greenhouse, Lever, or Ashby board in its HTML, scripts, or sitemap. "
                     "Paste the board URL to add it." if careers_found else
                     "No careers page linked from the official website. Enter the careers URL to retry.")
    return out


def discover_company(factory: Callable[[], Session], fetcher: SiteFetcher, client: HttpClient, company_id: uuid.UUID,
                     limits: ScanLimits = STANDARD) -> DiscoveryOutcome:
    """Runs discovery for one company and stores the result. No transaction is held during HTTP requests."""
    with factory() as session:
        company = session.get(Company, company_id)
        if company is None:
            return DiscoveryOutcome("failed", error="Company not found")
        domain = company.domain if company.verification_status == "verified" else None  # never a provider-reported one
        careers_url = company.careers_url
        company_name = company.name
        company.discovery_status = "discovering"
        session.commit()

    out = explore(fetcher, domain, careers_url, limits)
    if len(out.boards) > limits.boards:
        _note(out, "ats", f"{len(out.boards)} board candidates found; verified the first {limits.boards} (general boards first)")
    verified: list[uuid.UUID] = []
    for ref, page_url in out.boards[:limits.boards]:
        with factory() as session:
            existing = known_board(session, ref)
            if existing and existing.company_id and existing.company_id != company_id:
                _note(out, "ats", f"{ref.provider} board '{ref.board}' is already linked to another company; needs review", page_url)
                continue
        try:
            reported = verify_board(client, ref.provider, ref.board)
            error = None
        except SourceError as exc:
            reported, error = None, exc
        # Ownership needs the official link AND no contradicting identity: the provider's board name and any JSON-LD
        # hiring organization on the linking page must not clearly name a different employer.
        names = [n for n in [reported, *out.hiring_orgs.get(page_url, [])] if n]
        conflicts = [n for n in names if not names_consistent(company_name, domain, n)]
        with factory() as session:
            board = record_verification(session, ref, company_id, page_url, error,
                                        ownership=None if conflicts else f"Named on the employer's official site: {page_url}")
            board.provider_name = reported or board.provider_name
            role = out.roles.get((ref.provider, ref.board.lower()))
            if role or (reported and TALENT_POOL_HINT.search(reported)):  # e.g. Greenhouse name "GitLab Prospects"
                board.board_role = "talent_pool"
            if conflicts:
                manual = (board.ownership_evidence or "").startswith(MANUAL_OWNERSHIP)
                if board.ownership_verified_at and not manual:
                    board.ownership_verified_at = None  # concrete contradicting evidence: stop importing under this employer
                if not board.ownership_verified_at:
                    board.verification_status = "needs_review"
                _note(out, "ownership", f"{ref.provider} board '{ref.board}': provider names {', '.join(repr(c) for c in conflicts)}, "
                      f"which doesn't match {company_name!r}{' (user-confirmed ownership kept)' if manual else '; needs review'}", page_url)
            elif error is None and board.ownership_verified_at:
                verified.append(board.id)
                enqueue_sync(session, board.id)
                _note(out, "ownership", f"{ref.provider} board '{ref.board}' verified: linked from the official site, official API "
                      f"answered{f', provider name {reported!r} matches' if reported else ' (provider reports no employer name)'}", page_url)
            else:
                _note(out, "ats", f"{ref.provider} board '{ref.board}' did not verify: {error}", page_url)
            session.commit()
    if out.boards and not verified:
        out.status, out.error = "needs_review", out.error or "ATS links were found but no board could be verified."
    with factory() as session:
        company = session.get(Company, company_id)
        assert company is not None
        company.discovery_status = out.status
        company.discovery_error = out.error
        company.discovery_checked_at = datetime.now(timezone.utc)
        company.discovery_evidence = [*(company.discovery_evidence or []), *out.evidence][-50:]
        if out.unsupported:
            company.unsupported_ats = out.unsupported
        if out.website and company.domain:
            company.website = company.website or out.website
            company.website_verified_at = datetime.now(timezone.utc)
        if out.careers_url and not careers_url:
            company.careers_url = out.careers_url[:2048]
        session.commit()
    return out


# --- Durable task queue -------------------------------------------------------------------------


def enqueue_discovery(session: Session, company_ids: list[uuid.UUID], force: bool = False) -> int:
    """Queues career-source discovery. Skips companies checked within RECHECK_AFTER (failed lookups included)
    unless forced; an already queued/running task is never duplicated."""
    stmt = select(Company.id).where(Company.id.in_(company_ids))
    if not force:
        stmt = stmt.where(or_(Company.discovery_checked_at.is_(None),
                              Company.discovery_checked_at < datetime.now(timezone.utc) - RECHECK_AFTER))
    ids = list(session.scalars(stmt))
    if not ids:
        return 0
    if force:  # a manual retry resets backoff for a task still waiting
        session.execute(update(DiscoveryTask).where(DiscoveryTask.kind == "discover", DiscoveryTask.company_id.in_(ids),
                                                    DiscoveryTask.status == "queued").values(run_after=datetime.now(timezone.utc)))
    result = session.execute(
        pg_insert(DiscoveryTask).values([{"id": uuid.uuid4(), "kind": "discover", "company_id": i} for i in ids])
        .on_conflict_do_nothing().returning(DiscoveryTask.id)
    )
    return len(result.all())


def _claim(session: Session, company_ids: list[uuid.UUID] | None, kinds: tuple[str, ...] | None = None,
           run_only: bool = False) -> DiscoveryTask | None:
    now = datetime.now(timezone.utc)
    # Restart recovery: a task left "running" by a crashed process goes back to the queue.
    session.execute(update(DiscoveryTask).where(DiscoveryTask.status == "running", DiscoveryTask.locked_at < now - STALE_TASK)
                    .values(status="queued", locked_at=None))
    stmt = select(DiscoveryTask).where(DiscoveryTask.status == "queued", DiscoveryTask.run_after <= now)
    if kinds:
        stmt = stmt.where(DiscoveryTask.kind.in_(kinds))
    if run_only:  # the background worker takes only run tasks; discovery and its first sync run where requested
        stmt = stmt.where(DiscoveryTask.run_id.is_not(None))
    if company_ids is not None:
        board_ids = select(JobSource.id).where(JobSource.company_id.in_(company_ids))
        stmt = stmt.where(or_(DiscoveryTask.company_id.in_(company_ids), DiscoveryTask.source_id.in_(board_ids)))
    # Discovery before sync, then queue order (a run queues boards overdue-first); SKIP LOCKED: never the same task twice.
    task = session.scalar(stmt.order_by(DiscoveryTask.kind, DiscoveryTask.run_after, DiscoveryTask.created_at, DiscoveryTask.id)
                          .limit(1).with_for_update(skip_locked=True))
    if task:
        task.status, task.locked_at, task.attempts = "running", now, task.attempts + 1
    session.commit()
    return task


def run_tasks(
    factory: Callable[[], Session], client: HttpClient, fetcher: SiteFetcher, max_tasks: int,
    company_ids: list[uuid.UUID] | None = None, limits: ScanLimits = STANDARD,
    kinds: tuple[str, ...] | None = None, run_only: bool = False, env: DiscoverySettingsEnv | None = None,
) -> list[dict[str, Any]]:
    """Processes up to `max_tasks` due tasks (optionally only some companies or kinds). Never starts an unbounded
    crawl: a sync task exists only for a verified board. One failure never stops the others. Discovery tasks retry
    with backoff; a failed board sync isn't retried here (its board's own backoff schedules the next check)."""
    env = env or DiscoverySettingsEnv()
    results: list[dict[str, Any]] = []
    for _ in range(max_tasks):
        with factory() as session:
            task = _claim(session, company_ids, kinds, run_only)
            if task is None:
                break
            task_id, kind, company_id, source_id, attempts = task.id, task.kind, task.company_id, task.source_id, task.attempts
            claimed_at = task.locked_at  # ownership token: changes if the task is ever handed to another worker
            run_id, payload = task.run_id, task.payload or {}
        result: dict[str, Any] = {"task_id": str(task_id), "kind": kind, "company_id": str(company_id) if company_id else None,
                                  "source_id": str(source_id) if source_id else None}
        error: str | None = None
        transient = False
        try:
            if kind == "discover":
                out = discover_company(factory, fetcher, client, company_id, limits)  # type: ignore[arg-type]
                result.update(status=out.status, error=out.error)
                transient = out.transient
                error = out.error if out.transient else None
            elif kind == "feeds":
                result.update(status="synced", outcomes=run_feeds(factory, client, env, payload))
            else:
                before, t0 = client.requests, time.monotonic()
                try:
                    result.update(status="synced", **sync_board(factory, client, source_id, env))  # type: ignore[arg-type]
                finally:
                    result["requests"], result["duration_ms"] = client.requests - before, round((time.monotonic() - t0) * 1000)
        except SourceError as exc:
            error = str(exc)
            result.update(status="failed", error=error)
        except Exception as exc:  # isolate: record and continue with the next task
            log.exception("Discovery task %s failed", task_id)
            error, transient = f"Unexpected error ({type(exc).__name__})", kind == "discover"
            result.update(status="failed", error=error)
        with factory() as session:
            task = session.get(DiscoveryTask, task_id, with_for_update=True)
            if task is None or task.status != "running" or task.locked_at != claimed_at:
                # Taken over after looking crashed: the current owner records the result, so it isn't counted twice.
                log.warning("Discovery task %s was reclaimed by another worker; result discarded", task_id)
                session.rollback()
                results.append({**result, "status": "reclaimed"})
                continue
            task.locked_at, task.last_error = None, error
            if run_id:
                for outcome in _run_outcomes(kind, result, source_id, session):
                    record_outcome(session, run_id, outcome)
            if error and transient and attempts < MAX_ATTEMPTS:
                task.status, task.run_after = "queued", datetime.now(timezone.utc) + RETRY_BASE * (2 ** (attempts - 1))
                result["retry_at"] = task.run_after.isoformat()
            else:
                task.status = "failed" if error else "done"
            session.flush()
            if run_id:
                finish_if_done(session, run_id)
            session.commit()
        results.append(result)
    return results


def _run_outcomes(kind: str, result: dict[str, Any], source_id: uuid.UUID | None, session: Session) -> list[dict[str, Any]]:
    """The run's per-source outcome(s) for a finished sync or feeds task."""
    if kind == "feeds":
        return result.get("outcomes") or [{"kind": "aggregator", "provider": "feeds", "company": "Job feeds", "status": "failed",
                                           "error": result.get("error"), **dict.fromkeys(("retrieved", "inserted", "updated", "unique",
                                           "matched", "unknown", "duplicates", "requests"), 0)}]
    board = session.get(JobSource, source_id) if source_id else None
    company = session.get(Company, board.company_id) if board and board.company_id else None
    base = {k: result.get(k, 0) for k in ("retrieved", "inserted", "updated", "unique", "matched", "unknown", "duplicates",
                                          "missing", "closed", "requests", "duration_ms")}
    return [{"kind": "board", "source_id": str(source_id), "provider": board.provider if board else None,
             "board_token": board.board_token if board else None, "company": company.name if company else None,
             "status": "succeeded" if result["status"] == "synced" else "failed", "error": result.get("error"),
             "warnings": result.get("warnings") or [], **base}]
