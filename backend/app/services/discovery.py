"""Job discovery: configured sources, manual runs, deterministic filtering, review, and saving leads.

Discovery only ever writes job_sources, discovered_jobs, discovery_runs, and discovery_settings. Saving a lead
creates (or links) a saved `jobs` row; nothing here creates an Application.
"""

import logging
import re
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import and_, case, exists, func, literal_column, nulls_first, nulls_last, or_, select, tuple_, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, aliased

from app.config import DiscoverySettingsEnv
from app.models import (
    CandidateProfile,
    Company,
    DiscoveredJob,
    DiscoveryGeneration,
    DiscoveryRun,
    DiscoverySettings,
    Job,
    JobMatch,
    JobSource,
    ProviderRequest,
)
from app.schemas import (
    DiscoveryRunRead,
    DiscoveredJobDetail,
    DiscoveredJobRead,
    DiscoveryFilters,
    ExperienceMatch,
    JobCreate,
    JobRequirements,
    JobSourceCreate,
    JobSourceUpdate,
    PostingLink,
)
from app.services.aggregators import BudgetExceeded, fetch_himalayas, fetch_jsearch, jsearch_queries, query_key
from app.services.companies import ConflictError, NotFoundError, Resolver, known_board, needs_verification, record_verification, touch_companies
from app.services.discovery_filters import ROLE_FAMILIES, ROLE_FAMILY_KEYS, SENIORITY_WORDS, evaluate
from app.services.job_insights import non_vacancy_reason, required_experience, requirements, technologies
from app.services.job_sources import (
    JOB_COLUMNS,
    PROVIDERS,
    AtsRef,
    HttpClient,
    NormalizedJob,
    SourceError,
    fetch_jobs,
    parse_ats_url,
    verify_board,
)

log = logging.getLogger(__name__)

UPSERT_CHUNK = 500  # keeps each INSERT well under PostgreSQL's 65k bind-parameter limit
STALE_RUN = timedelta(minutes=30)  # a "running" row older than this is from a crashed process
RUN_LOCK_KEY = 4_200_421  # pg advisory lock id serializing "is a run active?" + "start run"

# --- Generations --------------------------------------------------------------------------------
# Job Discovery shows one generation: the highest discovery_generations.id. Every writer stamps the generation
# that was active when it started, and an upsert never lowers a row's generation, so a run that started before an
# archive can't bring archived jobs back into view.
ACTIVE_GENERATION = select(func.max(DiscoveryGeneration.id)).scalar_subquery()


def active_generation(session: Session) -> int:
    return session.scalar(select(func.max(DiscoveryGeneration.id))) or 1


def archive_active_generation(session: Session, label: str) -> dict[str, int]:
    """Starts an empty generation. Archived jobs keep every field, review decision, saved link, and duplicate
    link; they stay in the table for deduplication and history. Development CLI only (no API endpoint)."""
    session.execute(select(func.pg_advisory_xact_lock(RUN_LOCK_KEY)))
    old = active_generation(session)
    archived = session.scalar(select(func.count()).select_from(DiscoveredJob).where(DiscoveredJob.generation == old)) or 0
    new = DiscoveryGeneration(label=label[:200], archived_jobs=archived)
    session.add(new)
    session.commit()
    return {"archived_generation": old, "active_generation": new.id, "archived_jobs": archived}



# --- Sources ------------------------------------------------------------------------------------


def list_sources(session: Session) -> list[JobSource]:
    return list(session.scalars(select(JobSource).order_by(JobSource.company_name, JobSource.id)))


def create_source(session: Session, data: JobSourceCreate) -> JobSource:
    """Manually added board (unverified until its first successful fetch). A board always belongs to a company:
    without one, a manual registry entry is created for it (never matched to another company by name)."""
    values = data.model_dump()
    if values["company_id"] is None:
        company = Company(name=data.company_name, origin="manual", watchlist=True, verification_status="unresolved")
        session.add(company)
        session.flush()
        values["company_id"] = company.id
    elif session.get(Company, values["company_id"]) is None:
        raise NotFoundError("Company not found")
    # The user named this board for this employer: that is the ownership confirmation; its API is checked on sync.
    source = JobSource(**values, verification_status="unverified", ownership_verified_at=datetime.now(timezone.utc),
                       ownership_evidence="Added by the user")
    session.add(source)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        raise ConflictError("This board is already configured") from None
    return source


def get_source(session: Session, source_id: uuid.UUID) -> JobSource:
    source = session.get(JobSource, source_id)
    if source is None:
        raise NotFoundError("Job source not found")
    return source


def update_source(session: Session, source_id: uuid.UUID, data: JobSourceUpdate) -> JobSource:
    source = get_source(session, source_id)
    for field, value in data.model_dump(exclude_unset=True).items():
        setattr(source, field, value)
    session.commit()
    session.refresh(source)
    return source


def delete_source(session: Session, source_id: uuid.UUID) -> None:
    """Removes the configuration only; jobs already discovered from it are kept (source_id set to NULL)."""
    session.delete(get_source(session, source_id))
    session.commit()


# --- Filters ------------------------------------------------------------------------------------


def get_filters(session: Session) -> DiscoveryFilters:
    row = session.get(DiscoverySettings, 1)
    return DiscoveryFilters.model_validate(row.filters) if row else DiscoveryFilters()


def save_filters(session: Session, filters: DiscoveryFilters) -> DiscoveryFilters:
    """Stores preferences and re-evaluates every discovered job so results always reflect current filters."""
    criteria = filters.model_dump()
    row = session.get(DiscoverySettings, 1)
    if row is None:
        session.add(DiscoverySettings(id=1, filters=criteria))
    else:
        row.filters = criteria
    # shortcut: loads every discovered job into memory; fine for one person's leads, batch with yield_per past ~50k rows.
    for job in session.scalars(select(DiscoveredJob)):
        job.filter_result, job.filter_reasons = evaluate(job, criteria)
    session.commit()
    return filters


# --- Runs ---------------------------------------------------------------------------------------


class NoSourcesError(ConflictError):
    """Nothing to check: no provider enabled and no eligible employer board. No run is recorded."""


def _flag_duplicates(session: Session, inserted_ids: list[uuid.UUID]) -> int:
    """Points each new row at the earliest older row that is probably the same job: same canonical posting URL,
    same ATS posting (provider-agnostic key), or same employer requisition id at the same company. Rows are
    flagged, never merged."""
    if not inserted_ids:
        return 0
    d, older = DiscoveredJob, aliased(DiscoveredJob)
    same = or_(
        and_(d.canonical_url.is_not(None), older.canonical_url == d.canonical_url),
        and_(d.ats_job_key.is_not(None), older.ats_job_key == d.ats_job_key),
        # Across sources only: one provider's postings with different ids are different jobs, even when an
        # employer reuses a requisition id for several postings (e.g. per team or location).
        and_(d.requisition_id.is_not(None), d.company_id.is_not(None), older.provider != d.provider,
             older.company_id == d.company_id, older.requisition_id == d.requisition_id),
    )
    earliest = (
        select(older.id)
        .where(same, tuple_(older.first_seen_at, older.id) < tuple_(d.first_seen_at, d.id))
        .order_by(older.first_seen_at, older.id)
        .limit(1)
        .scalar_subquery()
    )
    flagged = session.execute(
        update(d).where(d.id.in_(inserted_ids)).values(duplicate_of_id=earliest).returning(d.duplicate_of_id)
    )
    return sum(1 for (dup,) in flagged if dup is not None)


def persist_jobs(
    session: Session, jobs: list[NormalizedJob], criteria: dict[str, Any],
    source_id: uuid.UUID | None, board_company_id: uuid.UUID | None, generation: int, via: str,
) -> tuple[dict[str, int], list[tuple[AtsRef, uuid.UUID, str]]]:
    """Resolves employers, batch-upserts on (provider, source_posting_id), flags possible duplicates.
    Returns stats and ATS boards seen in aggregator links (candidates for verification)."""
    unique = list({(j.provider, j.source_posting_id): j for j in jobs}.values())
    resolver = Resolver(session)
    # Exact repeats of one posting (e.g. returned by several searches) show as retrieved - unique, not duplicates.
    stats = {"unique": len(unique), "inserted": 0, "updated": 0, "duplicates": 0, "matched": 0, "unknown": 0}
    candidates: dict[tuple[str, str], tuple[AtsRef, uuid.UUID, str]] = {}
    seen_companies: set[uuid.UUID] = set()
    boards: dict[tuple[str, str], JobSource | None] = {}
    inserted_ids: list[uuid.UUID] = []
    now = datetime.now(timezone.utc).isoformat()
    for start in range(0, len(unique), UPSERT_CHUNK):
        rows = []
        for job in unique[start : start + UPSERT_CHUNK]:
            ref = next((r for r in map(parse_ats_url, job.ats_urls) if r), None)
            board_company = board_company_id
            if ref and board_company is None:
                key = (ref.provider, ref.board.lower())
                if key not in boards:
                    boards[key] = known_board(session, ref)
                if boards[key] is not None:
                    board_company = boards[key].company_id  # type: ignore[union-attr]
            company_id = resolver.resolve(job.company, job.company_domain, job.company_alias, board_company)
            seen_companies.add(company_id)
            if ref and source_id is None:
                candidates.setdefault((ref.provider, ref.board.lower()), (ref, company_id, next(u for u in job.ats_urls if parse_ats_url(u))))
            result, reasons = evaluate(job, criteria)
            stats["matched"] += result == "match"
            stats["unknown"] += result == "unknown"
            rows.append({**{k: getattr(job, k) for k in JOB_COLUMNS}, "id": uuid.uuid4(), "source_id": source_id,
                         "company_id": company_id, "filter_result": result, "filter_reasons": reasons,
                         "review_status": "new", "generation": generation,
                         # Recomputed from the posting on every sync: same text, same result.
                         "non_vacancy_reason": non_vacancy_reason(job.title, job.description, job.company),
                         "missing_count": 0, "closed_at": None,  # listed again: open
                         "appearances": [{"generation": generation, "via": via, "provider": job.provider,
                                          "source_id": str(source_id) if source_id else None, "first_seen_at": now}]})
        stmt = pg_insert(DiscoveredJob).values(rows)
        keep = ("id", "provider", "source_posting_id", "review_status", "generation", "appearances")
        refreshed = {k: stmt.excluded[k] for k in rows[0] if k not in keep}
        d = DiscoveredJob.__table__.c
        newer = stmt.excluded.generation > func.coalesce(d.generation, 0)
        stmt = stmt.on_conflict_do_update(
            constraint="uq_discovered_jobs_provider_posting",
            # first_seen_at, review_status, saved_job_id and duplicate_of_id are preserved on re-discovery. Entering a
            # newer generation appends an appearance; the original history stays.
            set_={**refreshed, "last_seen_at": func.now(), "updated_at": func.now(),
                  "generation": func.greatest(d.generation, stmt.excluded.generation),
                  "appearances": case((newer, d.appearances.op("||")(stmt.excluded.appearances)), else_=d.appearances)},
        ).returning(DiscoveredJob.id, literal_column("xmax = 0"))  # PostgreSQL: xmax is 0 only for inserted rows
        inserted_ids += [row_id for row_id, was_inserted in session.execute(stmt) if was_inserted]
    stats["inserted"] = len(inserted_ids)
    stats["updated"] = len(unique) - len(inserted_ids)
    stats["duplicates"] += _flag_duplicates(session, inserted_ids)
    stats["companies_created"] = resolver.created
    touch_companies(session, seen_companies)
    return stats, list(candidates.values())


def _month_start() -> datetime:
    now = datetime.now(timezone.utc)
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def jsearch_used_this_month(session: Session) -> int:
    return session.scalar(
        select(func.count()).select_from(ProviderRequest)
        .where(ProviderRequest.provider == "jsearch", ProviderRequest.requested_at >= _month_start())
    ) or 0


SYNCABLE = (JobSource.enabled, JobSource.ownership_verified_at.is_not(None), JobSource.company_id.is_not(None),
            JobSource.provider.in_(PROVIDERS),  # ownership can be verified for platforms we can't import yet (Workday)
            JobSource.verification_status != "failed")  # a board that now answers 404 is not requested again


def feed_plan(session: Session, env: DiscoverySettingsEnv) -> dict[str, Any]:
    """What the aggregator feeds of a run will do (and why others are skipped). Contacts nothing."""
    criteria = get_filters(session).model_dump()
    families = criteria["role_families"] or list(ROLE_FAMILY_KEYS)
    geos = criteria["geographies"] or ["remote_us"]
    skipped: list[dict[str, Any]] = []
    warnings: list[str] = []
    if not env.himalayas_enabled:
        skipped.append({"provider": "himalayas", "reason": "Disabled (DISCOVERY_HIMALAYAS_ENABLED=false)"})
    elif "remote_us" not in geos:
        skipped.append({"provider": "himalayas", "reason": "Remote-only source; US remote isn't in your geographies"})
    queries: list[dict[str, str]] = []
    jsearch_down = True  # not configured or out of budget (a fully cached JSearch still counts as coverage)
    if not env.jsearch_configured:
        skipped.append({"provider": "jsearch", "reason": "Not configured (set JSEARCH_ENABLED=true and JSEARCH_API_KEY)"})
    else:
        used = jsearch_used_this_month(session)
        budget = min(env.jsearch_monthly_limit - used, env.jsearch_max_requests_per_run)
        fresh_after = datetime.now(timezone.utc) - timedelta(hours=env.jsearch_cache_hours)
        recent = set(session.scalars(
            select(ProviderRequest.query_key).where(ProviderRequest.provider == "jsearch", ProviderRequest.requested_at >= fresh_after)
        ))
        pending = [q for q in jsearch_queries([ROLE_FAMILIES[f][1] for f in families], geos) if query_key(q) not in recent]
        if budget <= 0:
            skipped.append({"provider": "jsearch", "reason": f"Request budget used ({used}/{env.jsearch_monthly_limit} this month)"})
        elif not pending:
            jsearch_down = False
            skipped.append({"provider": "jsearch", "reason": f"All searches ran in the last {env.jsearch_cache_hours} h (cached)"})
        else:
            jsearch_down = False
            queries = pending[:budget]
    if jsearch_down and geos != ["remote_us"]:
        warnings.append("JSearch isn't available (not configured or out of budget): on-site and hybrid NJ/NYC jobs come only from "
                        "verified employer boards.")
    return {"criteria": criteria, "queries": queries, "himalayas": env.himalayas_enabled and "remote_us" in geos,
            "skipped": skipped, "warnings": warnings}


def _jsearch_reserver(factory: Callable[[], Session], env: DiscoverySettingsEnv) -> Callable[[str], None]:
    sent = [0]

    def reserve(key: str) -> None:
        # Logged (committed) before the request is sent, so crashes and retries still count against the budget.
        with factory() as session:
            used = jsearch_used_this_month(session)
            if used >= env.jsearch_monthly_limit or sent[0] >= env.jsearch_max_requests_per_run:
                raise BudgetExceeded(f"Request budget reached ({used}/{env.jsearch_monthly_limit} this month, "
                                     f"{sent[0]}/{env.jsearch_max_requests_per_run} this run); no request sent")
            session.add(ProviderRequest(provider="jsearch", query_key=key))
            session.commit()
        sent[0] += 1

    return reserve


def mark_synced(session: Session, source_id: uuid.UUID, interval_hours: int = 24, count: int | None = None) -> None:
    """Only called for ownership-verified boards: a successful fetch through the official API completes verification.
    Schedules the next sync one interval from now and clears the failure backoff."""
    session.execute(update(JobSource).where(JobSource.id == source_id).values(
        last_success_at=func.now(), last_checked_at=func.now(), last_error=None, last_error_at=None,
        next_sync_at=func.now() + timedelta(hours=interval_hours), sync_failures=0,
        last_sync_count=func.coalesce(count, JobSource.last_sync_count),
        verification_status="verified", verified_at=func.coalesce(JobSource.verified_at, func.now()),
        verification_evidence=func.coalesce(JobSource.verification_evidence, "Official API answered a job import")))
    # Evidence from the postings themselves: a board listing only interest forms is a prospect pool.
    total, forms = session.execute(select(func.count(), func.count(DiscoveredJob.non_vacancy_reason)).where(
        DiscoveredJob.source_id == source_id, DiscoveredJob.generation == ACTIVE_GENERATION)).one()
    if total:
        session.execute(update(JobSource).where(JobSource.id == source_id).values(
            board_role="talent_pool" if forms == total else "general"))


def _verify_candidates(
    factory: Callable[[], Session], client: HttpClient, candidates: list[tuple[AtsRef, uuid.UUID, str]], limit: int
) -> tuple[int, list[str]]:
    """Verifies up to `limit` new ATS boards seen in aggregator links (one request each). Verified boards become
    eligible for later runs; they are not fetched in this one."""
    with factory() as session:
        todo = [c for c in candidates if needs_verification(session, c[0])]
    verified, notes = 0, []
    for ref, company_id, url in todo[:limit]:
        try:
            verify_board(client, ref.provider, ref.board)
            error = None
        except SourceError as exc:
            error = exc
        with factory() as session:
            record_verification(session, ref, company_id, url, error)
            session.commit()
        verified += error is None
    if len(todo) > limit:
        notes.append(f"{len(todo) - limit} more employer boards were found in job links; they'll be verified on later runs.")
    return verified, notes


def reclassify_non_vacancies(session: Session, apply: bool) -> list[tuple[DiscoveredJob, str | None, str | None]]:
    """Re-runs the non-vacancy rules over stored postings (offline). Returns (job, old, new) for every change.
    Nothing is deleted: excluded postings keep their identity, history, review state, and saved links."""
    # shortcut: loads every posting; fine for one person's data, batch with yield_per past ~50k rows.
    changes = []
    for job in session.scalars(select(DiscoveredJob)):
        new = non_vacancy_reason(job.title, job.description, job.company)
        if new != job.non_vacancy_reason:
            changes.append((job, job.non_vacancy_reason, new))
            if apply:
                job.non_vacancy_reason = new
    if apply:
        session.commit()
    return changes


def _with_progress(session: Session, runs: list[DiscoveryRun]) -> list[DiscoveryRunRead]:
    from app.services.sync import progress  # sync imports this module

    counts = progress(session, [r.id for r in runs])
    return [DiscoveryRunRead.model_validate(r).model_copy(update=counts.get(r.id, {})) for r in runs]


def get_run(session: Session, run_id: uuid.UUID) -> DiscoveryRunRead:
    run = session.get(DiscoveryRun, run_id)
    if run is None:
        raise NotFoundError("Discovery run not found")
    return _with_progress(session, [run])[0]


def list_runs(session: Session, limit: int, offset: int) -> list[DiscoveryRunRead]:
    stmt = select(DiscoveryRun).order_by(DiscoveryRun.started_at.desc(), DiscoveryRun.id.desc())
    return _with_progress(session, list(session.scalars(stmt.limit(limit).offset(offset))))


# --- Discovered jobs ----------------------------------------------------------------------------

ATS_PROVIDERS = ("greenhouse", "lever", "lever_eu", "ashby")


@dataclass(frozen=True)
class JobQuery:
    """Inbox filters. Missing optional data (salary, posting date, seniority) never hides a job unless the
    filter asks for exactly that value."""

    filter_result: str | None = None
    review_status: str | None = None
    provider: str | None = None
    q: str | None = None
    hide_duplicates: bool = False
    company_id: uuid.UUID | None = None
    role_family: str | None = None
    location: str | None = None
    workplace: str | None = None  # remote | hybrid | onsite | unknown
    seniority: str | None = None
    min_salary: int | None = None
    salary_currency: str = "USD"
    posted_within_days: int | None = None
    saved_state: str | None = None  # saved | unsaved | never
    employment_types: tuple[str, ...] = ()  # any of EMPLOYMENT_TYPES and/or "unknown"; empty = no filter
    sort: str = "discovered"


def _words_regex(words: tuple[str, ...]) -> str:
    # PostgreSQL ARE: \y is a word boundary; re.escape output is valid there (backslash + punctuation = literal).
    return r"\y(" + "|".join(re.escape(w) for w in words) + r")\y"


def _comparable_salary(currency: str) -> Any:
    """Annual salary in the given currency, else NULL. Hourly/monthly or other currencies are never compared."""
    d = DiscoveredJob
    annual = or_(d.salary_interval.ilike("%year%"), d.salary_interval.ilike("%annual%"))
    return case((and_(d.salary_currency == currency, annual), func.coalesce(d.salary_max, d.salary_min)), else_=None)


def _first_appearance() -> Any:
    """True for the earliest active row of each job. Rows linked by duplicate_of_id share one identity, so a job seen
    through two sources (or matching an archived record) is one card and one count."""
    d, o = DiscoveredJob, aliased(DiscoveredJob)
    return ~exists().where(
        o.generation == ACTIVE_GENERATION,
        func.coalesce(o.duplicate_of_id, o.id) == func.coalesce(d.duplicate_of_id, d.id),
        # The original record wins, then the earliest seen.
        tuple_(o.duplicate_of_id.is_not(None), o.first_seen_at, o.id) < tuple_(d.duplicate_of_id.is_not(None), d.first_seen_at, d.id),
    )


def _conditions(q: JobQuery, include_result: bool = True) -> list[Any]:
    d = DiscoveredJob
    # Archived generations, non-vacancies, and postings the employer no longer lists never show.
    out: list[Any] = [d.generation == ACTIVE_GENERATION, d.non_vacancy_reason.is_(None), d.closed_at.is_(None)]
    if include_result and q.filter_result:
        out.append(d.filter_result == q.filter_result)
    if q.review_status:
        out.append(d.review_status == q.review_status)
    if q.provider:
        out.append(d.provider == q.provider)
    if q.q:
        out.append(or_(*(c.icontains(q.q, autoescape=True) for c in (d.title, d.company, d.location))))
    if q.hide_duplicates:
        out.append(_first_appearance())
    if q.company_id:
        out.append(d.company_id == q.company_id)
    if q.role_family:
        out.append(d.title.op("~*")(_words_regex(ROLE_FAMILIES[q.role_family][2])))
    if q.location:
        out.append(d.location.icontains(q.location, autoescape=True))
    if q.workplace:
        out.append(d.workplace_type.is_(None) if q.workplace == "unknown" else d.workplace_type == q.workplace)
    if q.seniority:
        out.append(or_(d.seniority == q.seniority, d.title.op("~*")(_words_regex(SENIORITY_WORDS[q.seniority]))))
    if q.min_salary is not None:
        # Excludes only jobs whose comparable salary is known to be lower; unknown salary stays visible.
        out.append(func.coalesce(_comparable_salary(q.salary_currency) >= q.min_salary, True))
    if q.posted_within_days is not None:
        cutoff = datetime.now(timezone.utc) - timedelta(days=q.posted_within_days)
        out.append(or_(d.posted_at.is_(None), d.posted_at >= cutoff))  # unknown posting date stays visible
    if q.employment_types:
        known = [t for t in q.employment_types if t != "unknown"]
        either = [d.employment_type.in_(known)] if known else []
        if "unknown" in q.employment_types:
            either.append(d.employment_type.is_(None))  # missing type is shown only when "Unknown" is selected
        out.append(or_(*either))
    if q.saved_state == "saved":
        out.append(d.review_status == "saved")
    elif q.saved_state == "unsaved":
        out.append(and_(d.unsaved_at.is_not(None), d.review_status != "saved"))
    elif q.saved_state == "never":
        out.append(and_(d.saved_job_id.is_(None), d.review_status != "saved"))
    return out


def _order(q: JobQuery) -> list[Any]:
    d = DiscoveredJob
    newest = [d.first_seen_at.desc(), d.id.desc()]
    return {
        "discovered": newest,
        "posted": [nulls_last(d.posted_at.desc()), *newest],
        "salary": [nulls_last(_comparable_salary(q.salary_currency).desc()), *newest],
        "title": [func.lower(d.title), *newest],
        "company": [func.lower(d.company), func.lower(d.title), *newest],
    }[q.sort]


def list_jobs(session: Session, q: JobQuery, limit: int, offset: int) -> list[DiscoveredJobRead]:
    stmt = select(DiscoveredJob).where(*_conditions(q)).order_by(*_order(q)).limit(limit).offset(offset)
    return _enrich(session, list(session.scalars(stmt)))


def summary(session: Session, q: JobQuery) -> dict[str, Any]:
    """Counts over the active generation (not a page). A job seen through several sources counts once."""
    d = DiscoveredJob
    distinct = (d.generation == ACTIVE_GENERATION, d.non_vacancy_reason.is_(None), d.closed_at.is_(None), _first_appearance())
    total = session.scalar(select(func.count()).select_from(d).where(*distinct)) or 0
    by_source = dict(session.execute(select(d.provider, func.count()).where(*distinct).group_by(d.provider)).all())
    archived = session.scalar(
        select(func.count()).select_from(d).where(or_(d.generation.is_(None), d.generation != ACTIVE_GENERATION))
    ) or 0
    matching = session.scalar(select(func.count()).select_from(d).where(*_conditions(q))) or 0
    by_result = dict(session.execute(
        select(d.filter_result, func.count()).where(*_conditions(q, include_result=False)).group_by(d.filter_result)
    ).all())
    non_vacancies = session.scalar(select(func.count()).select_from(d).where(
        d.generation == ACTIVE_GENERATION, d.non_vacancy_reason.is_not(None), d.closed_at.is_(None))) or 0
    closed = session.scalar(select(func.count()).select_from(d).where(d.generation == ACTIVE_GENERATION, d.closed_at.is_not(None))) or 0
    return {"total": total, "matching": matching, "by_source": by_source, "archived": archived, "non_vacancies": non_vacancies,
            "closed": closed,
            "by_result": {k: by_result.get(k, 0) for k in ("match", "unknown", "rejected")}}


def _experience(session: Session, saved_job_ids: set[uuid.UUID]) -> dict[uuid.UUID, ExperienceMatch]:
    """Latest saved AI assessment per saved job. "evaluation_available" if made after the current profile was
    saved, else "previously_evaluated". Reads only; never starts an evaluation."""
    if not saved_job_ids:
        return {}
    profile = session.get(CandidateProfile, 1)
    out: dict[uuid.UUID, ExperienceMatch] = {}
    for m in session.scalars(select(JobMatch).where(JobMatch.job_id.in_(saved_job_ids)).order_by(JobMatch.created_at.desc())):
        if m.job_id not in out:
            current = profile is not None and m.created_at >= profile.updated_at
            out[m.job_id] = ExperienceMatch(
                status="evaluation_available" if current else "previously_evaluated", match_id=m.id,
                overall_match=m.overall_match, evaluated_at=m.created_at,
                recommendation=(m.assessment or {}).get("recommendation"),
            )
    return out


def _enrich(session: Session, jobs: list[DiscoveredJob], read: type[DiscoveredJobRead] = DiscoveredJobRead) -> list[Any]:
    """Adds technologies, the best posting link, the verified company website, and experience-match state."""
    # An aggregator listing whose ATS posting we also have from the employer's own board links to that posting.
    keys = {j.ats_job_key for j in jobs if j.ats_job_key and j.provider == "jsearch"}
    direct = dict(session.execute(
        select(DiscoveredJob.ats_job_key, DiscoveredJob.posting_url)
        .where(DiscoveredJob.ats_job_key.in_(keys), DiscoveredJob.provider.in_(ATS_PROVIDERS),
               DiscoveredJob.posting_url.is_not(None))
    ).all()) if keys else {}
    experience = _experience(session, {j.saved_job_id for j in jobs if j.saved_job_id})
    out = []
    for j in jobs:
        link = None
        if j.provider in ATS_PROVIDERS and j.posting_url:
            link = PostingLink(url=j.posting_url, kind="employer", via=j.provider)  # the employer's job posting page
        elif j.provider == "jsearch" and (url := direct.get(j.ats_job_key)):
            link = PostingLink(url=url, kind="employer", via="employer ATS")
        elif j.posting_url:
            # Himalayas' terms require its applicationLink; a JSearch link may itself be an ATS posting.
            employer = j.provider == "jsearch" and parse_ats_url(j.posting_url) is not None
            link = PostingLink(url=j.posting_url, kind="employer" if employer else "aggregator", via=j.provider)
        item = read.model_validate(j).model_copy(update={
            "technologies": technologies(j.description),
            "experience_required": required_experience(j.description),
            "posting_link": link,
            "experience_match": experience.get(j.saved_job_id, ExperienceMatch(status="not_evaluated")) if j.saved_job_id
            else ExperienceMatch(status="not_evaluated"),
        })
        if j.company_ref is not None:
            c = j.company_ref
            website = c.website if c.verification_status == "verified" and c.website else None
            item = item.model_copy(update={"company_ref": item.company_ref.model_copy(update={"website": website})})  # type: ignore[union-attr]
        out.append(item)
    return out


def job_detail(session: Session, job_id: uuid.UUID) -> DiscoveredJobDetail:
    job = _get_job(session, job_id)
    [detail] = _enrich(session, [job], DiscoveredJobDetail)
    return detail.model_copy(update={"requirements": JobRequirements(**requirements(job.description))})


def _get_job(session: Session, job_id: uuid.UUID, lock: bool = False) -> DiscoveredJob:
    stmt = select(DiscoveredJob).where(DiscoveredJob.id == job_id)
    job = session.scalar(stmt.with_for_update(of=DiscoveredJob) if lock else stmt)
    if job is None:
        raise NotFoundError("Discovered job not found")
    return job


def log_review(job: DiscoveredJob, action: str) -> None:
    job.review_history = [*(job.review_history or []), {"action": action, "at": datetime.now(timezone.utc).isoformat()}]


def set_review_status(session: Session, job_id: uuid.UUID, review_status: str) -> DiscoveredJobRead:
    job = _get_job(session, job_id, lock=True)
    if job.review_status == "saved":
        raise ConflictError("This job is in Saved Jobs. Remove it from Saved Jobs first.")
    if job.review_status != review_status:
        job.review_status = review_status
        log_review(job, review_status)
    session.commit()
    session.refresh(job)
    return _enrich(session, [job])[0]


def save_job(session: Session, job_id: uuid.UUID) -> DiscoveredJobRead:
    """Puts the lead in Saved Jobs. Reuses the job it was saved as before (even if later unsaved), else an
    existing saved job with the same posting URL, else creates one. Idempotent. Never creates an Application."""
    found = _get_job(session, job_id, lock=True)  # row lock: double clicks can't create two saved jobs
    target = session.get(Job, found.saved_job_id) if found.saved_job_id else None
    if target is None:
        # The same job seen through another source (incl. an archived appearance) reuses that saved job.
        root = func.coalesce(DiscoveredJob.duplicate_of_id, DiscoveredJob.id)
        sibling = session.scalar(
            select(DiscoveredJob.saved_job_id).where(
                root == (found.duplicate_of_id or found.id), DiscoveredJob.saved_job_id.is_not(None))
            .order_by(DiscoveredJob.first_seen_at).limit(1)
        )
        target = session.get(Job, sibling) if sibling else None
    if target is None:
        data = JobCreate(
            company=found.company,
            title=found.title,
            location=found.location,
            posting_url=found.posting_url,
            description=found.description,
            notes=f"Found via job discovery ({found.provider}).",
        )
        values = data.model_dump(mode="json")
        target = (
            # Prefer a job that is still in Saved Jobs over one that was removed, so no URL is active twice.
            session.scalar(select(Job).where(Job.posting_url == values["posting_url"])
                           .order_by(Job.unsaved_at.is_not(None), Job.created_at).limit(1))
            if values["posting_url"] else None
        )
        if target is None:
            target = Job(**values)
            session.add(target)
            session.flush()
    changed = found.review_status != "saved" or target.unsaved_at is not None
    target.unsaved_at = None
    found.saved_job_id = target.id
    found.review_status = "saved"
    if changed:
        log_review(found, "saved")
    session.commit()
    session.refresh(found)
    return _enrich(session, [found])[0]


def coverage(session: Session, env: DiscoverySettingsEnv) -> dict[str, Any]:
    """What the next run can reach, without contacting anything. Never includes keys."""
    criteria = get_filters(session).model_dump()
    remote = "remote_us" in (criteria["geographies"] or ["remote_us"])
    boards_total = session.scalar(select(func.count()).select_from(JobSource).where(JobSource.enabled)) or 0
    boards_verified = session.scalar(select(func.count()).select_from(JobSource).where(*SYNCABLE)) or 0
    used = jsearch_used_this_month(session)
    providers = [
        {"provider": "himalayas", "kind": "aggregator", "available": env.himalayas_enabled and remote,
         "detail": "Free remote-jobs feed, no key. Remote roles only; eligibility varies by job."
         if env.himalayas_enabled else "Disabled (DISCOVERY_HIMALAYAS_ENABLED=false)."},
        {"provider": "jsearch", "kind": "aggregator", "available": env.jsearch_configured and used < env.jsearch_monthly_limit,
         "detail": (f"{used}/{env.jsearch_monthly_limit} requests used this month; up to {env.jsearch_max_requests_per_run} per run."
                    if env.jsearch_configured else "Not configured. Optional: set JSEARCH_ENABLED=true and JSEARCH_API_KEY.")},
        {"provider": "ats_boards", "kind": "ats_boards", "available": boards_verified > 0,
         "detail": f"{boards_verified} of {boards_total} enabled employer boards have verified employer ownership and are "
                   f"synced; every one is checked on Run discovery"
                   + (f" and automatically every {env.sync_interval_hours} h." if env.auto_sync_enabled and env.worker_enabled
                      else " (automatic sync is off).")},
    ]
    warnings = []
    if not env.jsearch_configured:
        warnings.append("Geographic coverage is limited: without JSearch, on-site and hybrid NJ/NYC jobs come only from monitored employer boards.")
    return {"providers": providers, "boards_total": boards_total, "boards_verified": boards_verified,
            "boards_per_run": boards_verified, "warnings": warnings}
