"""Concurrency guarantees of the task queue, with real separate connections (as separate API processes would have).
These tests commit rows, so each cleans up after itself."""

import uuid
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from sqlalchemy import Engine, delete, select, update
from sqlalchemy.orm import Session

from app.config import DiscoverySettingsEnv, get_discovery_env
from app.models import Company, DiscoveryRun, DiscoveryTask, JobSource
from app.services import career_discovery as cd
from app.services import sync


@pytest.fixture
def committed(engine: Engine) -> Iterator[dict[str, Any]]:
    """Two verified boards and their queued sync tasks, committed (visible to every connection)."""
    tag = uuid.uuid4().hex[:8]
    with Session(engine) as s:
        c = Company(name=f"Concurrency {tag}", domain=f"conc-{tag}.example", origin="manual", watchlist=False, verification_status="verified")
        s.add(c)
        s.flush()
        boards = [JobSource(provider="greenhouse", board_token=f"conc{tag}{i}", company_name=c.name, company_id=c.id, enabled=True,
                            verification_status="verified", ownership_verified_at=datetime.now(timezone.utc)) for i in range(2)]
        s.add_all(boards)
        s.flush()
        tasks = [DiscoveryTask(id=uuid.uuid4(), kind="sync", source_id=b.id, status="queued") for b in boards]
        s.add_all(tasks)
        s.commit()
        ids = {"company": c.id, "boards": [b.id for b in boards], "tasks": [t.id for t in tasks]}
    yield ids
    with Session(engine) as s:
        s.execute(delete(DiscoveryTask).where(DiscoveryTask.source_id.in_(ids["boards"])))
        s.execute(delete(JobSource).where(JobSource.id.in_(ids["boards"])))
        s.execute(delete(Company).where(Company.id == ids["company"]))
        s.commit()


def test_two_processes_never_claim_the_same_task(engine: Engine, committed: dict[str, Any]) -> None:
    a, b = Session(engine), Session(engine)
    try:
        # Process A has selected and locked a task but not committed yet; process B claims concurrently.
        locked = a.scalar(select(DiscoveryTask).where(DiscoveryTask.id.in_(committed["tasks"]), DiscoveryTask.status == "queued")
                          .order_by(DiscoveryTask.id).limit(1).with_for_update(skip_locked=True))
        other = b.scalar(select(DiscoveryTask).where(DiscoveryTask.id.in_(committed["tasks"]), DiscoveryTask.status == "queued")
                         .order_by(DiscoveryTask.id).limit(1).with_for_update(skip_locked=True))
        assert locked is not None and other is not None and locked.id != other.id  # SKIP LOCKED: B gets the other task
        third = Session(engine)
        nothing = third.scalar(select(DiscoveryTask).where(DiscoveryTask.id.in_(committed["tasks"]))
                               .with_for_update(skip_locked=True))
        third.close()
        assert nothing is None  # both are taken; a third worker gets none and does no work
    finally:
        a.rollback(), b.rollback(), a.close(), b.close()


def test_a_board_cannot_be_queued_twice_across_connections(engine: Engine, committed: dict[str, Any]) -> None:
    from app.services.companies import enqueue_sync

    with Session(engine) as s:
        enqueue_sync(s, committed["boards"][0])  # already queued: the unique index turns this into a no-op
        s.commit()
        assert len(s.scalars(select(DiscoveryTask).where(DiscoveryTask.source_id == committed["boards"][0])).all()) == 1


def test_reclaimed_task_result_is_not_counted_twice(engine: Engine, committed: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    """Worker A looks crashed (took over STALE_TASK); the task goes to B. When A finally finishes, it discards its result."""
    factory = lambda: Session(engine)  # noqa: E731
    with Session(engine) as s:
        run = DiscoveryRun(status="running", sources_checked=0, sources_failed=0, jobs_retrieved=0, jobs_inserted=0, jobs_updated=0,
                           duplicates=0, requests=0, jobs_unique=0, jobs_matched=0, jobs_unknown=0, companies_created=0,
                           boards_verified=0, warnings=[], sources=[])
        s.add(run)
        s.flush()
        run_id = run.id
        s.execute(update(DiscoveryTask).where(DiscoveryTask.id == committed["tasks"][0]).values(run_id=run_id))
        s.execute(delete(DiscoveryTask).where(DiscoveryTask.id == committed["tasks"][1]))
        s.commit()

    def slow_sync(*args: Any, **kw: Any) -> dict[str, Any]:
        # While A is still working, the task is reclaimed and claimed by B (simulated here).
        with Session(engine) as s:
            s.execute(update(DiscoveryTask).where(DiscoveryTask.id == committed["tasks"][0])
                      .values(locked_at=datetime.now(timezone.utc) + timedelta(seconds=1)))
            s.commit()
        return {"retrieved": 1, "unique": 1, "inserted": 1, "updated": 0, "duplicates": 0, "matched": 0, "unknown": 0,
                "missing": 0, "closed": 0, "warnings": []}

    monkeypatch.setattr(cd, "sync_board", slow_sync)
    try:
        from app.services.job_sources import HttpClient

        [result] = cd.run_tasks(factory, HttpClient(), cd.SiteFetcher(), max_tasks=1, kinds=("sync",), run_only=True)
        assert result["status"] == "reclaimed"
        with Session(engine) as s:
            r = s.get(DiscoveryRun, run_id)
            assert r is not None and (r.sources_checked, r.jobs_inserted, r.status) == (0, 0, "running")
            assert s.get(DiscoveryTask, committed["tasks"][0]).status == "running"  # still B's
    finally:
        with Session(engine) as s:
            s.execute(update(DiscoveryTask).where(DiscoveryTask.run_id == run_id).values(run_id=None))
            s.execute(delete(DiscoveryRun).where(DiscoveryRun.id == run_id))
            s.commit()


def test_scheduler_in_two_processes_starts_one_run(engine: Engine, committed: dict[str, Any]) -> None:
    factory = lambda: Session(engine)  # noqa: E731
    with Session(engine) as s:
        s.execute(delete(DiscoveryTask).where(DiscoveryTask.id.in_(committed["tasks"])))
        s.commit()
    env = DiscoverySettingsEnv(auto_sync_enabled=True)
    first = second = None
    try:
        first = sync.scheduler_tick(factory, env)
        second = sync.scheduler_tick(factory, env)  # the other process's tick: a run is already active
        assert first is not None and second is None
    finally:
        with Session(engine) as s:
            for rid in (first, second):
                if rid:
                    s.execute(delete(DiscoveryTask).where(DiscoveryTask.run_id == rid))
                    s.execute(delete(DiscoveryRun).where(DiscoveryRun.id == rid))
            s.commit()


def test_normal_settings_keep_the_minimum_resync_gap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DISCOVERY_MIN_RESYNC_MINUTES", raising=False)  # tests set it to 0; production doesn't
    assert get_discovery_env().min_resync_minutes == 60 and DiscoverySettingsEnv().min_resync_minutes == 60
