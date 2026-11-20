"""FastAPI application entrypoint.

Routes
------
``GET /health``                        component health + sync status
``GET /api/state``                     consolidated snapshot of the fuel network
``GET /api/stations``                  stations, read from the stored state
``GET /api/depots``                    depots, read from the stored state
``GET /api/routes``                    routes, read from the stored state
``GET /api/events``                    domain events, read from the stored state
``GET /api/supply-arrivals``           scheduled arrivals, read from the stored state
``GET /api/history/{station_id}``      demand history for one station
``GET /api/alerts``                    platform-generated alerts
``GET /metrics``                       Prometheus exposition

Every read path serves the cached state that the background sync service
keeps fresh. When the simulator is unreachable the API keeps serving
last-known state and says so, with a ``degraded`` flag and an age.
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Path, Query, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .config import get_settings
from .db import Database
from .db import database as default_database
from .logging_config import configure_logging
from .metrics import install_metrics_middleware
from .simulator_client import SimulatorClient, build_client
from .simulator_errors import (
    SimulatorAPIError,
    SimulatorConnectionError,
    SimulatorTimeoutError,
    SimulatorValidationError,
)
from .state_cache import StateCache
from .sync_service import SyncService

logger = logging.getLogger(__name__)

DESCRIPTION = """
Backend for the **Fuel Supply Intelligence & Resilience Platform**.

It reads the BUP Fuel Supply Simulator over HTTP and exposes a stable,
validated view of the fuel network for the operator dashboard.

REST is the source of truth and SSE is only a hint that something changed:
every state this API serves was fetched over REST and validated.
"""


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Open the simulator client and database, and run the sync service."""
    settings = get_settings()
    configure_logging(settings.log_level)

    app.state.settings = settings
    app.state.simulator = build_client(settings)
    app.state.database = default_database
    app.state.cache = StateCache()
    app.state.sync = SyncService(
        client=app.state.simulator,
        database=app.state.database,
        cache=app.state.cache,
        settings=settings,
    )

    await app.state.database.connect()

    try:
        if settings.sync_enabled:
            await app.state.sync.start()
        yield
    finally:
        await app.state.sync.stop()
        await app.state.database.disconnect()
        await app.state.simulator.aclose()


app = FastAPI(
    title="Fuel Supply Intelligence & Resilience Platform API",
    description=DESCRIPTION,
    version=get_settings().app_version,
    lifespan=lifespan,
)

install_metrics_middleware(app)

# The operator dashboard is served from a different origin than the API, so
# the browser needs CORS. Origins are configurable via CORS_ALLOWED_ORIGINS;
# the default covers the local docker-compose ports.
_allowed_origins = [
    origin.strip()
    for origin in os.getenv(
        "CORS_ALLOWED_ORIGINS",
        "http://localhost:5173,http://localhost:3000,http://127.0.0.1:5173",
    ).split(",")
    if origin.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins,
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)


# -- dependency helpers ----------------------------------------------------


def get_simulator(request: Request) -> SimulatorClient:
    """Return the process-wide simulator client."""
    return request.app.state.simulator


def get_database(request: Request) -> Database:
    """Return the process-wide database handle."""
    return request.app.state.database


def get_cache(request: Request) -> StateCache:
    """Return the process-wide state cache."""
    return request.app.state.cache


def get_sync(request: Request) -> SyncService:
    """Return the process-wide sync service."""
    return request.app.state.sync


# -- health ----------------------------------------------------------------


async def _simulator_health(client: SimulatorClient) -> dict[str, Any]:
    """Check the simulator liveness endpoint (fault-immune by design)."""
    try:
        # ``fast`` keeps the probe inside the health-check timeout: when the
        # simulator is down, backing off and retrying only delays the answer
        # the orchestrator is waiting for.
        health = await client.health(fast=True)
    except (SimulatorConnectionError, SimulatorTimeoutError) as exc:
        return {"status": "unreachable", "error": str(exc)}
    except SimulatorAPIError as exc:
        return {"status": "error", "error": str(exc)}
    except SimulatorValidationError as exc:
        return {"status": "degraded", "error": str(exc)}
    return {
        "status": health.status,
        "database": health.database,
        "simulation": {
            "status": health.simulation.status.value,
            "tick": health.simulation.tick,
        },
    }


async def _database_health(db: Database) -> dict[str, Any]:
    """Check that Postgres answers a trivial query."""
    try:
        ok = await db.ping()
    except Exception as exc:
        return {"status": "error", "error": str(exc)}
    return {"status": "ok" if ok else "error"}


@app.get("/health", tags=["ops"])
async def health(
    request: Request,
    client: SimulatorClient = Depends(get_simulator),
    db: Database = Depends(get_database),
) -> JSONResponse:
    """Report the health of the backend, the simulator and the database."""
    simulator_status = await _simulator_health(client)
    database_status = await _database_health(db)

    components = {
        "backend": {"status": "ok"},
        "simulator": simulator_status,
        "database": database_status,
    }
    all_ok = (
        simulator_status.get("status") in {"ok", "degraded"}
        and database_status.get("status") == "ok"
    )
    overall = "ok" if all_ok else "degraded"
    code = status.HTTP_200_OK if all_ok else status.HTTP_503_SERVICE_UNAVAILABLE

    return JSONResponse(
        status_code=code,
        content={
            "status": overall,
            "service": get_settings().app_name,
            "version": get_settings().app_version,
            "components": components,
            "sync": request.app.state.sync.status(),
        },
    )


# -- cached state ----------------------------------------------------------


def _require_state(cache: StateCache) -> Any:
    """Return the cached snapshot, or raise 503 when there is none yet."""
    state = cache.state
    if state is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="no simulator state available yet",
        )
    return state


def _state_envelope(cache: StateCache, payload: dict[str, Any]) -> dict[str, Any]:
    """Wrap a payload with the cache's freshness metadata."""
    return {**payload, **cache.metadata()}


@app.get("/api/state", tags=["state"])
async def get_state(
    request: Request,
    cache: StateCache = Depends(get_cache),
) -> dict[str, Any]:
    """Return a consolidated snapshot of the simulated fuel network.

    Served from the cached state the background sync service maintains, so
    the dashboard never blocks on the simulator.
    """
    state = _require_state(cache)
    return _state_envelope(cache, state.to_payload())


@app.get("/api/stations", tags=["state"])
async def get_stations(
    cache: StateCache = Depends(get_cache),
) -> dict[str, Any]:
    """Return every station, read from the stored state."""
    state = _require_state(cache)
    payload = {"stations": [s.model_dump(mode="json") for s in state.stations]}
    return _state_envelope(cache, payload)


@app.get("/api/depots", tags=["state"])
async def get_depots(
    cache: StateCache = Depends(get_cache),
) -> dict[str, Any]:
    """Return every depot, read from the stored state."""
    state = _require_state(cache)
    payload = {"depots": [d.model_dump(mode="json") for d in state.depots]}
    return _state_envelope(cache, payload)


@app.get("/api/routes", tags=["state"])
async def get_routes(
    cache: StateCache = Depends(get_cache),
) -> dict[str, Any]:
    """Return every route, read from the stored state."""
    state = _require_state(cache)
    payload = {"routes": [r.model_dump(mode="json") for r in state.routes]}
    return _state_envelope(cache, payload)


@app.get("/api/events", tags=["state"])
async def get_events(
    cache: StateCache = Depends(get_cache),
) -> dict[str, Any]:
    """Return the domain events, read from the stored state."""
    state = _require_state(cache)
    payload = {"events": [e.model_dump(mode="json") for e in state.events]}
    return _state_envelope(cache, payload)


@app.get("/api/supply-arrivals", tags=["state"])
async def get_supply_arrivals(
    cache: StateCache = Depends(get_cache),
) -> dict[str, Any]:
    """Return the scheduled supply arrivals, read from the stored state."""
    state = _require_state(cache)
    payload = {"supply_arrivals": [a.model_dump(mode="json") for a in state.supply_arrivals]}
    return _state_envelope(cache, payload)


@app.get("/api/allocations", tags=["state"])
async def get_allocations(
    cache: StateCache = Depends(get_cache),
) -> dict[str, Any]:
    """Return the allocation ledger, read from the stored state."""
    state = _require_state(cache)
    payload = {"allocations": [a.model_dump(mode="json") for a in state.allocations]}
    return _state_envelope(cache, payload)


@app.get("/api/history/{station_id}", tags=["state"])
async def get_history(
    station_id: str = Path(..., min_length=1, max_length=64),
    limit: int = Query(default=200, ge=1, le=2000),
    db: Database = Depends(get_database),
) -> dict[str, Any]:
    """Return the stored demand history for one station.

    Read from Postgres, not from the simulator: this is the time series the
    intelligence layer will read from.
    """
    from sqlalchemy import select

    from .models import DemandObservationRow

    try:
        async with db.session() as session:
            result = await session.execute(
                select(DemandObservationRow)
                .where(DemandObservationRow.station_id == station_id)
                .order_by(DemandObservationRow.tick.desc())
                .limit(limit)
            )
            rows = list(result.scalars())
    except Exception as exc:
        logger.warning("could not read demand history: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="demand history is not available",
        ) from exc

    payload = {
        "station_id": station_id,
        "count": len(rows),
        "observations": [
            {
                "id": row.simulator_id,
                "station_id": row.station_id,
                "fuel_type": row.fuel_type,
                "tick": row.tick,
                "sim_time": row.sim_time.isoformat(),
                "demand_liters": row.demand_liters,
                "served_liters": row.served_liters,
                "unmet_liters": row.unmet_liters,
            }
            for row in rows
        ],
    }
    return payload


@app.get("/api/alerts", tags=["state"])
async def get_alerts(
    limit: int = Query(default=100, ge=1, le=1000),
    db: Database = Depends(get_database),
) -> dict[str, Any]:
    """Return the platform-generated alerts, newest first."""
    from sqlalchemy import select

    from .models import Alert

    try:
        async with db.session() as session:
            result = await session.execute(
                select(Alert).order_by(Alert.created_at.desc()).limit(limit)
            )
            rows = list(result.scalars())
    except Exception as exc:
        logger.warning("could not read alerts: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="alerts are not available",
        ) from exc

    return {
        "count": len(rows),
        "alerts": [
            {
                "id": row.id,
                "created_at": row.created_at.isoformat(),
                "kind": row.kind,
                "severity": row.severity,
                "entity_type": row.entity_type,
                "entity_id": row.entity_id,
                "sim_tick": row.sim_tick,
                "message": row.message,
                "resolved": row.resolved,
            }
            for row in rows
        ],
    }


@app.get("/", tags=["meta"])
async def root() -> dict[str, str]:
    """Service banner."""
    return {
        "service": get_settings().app_name,
        "version": get_settings().app_version,
        "docs": "/docs",
        "health": "/health",
        "state": "/api/state",
        "stations": "/api/stations",
        "history": "/api/history/{station_id}",
    }
