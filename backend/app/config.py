"""Application settings read from environment variables."""

import os
from dataclasses import dataclass

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
