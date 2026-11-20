"""Pydantic request/response schemas."""

import uuid
from datetime import datetime
from typing import Annotated

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, HttpUrl, StringConstraints

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
