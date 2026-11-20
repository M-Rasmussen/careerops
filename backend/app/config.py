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
