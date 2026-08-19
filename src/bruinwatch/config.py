"""Typed configuration, loaded from the environment or a local ``.env``."""

from __future__ import annotations

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="BRUINWATCH_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    discord_token: SecretStr
    owner_id: int | None = None
    #: Sync slash commands to one guild for instant availability during
    #: development; global command propagation otherwise takes about an hour.
    dev_guild_id: int | None = None

    database_url: str = "postgresql+asyncpg://bruinwatch:bruinwatch@localhost:5432/bruinwatch"

    user_agent: str = "bruinwatch/2.0 (+https://github.com/mting314/speedchat-bot)"
    #: Ceiling on in-flight requests to sa.ucla.edu across the whole process.
    max_concurrency: int = Field(default=10, ge=1, le=50)
    request_timeout: float = Field(default=20.0, gt=0)
    max_retries: int = Field(default=3, ge=0, le=10)

    scheduler_enabled: bool = True
    #: Consecutive scrape failures before the watched-section job backs off.
    circuit_breaker_threshold: int = Field(default=5, ge=1)

    log_level: str = "INFO"
    log_json: bool = False
    healthcheck_port: int | None = 8080

    @property
    def sync_database_url(self) -> str:
        """Alembic runs synchronously, so it needs the psycopg-free plain URL."""
        return self.database_url.replace("+asyncpg", "")
