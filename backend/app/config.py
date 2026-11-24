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
    #: The timeout for the health probe. It is on the container
    #: healthcheck's critical path, so it is deliberately much shorter than
    #: a normal request: an unhealthy simulator should be reported quickly,
    #: not waited out.
    simulator_health_timeout: float = Field(
        default=2.0,
        alias="SIMULATOR_HEALTH_TIMEOUT",
        gt=0,
    )
    #: How many consecutive failures open the simulator's circuit breaker.
    #: Five is enough to tell a real outage from one slow call, and low
    #: enough that a genuine outage is recognised within one poll cycle.
    circuit_breaker_failures: int = Field(
        default=5,
        alias="CIRCUIT_BREAKER_FAILURES",
        ge=1,
        le=100,
    )
    #: How long the breaker stays open before it lets a probe call through.
    circuit_breaker_recovery_seconds: float = Field(
        default=20.0,
        alias="CIRCUIT_BREAKER_RECOVERY_SECONDS",
        gt=0,
    )

    # --- Database ----------------------------------------------------------
    database_url: str = Field(
        default="postgresql+asyncpg://fuel:fuel@postgres:5432/fuel",
        alias="DATABASE_URL",
    )
    db_pool_min_size: int = Field(default=1, alias="DB_POOL_MIN_SIZE", ge=0)
    db_pool_max_size: int = Field(default=10, alias="DB_POOL_MAX_SIZE", ge=1)
    #: How many times a deadlocked transaction is retried before the
    #: error is raised. Postgres kills one side of a deadlock, and the SQL
    #: standard says the application should retry it.
    db_deadlock_retries: int = Field(default=3, alias="DB_DEADLOCK_RETRIES", ge=0)
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

    # --- Intelligence -------------------------------------------------------
    #: How many ticks ahead the demand forecast reaches. One simulated day
    #: (96 ticks at 15 minutes). The horizon is the platform's *sight
    #: line*: with a six-hour horizon a station can only be seen running
    #: dry six hours before it happens, which is far too late to ship fuel
    #: to it. A day is also the natural planning window for a fuel station
    #: and matches the simulator's own resupply cadence.
    forecast_horizon_ticks: int = Field(default=96, alias="FORECAST_HORIZON_TICKS", gt=0)
    #: Minimum confidence the learned model must reach to be trusted.
    forecast_min_confidence: float = Field(
        default=0.35, alias="FORECAST_MIN_CONFIDENCE", ge=0, le=1
    )
    #: Minimum training rows the learned model needs before it is trusted.
    forecast_min_training_rows: int = Field(default=30, alias="FORECAST_MIN_TRAINING_ROWS", gt=0)
    #: How many stored observations the forecaster reads per evaluation.
    forecast_history_limit: int = Field(default=5000, alias="FORECAST_HISTORY_LIMIT", gt=0)
    #: How often the learned demand model is retrained, in seconds.
    model_train_interval: int = Field(default=300, alias="MODEL_TRAIN_INTERVAL", gt=0)
    #: Whether the prediction-error aggregate is computed in SQL or in
    #: Python. SQL is the default and the only sensible production
    #: setting: reading every row of ``prediction_evaluations`` on every
    #: sync cycle is what made the platform's p99 ten seconds long.
    #:
    #: The Python path is kept so the "before" side of the before-and-
    #: after in ``docs/load-test-report.md`` can be reproduced by
    #: flipping this one setting, rather than being a remembered number.
    prediction_error_aggregate_in_database: bool = Field(
        default=True,
        alias="PREDICTION_ERROR_AGGREGATE_IN_DATABASE",
    )
    #: Whether the platform dispatches fuel in response to risk.
    dispatch_enabled: bool = Field(default=True, alias="DISPATCH_ENABLED")

    # --- Observability -----------------------------------------------------
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    metrics_enabled: bool = Field(default=True, alias="METRICS_ENABLED")
    metrics_path: str = Field(default="/metrics", alias="METRICS_PATH")
    log_format: str = Field(default="json", alias="LOG_FORMAT")
    #: How long the backend may take to drain on shutdown. Bounded so a
    #: wedged dependency cannot hold the container open until Docker sends
    #: SIGKILL.
    shutdown_grace_seconds: float = Field(
        default=15.0,
        alias="SHUTDOWN_GRACE_SECONDS",
        gt=0,
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the cached settings singleton."""
    return Settings()
