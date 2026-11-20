from fastapi import FastAPI

from app.api import health, jobs

app = FastAPI(title="CareerOps API", version="0.1.0")
app.include_router(health.router)
app.include_router(jobs.router)
