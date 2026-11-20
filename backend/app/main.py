from fastapi import FastAPI

from app.api import applications, health, jobs

app = FastAPI(title="CareerOps API", version="0.2.0")
app.include_router(health.router)
app.include_router(jobs.router)
app.include_router(applications.router)
