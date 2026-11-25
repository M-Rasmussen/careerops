"""Pydantic request/response schemas."""

import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, HttpUrl, StringConstraints, computed_field

from app.models import ApplicationStatus
from app.services.discovery_filters import GEOGRAPHIES, ROLE_FAMILY_KEYS, SENIORITY_LEVELS
from app.services.job_sources import BOARD_TOKEN_PATTERN, PROVIDERS, board_page_url

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
    unsaved_at: datetime | None = None
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


# --- Job discovery (Phase 4B) ---

Provider = Literal["greenhouse", "lever", "lever_eu", "ashby"]
BoardProvider = Literal["greenhouse", "lever", "lever_eu", "ashby", "workday"]  # workday: recorded, not imported
JobProvider = Literal["greenhouse", "lever", "lever_eu", "ashby", "himalayas", "jsearch"]
BoardToken = Annotated[str, StringConstraints(strip_whitespace=True, pattern=BOARD_TOKEN_PATTERN)]
Keyword = Annotated[RequiredStr, Field(max_length=100)]
Keywords = Annotated[list[Keyword], Field(max_length=50)]
FilterResult = Literal["match", "unknown", "rejected"]
ReviewStatus = Literal["new", "saved", "skipped", "later"]


class JobSourceCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: Provider
    company_name: Annotated[RequiredStr, Field(max_length=200)]
    board_token: BoardToken
    enabled: bool = True
    company_id: uuid.UUID | None = None


class JobSourceUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Omitted = unchanged; explicit null is rejected (defaults are not validated).
    company_name: Annotated[RequiredStr, Field(max_length=200)] = None  # type: ignore[assignment]
    enabled: bool = None  # type: ignore[assignment]


class JobSourceRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    provider: BoardProvider
    company_name: str
    board_token: str
    ats_host: str | None = None
    career_site: str | None = None
    enabled: bool
    last_success_at: datetime | None
    last_error: str | None
    last_error_at: datetime | None
    created_at: datetime
    updated_at: datetime
    company_id: uuid.UUID | None
    # verified = the official API answered; needs_review = it answered but employer ownership isn't proven.
    verification_status: Literal["unverified", "verified", "needs_review", "failed"]
    verification_evidence: str | None
    verified_at: datetime | None
    last_checked_at: datetime | None
    ownership_verified_at: datetime | None = None
    ownership_evidence: str | None = None
    provider_name: str | None = None  # employer name the ATS reports for this board, when it does
    board_role: Literal["general", "talent_pool"] | None = None  # from evidence only; None = unknown
    # Sync: last_success_at = last successful sync, last_checked_at = last attempt, next_sync_at = when it's due.
    next_sync_at: datetime | None = None
    sync_failures: int = 0
    # Filled by the company services: queued | syncing | failed | ok | never | not_imported; active postings it lists.
    sync_status: str | None = None
    active_jobs: int = 0

    @computed_field  # type: ignore[prop-decorator]
    @property
    def region(self) -> str:
        return "EU" if self.provider == "lever_eu" else "Global"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def board_url(self) -> str:
        if self.provider == "workday":
            return f"https://{self.ats_host}/{self.career_site}"
        return board_page_url(self.provider, self.board_token)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def ingestion_supported(self) -> bool:
        """Separate from ownership: whether CareerOps can import this board's jobs (no Workday adapter yet)."""
        return self.provider in PROVIDERS


class DiscoveryFilters(BaseModel):
    """Empty lists / null mean "no preference" for that criterion."""

    model_config = ConfigDict(extra="forbid")

    # Defaults cover every role family and geography so discovery works before any setup.
    role_families: list[Literal[ROLE_FAMILY_KEYS]] = Field(default_factory=lambda: list(ROLE_FAMILY_KEYS))  # type: ignore[valid-type]
    geographies: list[Literal[GEOGRAPHIES]] = Field(default_factory=lambda: list(GEOGRAPHIES))  # type: ignore[valid-type]
    title_keywords: Keywords = Field(default_factory=list, description="Extra title words that count as a match")
    excluded_keywords: Keywords = Field(default_factory=list, description="Reject if the title contains any")
    locations: Keywords = Field(default_factory=list, description="Location must contain one (remote jobs exempt if remote is accepted)")
    workplace_types: list[Literal["remote", "hybrid", "onsite"]] = Field(default_factory=list)
    seniority: list[Literal[SENIORITY_LEVELS]] = Field(default_factory=list)  # type: ignore[valid-type]
    min_salary: Annotated[int, Field(ge=0, le=10_000_000)] | None = None
    salary_currency: Annotated[str, StringConstraints(pattern=r"^[A-Z]{3}$")] = "USD"


class CompanyBrief(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    watchlist: bool
    verification_status: str
    website: str | None = None  # filled only when the company's identity is verified


class PostingLink(BaseModel):
    url: str
    kind: Literal["employer", "aggregator"]  # employer = the employer's own ATS posting
    via: str  # provider whose URL this is


class ExperienceMatch(BaseModel):
    """AI experience assessment state. Never computed here: only read from saved assessments."""

    status: Literal["not_evaluated", "evaluation_available", "previously_evaluated"]
    match_id: uuid.UUID | None = None
    overall_match: str | None = None
    evaluated_at: datetime | None = None
    recommendation: str | None = None


class DiscoveredJobRead(BaseModel):
    """List item: compact (no description). Details come from GET /api/discovery/jobs/{id}."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    source_id: uuid.UUID | None
    company_id: uuid.UUID | None
    company_ref: CompanyBrief | None = Field(serialization_alias="company_info")
    ats_job_key: str | None
    requisition_id: str | None
    seniority: str | None
    provider: JobProvider
    source_posting_id: str
    company: str
    title: str
    location: str | None
    workplace_type: str | None
    employment_type: str | None
    posting_url: str | None
    # The provider's own apply-form URL (Lever/Ashby applyUrl). None when the source has none; never constructed.
    application_url: str | None = None
    salary_min: int | None
    salary_max: int | None
    salary_currency: str | None
    salary_interval: str | None
    source_updated_at: datetime | None
    posted_at: datetime | None
    first_seen_at: datetime
    last_seen_at: datetime
    filter_result: FilterResult
    filter_reasons: list[str]
    review_status: ReviewStatus
    saved_job_id: uuid.UUID | None
    unsaved_at: datetime | None
    duplicate_of_id: uuid.UUID | None
    # Filled by the service.
    technologies: list[str] = Field(default_factory=list)
    # Years of experience the posting explicitly asks for (empty = not specified). Not the AI experience match.
    experience_required: list[str] = Field(default_factory=list)
    posting_link: PostingLink | None = None
    experience_match: ExperienceMatch = ExperienceMatch(status="not_evaluated")


class JobRequirements(BaseModel):
    sections_found: bool
    required: list[str]
    preferred: list[str]
    experience: list[str]
    technologies: list[str]


class DiscoveredJobDetail(DiscoveredJobRead):
    description: str | None
    review_history: list[dict]
    appearances: list[dict] = Field(default_factory=list)  # source history, one entry per generation
    non_vacancy_reason: str | None = None  # why it's hidden from Discovery, if it is
    closed_at: datetime | None = None  # when its employer stopped listing it (kept for history and saved jobs)
    requirements: JobRequirements | None = None


class DiscoverySummary(BaseModel):
    total: int  # distinct jobs in the active generation (a job seen through several sources counts once)
    matching: int  # jobs matching the current filters
    by_result: dict[str, int]  # preference results among the current filters, ignoring the result filter
    by_source: dict[str, int] = Field(default_factory=dict)  # provider -> distinct active jobs
    archived: int = 0  # rows kept from earlier generations (history and deduplication only)
    non_vacancies: int = 0  # active postings hidden because they aren't a specific opening (interest forms etc.)
    closed: int = 0  # postings their employer stopped listing (confirmed by 2 complete syncs); kept, hidden


DiscoverySort = Literal["discovered", "posted", "salary", "title", "company"]


class ReviewUpdate(BaseModel):
    """Saving goes through POST .../save so it always creates or links a saved job."""

    model_config = ConfigDict(extra="forbid")

    review_status: Literal["new", "skipped", "later"]


class DiscoveryRunRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    status: Literal["running", "succeeded", "partial", "failed"]
    sources_checked: int
    sources_failed: int
    jobs_retrieved: int
    jobs_inserted: int
    jobs_updated: int
    duplicates: int
    requests: int
    jobs_unique: int
    jobs_matched: int
    jobs_unknown: int
    companies_created: int
    boards_verified: int
    warnings: list[str]
    sources: list[dict]
    started_at: datetime
    finished_at: datetime | None
    duration_ms: int | None
    trigger: Literal["manual", "scheduled"] = "manual"
    # Progress from the task queue (filled by the service): boards and feeds queued for this run.
    tasks_total: int = 0
    tasks_done: int = 0
    tasks_failed: int = 0
    tasks_remaining: int = 0


# --- Company registry ---

DomainStr = Annotated[str, StringConstraints(strip_whitespace=True, max_length=253)]
UrlOrNone = Annotated[HttpUrl | None, Field(max_length=2048)]
ShortOpt = Annotated[OptionalStr | None, Field(max_length=300), AfterValidator(_empty_to_none)]


class CompanyCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Annotated[RequiredStr, Field(max_length=200)]
    domain: DomainStr | None = None
    website: UrlOrNone = None
    careers_url: UrlOrNone = None
    industry: ShortOpt = None
    engineering_focus: ShortOpt = None
    target_geography: ShortOpt = None
    watchlist: bool = False
    notes: Notes = None


class CompanyUpdate(BaseModel):
    """Partial update; omitted fields are unchanged. Send null to clear optional fields."""

    model_config = ConfigDict(extra="forbid")

    name: Annotated[RequiredStr, Field(max_length=200)] = None  # type: ignore[assignment]
    domain: DomainStr | None = None
    website: UrlOrNone = None
    careers_url: UrlOrNone = None
    industry: ShortOpt = None
    engineering_focus: ShortOpt = None
    target_geography: ShortOpt = None
    watchlist: bool = None  # type: ignore[assignment]
    notes: Notes = None


class CompanyRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    domain: str | None
    website: str | None
    careers_url: str | None
    industry: str | None
    engineering_focus: str | None
    target_geography: str | None
    origin: Literal["curated_seed", "manual", "discovered"]
    watchlist: bool
    verification_status: Literal["verified", "provider_reported", "unresolved"]
    notes: str | None
    first_seen_at: datetime | None
    last_seen_at: datetime | None
    created_at: datetime
    updated_at: datetime
    discovery_status: Literal[
        "not_checked", "discovering", "verified", "needs_review", "unsupported_ats", "not_found", "failed"
    ] = "not_checked"
    discovery_evidence: list[dict] = Field(default_factory=list)
    discovery_error: str | None = None
    discovery_checked_at: datetime | None = None
    website_verified_at: datetime | None = None
    unsupported_ats: str | None = None
    # Filled by the service: ATS boards and job counts.
    ats_status: Literal["verified", "unverified", "needs_review", "failed", "none"] = "none"
    boards: list[JobSourceRead] = Field(default_factory=list)
    job_count: int = 0  # distinct jobs in the active Job Discovery generation
    last_checked_at: datetime | None = None
    last_sync_at: datetime | None = None


class VerifyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: Annotated[RequiredStr, Field(max_length=2048)]


class ProviderCoverage(BaseModel):
    provider: str
    kind: Literal["aggregator", "ats_boards"]
    available: bool
    detail: str


class DiscoveryCoverage(BaseModel):
    providers: list[ProviderCoverage]
    boards_total: int
    boards_verified: int
    boards_per_run: int
    warnings: list[str]
