# CareerOps

Local-first job-search tracking and future agentic automation, with a free static portfolio demo.

**Status:** Phase 2 (backend). A Dockerized FastAPI and PostgreSQL 17 backend with saved jobs, confirmed applications, and Excel export. No frontend or AI features yet. See `docs/PROJECT_SPEC.md` for the roadmap.

## Requirements

- WSL (or Linux/macOS) with Bash
- Docker Desktop with WSL integration (Docker Compose v2)

No local Python is required: the API, migrations, and tests all run in containers.

## Quick start

```bash
cp .env.example .env        # then edit POSTGRES_PASSWORD
./scripts/start.sh          # builds, starts, waits until healthy
```

`scripts/start.sh` wraps `docker compose up -d --build --wait`, and that command works on its own too. You can run the scripts from any directory.

- Dashboard: http://127.0.0.1:5173 (React; change `WEB_PORT` in `.env`). It proxies `/api` to the API service.
- API: http://127.0.0.1:8000 (bound to localhost only; change `API_PORT` in `.env` if 8000 is taken)
- Interactive docs: http://127.0.0.1:8000/docs
- Health: http://127.0.0.1:8000/health → `{"status":"ok","database":"ok"}` (503 if the DB is unreachable)

On startup the API container runs `alembic upgrade head` before serving, so migrations are applied deterministically.

## Commands

| Task | Command |
|---|---|
| Start / rebuild | `./scripts/start.sh` |
| Stop (keeps data) | `./scripts/stop.sh` (runs `docker compose stop`) |
| Run tests | `./scripts/test.sh` (extra args go to pytest, e.g. `-v`) |
| Frontend tests | `cd frontend && npm ci && npm test` |
| Frontend dev server (hot reload) | `cd frontend && npm run dev` (proxies to `API_PROXY_TARGET`, default `http://127.0.0.1:8001`) |
| Demo build (static, no backend) | `cd frontend && npm run build:demo` → `frontend/dist-demo/` |
| Logs | `docker compose logs -f api` |
| DB shell | `docker compose exec db psql -U careerops -d careerops` |
| Back up | `./scripts/backup.sh` |
| Restore | `./scripts/restore.sh backups/<file>.dump [target_db]` |

## API

| Method | Path | Notes |
|---|---|---|
| GET | `/health` | App and DB connectivity |
| POST | `/api/jobs` | Create a job **lead**. `company`, `title` required; `posting_url` must be http(s). Returns 201. |
| GET | `/api/jobs` | Newest first; `limit` (1–200, default 50), `offset` |
| GET | `/api/jobs/{id}` | 404 if not found, 422 if the ID isn't a UUID |

| POST | `/api/applications` | Record a **confirmed** submission. Requires `job_id`, `applied_date`, and `"submission_confirmed": true`. Optional `status` (default `applied`), `follow_up_date`, `notes`. 201; 404 if the job doesn't exist; 409 if that job already has an application. |
| GET | `/api/applications` | Newest `applied_date` first. Filters: `status`, `company` (case-insensitive substring), `applied_from`, `applied_to` (inclusive `YYYY-MM-DD`). `limit` (1–200, default 50), `offset`. Each item includes its `job`. |
| GET | `/api/applications/{id}` | 404 if not found |
| PATCH | `/api/applications/{id}` | Partial update of `status`, `applied_date`, `follow_up_date`, `notes`. `job_id` can't change. Send `null` to clear `follow_up_date`/`notes`. |
| DELETE | `/api/applications/{id}` | 204. The saved job is kept. |
| GET | `/api/applications/export.xlsx` | Excel download of all applications matching the same filters (no pagination). Columns: Company, Job Title, Application Date, Status, Posting URL, Follow-up Date, Notes. |
| GET | `/api/ai/status` | Whether an AI provider is configured (provider/model names only; never the key or URL) |
| GET / PUT | `/api/profile` | Your resume/profile text (`null` until saved). `PUT {"resume_text": ...}` is the only way it changes. |
| POST | `/api/matches` | `{"job_id", "job_description", "force": false}` runs the AI match agent. 201 new; 200 when an assessment for identical inputs is reused (no AI call); 503 no provider; 409 no profile; 429 daily limit; 502/504 agent or provider failure (the run is still recorded). |
| GET | `/api/matches`, `/api/matches/{id}` | Previous assessments, newest first (optional `job_id`, `limit`, `offset`), each with its agent run trace |

A saved job is a lead, **not** a submitted application. Saving a job never creates an application. An application row exists only when you explicitly confirm the submission (`submission_confirmed: true`).

Statuses: `applied`, `interviewing`, `offer`, `rejected`, `withdrawn`. Rules: `applied_date` can't be in the future (one day of slack for time zones), and `follow_up_date` can't be before `applied_date`. Both rules are also enforced by database CHECK constraints. Errors use FastAPI's standard `{"detail": ...}` body: 404 not found, 409 conflict, 422 validation.

Timestamps are UTC ISO-8601; `applied_date`/`follow_up_date` are calendar dates (`YYYY-MM-DD`). Full interactive docs are at `/docs`.

```bash
curl -X POST http://127.0.0.1:8000/api/jobs -H 'Content-Type: application/json' \
  -d '{"company":"Example Corp","title":"Backend Engineer","posting_url":"https://example.com/jobs/1"}'
curl http://127.0.0.1:8000/api/jobs
curl -X POST http://127.0.0.1:8000/api/applications -H 'Content-Type: application/json' \
  -d '{"job_id":"<job id>","submission_confirmed":true,"applied_date":"2026-11-20"}'
curl -o applications.xlsx 'http://127.0.0.1:8000/api/applications/export.xlsx?status=interviewing'
```

Exported text cells are always written as plain text, so a value like `=1+1` in notes is never run as a spreadsheet formula.

## AI job matching (optional)

Disabled by default; the tracker works without it. To enable, set in `.env` and run `./scripts/start.sh`:

```bash
AI_PROVIDER=openai                       # any OpenAI-compatible /chat/completions API
AI_MODEL=<model name>
AI_BASE_URL=https://api.openai.com/v1    # or http://ollama:11434/v1 for the bundled local Ollama (free, no key)
AI_API_KEY=<key, if the provider needs one>
```

**Local model (no paid calls):** `docker compose --profile local-ai up -d ollama`, then `docker compose exec ollama ollama pull qwen3:4b-instruct-2507-q4_K_M` and set `AI_MODEL` to that tag. Models persist in the `careerops_ollama` volume; Ollama isn't published to the host and doesn't start without the profile. CPU inference is slow, so set `AI_TIMEOUT_SECONDS=300`. Smoke test with fictional data in an isolated `careerops_smoke` database: `docker compose exec api python -m scripts.smoke_ai`.

**Optional NVIDIA GPU:** add the override when starting Ollama: `docker compose -f compose.yaml -f compose.gpu.yaml --profile local-ai up -d ollama`. It reuses the same model volume; without it Ollama stays on CPU. Check with `docker compose exec ollama ollama ps` (`PROCESSOR` should read `100% GPU`). On an RTX 3060 12 GB, the 4B model's warm smoke test takes ~14 s (CPU: ~170 s) and uses ~4 GB VRAM; the first request after startup adds ~40 s to load the model.

Limits (also in `.env`): `AI_MAX_OUTPUT_TOKENS` (2000; sent as `AI_TOKEN_PARAM`, either `max_tokens` (default) or `max_completion_tokens` for newer OpenAI models), `AI_MAX_STEPS` (6 model calls per analysis), `AI_TIMEOUT_SECONDS` (60 per call), `AI_MAX_RUNS_PER_DAY` (20). Paid providers bill per token. Analysis runs only when you click **Analyze match**, and identical inputs reuse the earlier assessment.

The agent is a small explicit loop (`backend/app/agent/`) with three allowlisted tools: `get_saved_job` (only the job being analyzed), `get_candidate_profile` (read-only), and `save_match_assessment` (Pydantic-validated; refused unless both inputs were read and every evidence quote appears verbatim in your resume). Each run is stored in `agent_runs` with status, tool calls, errors, and token usage. Resume text isn't logged there.

## Persistence

PostgreSQL data lives in the Docker **named volume** `careerops_pgdata`, not in the container filesystem. It survives:

- `docker compose stop` / `./scripts/stop.sh`
- `docker compose down` (containers removed, volume kept)
- image rebuilds and computer reboots

Containers use `restart: unless-stopped`, so after a reboot they come back once Docker Desktop starts, unless you stopped them.

> ⚠️ **Never run `docker compose down -v` or `docker volume prune`.** They delete the database volume. No script in this repo does either.

### Verify persistence yourself

```bash
./scripts/start.sh
curl -s -X POST http://127.0.0.1:8000/api/jobs -H 'Content-Type: application/json' \
  -d '{"company":"Persistence Check","title":"Test"}'
docker compose stop && docker compose up -d --wait
curl -s http://127.0.0.1:8000/api/jobs     # the job is still listed
```

## Backup and restore

Persistence is not a backup: a deleted volume or a corrupted database needs a separate copy.

- `./scripts/backup.sh` writes `backups/careerops_<UTC timestamp>.dump` (`pg_dump` custom format). `backups/` is git-ignored. Copy dumps somewhere off the machine for real protection.
- `./scripts/restore.sh <dump> <scratch_db>` restores into a **separate** database (created if missing). Use this to check that a backup is good without touching live data.
- `./scripts/restore.sh <dump>` restores into the **live** database. You must type the database name to confirm. The script stops the API during the restore and starts it again afterwards.

## Tests

`./scripts/test.sh` runs pytest inside the running API container against a dedicated database, `careerops_test` (`TEST_POSTGRES_DB`). The suite creates that database if it is missing and migrates it with Alembic. Each test runs in a transaction that is rolled back. The suite refuses to run if the test DB name equals the dev DB name, so it never modifies development data.

Coverage: health (OK and DB-down 503); jobs (creation, validation, get/404, listing, pagination, persistence); applications (confirmed-submission requirement, status and date validation, 404/409, partial updates and null handling, delete keeping the job, status/company/date filters, pagination bounds, DB CHECK/FK constraints); Excel export (columns, values, filters, empty export, formula-injection safety); and a check that the migrations match the SQLAlchemy models.

## Demo mode

`npm run build:demo` builds the same React app with `VITE_DEMO_MODE=true`. In that build, a browser-only adapter (`frontend/src/demo/`) replaces the API client. The demo uses fictional seed data, saves changes to localStorage, labels every page as a demo, and has a Reset demo button. It makes no API calls (the live client is left out of the bundle). Its Excel download is a pre-generated sample workbook (`frontend/public/demo/sample-applications.xlsx`) produced by the backend exporter from the seed. `frontend/scripts/demo_workbook.py` regenerates it. The output is a static site for Cloudflare Pages or similar hosting. Deployment is not set up yet.

## Layout

```
compose.yaml            # db (postgres:17 + named volume), api, web
.env.example            # copy to .env; .env is git-ignored
scripts/                # start, stop, test, backup, restore
backend/
  Dockerfile, entrypoint.sh, requirements*.txt
  alembic.ini, alembic/versions/   # migrations (no create_all at runtime)
  app/
    main.py             # FastAPI app and routers
    config.py           # env-based settings
    db.py               # engine, session, Base
    models.py           # SQLAlchemy models
    schemas.py          # Pydantic schemas
    services/           # jobs, applications, excel export logic
    api/                # health, jobs, applications routes
  tests/
frontend/
  Dockerfile            # build, then `vite preview` with /api proxy
  src/types.ts          # API types + DataAdapter interface
  src/api/              # live adapter (fetch), paging helper, context
  src/demo/             # fictional seed + localStorage demo adapter
  src/pages/            # Dashboard, Saved jobs, Applications
  src/test/             # Vitest + Testing Library
```

## Design rules

- PostgreSQL data lives in a Docker named volume and is backed up separately.
- The full app runs locally; the public demo will be a static React build with synthetic data.
- A saved job is not a submitted application.
- Agent actions requiring external writes will have human approval gates.
- The incident-monitor integration will reuse an existing separate service.
