"""Employer board synchronization: runs, fair scheduling, job updates, and closed postings.

A run (manual "Run discovery" or the scheduler) only *queues* work: one `sync` task per eligible board and, when
enabled, one `feeds` task for aggregator feeds. Background workers (app.services.worker) process the queue in small
units, so no HTTP request performs the network work. Each finished task adds its outcome to the run atomically; the
last one finishes the run.

Fairness: boards are queued overdue-first (`next_sync_at`, then oldest success). A successful sync schedules the next
one an interval later; a failure backs off (doubling, capped at the interval) without blocking other boards.

Closed postings: after a complete, successful, non-empty sync, postings of that board that weren't listed get
`missing_count += 1`; at 2, and at least 12 hours after they were last listed, they are closed (hidden from Job
Discovery). Listed again: reopened. Failed or empty syncs never change this. Nothing is deleted, and saved jobs and applications are never touched.
"""

import json
import logging
import time
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import and_, case, cast, func, literal, nulls_first, select, text, update
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.config import DiscoverySettingsEnv
from app.models import Company, DiscoveredJob, DiscoveryRun, DiscoveryTask, JobSource
from app.services.aggregators import fetch_himalayas, fetch_jsearch
from app.services.discovery import (
    RUN_LOCK_KEY,
    STALE_RUN,
    SYNCABLE,
    ConflictError,
    NoSourcesError,
    _jsearch_reserver,
    _verify_candidates,
    active_generation,
    feed_plan,
    get_filters,
    mark_synced,
    persist_jobs,
)
from app.services.discovery_filters import ROLE_FAMILIES, ROLE_FAMILY_KEYS
from app.services.job_sources import PROVIDERS, HttpClient, SourceError, fetch_jobs

log = logging.getLogger(__name__)

CLOSE_AFTER_MISSING = 2  # consecutive complete syncs without the posting before it's treated as closed...
CLOSE_MIN_AGE = timedelta(hours=12)  # ...and at least this long since it was last listed (quick re-runs never close)
BACKOFF_BASE = timedelta(minutes=15)
RATE_LIMIT_BACKOFF = timedelta(hours=1)  # after the provider still answers 429 once retries are used up

_COUNTERS = ("retrieved", "inserted", "updated", "unique", "matched", "unknown")


def _empty_outcome(**meta: Any) -> dict[str, Any]:
    return {"status": "failed", "retrieved": 0, "unique": 0, "inserted": 0, "updated": 0, "duplicates": 0, "matched": 0,
            "unknown": 0, "missing": 0, "closed": 0, "error": None, "warnings": [], "requests": 0, "duration_ms": 0, **meta}


# --- Starting a run -----------------------------------------------------------------------------


def start_run(factory: Callable[[], Session], env: DiscoverySettingsEnv, trigger: str = "manual") -> uuid.UUID | None:
    """Queues a run and returns its id (scheduled: None when nothing is due or a run is active). Contacts nothing.
    Manual runs queue every eligible board except ones synced in the last `min_resync_minutes` (reported as
    deferred); scheduled runs queue only boards whose next sync is due."""
    now = datetime.now(timezone.utc)
    with factory() as session:
        session.execute(select(func.pg_advisory_xact_lock(RUN_LOCK_KEY)))
        if session.scalar(select(DiscoveryRun.id).where(DiscoveryRun.status == "running", DiscoveryRun.started_at > now - STALE_RUN)):
            if trigger == "scheduled":
                return None
            raise ConflictError("A discovery run is already in progress")
        plan = feed_plan(session, env)
        feeds = plan["himalayas"] or bool(plan["queries"])
        boards = session.execute(
            select(JobSource, Company.name).join(Company, Company.id == JobSource.company_id).where(*SYNCABLE)
            # Overdue first, then never/least recently synced; id breaks ties so the order is deterministic.
            .order_by(nulls_first(JobSource.next_sync_at.asc()), nulls_first(JobSource.last_success_at.asc()), JobSource.id)
        ).all()
        if trigger == "scheduled":
            boards = [(b, n) for b, n in boards if b.next_sync_at is None or b.next_sync_at <= now]
            if not boards:
                return None
        elif not boards and not feeds:
            reasons = "; ".join(f"{s['provider']}: {s['reason']}" for s in plan["skipped"]) or "no providers"
            raise NoSourcesError(f"No discovery sources are available ({reasons}). Run career-source discovery for a company "
                                 "in the Company directory, or verify an employer board.")
        recent = now - timedelta(minutes=env.min_resync_minutes)
        outcomes = [_empty_outcome(kind="aggregator", provider=s["provider"], company=s["provider"], status="skipped",
                                   error=s["reason"]) for s in plan["skipped"]]
        run = DiscoveryRun(status="running", trigger=trigger, sources_checked=0, sources_failed=0, jobs_retrieved=0,
                           jobs_inserted=0, jobs_updated=0, duplicates=0, requests=0, jobs_unique=0, jobs_matched=0,
                           jobs_unknown=0, companies_created=0, boards_verified=0, warnings=plan["warnings"], sources=[])
        session.add(run)
        session.flush()
        queued = 0
        for position, (board, company) in enumerate(boards):
            meta = {"kind": "board", "source_id": str(board.id), "company_id": str(board.company_id), "provider": board.provider,
                    "board_token": board.board_token, "company": company}
            if trigger == "manual" and board.last_success_at and board.last_success_at > recent:
                outcomes.append(_empty_outcome(**meta, status="deferred",
                                               error=f"Synced less than {env.min_resync_minutes} min ago; next check is scheduled"))
                continue
            # run_after keeps the overdue-first order exact (rows inserted in one transaction share now()).
            stmt = pg_insert(DiscoveryTask).values(id=uuid.uuid4(), kind="sync", source_id=board.id, run_id=run.id,
                                                   run_after=now + timedelta(microseconds=position))
            # A board already queued or syncing is never queued twice; an unowned queued task joins this run.
            owner = session.scalar(stmt.on_conflict_do_update(
                index_elements=["kind", "source_id"], index_where=text("status IN ('queued', 'running') AND kind = 'sync'"),
                set_={"run_id": func.coalesce(DiscoveryTask.run_id, stmt.excluded.run_id)},
            ).returning(DiscoveryTask.run_id))
            if owner == run.id:
                queued += 1
            else:
                outcomes.append(_empty_outcome(**meta, status="deferred", error="Already being synced by another run"))
        if feeds:
            session.add(DiscoveryTask(id=uuid.uuid4(), kind="feeds", run_id=run.id, status="queued",
                                      payload={"himalayas": plan["himalayas"], "queries": plan["queries"]}))
            queued += 1
        run.sources = outcomes
        if not queued:
            _finish(session, run)
        session.commit()
        return run.id


# --- Board sync ---------------------------------------------------------------------------------


def _backoff(failures: int, interval_hours: int, error: str) -> timedelta:
    delay = min(BACKOFF_BASE * (2 ** max(failures - 1, 0)), timedelta(hours=interval_hours))
    return max(delay, RATE_LIMIT_BACKOFF) if "429" in error else delay


def sync_board(factory: Callable[[], Session], client: HttpClient, source_id: uuid.UUID,
               env: DiscoverySettingsEnv | None = None) -> dict[str, Any]:
    """Imports an ownership-verified board's current postings into the active generation. Idempotent. Raises
    SourceError on failure after recording it on the board (with backoff for its next sync)."""
    interval = (env or DiscoverySettingsEnv()).sync_interval_hours
    with factory() as session:
        board = session.get(JobSource, source_id)
        if board is None or not board.enabled or board.ownership_verified_at is None or board.company_id is None:
            raise SourceError("Board is disabled or its employer ownership isn't verified")
        if board.provider not in PROVIDERS:
            raise SourceError(f"Board is disabled for import: no {board.provider} adapter yet")
        provider, token, company_id = board.provider, board.board_token, board.company_id
        company = session.get(Company, company_id)
        name = company.name if company else board.company_name
        criteria = get_filters(session).model_dump()
        generation = active_generation(session)
    try:
        jobs = fetch_jobs(client, provider, token, name)
        return _apply(factory, jobs, criteria, source_id, company_id, provider, name, generation, interval)
    except Exception as exc:  # any failure (provider or our own) backs off, so the scheduler never re-queues it in a loop
        error = str(exc) if isinstance(exc, SourceError) else f"Unexpected error ({type(exc).__name__})"
        with factory() as session:
            board = session.get(JobSource, source_id)
            if board is not None:
                board.sync_failures += 1
                board.last_error, board.last_error_at = error, datetime.now(timezone.utc)
                board.last_checked_at = datetime.now(timezone.utc)
                board.next_sync_at = datetime.now(timezone.utc) + _backoff(board.sync_failures, interval, error)
                session.commit()
        raise


def _apply(factory: Callable[[], Session], jobs: list[Any], criteria: dict[str, Any], source_id: uuid.UUID, company_id: uuid.UUID,
           provider: str, name: str, generation: int, interval: int) -> dict[str, Any]:
    stats: dict[str, Any] = {"retrieved": len(jobs), "unique": 0, "inserted": 0, "updated": 0, "duplicates": 0,
                             "matched": 0, "unknown": 0, "missing": 0, "closed": 0, "warnings": []}
    with factory() as session:
        if jobs:
            found, _ = persist_jobs(session, jobs, criteria, source_id, company_id, generation, "employer_ats")
            stats.update({k: found[k] for k in ("unique", "inserted", "updated", "duplicates", "matched", "unknown")})
            stats.update(_mark_missing(session, source_id, provider, {j.source_posting_id for j in jobs}, generation))
        else:
            # An empty answer could be a provider glitch: nothing is marked missing or closed on it.
            stats["warnings"].append(f"{name}: the board returned no postings; existing postings were left open")
        mark_synced(session, source_id, interval, len(jobs))
        session.commit()
    return stats


def _mark_missing(session: Session, source_id: uuid.UUID, provider: str, listed: set[str], generation: int) -> dict[str, int]:
    d = DiscoveredJob
    gone = and_(d.source_id == source_id, d.provider == provider, d.generation == generation, d.closed_at.is_(None),
                d.source_posting_id.not_in(listed))
    rows = session.execute(update(d).where(gone).values(
        missing_count=d.missing_count + 1,
        closed_at=case((and_(d.missing_count + 1 >= CLOSE_AFTER_MISSING, d.last_seen_at < func.now() - CLOSE_MIN_AGE), func.now()),
                       else_=None),
    ).returning(d.closed_at)).all()
    return {"missing": len(rows), "closed": sum(1 for (c,) in rows if c is not None)}


# --- Feeds (optional aggregators) ---------------------------------------------------------------


def run_feeds(factory: Callable[[], Session], client: HttpClient, env: DiscoverySettingsEnv, payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Himalayas / JSearch for one run (only when enabled). Same persistence and dedup as boards; never verifies
    board ownership (boards found in feed links stay "needs review")."""
    with factory() as session:
        criteria = get_filters(session).model_dump()
        generation = active_generation(session)
    families = criteria["role_families"] or list(ROLE_FAMILY_KEYS)
    units: list[tuple[str, Callable[[], tuple[list[Any], list[str]]]]] = []
    if payload.get("himalayas"):
        units.append(("himalayas", lambda: fetch_himalayas(client, [ROLE_FAMILIES[f][1] for f in families], env.himalayas_max_pages)))
    if payload.get("queries"):
        reserve = _jsearch_reserver(factory, env)
        units.append(("jsearch", lambda: fetch_jsearch(client, env.jsearch_api_key, payload["queries"], reserve)))
    outcomes, candidates = [], {}
    for provider, fetch in units:
        t0, before = time.monotonic(), client.requests
        outcome = _empty_outcome(kind="aggregator", provider=provider, company=f"{provider.capitalize()} (many employers)", source_id=None)
        try:
            jobs, partial = fetch()
            outcome.update(retrieved=len(jobs), warnings=partial, status="succeeded")
            if jobs:
                with factory() as session:
                    stats, found = persist_jobs(session, jobs, criteria, None, None, generation, "aggregator")
                    session.commit()
                outcome["companies_created"] = stats.pop("companies_created")
                outcome.update(stats)
                for ref, cid, url in found:
                    candidates.setdefault((ref.provider, ref.board.lower()), (ref, cid, url))
        except Exception as exc:  # one feed failing never stops the other or the boards
            if not isinstance(exc, SourceError):
                log.exception("Feed %s failed", provider)
            outcome.update(status="failed", error=str(exc) if isinstance(exc, SourceError) else f"Unexpected error ({type(exc).__name__})")
        outcome["requests"], outcome["duration_ms"] = client.requests - before, round((time.monotonic() - t0) * 1000)
        outcomes.append(outcome)
    verified, notes = _verify_candidates(factory, client, list(candidates.values()), env.max_verifications_per_run)
    if outcomes:
        outcomes[-1]["boards_verified"], outcomes[-1]["warnings"] = verified, outcomes[-1]["warnings"] + notes
    return outcomes


# --- Run accounting -----------------------------------------------------------------------------


def record_outcome(session: Session, run_id: uuid.UUID, outcome: dict[str, Any]) -> None:
    """Adds one board/feed outcome to its run in a single UPDATE, so concurrent workers never lose counts."""
    r = DiscoveryRun
    checked = outcome["status"] in ("succeeded", "failed")
    session.execute(update(r).where(r.id == run_id).values(
        sources=r.sources.op("||")(func.jsonb_build_array(cast(literal(_json(outcome)), JSONB))),
        sources_checked=r.sources_checked + int(checked),
        sources_failed=r.sources_failed + int(outcome["status"] == "failed"),
        duplicates=r.duplicates + outcome["duplicates"],
        requests=r.requests + outcome["requests"],
        companies_created=r.companies_created + outcome.get("companies_created", 0),
        boards_verified=r.boards_verified + outcome.get("boards_verified", 0),
        warnings=r.warnings.op("||")(cast(literal(_json(outcome.get("warnings") or [])), JSONB)),
        **{f"jobs_{k}": getattr(r, f"jobs_{k}") + outcome[k] for k in _COUNTERS},
    ))


def _json(value: Any) -> str:
    return json.dumps(value, default=str)


def _finish(session: Session, run: DiscoveryRun) -> None:
    run.status = "failed" if run.sources_checked and run.sources_failed == run.sources_checked else "partial" if run.sources_failed else "succeeded"
    run.finished_at = datetime.now(timezone.utc)
    run.duration_ms = round((run.finished_at - run.started_at).total_seconds() * 1000) if run.started_at else None


def finish_if_done(session: Session, run_id: uuid.UUID) -> None:
    """Closes the run once none of its tasks is queued or running (row lock: two workers can't both close it)."""
    run = session.scalar(select(DiscoveryRun).where(DiscoveryRun.id == run_id).with_for_update())
    if run is None or run.status != "running":
        return
    active = session.scalar(select(func.count()).select_from(DiscoveryTask).where(
        DiscoveryTask.run_id == run_id, DiscoveryTask.status.in_(("queued", "running"))))
    if not active:
        session.refresh(run)
        _finish(session, run)


def progress(session: Session, run_ids: list[uuid.UUID]) -> dict[uuid.UUID, dict[str, int]]:
    rows = session.execute(select(DiscoveryTask.run_id, DiscoveryTask.status, func.count())
                           .where(DiscoveryTask.run_id.in_(run_ids)).group_by(DiscoveryTask.run_id, DiscoveryTask.status)).all() if run_ids else []
    out: dict[uuid.UUID, dict[str, int]] = {}
    for rid, status, n in rows:
        p = out.setdefault(rid, {"tasks_total": 0, "tasks_done": 0, "tasks_failed": 0, "tasks_remaining": 0})
        p["tasks_total"] += n
        p["tasks_done" if status == "done" else "tasks_failed" if status == "failed" else "tasks_remaining"] += n
    return out


def scheduler_tick(factory: Callable[[], Session], env: DiscoverySettingsEnv) -> uuid.UUID | None:
    """Closes runs whose tasks all finished (e.g. after a crash), then queues a scheduled run when boards are due."""
    with factory() as session:
        for rid in session.scalars(select(DiscoveryRun.id).where(DiscoveryRun.status == "running")):
            finish_if_done(session, rid)
        # A run stuck "running" past STALE_RUN with no live tasks left was already closed above; one whose tasks are
        # still queued keeps going (the queue itself recovers crashed tasks).
        session.commit()
    return start_run(factory, env, trigger="scheduled") if env.auto_sync_enabled else None
