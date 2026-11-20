"""SQLAlchemy persistence models. Schema changes go through Alembic migrations."""

import enum
import uuid
from datetime import date, datetime

from typing import Any

from sqlalchemy import CheckConstraint, Date, DateTime, ForeignKey, Index, Integer, String, Text, func
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
