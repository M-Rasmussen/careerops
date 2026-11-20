# CareerOps — Product and architecture specification

Status: initial agreed blueprint (2026-11-20). Features below are planned unless otherwise indicated.

## Product goal
An actual private job-search workspace and an independently accessible portfolio demo. Users track jobs and confirmed applications, export/import Excel workbooks, eventually discover and match open positions using AI, and inspect operational failures through an existing Incident Monitoring System.

## Deployment modes
1. **Local/full:** Docker Compose; React, FastAPI, PostgreSQL named volume; optional background worker, model provider, and incident monitor later. Must survive computer shutdown and container recreation and be easy to start.
2. **Portfolio/demo:** Static React build on a free hosting tier. Uses synthetic fixture data and recorded, clearly labeled agent tool traces. Never calls paid AI services, reads actual resumes or applications, or exposes private credentials. Visitors can interact with local demo state and reset it. It reuses the full application's React components.

## Primary user experience
- View, search, and filter saved jobs.
- Create a job lead with company, title, location, URL, description, and notes.
- Record an application ONLY after actual submission is confirmed, with date, state, and follow-up information.
- Change statuses with an auditable history.
- Export/import Excel with safe, validated mapping.
- Later: identify relevant job openings from permitted ATS feeds, score matches against the user's profile and resume, and persist recommendations without confusing a lead with a submitted application.
- Later: agent steps and tool calls have run IDs, checkpoints, status, usage, and observable errors.
- Later: query the standalone incident-monitor API; send structured operational events without transmitting secrets or sensitive resumes.

## Nonfunctional requirements
- **Durability:** Docker named volume plus explicit scheduled/one-click backups, verified restore procedures. `docker compose stop` and `docker compose down` must not erase data; `down -v` is destructive and excluded from all scripts.
- **Restart:** startup from one command or script; dependencies have health checks; migrations run deterministically. Durable future jobs recover using checkpoints and idempotent external actions.
- **Cost:** no required paid services to run locally; no model hosting for the public demo. Meter and limit paid-model calls when agentic workflows are introduced.
- **Security:** .env secrets excluded from Git; local defaults bind to localhost; cloud deployment and real user accounts require authentication. Only synthetic sample data in public code and demo assets.
- **Maintainability:** modular monolith (FastAPI) for core product; keep incident-monitor separate; don't introduce a microservice for every component.
- **Testing:** automated unit and integration tests for validation, CRUD, migrations, persistence, and later agent approval policies.

## Planned stack
- Backend: Python 3.12+, FastAPI, Pydantic, SQLAlchemy 2, Alembic, PostgreSQL 17.
- Frontend: React + TypeScript + Vite.
- Spreadsheet: openpyxl (later).
- Containers: Docker Compose, persistent named volumes; Bash scripts for local dev.
- AI: provider adapter with cloud model or local Colibri/Ollama-compatible endpoint; custom tool orchestration, trace capture, and explicit approval gates (later).
- Monitoring: separate existing Node.js incident manager, integrated through a versioned REST adapter (later).
- CI: GitHub Actions (later).

## Initial data model
**jobs** (saved leads, not submissions):
- `id` UUID primary key; `company` required; `title` required; `location` nullable; `posting_url` nullable; `description` nullable; `notes` nullable; `created_at` and `updated_at` timestamp with time zone.
- Start with ordinary indexes on company and creation date. Add deduplication rules after actual job-source ingestion; do not assume URLs alone are reliable identifiers.

**applications** (actual submissions only):
- `id` UUID primary key; `job_id` UUID FK to jobs; `status` (e.g. `applied`, `interviewing`, `offer`, `rejected`, `withdrawn`); `applied_at` required; `notes` optional; `next_follow_up_at` optional; created/updated timestamps.
- Existence of a row means an actual submission has been confirmed. Enforce this in the write API and document it. Later add explicit submission evidence and audit/status history.

**Later tables**: `application_status_events`, `agent_runs`, `agent_steps`, `job_matches`, `profiles`, `integration_events`, `job_source_runs`.

## API milestone 1
- `GET /health` confirms app and database connectivity.
- `POST /api/jobs` validates and stores a job lead.
- `GET /api/jobs` returns saved leads; order newest first.
- `GET /api/jobs/{id}` returns a lead or 404.
- Optional in Phase 1: `POST /api/applications` ONLY with explicit confirmed-submission input.
- Phase 1 does not need a frontend.

## Roadmap and boundaries
- **Phase 1 (now):** Docker/FastAPI/PostgreSQL, migrations, minimal jobs API, tests, start/stop commands, persistence check.
- **Phase 2:** applications, status history, Excel export/import, React app; local-first usability.
- **Phase 3:** separate static/demo data adapter and free hosted portfolio demo; synthetic content only.
- **Phase 4:** job discovery integrations (permitted sources), LLM provider abstraction, tool schemas, matching, agent execution logs, spend limits.
- **Phase 5:** scheduled jobs, checkpoints, safe retries, incident-monitor integration, observability.
- **Phase 6:** human-approved application assistant; authorized submission workflows only where permitted.

## Important architecture decisions
- PostgreSQL is the source of truth, Excel is an import/export interface (not the primary database).
- A discovered job is a **lead**, not an application. Do not mark `applied` from a model suggestion.
- Static recruiter demo and local app share React UI but use different adapters to get data.
- Core app remains functional when AI or incident-monitoring services are down.
- Cloud hosting of full backend is optional; free static demo is the preferred resume URL.
- Keep the initial repository lean so Claude Code + Ponytail/Graphify do not incur avoidable context/token overhead.

## Evidence of production quality for portfolio
- Working local one-command startup and restart demonstration.
- A passing test suite and migrations.
- Verified persistence and database restore.
- README with demo URL, sanitized screenshots, architecture, and how to run locally.
- Later: agent traces, error-handling demonstrations, and incident-monitor dashboards with synthetic examples.
