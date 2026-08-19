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

    #: Optional at this level so the scraper CLIs -- which never talk to
    #: Discord -- can load settings without one. `bruinwatch` itself checks for
    #: it at startup and exits with a clear message if it is missing.
    discord_token: SecretStr | None = None
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

    #: Connection pool size. The default suits the long-lived bot; a one-shot
    #: job or a scale-to-zero web service wants far fewer, and a small managed
    #: Postgres (Cloud SQL micro allows ~25 total) can be exhausted by a couple
    #: of generous pools.
    db_pool_size: int = Field(default=10, ge=1, le=50)
    db_max_overflow: int = Field(default=5, ge=0, le=50)
    #: Pin a single connection instead of pooling. Needed to point any of the
    #: CLIs at an in-process PGlite, which serves one connection at a time.
    db_single_connection: bool = False

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
