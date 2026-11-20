"""Application configuration.

All values come from the environment (12-factor). No secret is hard-coded;
`.env.example` documents the expected keys and their defaults.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime settings for the backend service."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "Fuel Supply Intelligence & Resilience Platform"
    app_version: str = "0.1.0"
    debug: bool = False

    # --- Simulator integration -------------------------------------------
    simulator_base_url: str = Field(
        default="http://simulator:8000",
        alias="SIMULATOR_BASE_URL",
    )
    simulator_request_timeout: float = Field(
        default=10.0,
        alias="SIMULATOR_REQUEST_TIMEOUT",
        gt=0,
    )
    simulator_connect_timeout: float = Field(
        default=3.0,
        alias="SIMULATOR_CONNECT_TIMEOUT",
        gt=0,
    )
    simulator_max_retries: int = Field(
        default=3,
        alias="SIMULATOR_MAX_RETRIES",
        ge=0,
        le=10,
    )
    simulator_retry_base_delay: float = Field(
        default=0.2,
        alias="SIMULATOR_RETRY_BASE_DELAY",
        ge=0,
    )
    simulator_retry_max_delay: float = Field(
        default=5.0,
        alias="SIMULATOR_RETRY_MAX_DELAY",
        ge=0,
    )

    # --- Database ----------------------------------------------------------
    database_url: str = Field(
        default="postgresql+asyncpg://fuel:fuel@postgres:5432/fuel",
        alias="DATABASE_URL",
    )
    db_pool_min_size: int = Field(default=1, alias="DB_POOL_MIN_SIZE", ge=0)
    db_pool_max_size: int = Field(default=10, alias="DB_POOL_MAX_SIZE", ge=1)
    db_health_timeout: float = Field(default=3.0, alias="DB_HEALTH_TIMEOUT", gt=0)

    # --- Background sync ----------------------------------------------------
    sync_enabled: bool = Field(default=True, alias="SYNC_ENABLED")
    #: Seconds between two REST poll cycles.
    sync_poll_interval: float = Field(default=5.0, alias="SYNC_POLL_INTERVAL", gt=0)
    #: Base delay for the SSE reconnect backoff.
    sse_reconnect_base_delay: float = Field(default=1.0, alias="SSE_RECONNECT_BASE_DELAY", gt=0)
    #: Ceiling for the SSE reconnect backoff.
    sse_reconnect_max_delay: float = Field(default=30.0, alias="SSE_RECONNECT_MAX_DELAY", gt=0)
    #: How many demand observations to pull per ingest cycle.
    demand_history_limit: int = Field(default=500, alias="DEMAND_HISTORY_LIMIT", gt=0)

    # --- Observability -----------------------------------------------------
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    metrics_enabled: bool = Field(default=True, alias="METRICS_ENABLED")
    metrics_path: str = Field(default="/metrics", alias="METRICS_PATH")
    log_format: str = Field(default="json", alias="LOG_FORMAT")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the cached settings singleton."""
    return Settings()
