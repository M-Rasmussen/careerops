import uuid
from collections.abc import Callable
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.api.discovery import get_http_client
from app.db import get_db, get_session_factory
from app.schemas import CompanyCreate, CompanyRead, CompanyUpdate, JobSourceRead, VerifyRequest
from app.services import career_discovery
from app.services import companies as service
from app.services.job_sources import HttpClient

router = APIRouter(prefix="/api/companies", tags=["companies"])


def _handle(fn: Callable[[], object]) -> object:
    try:
        return fn()
    except service.NotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from None
    except service.ConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from None
    except (ValueError, service.VerificationError) as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from None


@router.get("", response_model=list[CompanyRead])
def list_companies(
    q: str | None = Query(None, max_length=200, description="Name or domain substring"),
    ats_status: Literal["verified", "unverified", "needs_review", "failed", "none"] | None = None,
    origin: Literal["curated_seed", "manual", "discovered"] | None = None,
    watchlist: bool | None = None,
    sort: Literal["name", "jobs", "last_seen"] = "name",
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    session: Session = Depends(get_db),
) -> list[CompanyRead]:
    return service.list_companies(session, q, ats_status, origin, watchlist, sort, limit, offset)


@router.post("", response_model=CompanyRead, status_code=status.HTTP_201_CREATED)
def create_company(payload: CompanyCreate, session: Session = Depends(get_db)) -> CompanyRead:
    company = _handle(lambda: service.create_company(session, payload))
    return service.company_detail(session, company.id)  # type: ignore[attr-defined]


@router.get("/{company_id}", response_model=CompanyRead)
def get_company(company_id: uuid.UUID, session: Session = Depends(get_db)) -> CompanyRead:
    return _handle(lambda: service.company_detail(session, company_id))  # type: ignore[return-value]


@router.patch("/{company_id}", response_model=CompanyRead)
def update_company(company_id: uuid.UUID, payload: CompanyUpdate, session: Session = Depends(get_db)) -> CompanyRead:
    _handle(lambda: service.update_company(session, company_id, payload))
    return service.company_detail(session, company_id)


@router.post("/{company_id}/verify", response_model=JobSourceRead)
def verify_board_url(
    company_id: uuid.UUID,
    payload: VerifyRequest,
    session: Session = Depends(get_db),
    client: HttpClient = Depends(get_http_client),
) -> JobSourceRead:
    """Detects a Greenhouse/Lever/Ashby board from a pasted URL and verifies it with one read-only request to the
    provider's official API. The pasted URL itself is never fetched. Returns the board with its outcome."""
    return JobSourceRead.model_validate(_handle(lambda: service.verify_url(session, client, company_id, payload.url)))


def get_site_fetcher() -> career_discovery.SiteFetcher:
    return career_discovery.SiteFetcher()


@router.post("/{company_id}/discover", response_model=CompanyRead)
def discover_careers(
    company_id: uuid.UUID,
    deep: bool = Query(False, description="Deeper scan of this one company (more pages and scripts; capped requests)"),
    session: Session = Depends(get_db),
    factory: Callable[[], Session] = Depends(get_session_factory),
    client: HttpClient = Depends(get_http_client),
    fetcher: career_discovery.SiteFetcher = Depends(get_site_fetcher),
) -> CompanyRead:
    """Retry discovery for one company: its official website, careers page, and ATS boards, then imports jobs from
    boards whose ownership was verified. Bounded per company (standard: 40 site requests; deep: 150); never crawls
    the registry."""
    _handle(lambda: service.get_company(session, company_id))
    session.commit()
    career_discovery.enqueue_discovery(session, [company_id], force=True)
    session.commit()
    limits = career_discovery.DEEP if deep else career_discovery.STANDARD
    career_discovery.run_tasks(factory, client, fetcher, max_tasks=1 + limits.boards, company_ids=[company_id], limits=limits)
    session.expire_all()
    return service.company_detail(session, company_id)
