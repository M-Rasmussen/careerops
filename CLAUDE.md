# CareerOps — Claude Code project instructions

## Purpose
CareerOps is a local-first, Dockerized job-application tracker that will progressively add AI-assisted job discovery, matching, and operational monitoring. The full app is private and runs locally; a safe, static React demo using synthetic data will be deployed publicly for portfolio viewers.

## Read first
- `docs/PROJECT_SPEC.md`: goals, scope, architecture, invariants, data model, roadmap.
- `docs/PHASE_01_PROMPT.md`: the **current** implementation task. Do not silently implement later phases.

## Stack and architecture
- Backend: Python 3.12+, FastAPI, Pydantic, SQLAlchemy 2, Alembic, psycopg.
- Database: PostgreSQL 17 in Docker with a named persistent volume.
- Frontend (later): React + TypeScript + Vite. Share components between live and demo adapters.
- Agents (later): explicit Python tool loop and an interchangeable OpenAI-compatible LLM provider.
- Operations (later): integrate the existing standalone Node/Express Incident Monitoring System through HTTP; don't copy its code into this repo.
- Dev environment: WSL Linux, Docker Desktop with WSL integration, Bash, Git, VS Code. Do not require PowerShell.

## Non-negotiable correctness and safety
- Never treat a container filesystem as durable storage; use named Docker volumes for the database.
- Never run `docker compose down -v`, `docker volume prune`, or destructive SQL without explicit user approval.
- Backups are separate from persistence. Include a tested `pg_dump` / restore process before real personal data is imported.
- Persist all application records and, later, agent execution/checkpoint data in PostgreSQL.
- Keep secrets in ignored `.env` or secret stores; commit `.env.example` only.
- An application is marked `applied` only after user-confirmed submission or verifiable confirmation. Discovery, draft generation, and clicking an apply link do not count.
- AI agents may suggest or draft actions but cannot submit applications, send external messages, or delete data without explicit approval and authorization.
- Do not scrape websites in violation of restrictions. Prefer permitted job feeds or official APIs.
- The public demo must not require a real backend or paid LLM API. Only synthetic records and clearly labeled prerecorded traces are public; no personal data, credentials, or production database copies.
- Use deterministic code for deduplication, validation, cost limits, and retries; the LLM must not control those policies.

## Engineering conventions
- Prefer simple, maintainable solutions over speculative abstractions. Do not add services, caches, queues, or frameworks unless the phase requires them.
- Use Python type hints, Pydantic validation, SQLAlchemy 2 idioms, Alembic migrations, and pytest.
- Separate API handlers, persistence models, schemas, and service logic.
- Use explicit UTC timestamps in persistent records; return unambiguous ISO-8601 values.
- Protect inputs with validation and bound SQL parameters; avoid SQL string concatenation.
- Write meaningful tests for behavior and failure cases. Do not claim tests passed unless executed.
- One feature branch per task, with descriptive commits. Show commands run, tests/results, changed files, and a concise handoff after each task.
- Ask before adding any paid service or broadening scope materially.

## Current milestone
Phase 1 is **only** a runnable FastAPI + PostgreSQL foundation with Docker Compose, a health check, the first relational migration, minimal job APIs, and persistence verification. Do not implement AI, scheduled searches, Incident Monitor connectivity, authentication, full frontend, or demo hosting in Phase 1.

## Workflow with Claude Code / Ponytail / Graphify
- For multi-file work, propose a concise plan, then implement only the accepted phase.
- Use Ponytail to reduce needless complexity but never remove security checks, tests, migrations, or data safeguards.
- Use Graphify for repository exploration when the codebase has grown enough to benefit. Do not spend setup effort building a graph of an empty repo.
- If requirements conflict, explain the trade-off before changing architecture.

## graphify

This project has a knowledge graph at graphify-out/ with god nodes, community structure, and cross-file relationships.

Rules:
- When graphify-out/graph.json exists, run `graphify query "<question>"` before your first grep, search or source-file read for a codebase question, and put this rule in every subagent prompt that explores code. Read raw files only for lines the graph or an earlier read already located. Use `graphify path "<A>" "<B>"` for relationships and `graphify explain "<concept>"` for focused concepts. When the answer is under a Markdown heading, `graphify section "<heading>"` prints that section's text, so the .md need not be opened. These return a scoped subgraph, usually much smaller than GRAPH_REPORT.md or raw grep output.
- If graphify-out/wiki/index.md exists, use it for broad navigation instead of raw source browsing.
- Read graphify-out/GRAPH_REPORT.md only for broad architecture review or when query/path/explain do not surface enough context.
- After modifying code, run `graphify update .` to keep the graph current (AST-only, no API cost).
- A committed graphify-out/graph.json is rewritten whole on every build, so a large diff to it in a pull request is expected. Do not flag it as churn or suggest skipping the graph rebuild or its hooks.
