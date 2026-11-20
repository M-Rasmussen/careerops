"""SQLAlchemy persistence models. Schema changes go through Alembic migrations."""

import enum
import uuid
from datetime import date, datetime

from sqlalchemy import CheckConstraint, Date, DateTime, ForeignKey, Index, String, Text, func
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
