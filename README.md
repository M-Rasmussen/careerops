# CareerOps

Local-first job-search tracking and future agentic automation, with a free static portfolio demo.

**Status:** Phase 1. A Dockerized FastAPI and PostgreSQL 17 backend with a minimal jobs API. No frontend or AI features yet. See `docs/PROJECT_SPEC.md` for the roadmap.

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
| Logs | `docker compose logs -f api` |
| DB shell | `docker compose exec db psql -U careerops -d careerops` |
| Back up | `./scripts/backup.sh` |
| Restore | `./scripts/restore.sh backups/<file>.dump [target_db]` |

## API (Phase 1)

| Method | Path | Notes |
|---|---|---|
| GET | `/health` | App and DB connectivity |
| POST | `/api/jobs` | Create a job **lead**. `company`, `title` required; `posting_url` must be http(s). Returns 201. |
| GET | `/api/jobs` | Newest first; `limit` (1–200, default 50), `offset` |
| GET | `/api/jobs/{id}` | 404 if not found, 422 if the ID isn't a UUID |

A saved job is a lead, **not** a submitted application. Timestamps are UTC ISO-8601.

```bash
curl -X POST http://127.0.0.1:8000/api/jobs -H 'Content-Type: application/json' \
  -d '{"company":"Example Corp","title":"Backend Engineer","posting_url":"https://example.com/jobs/1"}'
curl http://127.0.0.1:8000/api/jobs
```

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

Coverage: health (OK and DB-down 503), job creation, validation failures (missing, blank, or too-long fields, bad URLs, unknown fields), get by ID, 404 and malformed IDs, newest-first listing, pagination bounds, DB persistence, and a check that the migrations match the SQLAlchemy models.

## Layout

```
compose.yaml            # db (postgres:17 + named volume) and api
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
    services/jobs.py    # persistence logic
    api/                # health and jobs routes
  tests/
```

## Design rules

- PostgreSQL data lives in a Docker named volume and is backed up separately.
- The full app runs locally; the public demo will be a static React build with synthetic data.
- A saved job is not a submitted application.
- Agent actions requiring external writes will have human approval gates.
- The incident-monitor integration will reuse an existing separate service.
