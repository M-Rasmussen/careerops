"""Application settings read from environment variables."""

import os
from dataclasses import dataclass, field

from sqlalchemy import URL


def _require(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"Required environment variable {name} is not set")
    return value


@dataclass(frozen=True)
class Settings:
    db_user: str
    db_password: str
    db_host: str
    db_port: int
    db_name: str

    def database_url(self, database: str | None = None) -> URL:
        # URL.create escapes special characters in credentials safely.
        return URL.create(
            "postgresql+psycopg",
            username=self.db_user,
            password=self.db_password,
            host=self.db_host,
            port=self.db_port,
            database=database or self.db_name,
        )


def get_settings() -> Settings:
    return Settings(
        db_user=_require("POSTGRES_USER"),
        db_password=_require("POSTGRES_PASSWORD"),
        db_host=os.environ.get("POSTGRES_HOST", "db"),
        db_port=int(os.environ.get("POSTGRES_PORT", "5432")),
        db_name=_require("POSTGRES_DB"),
    )


def _int_env(name: str, default: int, minimum: int = 1) -> int:
    value = int(os.environ.get(name) or default)
    if value < minimum:
        raise RuntimeError(f"{name} must be >= {minimum}")
    return value


@dataclass(frozen=True)
class AISettings:
    """Optional AI provider settings. Missing values leave AI disabled; the tracker works without them."""

    provider: str  # "" (disabled) or "openai" (any OpenAI-compatible /chat/completions API)
    model: str
    base_url: str
    api_key: str = field(repr=False)  # never logged, returned by the API, or sent to the frontend
    max_output_tokens: int = 2000
    # Newer OpenAI models reject max_tokens and require max_completion_tokens; most other servers want max_tokens.
    token_param: str = "max_tokens"
    max_steps: int = 6
    timeout_seconds: int = 60
    max_runs_per_day: int = 20

    @property
    def configured(self) -> bool:
        return self.provider == "openai" and bool(self.model and self.base_url)


TOKEN_PARAMS = ("max_tokens", "max_completion_tokens")


def get_ai_settings() -> AISettings:
    token_param = os.environ.get("AI_TOKEN_PARAM", "").strip() or "max_tokens"
    if token_param not in TOKEN_PARAMS:
        raise RuntimeError(f"AI_TOKEN_PARAM must be one of {', '.join(TOKEN_PARAMS)}")
    return AISettings(
        provider=os.environ.get("AI_PROVIDER", "").strip().lower(),
        model=os.environ.get("AI_MODEL", "").strip(),
        base_url=os.environ.get("AI_BASE_URL", "").strip().rstrip("/"),
        api_key=os.environ.get("AI_API_KEY", "").strip(),
        max_output_tokens=_int_env("AI_MAX_OUTPUT_TOKENS", 2000),
        token_param=token_param,
        max_steps=_int_env("AI_MAX_STEPS", 6),
        timeout_seconds=_int_env("AI_TIMEOUT_SECONDS", 60),
        max_runs_per_day=_int_env("AI_MAX_RUNS_PER_DAY", 20, minimum=0),
    )


def _bool_env(name: str, default: bool) -> bool:
    value = os.environ.get(name, "").strip().lower()
    return default if not value else value in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class DiscoverySettingsEnv:
    """Discovery provider switches and request budgets. JSearch is off unless explicitly enabled with a key."""

    himalayas_enabled: bool = False  # employer ATS boards are the primary source
    himalayas_max_pages: int = 1  # pages per role-family query
    jsearch_enabled: bool = False
    jsearch_api_key: str = field(default="", repr=False)  # server-side only; never returned by the API
    jsearch_monthly_limit: int = 100  # every attempt, including retries, counts
    jsearch_max_requests_per_run: int = 5
    jsearch_cache_hours: int = 24  # a query fetched more recently than this is not re-requested
    # Board synchronization (background worker in the API process; PostgreSQL task queue).
    worker_enabled: bool = True  # processes queued runs; off = runs stay queued
    auto_sync_enabled: bool = True  # scheduler: queue boards whose next sync is due
    sync_interval_hours: int = 24
    sync_concurrency: int = 2  # boards fetched at the same time (each provider host still rate-limited)
    min_resync_minutes: int = 60  # Run discovery skips boards synced more recently (reported as deferred)
    max_verifications_per_run: int = 5

    @property
    def jsearch_configured(self) -> bool:
        return self.jsearch_enabled and bool(self.jsearch_api_key)


def get_discovery_env() -> DiscoverySettingsEnv:
    return DiscoverySettingsEnv(
        himalayas_enabled=_bool_env("DISCOVERY_HIMALAYAS_ENABLED", False),
        himalayas_max_pages=_int_env("DISCOVERY_HIMALAYAS_MAX_PAGES", 1),
        jsearch_enabled=_bool_env("JSEARCH_ENABLED", False),
        jsearch_api_key=os.environ.get("JSEARCH_API_KEY", "").strip(),
        jsearch_monthly_limit=_int_env("JSEARCH_MONTHLY_REQUEST_LIMIT", 100, minimum=0),
        jsearch_max_requests_per_run=_int_env("JSEARCH_MAX_REQUESTS_PER_RUN", 5, minimum=0),
        jsearch_cache_hours=_int_env("JSEARCH_CACHE_HOURS", 24, minimum=0),
        worker_enabled=_bool_env("DISCOVERY_WORKER_ENABLED", True),
        auto_sync_enabled=_bool_env("DISCOVERY_AUTO_SYNC_ENABLED", True),
        sync_interval_hours=_int_env("DISCOVERY_SYNC_INTERVAL_HOURS", 24),
        sync_concurrency=_int_env("DISCOVERY_SYNC_CONCURRENCY", 2),
        min_resync_minutes=_int_env("DISCOVERY_MIN_RESYNC_MINUTES", 60, minimum=0),
        max_verifications_per_run=_int_env("DISCOVERY_MAX_VERIFICATIONS_PER_RUN", 5, minimum=0),
    )
