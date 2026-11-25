import uuid
from collections.abc import Callable

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.config import DiscoverySettingsEnv, get_discovery_env
from app.db import get_db, get_session_factory
from app.schemas import (
    DiscoveredJobDetail,
    DiscoveredJobRead,
    DiscoveryCoverage,
    DiscoverySort,
    DiscoverySummary,
    DiscoveryFilters,
    DiscoveryRunRead,
    FilterResult,
    JobSourceCreate,
    JobSourceRead,
    JobSourceUpdate,
    JobProvider,
    ReviewStatus,
    ReviewUpdate,
)
from app.services import discovery as service
from app.services import sync
from app.services.discovery_filters import ROLE_FAMILY_KEYS, SENIORITY_LEVELS
from app.services.job_sources import EMPLOYMENT_TYPES, HttpClient

router = APIRouter(prefix="/api/discovery", tags=["job-discovery"])


def get_http_client() -> HttpClient:
    """One client per run, so polite request spacing applies across all sources in the run."""
    return HttpClient()


def _handle(fn: Callable[[], object]) -> object:
    try:
        return fn()
    except service.NotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from None
    except service.ConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from None


# --- Sources ---


@router.get("/sources", response_model=list[JobSourceRead])
def list_sources(session: Session = Depends(get_db)) -> list[JobSourceRead]:
    return [JobSourceRead.model_validate(s) for s in service.list_sources(session)]


@router.post("/sources", response_model=JobSourceRead, status_code=status.HTTP_201_CREATED)
def create_source(payload: JobSourceCreate, session: Session = Depends(get_db)) -> JobSourceRead:
    return JobSourceRead.model_validate(_handle(lambda: service.create_source(session, payload)))


@router.patch("/sources/{source_id}", response_model=JobSourceRead)
def update_source(source_id: uuid.UUID, payload: JobSourceUpdate, session: Session = Depends(get_db)) -> JobSourceRead:
    return JobSourceRead.model_validate(_handle(lambda: service.update_source(session, source_id, payload)))


@router.delete("/sources/{source_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_source(source_id: uuid.UUID, session: Session = Depends(get_db)) -> None:
    """Removes the source configuration; jobs already discovered from it are kept."""
    _handle(lambda: service.delete_source(session, source_id))


# --- Filters ---


@router.get("/filters", response_model=DiscoveryFilters)
def get_filters(session: Session = Depends(get_db)) -> DiscoveryFilters:
    return service.get_filters(session)


@router.put("/filters", response_model=DiscoveryFilters)
def put_filters(payload: DiscoveryFilters, session: Session = Depends(get_db)) -> DiscoveryFilters:
    """Saves preferences and re-evaluates all discovered jobs against them."""
    return service.save_filters(session, payload)


# --- Runs ---


RunExecutor = Callable[[Callable[[], Session], HttpClient, DiscoverySettingsEnv], None]


def get_run_executor() -> RunExecutor:
    """Wakes the background worker; the queued run is processed outside this request. (Tests drain it inline.)"""
    from app.main import worker

    return lambda factory, client, env: worker.notify()


@router.post("/runs", response_model=DiscoveryRunRead, status_code=status.HTTP_201_CREATED)
def start_run(
    session: Session = Depends(get_db),
    factory: Callable[[], Session] = Depends(get_session_factory),
    client: HttpClient = Depends(get_http_client),
    env: DiscoverySettingsEnv = Depends(get_discovery_env),
    execute: RunExecutor = Depends(get_run_executor),
) -> DiscoveryRunRead:
    """Queues a run: every verified board not synced in the last DISCOVERY_MIN_RESYNC_MINUTES (overdue first), plus
    enabled job feeds. Returns at once with status "running"; poll GET /runs/{id} for progress. 409 if a run is
    active or no source is available."""
    run_id = _handle(lambda: sync.start_run(factory, env))
    execute(factory, client, env)
    session.expire_all()
    return service.get_run(session, run_id)  # type: ignore[arg-type,return-value]


@router.get("/coverage", response_model=DiscoveryCoverage)
def get_coverage(
    session: Session = Depends(get_db), env: DiscoverySettingsEnv = Depends(get_discovery_env)
) -> DiscoveryCoverage:
    """Which providers the next run can use, and request budgets. Contacts nothing; never returns keys."""
    return DiscoveryCoverage.model_validate(service.coverage(session, env))


@router.get("/runs", response_model=list[DiscoveryRunRead])
def list_runs(
    limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0), session: Session = Depends(get_db)
) -> list[DiscoveryRunRead]:
    return service.list_runs(session, limit, offset)  # type: ignore[return-value]


@router.get("/runs/{run_id}", response_model=DiscoveryRunRead)
def get_run(run_id: uuid.UUID, session: Session = Depends(get_db)) -> DiscoveryRunRead:
    return _handle(lambda: service.get_run(session, run_id))  # type: ignore[return-value]


# --- Discovered jobs ---


def _job_query(
    filter_result: FilterResult | None = Query(None, description="Preference result"),
    review_status: ReviewStatus | None = None,
    provider: JobProvider | None = None,
    q: str | None = Query(None, max_length=200, description="Case-insensitive title/company/location substring"),
    hide_duplicates: bool = Query(False, description="Leave out possible duplicates of earlier postings"),
    company_id: uuid.UUID | None = None,
    role_family: Literal[ROLE_FAMILY_KEYS] | None = Query(None, description="Title matches a role family"),  # type: ignore[valid-type]
    location: str | None = Query(None, max_length=200, description="Location substring"),
    workplace: Literal["remote", "hybrid", "onsite", "unknown"] | None = None,
    seniority: Literal[SENIORITY_LEVELS] | None = Query(None, description="Stated by the source or in the title"),  # type: ignore[valid-type]
    min_salary: int | None = Query(None, ge=0, le=10_000_000, description="Hides only jobs whose comparable annual salary is lower; unknown salaries stay"),
    salary_currency: str = Query("USD", pattern=r"^[A-Z]{3}$"),
    posted_within_days: int | None = Query(None, ge=1, le=365, description="Jobs without a posting date stay"),
    saved_state: Literal["saved", "unsaved", "never"] | None = None,
    employment_type: list[Literal[(*EMPLOYMENT_TYPES, "unknown")]] = Query(  # type: ignore[valid-type]
        [], description="Repeatable. Jobs without a stated type are included only when 'unknown' is selected"
    ),
    sort: DiscoverySort = "discovered",
) -> service.JobQuery:
    return service.JobQuery(
        filter_result, review_status, provider, q, hide_duplicates, company_id, role_family, location, workplace,
        seniority, min_salary, salary_currency, posted_within_days, saved_state, tuple(employment_type), sort,
    )


@router.get("/jobs", response_model=list[DiscoveredJobRead])
def list_jobs(
    query: service.JobQuery = Depends(_job_query),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    session: Session = Depends(get_db),
) -> list[DiscoveredJobRead]:
    """Compact cards (no description); see GET /jobs/{id} for details."""
    return service.list_jobs(session, query, limit, offset)


# Declared before /jobs/{job_id} so "summary" isn't parsed as an ID.
@router.get("/jobs/summary", response_model=DiscoverySummary)
def jobs_summary(query: service.JobQuery = Depends(_job_query), session: Session = Depends(get_db)) -> DiscoverySummary:
    """Counts over the whole table: all unique discovered jobs, and how many match the same filters as /jobs."""
    return DiscoverySummary.model_validate(service.summary(session, query))


@router.get("/jobs/{job_id}", response_model=DiscoveredJobDetail)
def get_job(job_id: uuid.UUID, session: Session = Depends(get_db)) -> DiscoveredJobDetail:
    """Full description, conservatively extracted requirements, review history, and any saved AI assessment.
    Never starts an AI evaluation."""
    return _handle(lambda: service.job_detail(session, job_id))  # type: ignore[return-value]


@router.patch("/jobs/{job_id}", response_model=DiscoveredJobRead)
def review_job(job_id: uuid.UUID, payload: ReviewUpdate, session: Session = Depends(get_db)) -> DiscoveredJobRead:
    """Pass (skipped), review later, or restore (new). Saving uses POST /jobs/{id}/save; removing from Saved
    Jobs uses POST /api/jobs/{saved_job_id}/unsave."""
    return _handle(lambda: service.set_review_status(session, job_id, payload.review_status))  # type: ignore[return-value]


@router.post("/jobs/{job_id}/save", response_model=DiscoveredJobRead)
def save_job(job_id: uuid.UUID, session: Session = Depends(get_db)) -> DiscoveredJobRead:
    """Adds the lead to Saved Jobs, reusing its earlier saved job (even if unsaved) or one with the same posting
    URL. Idempotent. Never creates an application."""
    try:
        return _handle(lambda: service.save_job(session, job_id))  # type: ignore[return-value]
    except ValidationError as exc:
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"This posting can't be saved as-is: {exc.errors()[0]['msg']}",
        ) from None
