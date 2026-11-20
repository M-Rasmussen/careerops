import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.db import get_db
from app.schemas import JobCreate, JobRead
from app.services import jobs as job_service

router = APIRouter(prefix="/api/jobs", tags=["jobs"])


@router.post("", response_model=JobRead, status_code=status.HTTP_201_CREATED)
def create_job(payload: JobCreate, session: Session = Depends(get_db)) -> JobRead:
    return JobRead.model_validate(job_service.create_job(session, payload))


@router.get("", response_model=list[JobRead])
def list_jobs(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    session: Session = Depends(get_db),
) -> list[JobRead]:
    return [JobRead.model_validate(j) for j in job_service.list_jobs(session, limit, offset)]


@router.get("/{job_id}", response_model=JobRead)
def get_job(job_id: uuid.UUID, session: Session = Depends(get_db)) -> JobRead:
    job = job_service.get_job(session, job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
    return JobRead.model_validate(job)
