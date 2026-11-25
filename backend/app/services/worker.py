"""Background sync worker: a few threads inside the API process that work through the PostgreSQL task queue.

No Redis, Celery, or extra container. Restart-safe: everything lives in the queue (a task left "running" by a crash is
picked up again after STALE_TASK). Tasks are claimed with SKIP LOCKED, so extra threads or processes never take the same
task. Thread 0 also runs the scheduler every SCHEDULER_TICK seconds. Disable with DISCOVERY_WORKER_ENABLED=false.
"""

import logging
import threading
import time
from collections.abc import Callable

from sqlalchemy.orm import Session

from app.config import DiscoverySettingsEnv
from app.services.career_discovery import SiteFetcher, run_tasks
from app.services.job_sources import HostLimiter, HttpClient
from app.services.sync import scheduler_tick

log = logging.getLogger(__name__)

POLL_SECONDS = 5.0  # idle wait between queue checks (Run discovery wakes the worker at once)
SCHEDULER_TICK = 60.0


class SyncWorker:
    def __init__(self, factory: Callable[[], Session], env: DiscoverySettingsEnv) -> None:
        self._factory, self._env = factory, env
        self._wake, self._stop = threading.Event(), threading.Event()
        self._limiter = HostLimiter()  # provider rate limits hold across all threads
        self._threads: list[threading.Thread] = []

    def start(self) -> None:
        if not self._env.worker_enabled or self._threads:
            return
        for i in range(self._env.sync_concurrency):
            t = threading.Thread(target=self._loop, args=(i,), name=f"sync-worker-{i}", daemon=True)
            t.start()
            self._threads.append(t)
        log.info("Sync worker started (%d threads, auto sync %s, every %d h)", len(self._threads),
                 "on" if self._env.auto_sync_enabled else "off", self._env.sync_interval_hours)

    def notify(self) -> None:
        self._wake.set()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        for t in self._threads:
            t.join(timeout=30)
        self._threads.clear()

    def _loop(self, index: int) -> None:
        client, fetcher = HttpClient(limiter=self._limiter), SiteFetcher()
        last_tick = 0.0
        while not self._stop.is_set():
            try:
                if index == 0 and time.monotonic() - last_tick >= SCHEDULER_TICK:
                    last_tick = time.monotonic()
                    if scheduler_tick(self._factory, self._env):
                        self._wake.set()
                worked = run_tasks(self._factory, client, fetcher, max_tasks=1, kinds=("sync", "feeds"), run_only=True, env=self._env)
            except Exception:  # never let the thread die; the task (if any) is recovered from the queue
                log.exception("Sync worker iteration failed")
                worked = []
            if not worked:
                self._wake.wait(POLL_SECONDS)
                self._wake.clear()
