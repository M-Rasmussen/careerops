import uuid
from datetime import date, datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import Select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import Application, ApplicationStatus
from app.schemas import ApplicationCreate, ApplicationRead, ApplicationUpdate
from app.services import applications as service
from app.services.excel import applications_to_xlsx

router = APIRouter(prefix="/api/applications", tags=["applications"])

XLSX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _filters(
    status_: ApplicationStatus | None = Query(None, alias="status"),
    company: str | None = Query(None, max_length=200, description="Case-insensitive substring"),
    applied_from: date | None = Query(None, description="Inclusive"),
    applied_to: date | None = Query(None, description="Inclusive"),
) -> Select[tuple[Application]]:
    return service.filtered_applications(status_, company, applied_from, applied_to)


def _get_or_404(session: Session, application_id: uuid.UUID) -> Application:
    application = service.get_application(session, application_id)
    if application is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Application not found")
    return application


@router.post("", response_model=ApplicationRead, status_code=status.HTTP_201_CREATED)
def create_application(
    payload: ApplicationCreate, session: Session = Depends(get_db)
) -> ApplicationRead:
    try:
        application = service.create_application(session, payload)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from None
    except service.JobNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found") from None
    except service.DuplicateApplicationError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="An application for this job already exists"
        ) from None
    return ApplicationRead.model_validate(application)


@router.get("", response_model=list[ApplicationRead])
def list_applications(
    stmt: Select[tuple[Application]] = Depends(_filters),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    session: Session = Depends(get_db),
) -> list[ApplicationRead]:
    return [
        ApplicationRead.model_validate(a)
        for a in service.list_applications(session, stmt, limit, offset)
    ]


# Declared before /{application_id} so "export.xlsx" isn't parsed as an ID.
@router.get(
    "/export.xlsx",
    response_class=Response,
    responses={200: {"content": {XLSX_MEDIA_TYPE: {}}, "description": "Excel workbook"}},
)
def export_applications(
    stmt: Select[tuple[Application]] = Depends(_filters), session: Session = Depends(get_db)
) -> Response:
    """All applications matching the filters (no pagination) as an .xlsx workbook."""
    content = applications_to_xlsx(service.list_applications(session, stmt))
    filename = f"applications_{datetime.now(timezone.utc):%Y%m%d}.xlsx"
    return Response(
        content,
        media_type=XLSX_MEDIA_TYPE,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/{application_id}", response_model=ApplicationRead)
def get_application(application_id: uuid.UUID, session: Session = Depends(get_db)) -> ApplicationRead:
    return ApplicationRead.model_validate(_get_or_404(session, application_id))


@router.patch("/{application_id}", response_model=ApplicationRead)
def update_application(
    application_id: uuid.UUID, payload: ApplicationUpdate, session: Session = Depends(get_db)
) -> ApplicationRead:
    application = _get_or_404(session, application_id)
    try:
        updated = service.update_application(session, application, payload)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from None
    return ApplicationRead.model_validate(updated)


@router.delete("/{application_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_application(application_id: uuid.UUID, session: Session = Depends(get_db)) -> None:
    service.delete_application(session, _get_or_404(session, application_id))
