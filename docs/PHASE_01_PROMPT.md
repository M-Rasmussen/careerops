# Task for Claude Code — Phase 1: Docker + FastAPI + Persistent PostgreSQL

Read `CLAUDE.md` and `docs/PROJECT_SPEC.md` first. Work on this task only. Begin by briefly stating your plan, then implement it and run the tests.

## Deliverables
1. Set up a minimal `backend/` FastAPI project using Python 3.12+ with clean API, schema, model, and DB session modules. Add pinned or constrained dependencies and a Dockerfile.
2. Create `compose.yaml` with an API and PostgreSQL 17. Store PostgreSQL data in a Docker **named volume**. Bind API to localhost for local development. Include database health checks and wait for health before API startup. Use restart policies appropriate for local development.
3. Provide `.env.example`, robust `.gitignore`, and clear startup instructions. Never put real secrets in Git. Do not create unnecessary services.
4. Configure SQLAlchemy 2 and Alembic with a first migration for the `jobs` table from `docs/PROJECT_SPEC.md`. Do not depend on runtime `Base.metadata.create_all`.
5. Implement `GET /health`, `POST /api/jobs`, `GET /api/jobs`, and `GET /api/jobs/{id}`. Validate required strings and any provided URLs, ensure JSON serialization for UUIDs/timestamps, and return appropriate HTTP errors.
6. Provide `scripts/start.sh` and `scripts/stop.sh` (`stop` must **not** delete volumes). Make scripts work when invoked from any working directory. Keep startup to one command for users.
7. Add meaningful pytest tests for validation, job creation/listing, missing IDs, and DB connectivity. Use a dedicated test database or isolated test transactions; never delete data in the normal development DB when tests run.
8. Add a concise README explaining commands, local URL, persistence, and how to verify. If practical within this narrow phase, add a simple `pg_dump` backup command and documented restore process; make sure it does not overwrite the live DB without confirmation.

## Required verification
- Run `docker compose config` and ensure no configuration errors.
- Run `docker compose up --build -d` and verify containers are healthy.
- Verify `GET /health` reports a working DB.
- Create one example job through the API, list it, then run `docker compose stop` and `docker compose up -d`; verify the same job is still present.
- Run tests and report results. If Docker cannot be started in your environment, say so and report exactly which checks were not run.

## Constraints
- No agent framework, model API calls, React app, job scraping, scheduler, incident monitor service, or cloud deployment in this phase.
- Do not delete data volumes or use `docker compose down -v`.
- Prioritize clarity and modest dependencies over a complex enterprise structure.
- Explain any deviations before making them.
- Do **not** auto-commit; show the Git status and provide a suggested commit message for user review.

## Expected handoff
Summarize changed files, exact start/stop/test commands, executed checks and results, and any known limitations. Suggested commit: `feat: initialize persistent FastAPI and PostgreSQL foundation`.
