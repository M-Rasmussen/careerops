"""SQLAlchemy persistence models. Schema changes go through Alembic migrations."""

import enum
import uuid
from datetime import date, datetime

from typing import Any

from sqlalchemy import Boolean, CheckConstraint, Date, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class Job(Base):
    """A saved job lead. A lead is NOT a submitted application."""

    __tablename__ = "jobs"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    company: Mapped[str] = mapped_column(String(200))
    title: Mapped[str] = mapped_column(String(300))
    location: Mapped[str | None] = mapped_column(String(200))
    posting_url: Mapped[str | None] = mapped_column(String(2048))
    description: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)
    # Set when removed from Saved Jobs ("unsaved"). The row is kept: it anchors discovery links, AI assessments,
    # and dedup history, and saving the same posting again clears it instead of creating a duplicate.
    unsaved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        Index("ix_jobs_company", "company"),
        Index("ix_jobs_created_at", "created_at"),
    )


class ApplicationStatus(enum.StrEnum):
    APPLIED = "applied"
    INTERVIEWING = "interviewing"
    OFFER = "offer"
    REJECTED = "rejected"
    WITHDRAWN = "withdrawn"


class Application(Base):
    """A confirmed submission for a saved job. A row exists only after the user confirms submitting."""

    __tablename__ = "applications"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    # One application per saved job; RESTRICT so a job with an application can't be deleted out from under it.
    job_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("jobs.id", ondelete="RESTRICT"), unique=True
    )
    status: Mapped[str] = mapped_column(String(20), default=ApplicationStatus.APPLIED.value)
    applied_date: Mapped[date] = mapped_column(Date)
    follow_up_date: Mapped[date | None] = mapped_column(Date)
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    job: Mapped[Job] = relationship(lazy="joined")

    __table_args__ = (
        CheckConstraint(
            "status IN ('applied', 'interviewing', 'offer', 'rejected', 'withdrawn')",
            name="ck_applications_status",
        ),
        CheckConstraint(
            "follow_up_date IS NULL OR follow_up_date >= applied_date",
            name="ck_applications_follow_up_after_applied",
        ),
        Index("ix_applications_status", "status"),
        Index("ix_applications_applied_date", "applied_date"),
    )


class CandidateProfile(Base):
    """The single local user's professional profile. Written only by an explicit user save."""

    __tablename__ = "candidate_profiles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    resume_text: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    # Single-user app: exactly one profile row.
    __table_args__ = (CheckConstraint("id = 1", name="ck_candidate_profiles_singleton"),)


class AgentRun(Base):
    """One agent execution (success or failure). Holds metadata only: no credentials, no resume text."""

    __tablename__ = "agent_runs"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    kind: Mapped[str] = mapped_column(String(40))
    job_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("jobs.id", ondelete="RESTRICT"))
    status: Mapped[str] = mapped_column(String(20), default="running")
    provider: Mapped[str] = mapped_column(String(50))
    model: Mapped[str] = mapped_column(String(200))
    steps: Mapped[int] = mapped_column(Integer, default=0)
    tool_calls: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)
    error_type: Mapped[str | None] = mapped_column(String(40))
    error: Mapped[str | None] = mapped_column(Text)
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint(
            "status IN ('running', 'succeeded', 'failed')", name="ck_agent_runs_status"
        ),
        Index("ix_agent_runs_started_at", "started_at"),
        Index("ix_agent_runs_job_id", "job_id"),
    )


class JobMatch(Base):
    """A validated AI match assessment for a saved job, produced by exactly one agent run."""

    __tablename__ = "job_matches"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("agent_runs.id", ondelete="RESTRICT"), unique=True
    )
    job_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("jobs.id", ondelete="RESTRICT"))
    overall_match: Mapped[str] = mapped_column(String(30))
    assessment: Mapped[dict[str, Any]] = mapped_column(JSONB)
    job_description: Mapped[str] = mapped_column(Text)
    # sha256 of everything that determines the result; equal hash => reuse instead of a new LLM call.
    input_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    run: Mapped[AgentRun] = relationship(lazy="joined")
    job: Mapped[Job] = relationship(lazy="joined")

    __table_args__ = (
        CheckConstraint(
            "overall_match IN ('strong', 'moderate', 'weak', 'insufficient_information')",
            name="ck_job_matches_overall_match",
        ),
        Index("ix_job_matches_job_id", "job_id"),
        Index("ix_job_matches_input_hash", "input_hash"),
        Index("ix_job_matches_created_at", "created_at"),
    )


# --- Job discovery (Phase 4B) ---
# Discovered postings are leads awaiting review, kept apart from saved jobs and applications.

EMPLOYMENT_TYPE_CHECK = (
    "employment_type IS NULL OR employment_type IN "
    "('Full-time', 'Part-time', 'Contract', 'Contract-to-hire', 'Temporary', 'Internship')"
)
BOARD_PROVIDER_CHECK = "provider IN ('greenhouse', 'lever', 'lever_eu', 'ashby', 'workday')"  # workday: ownership only, no import yet
WORKDAY_SITE_CHECK = (
    "provider <> 'workday' OR (ats_host ~ '^[a-z0-9-]+\\.wd[0-9]+\\.myworkdayjobs\\.com$' AND career_site ~ '^[A-Za-z0-9_-]{1,100}$')"
)
DISCOVERY_STATUSES = "('not_checked', 'discovering', 'verified', 'needs_review', 'unsupported_ats', 'not_found', 'failed')"
JOB_PROVIDER_CHECK = "provider IN ('greenhouse', 'lever', 'lever_eu', 'ashby', 'himalayas', 'jsearch')"


class Company(Base):
    """An employer in the registry. Identity comes from a verified domain, a verified ATS board, or a provider's
    own company id (aliases); a display name alone never merges two companies."""

    __tablename__ = "companies"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(200))
    domain: Mapped[str | None] = mapped_column(String(253), unique=True)
    website: Mapped[str | None] = mapped_column(String(2048))
    careers_url: Mapped[str | None] = mapped_column(String(2048))
    industry: Mapped[str | None] = mapped_column(String(200))
    engineering_focus: Mapped[str | None] = mapped_column(String(300))
    target_geography: Mapped[str | None] = mapped_column(String(200))
    origin: Mapped[str] = mapped_column(String(20))
    watchlist: Mapped[bool] = mapped_column(Boolean, default=False)
    # verified: curated/manual domain or verified ATS board; provider_reported: an aggregator's company id or
    # website; unresolved: name only.
    verification_status: Mapped[str] = mapped_column(String(20))
    notes: Mapped[str | None] = mapped_column(Text)
    first_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Career-source discovery (official website -> careers page -> ATS board). Evidence is append-only:
    # [{"step": ..., "url": ..., "detail": ..., "at": ISO}].
    discovery_status: Mapped[str] = mapped_column(String(20), default="not_checked")
    discovery_evidence: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)
    discovery_error: Mapped[str | None] = mapped_column(Text)
    discovery_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    website_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    unsupported_ats: Mapped[str | None] = mapped_column(String(40))  # e.g. "workday", kept for future support
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        CheckConstraint("origin IN ('curated_seed', 'manual', 'discovered')", name="ck_companies_origin"),
        CheckConstraint(f"discovery_status IN {DISCOVERY_STATUSES}", name="ck_companies_discovery_status"),
        CheckConstraint(
            "verification_status IN ('verified', 'provider_reported', 'unresolved')",
            name="ck_companies_verification_status",
        ),
        Index("ix_companies_name", "name"),
    )


class CompanyAlias(Base):
    """A provider-specific identity key for a company, e.g. ("himalayas", company slug) or ("name", normalized
    name) for unresolved name-only employers."""

    __tablename__ = "company_aliases"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(30))
    value: Mapped[str] = mapped_column(String(300))

    __table_args__ = (
        UniqueConstraint("kind", "value", name="uq_company_aliases_kind_value"),
        Index("ix_company_aliases_company_id", "company_id"),
    )


class ProviderRequest(Base):
    """Every metered provider request attempt (logged before it is sent), for budgets and query caching."""

    __tablename__ = "provider_requests"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    provider: Mapped[str] = mapped_column(String(20))
    query_key: Mapped[str] = mapped_column(String(64))
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (Index("ix_provider_requests_provider_requested_at", "provider", "requested_at"),)


class JobSource(Base):
    """A public employer ATS board (the monitoring directory). Verified = its official API answered."""

    __tablename__ = "job_sources"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    provider: Mapped[str] = mapped_column(String(20))
    company_name: Mapped[str] = mapped_column(String(200))
    board_token: Mapped[str] = mapped_column(String(100))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    company_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("companies.id", ondelete="RESTRICT"))
    verification_status: Mapped[str] = mapped_column(String(20), default="unverified")
    verification_evidence: Mapped[str | None] = mapped_column(String(2048))
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    last_error_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Set only when the employer's official site links to this board (or the user confirmed it). Only such
    # boards feed the active Job Discovery generation.
    ownership_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ownership_evidence: Mapped[str | None] = mapped_column(String(2048))
    provider_name: Mapped[str | None] = mapped_column(String(200))  # employer name the ATS reports for the board
    board_role: Mapped[str | None] = mapped_column(String(20))  # general | talent_pool, only from evidence
    # Sync scheduling: overdue boards first; failures back off (sync_failures) without blocking other boards.
    next_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sync_failures: Mapped[int] = mapped_column(Integer, default=0)
    last_sync_count: Mapped[int | None] = mapped_column(Integer)  # postings returned by the last successful sync
    # Workday only: a career site is host + tenant (board_token) + site.
    ats_host: Mapped[str | None] = mapped_column(String(253))
    career_site: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        CheckConstraint(BOARD_PROVIDER_CHECK, name="ck_job_sources_provider"),
        CheckConstraint(WORKDAY_SITE_CHECK, name="ck_job_sources_workday_site"),
        CheckConstraint("board_role IS NULL OR board_role IN ('general', 'talent_pool')", name="ck_job_sources_board_role"),
        CheckConstraint(
            "verification_status IN ('unverified', 'verified', 'needs_review', 'failed')",
            name="ck_job_sources_verification_status",
        ),
        UniqueConstraint("provider", "board_token", name="uq_job_sources_provider_board"),
        Index("ix_job_sources_company_id", "company_id"),
        Index("ix_job_sources_next_sync_at", "next_sync_at"),
    )

    company: Mapped["Company | None"] = relationship(lazy="joined")


class DiscoveredJob(Base):
    """A posting retrieved from a job source. Identity is (provider, source_posting_id)."""

    __tablename__ = "discovered_jobs"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    # SET NULL: removing a source keeps the leads already found.
    source_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("job_sources.id", ondelete="SET NULL"))
    provider: Mapped[str] = mapped_column(String(20))
    source_posting_id: Mapped[str] = mapped_column(String(500))
    company: Mapped[str] = mapped_column(String(200))
    company_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("companies.id", ondelete="RESTRICT"))
    # "<ats>:<posting id>" when the posting is (or links to) a known ATS posting; same key = same job.
    ats_job_key: Mapped[str | None] = mapped_column(String(600))
    requisition_id: Mapped[str | None] = mapped_column(String(100))
    seniority: Mapped[str | None] = mapped_column(String(20))  # only when the provider states it
    employment_type: Mapped[str | None] = mapped_column(String(40))  # only when the provider states it
    # The source's original publish date. Never filled from an "updated" timestamp (that's source_updated_at).
    posted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    title: Mapped[str] = mapped_column(String(300))
    location: Mapped[str | None] = mapped_column(String(200))
    workplace_type: Mapped[str | None] = mapped_column(String(10))
    description: Mapped[str | None] = mapped_column(Text)
    posting_url: Mapped[str | None] = mapped_column(String(2048))  # official posting page from the source
    application_url: Mapped[str | None] = mapped_column(String(2048))  # provider-supplied apply form; never built
    canonical_url: Mapped[str | None] = mapped_column(String(2048))
    salary_min: Mapped[int | None] = mapped_column(Integer)
    salary_max: Mapped[int | None] = mapped_column(Integer)
    salary_currency: Mapped[str | None] = mapped_column(String(3))
    salary_interval: Mapped[str | None] = mapped_column(String(30))
    source_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    filter_result: Mapped[str] = mapped_column(String(10))
    filter_reasons: Mapped[list[str]] = mapped_column(JSONB, default=list)
    review_status: Mapped[str] = mapped_column(String(10), default="new")
    unsaved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Append-only user decisions: [{"action": "saved" | "unsaved" | "skipped" | "later" | "new", "at": ISO}].
    review_history: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)
    # Kept after "unsave" so saving again reuses the same saved job (and its AI assessments).
    saved_job_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("jobs.id", ondelete="RESTRICT"))
    # Earliest other posting with the same canonical URL: a possible duplicate, shown but never merged.
    duplicate_of_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("discovered_jobs.id", ondelete="SET NULL")
    )
    # Consecutive complete, successful syncs of its board that didn't list it; closed (hidden) after 2. Cleared if
    # the posting reappears. Never set from a failed or empty sync. Nothing is deleted.
    missing_count: Mapped[int] = mapped_column(Integer, default=0)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Set when the posting explicitly isn't a specific opening (interest form, talent pool); hidden from Discovery.
    non_vacancy_reason: Mapped[str | None] = mapped_column(Text)
    # Latest discovery generation this posting was retrieved in; Job Discovery shows only the active one.
    generation: Mapped[int | None] = mapped_column(ForeignKey("discovery_generations.id", ondelete="RESTRICT"))
    # Append-only: one entry per generation it appeared in ({"generation", "via", "provider", "source_id", ...}).
    appearances: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        CheckConstraint(JOB_PROVIDER_CHECK, name="ck_discovered_jobs_provider"),
        CheckConstraint("filter_result IN ('match', 'unknown', 'rejected')", name="ck_discovered_jobs_filter_result"),
        CheckConstraint(
            "review_status IN ('new', 'saved', 'skipped', 'later')", name="ck_discovered_jobs_review_status"
        ),
        CheckConstraint(
            "workplace_type IS NULL OR workplace_type IN ('remote', 'hybrid', 'onsite')",
            name="ck_discovered_jobs_workplace_type",
        ),
        CheckConstraint(EMPLOYMENT_TYPE_CHECK, name="ck_discovered_jobs_employment_type"),
        CheckConstraint(
            "review_status <> 'saved' OR saved_job_id IS NOT NULL", name="ck_discovered_jobs_saved_has_job"
        ),
        UniqueConstraint("provider", "source_posting_id", name="uq_discovered_jobs_provider_posting"),
        Index("ix_discovered_jobs_canonical_url", "canonical_url"),
        Index("ix_discovered_jobs_first_seen_at", "first_seen_at"),
        Index("ix_discovered_jobs_review_filter", "review_status", "filter_result"),
        Index("ix_discovered_jobs_company_id", "company_id"),
        Index("ix_discovered_jobs_ats_job_key", "ats_job_key"),
        Index("ix_discovered_jobs_posted_at", "posted_at"),
        Index("ix_discovered_jobs_generation", "generation"),
    )

    company_ref: Mapped["Company | None"] = relationship(lazy="joined")


class DiscoveryGeneration(Base):
    """A Job Discovery population. The highest id is active; archiving inserts a new one (nothing is deleted)."""

    __tablename__ = "discovery_generations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    label: Mapped[str] = mapped_column(String(200))
    archived_jobs: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class DiscoveryTask(Base):
    """Durable work queue: "discover" (company career source) or "sync" (import a verified board's jobs)."""

    __tablename__ = "discovery_tasks"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    kind: Mapped[str] = mapped_column(String(20))
    company_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"))
    source_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("job_sources.id", ondelete="CASCADE"))
    status: Mapped[str] = mapped_column(String(10), default="queued")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    run_after: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    run_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("discovery_runs.id", ondelete="SET NULL"))
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB)  # e.g. a run's feed plan
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        CheckConstraint("kind IN ('discover', 'sync', 'feeds')", name="ck_discovery_tasks_kind"),
        CheckConstraint("status IN ('queued', 'running', 'done', 'failed')", name="ck_discovery_tasks_status"),
        CheckConstraint(
            "(kind = 'discover' AND company_id IS NOT NULL) OR (kind = 'sync' AND source_id IS NOT NULL) OR "
            "(kind = 'feeds' AND run_id IS NOT NULL)",
            name="ck_discovery_tasks_target",
        ),
        Index("uq_discovery_tasks_active_company", "kind", "company_id", unique=True,
              postgresql_where=text("status IN ('queued', 'running') AND company_id IS NOT NULL AND kind = 'discover'")),
        Index("uq_discovery_tasks_active_source", "kind", "source_id", unique=True,
              postgresql_where=text("status IN ('queued', 'running') AND kind = 'sync'")),
        Index("ix_discovery_tasks_status_run_after", "status", "run_after"),
        Index("ix_discovery_tasks_run_id", "run_id"),
    )


class DiscoveryRun(Base):
    """One manually started discovery execution; per-source outcomes live in `sources` (JSONB)."""

    __tablename__ = "discovery_runs"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    status: Mapped[str] = mapped_column(String(20), default="running")
    trigger: Mapped[str] = mapped_column(String(10), default="manual")  # manual | scheduled
    sources_checked: Mapped[int] = mapped_column(Integer, default=0)
    sources_failed: Mapped[int] = mapped_column(Integer, default=0)
    jobs_retrieved: Mapped[int] = mapped_column(Integer, default=0)
    jobs_inserted: Mapped[int] = mapped_column(Integer, default=0)
    jobs_updated: Mapped[int] = mapped_column(Integer, default=0)
    duplicates: Mapped[int] = mapped_column(Integer, default=0)
    requests: Mapped[int] = mapped_column(Integer, default=0)
    jobs_unique: Mapped[int] = mapped_column(Integer, default=0)
    jobs_matched: Mapped[int] = mapped_column(Integer, default=0)
    jobs_unknown: Mapped[int] = mapped_column(Integer, default=0)
    companies_created: Mapped[int] = mapped_column(Integer, default=0)
    boards_verified: Mapped[int] = mapped_column(Integer, default=0)
    warnings: Mapped[list[str]] = mapped_column(JSONB, default=list)
    # [{source_id, provider, company, board_token, status, retrieved, inserted, updated, duplicates, duration_ms, error}]
    sources: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_ms: Mapped[int | None] = mapped_column(Integer)

    __table_args__ = (
        CheckConstraint(
            "status IN ('running', 'succeeded', 'partial', 'failed')", name="ck_discovery_runs_status"
        ),
        CheckConstraint("trigger IN ('manual', 'scheduled')", name="ck_discovery_runs_trigger"),
        Index("ix_discovery_runs_started_at", "started_at"),
    )


class DiscoverySettings(Base):
    """Single-row filter preferences (validated by schemas.DiscoveryFilters before storing)."""

    __tablename__ = "discovery_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    filters: Mapped[dict[str, Any]] = mapped_column(JSONB)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (CheckConstraint("id = 1", name="ck_discovery_settings_singleton"),)
