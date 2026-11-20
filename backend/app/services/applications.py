"""Application persistence logic. An application row means a user-confirmed submission."""

import uuid
from datetime import date

from sqlalchemy import Select, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import Application, ApplicationStatus, Job
from app.schemas import ApplicationCreate, ApplicationUpdate


class JobNotFoundError(Exception):
    pass


class DuplicateApplicationError(Exception):
    pass


def _check_dates(applied: date, follow_up: date | None) -> None:
    """Shared by create and update; the DB CHECK constraint is the final safeguard."""
    if follow_up is not None and follow_up < applied:
        raise ValueError("follow_up_date cannot be before applied_date")


def create_application(session: Session, data: ApplicationCreate) -> Application:
    _check_dates(data.applied_date, data.follow_up_date)
    if session.get(Job, data.job_id) is None:
        raise JobNotFoundError
    values = data.model_dump(exclude={"submission_confirmed"})
    # psycopg dumps Enum members by name, so store the plain value.
    values["status"] = data.status.value
    application = Application(**values)
    session.add(application)
    try:
        session.commit()
    except IntegrityError:
        # Only the unique job_id constraint can fail here; inputs are already validated.
        session.rollback()
        raise DuplicateApplicationError from None
    session.refresh(application)
    return application


def get_application(session: Session, application_id: uuid.UUID) -> Application | None:
    return session.get(Application, application_id)


def update_application(
    session: Session, application: Application, data: ApplicationUpdate
) -> Application:
    values = data.model_dump(exclude_unset=True)
    # Validate the merged result before touching the ORM object, so a rejected update leaves no dirty state.
    _check_dates(
        values.get("applied_date", application.applied_date),
        values.get("follow_up_date", application.follow_up_date),
    )
    if "status" in values:
        values["status"] = values["status"].value
    for field, value in values.items():
        setattr(application, field, value)
    session.commit()
    session.refresh(application)
    return application


def delete_application(session: Session, application: Application) -> None:
    session.delete(application)
    session.commit()


def filtered_applications(
    status: ApplicationStatus | None = None,
    company: str | None = None,
    applied_from: date | None = None,
    applied_to: date | None = None,
) -> Select[tuple[Application]]:
    """Newest-applied-first query with optional filters; shared by list and export."""
    stmt = select(Application).order_by(
        Application.applied_date.desc(), Application.created_at.desc(), Application.id.desc()
    )
    if status is not None:
        stmt = stmt.where(Application.status == status.value)
    if company:
        stmt = stmt.where(Application.job.has(Job.company.icontains(company, autoescape=True)))
    if applied_from is not None:
        stmt = stmt.where(Application.applied_date >= applied_from)
    if applied_to is not None:
        stmt = stmt.where(Application.applied_date <= applied_to)
    return stmt


def list_applications(
    session: Session, stmt: Select[tuple[Application]], limit: int | None = None, offset: int = 0
) -> list[Application]:
    return list(session.scalars(stmt.limit(limit).offset(offset)))
