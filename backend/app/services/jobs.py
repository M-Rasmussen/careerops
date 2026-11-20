"""Job lead persistence logic."""

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Job
from app.schemas import JobCreate


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
    stmt = (
        select(Job)
        .order_by(Job.created_at.desc(), Job.id.desc())
        .limit(limit)
        .offset(offset)
    )
    return list(session.scalars(stmt))
