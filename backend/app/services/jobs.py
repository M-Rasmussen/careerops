"""Job lead persistence logic."""

import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Application, DiscoveredJob, Job
from app.schemas import JobCreate
from app.services.discovery import log_review


class JobNotFoundError(Exception):
    pass


class HasApplicationError(Exception):
    pass


def create_job(session: Session, data: JobCreate) -> Job:
    values = data.model_dump()
    if data.posting_url is not None:
        values["posting_url"] = str(data.posting_url)
    job = Job(**values)
    session.add(job)
    session.commit()
    session.refresh(job)
    return job


def get_job(session: Session, job_id: uuid.UUID) -> Job | None:
    return session.get(Job, job_id)


def list_jobs(session: Session, limit: int, offset: int) -> list[Job]:
    """Active saved jobs only; unsaved ones are kept in the database but hidden here."""
    stmt = (
        select(Job)
        .where(Job.unsaved_at.is_(None))
        .order_by(Job.created_at.desc(), Job.id.desc())
        .limit(limit)
        .offset(offset)
    )
    return list(session.scalars(stmt))


def unsave_job(session: Session, job_id: uuid.UUID) -> Job:
    """Removes a job from Saved Jobs without deleting anything. Idempotent. Discovery leads saved as this job go
    back to the review queue (not "passed") and keep their link, so saving again reuses this row. Refused when an
    application is recorded: unsaving must never touch or imply withdrawing an application."""
    job = session.scalar(select(Job).where(Job.id == job_id).with_for_update())
    if job is None:
        raise JobNotFoundError
    if session.scalar(select(Application.id).where(Application.job_id == job_id)) is not None:
        raise HasApplicationError
    if job.unsaved_at is None:
        now = datetime.now(timezone.utc)
        job.unsaved_at = now
        for lead in session.scalars(
            select(DiscoveredJob).where(DiscoveredJob.saved_job_id == job_id, DiscoveredJob.review_status == "saved")
        ):
            lead.review_status = "new"
            lead.unsaved_at = now
            log_review(lead, "unsaved")
    session.commit()
    session.refresh(job)
    return job
