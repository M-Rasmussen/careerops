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
| GET | `/api/jobs/{id}` | 404 if not found, 422 if the ID isn't a UUID. `GET /api/jobs` lists active saved jobs only. |
| POST | `/api/jobs/{id}/unsave` | Remove from Saved Jobs. Not a delete: the row, its discovery link, AI assessments, and history stay, and saving it again reuses it. Idempotent. 409 if an application is recorded for the job (applications are never touched). |

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
| GET | `/api/companies` | Company registry. Filters: `q` (name/domain), `ats_status` (`verified`, `unverified`, `failed`, `none`), `origin`, `watchlist`; `sort` (`name`, `jobs`, `last_seen`); `limit`, `offset`. There is no ranking: every employer is treated the same. Each row includes its ATS boards and discovered-job count. |
| POST | `/api/companies` | Add an employer manually (`name`, optional `domain`, `watchlist`, notes, focus, geography). 409 if the domain exists. |
| GET / PATCH | `/api/companies/{id}` | Details with boards; edit watchlist, notes, domain, focus, geography. |
| POST | `/api/companies/{id}/verify` | `{"url": ...}` Greenhouse/Lever/Ashby job or board link: the board name is parsed from the URL (never fetched) and checked with one read-only request to the provider's official API. 422 for any other URL. |
| GET / POST | `/api/discovery/sources` | Employer ATS board directory (`greenhouse`, `lever`, `lever_eu`, `ashby`). Prefer the company verify endpoint. |
| PATCH / DELETE | `/api/discovery/sources/{id}` | Enable/disable monitoring; delete keeps jobs already discovered |
| GET / PUT | `/api/discovery/filters` | Search preferences (role families, geographies, keywords, seniority, salary); PUT re-checks every discovered job |
| GET | `/api/discovery/coverage` | Which providers the next run can use and request budgets. Contacts nothing; never returns keys. |
| POST | `/api/discovery/runs` | Queue a run (every eligible employer board + enabled feeds); returns at once with `status: running` and progress (`tasks_total`, `tasks_done`, `tasks_failed`, `tasks_remaining`). Poll `GET /api/discovery/runs/{id}`. 409 if one is running or no source is available. |
| GET | `/api/discovery/runs`, `/api/discovery/runs/{id}` | Run history: per-provider requests, retrieved/unique/new/updated counts, possible duplicates, matches, errors, skipped providers, warnings |
| GET | `/api/discovery/jobs` | Compact cards (no description). Filters: `q`, `role_family`, `location`, `workplace` (`remote`, `hybrid`, `onsite`, `unknown`), `seniority`, `min_salary` (+`salary_currency`, default USD), `posted_within_days`, `filter_result`, `review_status`, `provider`, `saved_state` (`saved`, `unsaved`, `never`), `company_id`, `hide_duplicates`; `sort` (`discovered`, `posted`, `salary`, `title`, `company`); `limit`, `offset`. Missing salary, posting date, or seniority never hides a job; salaries are only compared when annual in the chosen currency. Each card includes technologies named in the description, the best posting link (`employer` or `aggregator`), the company's website only if its identity is verified, and the experience-match state read from saved AI assessments. |
| GET | `/api/discovery/jobs/summary` | Same filters: `total` (distinct jobs in the active generation), `matching` (jobs matching the filters), `by_result` (preference results), `by_source` (distinct active jobs per provider), `archived` (postings kept from earlier generations). Counts come from the whole table, not a page. |
| POST | `/api/companies/{id}/discover` | Retry career-source discovery for one company (bounded) and import jobs from boards whose employer ownership is verified. |
| GET | `/api/discovery/jobs/{id}` | Full description, review history, and requirements read conservatively from the text (requirement/preferred lists only when the posting has those headings; years-of-experience lines; technologies). Never starts an AI evaluation. |
| PATCH | `/api/discovery/jobs/{id}` | `{"review_status": "new" \| "skipped" \| "later"}`; 409 once saved |
| POST | `/api/discovery/jobs/{id}/save` | Adds the lead to Saved jobs (links an existing saved job with the same posting URL). Idempotent; never creates an application. |

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

## Job discovery

Job discovery imports postings directly from employers: **verified company website → careers page → ATS board
(Greenhouse, Lever, Lever EU, Ashby) → official job API**. There is no schedule and no AI.

**Employer career-source discovery.** Under **Details** in the Company directory, **Retry discovery** (or the CLI below)
reads the company's verified website (curated or entered by you) or a careers URL you enter. It follows careers links on
that domain (at most 5 pages, robots.txt honoured, public addresses only, 2 MB per page, redirects only within the domain)
and looks for ATS links. A board counts as the employer's only when a page on its own site links to it, or you paste it;
its official API must then answer. Boards that only appear in a job feed are marked **Needs review** and never imported.
Workday, SmartRecruiters, iCIMS, Jobvite, Taleo and similar are recorded as **Unsupported ATS**. A Workday career site
(host, tenant, site) can be recorded as an ownership-verified board, but its jobs aren't imported until a Workday
adapter exists (`ingestion_supported: false`; no run or task ever requests it).

Detection also reads, as text and never executed, up to 8 of the official site's own scripts (including component
modules), JSON-LD `JobPosting` data and serialized page data, and as a last resort up to 2 job pages from the official
sitemap. Script names mentioning a platform (e.g. `workday-job-listings.js`) mark it as unsupported. Third-party
scripts, proxies and search services are never contacted. A page naming more than 3 boards is treated as a directory,
not as ownership evidence. Discovery collects every board candidate within these limits (a minor board found early
never hides the main one), verifies up to 3 (general boards first), and checks ownership for each board separately. A
board is labelled a **prospect / talent pool** only from evidence: link text such as "join our talent pool", a provider
board name such as "… Prospects", or postings that are all interest forms; otherwise **general** once it has real
openings. **Deep scan** (Company directory, or `--deep` on the CLI for exactly one company) reads up to 10 pages, 80
scripts and 5 sitemap job pages, capped at 150 site requests; the standard scan is capped at 40.

**Ownership** also needs no contradicting identity: the employer name Greenhouse reports for a board, and any JSON-LD
hiring organization on the linking page, must not clearly name someone else. A conflict marks the board **Needs
review** and stops imports; boards you confirmed yourself are kept. Equal names never prove ownership.

**Domain moves and careers on another domain** (e.g. a site that now redirects to a new domain) are recorded as
candidates. The Company directory offers **Use … as official domain** or **Use as careers page**; nothing is trusted
without that click, and the change is recorded.

**Duplicate company records** can be merged after checking the evidence (dry run by default; companies with different
domains are refused):

```bash
docker compose exec api python -m scripts.merge_companies --source <uuid> --target <uuid> --reason "evidence"
```

**Non-vacancies.** Interest forms, talent communities and prospect pools are hidden from Job discovery (counts, pages
and filters) with the reason stored on the posting; they're never deleted, and saved jobs and applications are
unaffected. Only explicit evidence counts: a title such as "Expression of Interest", a statement that the posting isn't
a current opening, or a prospect/talent-pool source label together with a "stay in touch for future opportunities"
text. Words like talent, prospect, general or evergreen alone never exclude a job, and evergreen roles for a specific
job stay. Each sync re-checks the posting text. Re-check stored postings offline with
`docker compose exec api python -m scripts.classify_vacancies` (`--apply` to store). Companies without a
verified domain are **Needs review**: domains are never guessed from a name and no search engine is used.

```bash
# Bounded: only the companies you name; the whole registry is never scanned.
docker compose exec api python -m scripts.career_discovery --company "GitLab" --company "Stripe"
```

Work is queued in PostgreSQL (`discovery_tasks`): one active task per company or board, crashed tasks are picked up again,
transient failures retry up to 3 times with backoff, and a finished lookup isn't repeated for 7 days unless you retry.
Each job keeps its official posting URL and, for Lever and Ashby, the provider's own application URL (never built from
an ID; Greenhouse only gives a posting page).

**Generations (reset without deleting).** Job discovery shows only the active generation. To start from an empty page
while keeping every posting, decision, saved-job link and duplicate link for history and deduplication (back up first):

```bash
./scripts/backup.sh
docker compose exec api python -m scripts.archive_discovery --confirm "Label for the new generation"
```

A posting found again keeps its identity and first-seen date and gains a new appearance; a job seen through several
sources is one card and one count. A run that started before an archive can't bring archived jobs back.

**Board synchronization.** A background worker inside the API process (no extra container) works through the
PostgreSQL task queue. **Run discovery** queues every ownership-verified, import-supported board (overdue first, then
least recently synced) except those synced in the last `DISCOVERY_MIN_RESYNC_MINUTES` (shown as deferred), plus the
enabled job feeds, and returns at once; the page shows progress (queued, completed, remaining, new jobs, failures) and
refreshes counts as boards finish. The scheduler queues boards whose next sync is due every
`DISCOVERY_SYNC_INTERVAL_HOURS` (default 24; `DISCOVERY_AUTO_SYNC_ENABLED=false` turns it off, and
`DISCOVERY_WORKER_ENABLED=false` stops processing). `DISCOVERY_SYNC_CONCURRENCY` boards are fetched at once, with
requests to each provider host spaced across workers. A board is never queued twice; a crashed worker's task is picked
up again; a failed board backs off (15 min, doubling, capped at the interval; at least 1 h after HTTP 429) without
holding up the others. Workday boards are never synced. A posting its employer stops listing is closed (hidden, kept)
only after 2 complete, successful syncs without it and at least 12 h since it was last listed; failed or empty syncs
never close anything, it reopens if listed again, and saved jobs and applications are never touched. Each run:

1. Searches **Himalayas** only if `DISCOVERY_HIMALAYAS_ENABLED=true` (off by default; its listings are shown as Himalayas with its apply links), once per selected role family, up to `DISCOVERY_HIMALAYAS_MAX_PAGES` pages each.
2. Searches **JSearch** only if `JSEARCH_ENABLED=true` and `JSEARCH_API_KEY` are set (RapidAPI). Each attempt, retries included, is logged before it is sent and counted against `JSEARCH_MONTHLY_REQUEST_LIMIT` and `JSEARCH_MAX_REQUESTS_PER_RUN`; a query is not repeated within `JSEARCH_CACHE_HOURS`. Without JSearch, on-site/hybrid NJ and NYC jobs come only from monitored employer boards, and the run says so.
3. Syncs every eligible employer ATS board (Greenhouse, Lever, Lever EU, Ashby) as described above. Curated and discovered employers are treated the same.
4. Resolves each employer: a known ATS board, then an exact domain, then the provider's own company id. A display name alone never links to a curated or verified company; name-only employers are kept as separate "unresolved" records.
5. Detects ATS boards from apply links (exact hosts `boards.greenhouse.io`, `job-boards.greenhouse.io`, `jobs.lever.co`, `jobs.eu.lever.co`, `jobs.ashbyhq.com`) and verifies up to `DISCOVERY_MAX_VERIFICATIONS_PER_RUN` new ones with one official-API request each. Links are parsed, never fetched; board names are never guessed. A board that answered "not found" isn't re-checked for 30 days.
6. Upserts on provider + posting ID (re-runs update, never duplicate) and flags possible duplicates across providers by canonical URL, ATS posting ID, or requisition ID at the same company. Review decisions, first-seen times, and saved-job links are kept. A failed fetch never deletes or closes anything, and one failing source doesn't stop the others.

Filters are plain rules: role families (backend, SWE II, full-stack, platform, DevOps, production support, SDET), geographies (NJ, NYC area, US-eligible remote), excluded words, seniority, minimum annual salary. Close engineering titles and missing data give **Unknown**, never a rejection.

**Company directory.** Import the 90 curated employers (company records only, contacts nothing, safe to re-run; your edits are kept):

```bash
docker compose exec api python -m scripts.seed_companies
```

All employers are treated equally. Watch the ones you care about, and paste a Greenhouse/Lever/Ashby link under **Details** to verify a board for monitoring. Employers found by discovery appear automatically.

**Reviewing jobs.** Each card shows the title, company (links to its directory profile), location, work arrangement, employment type and seniority when the source states them, salary exactly as given, and three separate dates: when the source says it was posted (if it does; an "updated" time is never used), when CareerOps first found it, and when it was last seen (not proof it's still open). **Preference match** comes from your search preferences; **experience match** comes only from an AI assessment you run yourself on AI matching. Opening the page never calls an AI model. Filters and sort are kept in the page URL.

**Save, Pass, Later, Restore.** Save puts the job in Saved Jobs: it never records an application. Pass hides it from the review queue without deleting it. **Remove from saved** on Saved Jobs takes it back out without deleting anything: the lead returns to the review queue marked "Removed from saved", later discovery runs never re-save it, and saving it again reuses the same saved job. A job with a recorded application can't be removed from Saved Jobs.

Himalayas data is shown with attribution, and Apply uses each posting's own link. **Save** copies a lead into Saved jobs (idempotent); nothing ever records an application.

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
