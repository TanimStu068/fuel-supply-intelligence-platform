"""FastAPI application entrypoint.

Routes
------
``GET /health``   Component health: backend + simulator + database.
``GET /api/state`` A consolidated snapshot of the simulated fuel network.
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .config import get_settings
from .db import Database
from .db import database as default_database
from .metrics import install_metrics_middleware
from .simulator_client import SimulatorClient, build_client
from .simulator_errors import (
    SimulatorAPIError,
    SimulatorConnectionError,
    SimulatorTimeoutError,
    SimulatorValidationError,
)
from .simulator_models import (
    Allocation,
    Depot,
    Instance,
    Metrics,
    Region,
    Route,
    SimulatorEvent,
    Station,
    SupplyArrival,
)

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)-8s %(name)s %(message)s",
)
logger = logging.getLogger(__name__)

DESCRIPTION = """
Backend for the **Fuel Supply Intelligence & Resilience Platform**.

It reads the BUP Fuel Supply Simulator over HTTP and exposes a stable,
validated view of the fuel network for the operator dashboard.
"""


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Open and close the simulator client and database pool."""
    settings = get_settings()
    app.state.settings = settings
    app.state.simulator = build_client(settings)
    app.state.database = default_database
    await app.state.database.connect()
    try:
        yield
    finally:
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


# -- health ----------------------------------------------------------------


async def _simulator_health(client: SimulatorClient) -> dict[str, Any]:
    """Check the simulator liveness endpoint (fault-immune by design)."""
    try:
        health = await client.health()
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
        },
    )


# -- state snapshot --------------------------------------------------------


@app.get("/api/state", tags=["state"])
async def get_state(
    request: Request,
    client: SimulatorClient = Depends(get_simulator),
) -> dict[str, Any]:
    """Return a consolidated snapshot of the simulated fuel network."""
    try:
        client.reset_stale()
        (
            instance,
            regions,
            depots,
            stations,
            routes,
            arrivals,
            events,
            allocations,
            metrics,
        ) = await _gather_state(client)
    except (SimulatorConnectionError, SimulatorTimeoutError) as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"simulator unreachable: {exc}",
        ) from exc
    except SimulatorValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"simulator returned malformed data: {exc}",
        ) from exc
    except SimulatorAPIError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"simulator error: {exc}",
        ) from exc

    snapshot: dict[str, Any] = {
        "instance": instance.model_dump(mode="json"),
        "regions": [r.model_dump(mode="json") for r in regions],
        "depots": [d.model_dump(mode="json") for d in depots],
        "stations": [s.model_dump(mode="json") for s in stations],
        "routes": [r.model_dump(mode="json") for r in routes],
        "supply_arrivals": [a.model_dump(mode="json") for a in arrivals],
        "events": [e.model_dump(mode="json") for e in events],
        "allocations": [a.model_dump(mode="json") for a in allocations],
        "metrics": metrics.model_dump(mode="json"),
        "stale": client.stale,
    }

    await _persist_snapshot(db=request.app.state.database, instance=instance, metrics=metrics)

    return snapshot


async def _gather_state(
    client: SimulatorClient,
) -> tuple[
    Instance,
    list[Region],
    list[Depot],
    list[Station],
    list[Route],
    list[SupplyArrival],
    list[SimulatorEvent],
    list[Allocation],
    Metrics,
]:
    """Fetch every section of the state snapshot in parallel."""
    import asyncio

    (
        instance,
        regions,
        depots,
        stations,
        routes,
        arrivals,
        events,
        allocations,
        metrics,
    ) = await asyncio.gather(
        client.get_instance(),
        client.get_regions(),
        client.get_depots(),
        client.get_stations(),
        client.get_routes(),
        client.get_supply_arrivals(),
        client.get_events(),
        client.get_allocations(),
        client.get_metrics(),
    )
    return (
        instance,
        regions,
        depots,
        stations,
        routes,
        arrivals,
        events,
        allocations,
        metrics,
    )


async def _persist_snapshot(db: Database, instance: Instance, metrics: Metrics) -> None:
    """Record the snapshot in Postgres; never fail the request on a DB error."""
    from .models import StateSnapshot

    try:
        async with db.session() as session:
            session.add(
                StateSnapshot(
                    sim_tick=instance.tick,
                    sim_time=instance.sim_time,
                    sim_status=instance.status.value,
                    scenario_id=instance.scenario_id,
                    service_level=metrics.service_level,
                    unmet_demand_liters=metrics.unmet_demand_liters,
                    served_demand_liters=metrics.served_demand_liters,
                    allocation_liters=metrics.allocation_liters,
                    allocation_failures=metrics.allocation_failures,
                    stale=False,
                    payload="{}",
                )
            )
            await session.commit()
    except Exception as exc:
        logger.warning("could not persist state snapshot: %s", exc)


@app.get("/", tags=["meta"])
async def root() -> dict[str, str]:
    """Service banner."""
    return {
        "service": get_settings().app_name,
        "version": get_settings().app_version,
        "docs": "/docs",
        "health": "/health",
        "state": "/api/state",
    }
