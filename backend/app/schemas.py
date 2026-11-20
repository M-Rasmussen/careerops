"""Pydantic request/response schemas."""

import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, HttpUrl, StringConstraints

from app.models import ApplicationStatus

RequiredStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
OptionalStr = Annotated[str, StringConstraints(strip_whitespace=True)]


def _empty_to_none(value: str | None) -> str | None:
    return value or None


class JobCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    company: Annotated[RequiredStr, Field(max_length=200)]
    title: Annotated[RequiredStr, Field(max_length=300)]
    location: Annotated[OptionalStr | None, Field(max_length=200), AfterValidator(_empty_to_none)] = None
    posting_url: Annotated[HttpUrl | None, Field(max_length=2048)] = None
    description: Annotated[OptionalStr | None, AfterValidator(_empty_to_none)] = None
    notes: Annotated[OptionalStr | None, AfterValidator(_empty_to_none)] = None


class JobRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    company: str
    title: str
    location: str | None
    posting_url: str | None
    description: str | None
    notes: str | None
    created_at: datetime
    updated_at: datetime


def _not_future(value: date) -> date:
    # One day of slack so a user ahead of UTC can still enter "today" in their time zone.
    if value > datetime.now(timezone.utc).date() + timedelta(days=1):
        raise ValueError("applied_date cannot be in the future")
    return value


AppliedDate = Annotated[date, AfterValidator(_not_future)]
Notes = Annotated[OptionalStr | None, AfterValidator(_empty_to_none)]


class ApplicationCreate(BaseModel):
    """Recording an application requires explicit confirmation that it was actually submitted."""

    model_config = ConfigDict(extra="forbid")

    job_id: uuid.UUID
    submission_confirmed: Literal[True]
    status: ApplicationStatus = ApplicationStatus.APPLIED
    applied_date: AppliedDate
    follow_up_date: date | None = None
    notes: Notes = None


class ApplicationUpdate(BaseModel):
    """Partial update. Omitted fields are unchanged; job_id cannot change."""

    model_config = ConfigDict(extra="forbid")

    # Defaults are not validated, so omitting these is fine but an explicit null is rejected.
    status: ApplicationStatus = None  # type: ignore[assignment]
    applied_date: AppliedDate = None  # type: ignore[assignment]
    follow_up_date: date | None = None
    notes: Notes = None


class ApplicationRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    job_id: uuid.UUID
    status: ApplicationStatus
    applied_date: date
    follow_up_date: date | None
    notes: str | None
    created_at: datetime
    updated_at: datetime
    job: JobRead
