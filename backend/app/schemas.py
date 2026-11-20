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


# --- AI matching (Phase 4A) ---

MAX_RESUME_CHARS = 50_000
MAX_JOB_DESCRIPTION_CHARS = 30_000


class ProfileUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    resume_text: Annotated[RequiredStr, Field(max_length=MAX_RESUME_CHARS)]


class ProfileRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    resume_text: str
    created_at: datetime
    updated_at: datetime


OverallMatch = Literal["strong", "moderate", "weak", "insufficient_information"]
QualificationCategory = Literal[
    "required_skill", "preferred_skill", "experience", "experience_level",
    "education", "certification", "project", "other",
]
ShortText = Annotated[RequiredStr, Field(max_length=500)]


class Qualification(BaseModel):
    model_config = ConfigDict(extra="forbid")

    requirement: ShortText
    category: QualificationCategory
    evidence: Annotated[list[ShortText], Field(max_length=5)] = Field(
        default_factory=list, description="Verbatim quotes copied from the resume. Never paraphrase."
    )
    note: Annotated[OptionalStr, Field(max_length=1000)] = ""


class Assessment(BaseModel):
    """Arguments of the save_match_assessment tool, and the stored assessment body."""

    model_config = ConfigDict(extra="forbid")

    overall_match: OverallMatch
    recommendation: Annotated[RequiredStr, Field(max_length=3000)] = Field(
        description="Explanation of the overall recommendation."
    )
    supported: Annotated[list[Qualification], Field(max_length=30)] = Field(
        description="Requirements clearly supported by resume evidence (evidence required)."
    )
    partial: Annotated[list[Qualification], Field(max_length=30)] = Field(
        description="Requirements only partly supported (evidence required)."
    )
    missing: Annotated[list[Qualification], Field(max_length=30)] = Field(
        description="Requirements missing from or not verifiable in the resume."
    )
    uncertainties: Annotated[list[Annotated[RequiredStr, Field(max_length=1000)]], Field(max_length=15)] = Field(
        default_factory=list, description="Insufficient or ambiguous information."
    )


class MatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: uuid.UUID
    job_description: Annotated[RequiredStr, Field(max_length=MAX_JOB_DESCRIPTION_CHARS)]
    # Re-run the agent even if an assessment for identical inputs exists.
    force: bool = False


class AgentRunRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    kind: str
    status: str
    provider: str
    model: str
    steps: int
    tool_calls: list[dict]
    error_type: str | None
    error: str | None
    input_tokens: int | None
    output_tokens: int | None
    started_at: datetime
    finished_at: datetime | None


class JobMatchRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    job_id: uuid.UUID
    overall_match: OverallMatch
    assessment: Assessment
    job_description: str
    created_at: datetime
    job: JobRead
    run: AgentRunRead
    reused: bool = False


class AIStatus(BaseModel):
    configured: bool
    provider: str | None
    model: str | None
    max_steps: int
    max_runs_per_day: int
