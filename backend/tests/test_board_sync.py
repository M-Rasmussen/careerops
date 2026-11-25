"""Board synchronization: every board checked, fair scheduling, duplicate-task prevention, backoff, rate limits,
scheduled vs manual runs, run progress, and new/updated/missing/closed postings. Synthetic data; no real network."""

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.api.discovery import get_http_client
from app.config import DiscoverySettingsEnv, get_discovery_env
from app.main import app
from app.models import Application, DiscoveredJob, DiscoveryRun, DiscoveryTask, Job, JobSource
from app.services import sync
from app.services.job_sources import HostLimiter
from app.services.worker import SyncWorker
from tests.conftest import drain_runs
from tests.discovery_fixtures import fake_client, http_error
from tests.test_employer_discovery import GH_API, company, factory, summary  # noqa: F401 (fixture)

ENV = DiscoverySettingsEnv(min_resync_minutes=0, auto_sync_enabled=True, sync_interval_hours=24)


def gh(*ids: int, title: str = "Engineer") -> dict[str, Any]:
    return {"jobs": [{"id": i, "title": f"{title} {i}", "absolute_url": f"https://job-boards.greenhouse.io/x/jobs/{i}",
                      "content": f"&lt;p&gt;Role {i}.&lt;/p&gt;"} for i in ids]}


def boards(db: Session, n: int, **kw: Any) -> list[JobSource]:
    out = []
    for i in range(n):
        c = company(db, name=f"Employer {i:02d}", domain=f"employer{i:02d}.example")
        b = JobSource(provider="greenhouse", board_token=f"board{i:02d}", company_name=c.name, company_id=c.id, enabled=True,
                      verification_status="verified", ownership_verified_at=datetime.now(timezone.utc), **kw)
        db.add(b)
        out.append(b)
    db.commit()
    return out


def routes_for(bs: list[JobSource], payload: Any = None) -> dict[str, list[Any]]:
    return {f"{GH_API}{b.board_token}/jobs": [payload if payload is not None else gh(100 + i)] for i, b in enumerate(bs)}


@pytest.fixture(autouse=True)
def _factory(factory: Any) -> None:
    global FACTORY
    FACTORY = factory  # sessions on the test connection (the code closes the sessions it opens)


FACTORY: Any = None


def run_now(db: Session, routes: dict[str, list[Any]], env: DiscoverySettingsEnv = ENV, trigger: str = "manual") -> dict[str, Any]:
    run_id = sync.start_run(FACTORY, env, trigger)
    assert run_id is not None
    drain_runs(FACTORY, fake_client(routes)[0], env)
    run = db.get(DiscoveryRun, run_id)
    db.refresh(run)
    return {"run": run, "progress": sync.progress(db, [run_id]).get(run_id, {})}


# --- Every board, fairly ---


def test_more_than_ten_boards_are_all_synced_in_one_run(client: TestClient, db_session: Session) -> None:
    bs = boards(db_session, 12)
    r = run_now(db_session, routes_for(bs))
    assert (r["run"].status, r["run"].sources_checked, r["run"].jobs_inserted) == ("succeeded", 12, 12)
    assert r["progress"] == {"tasks_total": 12, "tasks_done": 12, "tasks_failed": 0, "tasks_remaining": 0}
    assert summary(client)["total"] == 12


def test_overdue_boards_are_queued_first_and_order_is_deterministic(db_session: Session) -> None:
    bs = boards(db_session, 3)
    now = datetime.now(timezone.utc)
    for b, due in zip(bs, (now - timedelta(hours=1), now - timedelta(days=2), None)):
        b.next_sync_at = due
    db_session.commit()
    run_id = sync.start_run(FACTORY, ENV)
    queued = db_session.scalars(select(DiscoveryTask.source_id).where(DiscoveryTask.run_id == run_id).order_by(DiscoveryTask.run_after)).all()
    assert queued == [bs[2].id, bs[1].id, bs[0].id]  # never scheduled, then most overdue


def test_scheduled_runs_take_only_due_boards_and_every_board_comes_around(db_session: Session) -> None:
    bs = boards(db_session, 4)
    now = datetime.now(timezone.utc)
    for b, due in zip(bs, (now - timedelta(minutes=5), now + timedelta(hours=3), now + timedelta(hours=10), None)):
        b.next_sync_at = due
    db_session.commit()
    first = run_now(db_session, routes_for(bs), trigger="scheduled")
    assert first["run"].trigger == "scheduled" and first["run"].sources_checked == 2  # due + never scheduled
    for b in bs:
        db_session.refresh(b)
    # Synced boards move a full interval out; the others become due in turn, so none is skipped forever.
    assert all(b.next_sync_at > now + timedelta(hours=23) for b in (bs[0], bs[3]))
    db_session.execute(update(JobSource).where(JobSource.id.in_([bs[1].id, bs[2].id])).values(next_sync_at=func.now() - timedelta(seconds=1)))
    db_session.commit()
    second = run_now(db_session, routes_for(bs), trigger="scheduled")
    assert {s["board_token"] for s in second["run"].sources if s["kind"] == "board"} == {"board01", "board02"}
    assert sync.start_run(FACTORY, ENV, "scheduled") is None  # nothing due: no run, no requests


def test_manual_run_defers_recently_synced_boards(db_session: Session) -> None:
    bs = boards(db_session, 2)
    bs[0].last_success_at = datetime.now(timezone.utc) - timedelta(minutes=5)
    db_session.commit()
    r = run_now(db_session, routes_for(bs), env=DiscoverySettingsEnv(min_resync_minutes=60))
    by_token = {s["board_token"]: s["status"] for s in r["run"].sources if s["kind"] == "board"}
    assert by_token == {"board00": "deferred", "board01": "succeeded"} and r["run"].status == "succeeded"


def test_failed_board_backs_off_without_blocking_others(db_session: Session) -> None:
    bs = boards(db_session, 3)
    routes = routes_for(bs)
    routes[f"{GH_API}board01/jobs"] = [http_error(f"{GH_API}board01/jobs", 500)]
    r = run_now(db_session, routes)
    assert (r["run"].status, r["run"].sources_checked, r["run"].sources_failed) == ("partial", 3, 1)
    failed = db_session.get(JobSource, bs[1].id)
    db_session.refresh(failed)
    assert failed.sync_failures == 1 and failed.last_error == "HTTP 500"
    assert timedelta(minutes=14) < failed.next_sync_at - datetime.now(timezone.utc) < timedelta(minutes=16)
    r2 = run_now(db_session, routes)  # fails again: the wait doubles (capped at the interval)
    db_session.refresh(failed)
    assert failed.sync_failures == 2 and failed.next_sync_at - datetime.now(timezone.utc) > timedelta(minutes=29)
    assert r2["run"].sources_checked == 3


def test_unsupported_and_unowned_boards_are_never_queued(db_session: Session) -> None:
    c = company(db_session)
    db_session.add_all([
        JobSource(provider="workday", board_token="acme", ats_host="acme.wd1.myworkdayjobs.com", career_site="ext", company_name=c.name,
                  company_id=c.id, enabled=True, verification_status="verified", ownership_verified_at=datetime.now(timezone.utc)),
        JobSource(provider="greenhouse", board_token="unowned", company_name=c.name, company_id=c.id, enabled=True, verification_status="needs_review"),
    ])
    db_session.commit()
    with pytest.raises(sync.NoSourcesError):
        sync.start_run(FACTORY, ENV)


# --- Duplicate prevention and progress ---


def test_a_board_is_never_queued_twice(client: TestClient, db_session: Session) -> None:
    bs = boards(db_session, 2)
    from app.services.companies import enqueue_sync

    enqueue_sync(db_session, bs[0].id)  # e.g. queued by career discovery, not part of any run
    db_session.commit()
    run_id = sync.start_run(FACTORY, ENV)
    tasks = db_session.scalars(select(DiscoveryTask).where(DiscoveryTask.kind == "sync")).all()
    assert len(tasks) == 2 and {t.run_id for t in tasks} == {run_id}  # the existing task joined the run
    with pytest.raises(sync.ConflictError):
        sync.start_run(FACTORY, ENV)  # one run at a time
    assert sync.start_run(FACTORY, ENV, "scheduled") is None


def test_progress_is_reported_while_running(client: TestClient, db_session: Session) -> None:
    bs = boards(db_session, 3)
    app.dependency_overrides[get_discovery_env] = lambda: ENV
    from app.api.discovery import get_run_executor

    app.dependency_overrides[get_run_executor] = lambda: lambda *a: None  # the worker hasn't started yet
    started = client.post("/api/discovery/runs").json()
    assert (started["status"], started["tasks_total"], started["tasks_remaining"]) == ("running", 3, 3)
    drain_runs(FACTORY, fake_client(routes_for(bs))[0], ENV)
    done = client.get(f"/api/discovery/runs/{started['id']}").json()
    assert (done["status"], done["tasks_done"], done["tasks_remaining"], done["jobs_inserted"]) == ("succeeded", 3, 0, 3)
    assert client.get("/api/discovery/runs", params={"limit": 1}).json()[0]["tasks_done"] == 3


# --- Rate limiting ---


def test_host_limiter_spaces_requests_per_host_across_clients() -> None:
    clock = [0.0]
    sleeps: list[float] = []
    limiter = HostLimiter(min_interval=1.0, sleep=lambda s: (sleeps.append(s), clock.__setitem__(0, clock[0] + s)), clock=lambda: clock[0])
    limiter.wait("boards-api.greenhouse.io")
    limiter.wait("boards-api.greenhouse.io")  # a second worker, same host: waits
    limiter.wait("api.ashbyhq.com")  # another provider: no wait
    assert sleeps == [1.0]


def test_rate_limited_board_waits_at_least_an_hour(db_session: Session) -> None:
    bs = boards(db_session, 1)
    r = run_now(db_session, {f"{GH_API}board00/jobs": [http_error(f"{GH_API}board00/jobs", 429)]})
    db_session.refresh(bs[0])
    assert r["run"].status == "failed" and bs[0].next_sync_at - datetime.now(timezone.utc) > timedelta(minutes=59)


# --- New, updated, missing, closed ---


def job(db: Session, posting: str) -> DiscoveredJob:
    return db.scalar(select(DiscoveredJob).where(DiscoveredJob.source_posting_id == posting))


def test_new_updated_missing_closed_and_reopened(client: TestClient, db_session: Session) -> None:
    [b] = boards(db_session, 1)
    url = f"{GH_API}board00/jobs"
    r1 = run_now(db_session, {url: [gh(1, 2, 3)]})
    assert r1["run"].jobs_inserted == 3
    # Saved and applied: must survive the employer closing the posting.
    saved = client.post(f"/api/discovery/jobs/{job(db_session, '3').id}/save").json()
    db_session.add(Application(job_id=saved["saved_job_id"], status="applied", applied_date=datetime(2026, 11, 2).date()))
    db_session.commit()
    r2 = run_now(db_session, {url: [gh(1, 2, 4, title="Senior Engineer")]})  # 3 missing once; 4 new; 1-2 updated
    assert (r2["run"].jobs_inserted, r2["run"].jobs_updated) == (1, 2)
    assert r2["run"].sources[-1]["missing"] == 1 and r2["run"].sources[-1]["closed"] == 0
    db_session.refresh(job(db_session, "3"))
    assert job(db_session, "3").missing_count == 1 and job(db_session, "3").closed_at is None  # one miss isn't enough
    assert job(db_session, "1").title == "Senior Engineer 1"
    r3 = run_now(db_session, {url: [gh(1, 2, 4)]})
    assert r3["run"].sources[-1]["closed"] == 0  # missing twice, but it was listed only moments ago: not yet
    db_session.execute(update(DiscoveredJob).where(DiscoveredJob.source_posting_id == "3").values(last_seen_at=func.now() - timedelta(hours=13)))
    db_session.commit()
    r3 = run_now(db_session, {url: [gh(1, 2, 4)]})
    assert r3["run"].sources[-1]["closed"] == 1
    closed = job(db_session, "3")
    db_session.refresh(closed)
    assert closed.closed_at is not None and closed.description  # kept with its description
    s = summary(client)
    assert (s["total"], s["closed"]) == (3, 1)
    assert [j["title"] for j in client.get("/api/jobs").json()] == ["Engineer 3"]  # Saved Jobs unchanged
    assert db_session.scalar(select(func.count()).select_from(Application)) == 1
    assert closed.review_status == "saved" and str(closed.saved_job_id) == saved["saved_job_id"]
    assert client.get(f"/api/discovery/jobs/{closed.id}").json()["closed_at"]
    run_now(db_session, {url: [gh(1, 2, 3, 4)]})  # listed again: reopened, same record
    db_session.refresh(closed)
    assert (closed.closed_at, closed.missing_count) == (None, 0) and summary(client)["total"] == 4
    assert db_session.scalar(select(func.count()).select_from(DiscoveredJob)) == 4  # never duplicated


def test_failed_or_empty_syncs_never_close_postings(client: TestClient, db_session: Session) -> None:
    [b] = boards(db_session, 1)
    url = f"{GH_API}board00/jobs"
    run_now(db_session, {url: [gh(1, 2)]})
    for _ in range(3):
        run_now(db_session, {url: [{"jobs": []}]})  # an empty answer could be a glitch
        run_now(db_session, {url: [http_error(url, 503)]})
    assert db_session.scalar(select(func.count()).select_from(DiscoveredJob).where(DiscoveredJob.missing_count > 0)) == 0
    assert summary(client)["total"] == 2


def test_closing_only_affects_the_active_generation(client: TestClient, db_session: Session) -> None:
    from app.services.discovery import archive_active_generation

    [b] = boards(db_session, 1)
    url = f"{GH_API}board00/jobs"
    run_now(db_session, {url: [gh(1, 2)]})
    archive_active_generation(db_session, "later")
    db_session.execute(update(DiscoveredJob).values(last_seen_at=func.now() - timedelta(days=2)))  # old enough to close
    db_session.commit()
    for _ in range(2):
        run_now(db_session, {url: [gh(1)]})
    assert job(db_session, "2").closed_at is None  # archived history isn't rewritten


# --- Worker switch ---


def test_worker_does_not_start_when_disabled() -> None:
    w = SyncWorker(lambda: None, DiscoverySettingsEnv(worker_enabled=False))  # type: ignore[arg-type,return-value]
    w.start()
    assert w._threads == []


def test_scheduler_respects_the_auto_sync_switch(db_session: Session) -> None:
    boards(db_session, 1)
    assert sync.scheduler_tick(FACTORY, DiscoverySettingsEnv(auto_sync_enabled=False)) is None
    assert sync.scheduler_tick(FACTORY, ENV) is not None  # the never-synced board is due


def test_company_directory_shows_board_sync_state(client: TestClient, db_session: Session) -> None:
    [b] = boards(db_session, 1)
    queued = sync.start_run(FACTORY, ENV)
    assert queued
    [read] = client.get(f"/api/companies/{b.company_id}").json()["boards"]
    assert (read["sync_status"], read["active_jobs"]) == ("queued", 0)
    drain_runs(FACTORY, fake_client({f"{GH_API}board00/jobs": [gh(1, 2)]})[0], ENV)
    db_session.expire_all()  # the API shares this test session; each real request has a fresh one
    [read] = client.get(f"/api/companies/{b.company_id}").json()["boards"]
    assert (read["sync_status"], read["active_jobs"], read["sync_failures"]) == ("ok", 2, 0) and read["next_sync_at"]


def test_unexpected_failure_after_fetch_still_backs_off(db_session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    [b] = boards(db_session, 1)
    monkeypatch.setattr(sync, "persist_jobs", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db hiccup")))
    r = run_now(db_session, {f"{GH_API}board00/jobs": [gh(1)]})
    db_session.refresh(b)
    assert r["run"].status == "failed" and b.sync_failures == 1 and b.last_error == "Unexpected error (RuntimeError)"
    assert b.next_sync_at > datetime.now(timezone.utc) + timedelta(minutes=14)  # not due again on the next tick
