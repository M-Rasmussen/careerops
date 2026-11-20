from fastapi import FastAPI

from app.api import applications, health, jobs, matching

app = FastAPI(title="CareerOps API", version="0.4.0")
app.include_router(health.router)
app.include_router(jobs.router)
app.include_router(applications.router)
app.include_router(matching.router)
