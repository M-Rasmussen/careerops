import uuid
from collections.abc import Callable

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy.orm import Session

from app.agent.provider import ModelProvider, build_provider
from app.config import AISettings, get_ai_settings
from app.db import get_db, get_session_factory
from app.schemas import AIStatus, JobMatchRead, MatchRequest, ProfileRead, ProfileUpdate
from app.services import matching as service

router = APIRouter(prefix="/api", tags=["ai-matching"])


def get_provider(settings: AISettings = Depends(get_ai_settings)) -> ModelProvider | None:
    return build_provider(settings)


@router.get("/ai/status", response_model=AIStatus)
def ai_status(
    settings: AISettings = Depends(get_ai_settings), provider: ModelProvider | None = Depends(get_provider)
) -> AIStatus:
    # Never includes the API key or base URL.
    return AIStatus(
        configured=provider is not None,
        provider=provider.name if provider else None,
        model=provider.model if provider else None,
        max_steps=settings.max_steps,
        max_runs_per_day=settings.max_runs_per_day,
    )


@router.get("/profile", response_model=ProfileRead | None)
def get_profile(session: Session = Depends(get_db)) -> ProfileRead | None:
    profile = service.get_profile(session)
    return ProfileRead.model_validate(profile) if profile else None


@router.put("/profile", response_model=ProfileRead)
def put_profile(payload: ProfileUpdate, session: Session = Depends(get_db)) -> ProfileRead:
    return ProfileRead.model_validate(service.save_profile(session, payload))


@router.post(
    "/matches",
    response_model=JobMatchRead,
    status_code=status.HTTP_201_CREATED,
    responses={200: {"description": "Existing assessment for identical inputs reused (no AI call)"}},
)
def request_match(
    payload: MatchRequest,
    response: Response,
    session: Session = Depends(get_db),
    factory: Callable[[], Session] = Depends(get_session_factory),
    settings: AISettings = Depends(get_ai_settings),
    provider: ModelProvider | None = Depends(get_provider),
) -> JobMatchRead:
    if provider is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="AI provider is not configured. Set AI_PROVIDER, AI_MODEL, and AI_BASE_URL in .env.",
        )
    try:
        # Sync endpoint: FastAPI runs it in a worker thread, so the blocking LLM call never stalls the event loop.
        match_id, reused = service.request_match(factory, provider, settings, payload)
    except service.JobNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found") from None
    except service.ProfileMissingError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Save your candidate profile before requesting a match"
        ) from None
    except service.RateLimitedError:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Daily AI analysis limit reached ({settings.max_runs_per_day} runs per 24 hours)",
        ) from None
    except service.AgentFailedError as exc:
        code = status.HTTP_504_GATEWAY_TIMEOUT if exc.error_type == "timeout" else status.HTTP_502_BAD_GATEWAY
        raise HTTPException(
            status_code=code, detail=f"AI analysis failed: {exc.error} (run {exc.run_id} was recorded)"
        ) from None
    if reused:
        response.status_code = status.HTTP_200_OK
    match = service.get_match(session, match_id)
    return JobMatchRead.model_validate(match).model_copy(update={"reused": reused})


@router.get("/matches", response_model=list[JobMatchRead])
def list_matches(
    job_id: uuid.UUID | None = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    session: Session = Depends(get_db),
) -> list[JobMatchRead]:
    return [JobMatchRead.model_validate(m) for m in service.list_matches(session, job_id, limit, offset)]


@router.get("/matches/{match_id}", response_model=JobMatchRead)
def get_match(match_id: uuid.UUID, session: Session = Depends(get_db)) -> JobMatchRead:
    match = service.get_match(session, match_id)
    if match is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Match not found")
    return JobMatchRead.model_validate(match)
