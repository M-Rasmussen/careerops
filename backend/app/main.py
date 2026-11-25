from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api import applications, companies, discovery, health, jobs, matching
from app.config import get_discovery_env
from app.db import SessionLocal
from app.services.worker import SyncWorker

worker = SyncWorker(SessionLocal, get_discovery_env())


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    worker.start()  # no-op when DISCOVERY_WORKER_ENABLED=false (tests)
    yield
    worker.stop()


app = FastAPI(title="CareerOps API", version="0.6.0", lifespan=lifespan)
app.include_router(health.router)
app.include_router(jobs.router)
app.include_router(applications.router)
app.include_router(matching.router)
app.include_router(discovery.router)
app.include_router(companies.router)
