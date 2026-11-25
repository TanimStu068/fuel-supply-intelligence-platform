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

import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Path, Query, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .config import get_settings
from .db import Database
from .db import database as default_database
from .decision.service import DecisionError, DecisionService
from .intelligence.dispatcher import Dispatcher
from .intelligence.engine import IntelligenceEngine
from .intelligence.forecasting import ForecastSelector, ForecastSelectorConfig
from .intelligence.risk import RiskLevel
from .intelligence_service import IntelligenceService
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
    app.state.decision = DecisionService(
        app.state.simulator,
        repository=app.state.database,
        database=app.state.database,
        # Zero means unbounded — that is how the "before" side of the
        # load test's second bottleneck is reproduced.
        history_limit=settings.decision_history_limit,
    )
    app.state.intelligence = IntelligenceService(
        database=app.state.database,
        cache=app.state.cache,
        engine=IntelligenceEngine(
            selector=ForecastSelector(
                config=ForecastSelectorConfig(
                    min_confidence=settings.forecast_min_confidence,
                    min_training_rows=settings.forecast_min_training_rows,
                )
            ),
            horizon_ticks=settings.forecast_horizon_ticks,
        ),
        dispatcher=Dispatcher(
            app.state.simulator,
            enabled=settings.dispatch_enabled,
        ),
        decision=app.state.decision,
        client=app.state.simulator,
    )
    app.state.sync = SyncService(
        client=app.state.simulator,
        database=app.state.database,
        cache=app.state.cache,
        settings=settings,
        intelligence=app.state.intelligence,
    )

    await app.state.database.connect()

    try:
        if settings.sync_enabled:
            await app.state.sync.start()
        logger.info("backend is ready")
        yield
    finally:
        # Graceful shutdown, in the order the dependencies need:
        #
        # 1. Stop taking new work. The readiness flag makes the container
        #    report "not ready" while it drains, so an orchestrator stops
        #    routing new traffic here instead of cutting it off mid-request.
        # 2. Stop the background loops, so nothing starts a new database
        #    session or simulator call while we are closing.
        # 3. Close the database and the simulator client, so no connection
        #    is left dangling.
        #
        # The whole sequence is bounded by ``shutdown_grace_seconds`` so a
        # wedged dependency cannot hold the container open forever and make
        # Docker escalate to SIGKILL.
        app.state.shutting_down = True
        logger.info("backend is shutting down; draining")
        await app.state.sync.stop()
        try:
            await asyncio.wait_for(
                _close_dependencies(app),
                timeout=settings.shutdown_grace_seconds,
            )
        except TimeoutError:
            logger.warning(
                "shutdown grace period expired; closing anyway",
                extra={"grace_seconds": settings.shutdown_grace_seconds},
            )


async def _close_dependencies(app: FastAPI) -> None:
    """Close the database and the simulator client."""
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
        "http://localhost:5173,http://localhost:3000,http://127.0.0.1:5173,"
        # 4173 is the port the Playwright preview server uses.
        "http://localhost:4173,http://127.0.0.1:4173",
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


def get_intelligence(request: Request) -> IntelligenceService:
    """Return the process-wide intelligence service."""
    return request.app.state.intelligence


def get_decision(request: Request) -> DecisionService:
    """Return the process-wide decision service."""
    return request.app.state.decision


# -- request bodies ---------------------------------------------------------


class ApprovalRequest(BaseModel):
    """The body of an approval request."""

    #: Who approved it. The audit trail needs a name.
    decided_by: str = Field(default="operator", max_length=64)
    #: Whether to send it to the simulator straight away.
    submit: bool = True


class RejectionRequest(BaseModel):
    """The body of a rejection request."""

    decided_by: str = Field(default="operator", max_length=64)
    reason: str = Field(default="", max_length=500)


class DecisionSettingsRequest(BaseModel):
    """The body of a settings update."""

    #: The auto-approve mode. Off by default.
    auto_mode: bool = False


async def persist_decision(recommendation: Any) -> None:
    """Write one recommendation to the audit trail.

    This goes through the retrying write helper. The approval path writes
    at the same time as the sync service persists the decision history,
    and that pair is what deadlocks on Postgres; the helper retries the
    transaction, which is what the SQL standard says to do with a
    ``40001``.
    """
    from .repository import write_recommendation

    database = default_database

    async def work(session) -> None:
        await write_recommendation(
            session,
            recommendation=recommendation,
            simulator_response=json.dumps(recommendation.simulator_response or {}, default=str),
        )

    try:
        await database.write(work)
    except Exception as exc:
        logger.warning("could not persist the decision: %s", exc)


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


@app.get("/ready", tags=["ops"])
async def ready(request: Request) -> JSONResponse:
    """Report whether the backend is ready to take traffic.

    Readiness is deliberately *narrower* than liveness. The backend is
    alive as soon as the process is running, but it is only ready when it
    has a database connection and a simulator client, and it stops being
    ready the moment it starts draining at shutdown.

    The distinction matters during a rolling restart: Docker's healthcheck
    should keep failing while the container drains, so the orchestrator
    stops sending it traffic before the socket is closed.
    """
    shutting_down = bool(getattr(request.app.state, "shutting_down", False))

    database_ok = False
    try:
        database_ok = await request.app.state.database.ping()
    except Exception:
        database_ok = False

    ready_now = database_ok and not shutting_down

    return JSONResponse(
        status_code=status.HTTP_200_OK if ready_now else status.HTTP_503_SERVICE_UNAVAILABLE,
        content={
            "ready": ready_now,
            "shutting_down": shutting_down,
            "database": "ok" if database_ok else "error",
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


@app.get("/api/alerts", tags=["intelligence"])
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


# -- intelligence -----------------------------------------------------------


@app.get("/api/risk", tags=["intelligence"])
async def get_risk(
    station_id: str | None = Query(default=None),
    fuel_type: str | None = Query(default=None),
    service: IntelligenceService = Depends(get_intelligence),
) -> dict[str, Any]:
    """Return the risk assessment for every station and fuel.

    Each assessment carries the signals that produced it, so an operator can
    see *why* a station is red. When a forecast was produced by the
    rule-based fallback, the entry says so and is flagged for human review.
    """
    result = service.last_result
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="the intelligence layer has not evaluated the network yet",
        )

    risks = result.risks
    if station_id:
        risks = [risk for risk in risks if risk.station_id == station_id]
    if fuel_type:
        risks = [risk for risk in risks if risk.fuel_type == fuel_type.upper()]

    return {
        "evaluated_at": result.evaluated_at.isoformat(),
        "models_used": result.models_used,
        "active_model": result.active_model if hasattr(result, "active_model") else None,
        "needs_human_review_count": result.needs_human_review_count,
        "fallback_activations": result.fallback_activations,
        "summary": {
            level.value: sum(1 for risk in result.risks if risk.risk_level.value == level.value)
            for level in RiskLevel
        },
        "count": len(risks),
        "risks": [risk.to_payload() for risk in risks],
    }


@app.get("/api/forecast", tags=["intelligence"])
async def get_forecast(
    station_id: str | None = Query(default=None),
    fuel_type: str | None = Query(default=None),
    service: IntelligenceService = Depends(get_intelligence),
) -> dict[str, Any]:
    """Return the demand forecasts for every station and fuel."""
    result = service.last_result
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="the intelligence layer has not evaluated the network yet",
        )

    forecasts = result.forecasts
    if station_id:
        forecasts = [f for f in forecasts if f.station_id == station_id]
    if fuel_type:
        forecasts = [f for f in forecasts if f.fuel_type == fuel_type.upper()]

    return {
        "evaluated_at": result.evaluated_at.isoformat(),
        "models_used": result.models_used,
        "count": len(forecasts),
        "forecasts": [forecast.to_payload() for forecast in forecasts],
    }


@app.get("/api/anomalies", tags=["intelligence"])
async def get_anomalies(
    service: IntelligenceService = Depends(get_intelligence),
) -> dict[str, Any]:
    """Return the detected anomalies and bottlenecks."""
    result = service.last_result
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="the intelligence layer has not evaluated the network yet",
        )

    return {
        "evaluated_at": result.evaluated_at.isoformat(),
        "count": len(result.anomalies),
        "anomalies": [anomaly.to_payload() for anomaly in result.anomalies],
        "bottlenecks": [bottleneck.to_payload() for bottleneck in result.bottlenecks],
    }


@app.post("/api/model/train", tags=["intelligence"])
async def train_model(
    service: IntelligenceService = Depends(get_intelligence),
) -> dict[str, Any]:
    """Retrain the learned demand model on the stored history.

    Returns the training metadata, including the validation error, so the
    operator can see how good the model is before trusting it.
    """
    return await service.train_model()


@app.get("/api/decisions", tags=["intelligence"])
async def get_decisions(
    service: IntelligenceService = Depends(get_intelligence),
) -> dict[str, Any]:
    """Return the most recent dispatch decisions.

    The simulator never refills a station on its own, so these decisions are
    what actually move fuel. Each one carries the reason it was made.
    """
    decisions = service.last_decisions
    return {
        "count": len(decisions),
        "decisions": [decision.to_payload() for decision in decisions],
    }


@app.get("/api/model-status", tags=["intelligence"])
async def get_model_status(
    service: IntelligenceService = Depends(get_intelligence),
) -> dict[str, Any]:
    """Report which predictor is active and how well it is doing."""
    engine = service.engine
    selector = engine.selector
    learned = selector.learned.model

    return {
        "active_model": selector.active_model.value,
        "ml_kill_switch": selector.kill_switch,
        #: The injected prediction fault, if one is active. It is reported
        #: here because the operator's first question about a platform
        #: that suddenly stopped using the model is "is the model broken,
        #: or did somebody turn it off?" — and this is where the answer is.
        "prediction_fault": selector.learned.injected_fault,
        "fallback_reasons": selector.fallback_counts,
        "learned_model": (
            {
                "trained": True,
                "training_rows": learned.training_rows,
                "validation_mae_liters": round(learned.validation_mae, 4),
                "training_mae_liters": round(learned.training_mae, 4),
                "trained_at": learned.trained_at.isoformat(),
                # The one-step error, and the error over a whole forecast
                # horizon. The second one is the number that decides
                # whether the model is actually used.
                "baseline_validation_mae": round(
                    float(learned.metadata.get("baseline_validation_mae", 0.0)), 4
                ),
                "horizon_total_mae_liters": round(
                    float(learned.metadata.get("horizon_total_mae_liters", 0.0)), 1
                ),
                "baseline_horizon_total_mae_liters": round(
                    float(learned.metadata.get("baseline_horizon_total_mae_liters", 0.0)), 1
                ),
                "beats_baseline": bool(learned.metadata.get("beats_baseline", 0.0)),
                "beats_baseline_at_horizon": bool(
                    learned.metadata.get("beats_baseline_at_horizon", 0.0)
                ),
                **{key: round(value, 4) for key, value in learned.metadata.items()},
            }
            if learned
            else {"trained": False}
        ),
    }


class KillSwitchRequest(BaseModel):
    """The body of a kill-switch update."""

    #: Turn the learned model on (True) or off (False).
    enabled: bool


@app.post("/api/model/kill-switch", tags=["intelligence"])
async def set_kill_switch(
    body: KillSwitchRequest,
    service: IntelligenceService = Depends(get_intelligence),
) -> dict[str, Any]:
    """Turn the learned demand model on or off.

    The **kill-switch**. With it on, the platform stops using the learned
    model entirely: every forecast comes from the rule-based predictor, every
    forecast is flagged for human review, and the decision layer uses the
    days-of-cover fallback policy.

    It exists because a model can look healthy on every metric the platform
    tracks and still be producing bad numbers for *this* network. When that
    happens an operator needs to stop the model *now* — without a redeploy,
    and without waiting for a confidence score to decay.
    """
    enabled = service.set_kill_switch(body.enabled)
    return {
        "ml_kill_switch": enabled,
        "active_model": service.engine.selector.active_model.value,
        "effect": (
            "the learned model is disabled; every forecast uses the rule-based "
            "predictor and is flagged for human review"
            if enabled
            else "the learned model may be used again, subject to the normal confidence checks"
        ),
    }


class PredictionFaultRequest(BaseModel):
    """The body of a prediction-fault update."""

    #: The fault to inject: ``"predict_error"``, or ``None`` to clear it.
    fault: str | None = None


@app.post("/api/model/fault", tags=["intelligence"])
async def set_prediction_fault(
    body: PredictionFaultRequest,
    service: IntelligenceService = Depends(get_intelligence),
) -> dict[str, Any]:
    """Break the learned demand model on purpose, or repair it.

    The prediction component is a **module inside the backend**, so there
    is no container to stop and no service to take down. The only honest
    way to show that the fallback takes over when the model *fails* —
    rather than when an operator has politely switched it off — is to
    break it on demand, on a running stack.

    ``"predict_error"`` makes the model raise the way a broken model
    raises. ``null`` clears the fault.

    This is the counterpart to the kill-switch, and the two are not the
    same. The kill-switch is a *decision*: the model is healthy and has
    been told not to be used. This is a *fault*: the model is broken and
    the platform has to notice on its own. Both must end up in the same
    place — the rule-based predictor, with every forecast flagged for a
    human — and the difference is in the reason the forecast carries.
    """
    fault = service.inject_prediction_fault(body.fault)
    return {
        "prediction_fault": fault,
        "active_model": service.engine.selector.active_model.value,
        "effect": (
            "the learned model will raise on every forecast; every forecast "
            "falls back to the rule-based predictor and is flagged for human review"
            if fault
            else "the injected fault is cleared; the learned model is used again, "
            "subject to the normal confidence checks"
        ),
    }


@app.get("/api/recommendations", tags=["decision"])
async def get_recommendations(
    status: str | None = Query(default=None, description="Filter by status."),
    station_id: str | None = Query(default=None, description="Filter by station."),
    limit: int | None = Query(default=None, ge=1, le=1000, description="How many to return."),
    service: DecisionService = Depends(get_decision),
) -> dict[str, Any]:
    """Return the allocation recommendations, newest first.

    Each recommendation carries everything an operator needs to judge it:
    the station, fuel, depot and route, the quantity, the projected
    stockout before and after, the signals that drove it, the constraints
    that were considered, a confidence, and the alternatives that were
    ranked and rejected.

    The list is **capped**. The dashboard polls this endpoint every few
    seconds, and a response that grows with the life of the process
    eventually stops being served at all. The full history is in
    ``/api/decision-history``, which reads it from Postgres.
    """
    page = limit if limit is not None else service.page_limit
    recommendations = service.list_recommendations(status=status, station_id=station_id, limit=page)
    return {
        "count": len(recommendations),
        "total": service.recommendation_count,
        "auto_mode": service.auto_mode,
        "fallback_reason": service.fallback_reason,
        "recommendations": [item.to_payload() for item in recommendations],
    }


@app.get("/api/recommendations/{recommendation_id}", tags=["decision"])
async def get_recommendation(
    recommendation_id: str = Path(..., description="The recommendation's id."),
    service: DecisionService = Depends(get_decision),
) -> dict[str, Any]:
    """Return one recommendation."""
    recommendation = service.get(recommendation_id)
    if recommendation is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"no recommendation with id {recommendation_id}",
        )
    return recommendation.to_payload()


@app.post("/api/recommendations/{recommendation_id}/simulate", tags=["decision"])
async def simulate_recommendation(
    recommendation_id: str = Path(..., description="The recommendation's id."),
    service: DecisionService = Depends(get_decision),
) -> dict[str, Any]:
    """Project what would happen if this recommendation were approved.

    The projection is **local**. Nothing is written to the simulator and
    the recommendation's status does not change — this is the "simulate"
    step of the brief's loop, before the "act" step.
    """
    try:
        return service.simulate(recommendation_id)
    except DecisionError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


@app.post("/api/recommendations/{recommendation_id}/approve", tags=["decision"])
async def approve_recommendation(
    recommendation_id: str = Path(..., description="The recommendation's id."),
    body: ApprovalRequest | None = None,
    service: DecisionService = Depends(get_decision),
) -> dict[str, Any]:
    """Approve a recommendation and submit it to the simulator.

    The submission uses the recommendation's deterministic idempotency key,
    so approving twice is a **replay** rather than a second shipment.
    Every 409 the simulator can return is handled and explained.
    """
    payload = body or ApprovalRequest()
    try:
        recommendation = await service.approve(
            recommendation_id,
            decided_by=payload.decided_by,
            submit=payload.submit,
        )
    except DecisionError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    await persist_decision(recommendation)
    return recommendation.to_payload()


@app.post("/api/recommendations/{recommendation_id}/reject", tags=["decision"])
async def reject_recommendation(
    recommendation_id: str = Path(..., description="The recommendation's id."),
    body: RejectionRequest | None = None,
    service: DecisionService = Depends(get_decision),
) -> dict[str, Any]:
    """Reject a recommendation. Nothing is sent to the simulator."""
    payload = body or RejectionRequest()
    try:
        recommendation = service.reject(
            recommendation_id,
            decided_by=payload.decided_by,
            reason=payload.reason,
        )
    except DecisionError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    await persist_decision(recommendation)
    return recommendation.to_payload()


@app.post("/api/recommendations/propose", tags=["decision"])
async def propose_recommendations(
    service: IntelligenceService = Depends(get_intelligence),
    decision: DecisionService = Depends(get_decision),
) -> dict[str, Any]:
    """Ask the engine for recommendations right now.

    The background sync proposes on every cycle; this endpoint forces a
    fresh pass, which is what the operator dashboard calls after a change.

    It goes through the same shared path as the background sync, so it picks
    the same policy: the model-driven optimizer when the intelligence layer
    has usable forecasts, and the rule-based fallback when it does not.
    """
    state = service.cache_state()
    if state is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="the platform has no state yet; the simulator is not reachable",
        )

    recommendations = await service.propose_now()

    return {
        "count": len(recommendations),
        "used_fallback_policy": decision.fallback_reason is not None,
        "recommendations": [item.to_payload() for item in recommendations],
    }


@app.get("/api/decision-settings", tags=["decision"])
async def get_decision_settings(
    service: DecisionService = Depends(get_decision),
) -> dict[str, Any]:
    """Report the decision engine's settings and counters."""
    counters = service.counters
    proposed = counters.get("proposed", 0)
    approved = counters.get("approved", 0)
    return {
        "auto_mode": service.auto_mode,
        "fallback_reason": service.fallback_reason,
        "counters": counters,
        "approval_rate": round(approved / proposed, 4) if proposed else 0.0,
        "conflicts_by_code": service.conflicts_by_code,
        "policy": {
            "approach": "priority-greedy",
            "target_cover_hours": service.optimizer.config.target_cover_hours,
            "min_shipment_liters": service.optimizer.config.min_shipment_liters,
            "max_alternatives": service.optimizer.config.max_alternatives,
        },
        "fallback_policy": {
            "approach": "lowest days-of-cover first",
            "used": service.fallback_reason is not None,
        },
    }


@app.post("/api/decision-settings", tags=["decision"])
async def update_decision_settings(
    body: DecisionSettingsRequest,
    service: DecisionService = Depends(get_decision),
) -> dict[str, Any]:
    """Turn the auto-approve mode on or off.

    The auto-mode is **off by default**. When it is on it still only
    approves a narrow class of action: a WATCH-level station, a confident
    forecast, no human-review flag, and every constraint satisfied.
    """
    service.set_auto_mode(body.auto_mode)
    return {
        "auto_mode": service.auto_mode,
        "counters": service.counters,
    }


@app.get("/api/decision-history", tags=["decision"])
async def get_decision_history(
    limit: int = Query(default=50, ge=1, le=500),
    status: str | None = Query(default=None),
    station_id: str | None = Query(default=None),
    database: Database = Depends(get_database),
) -> dict[str, Any]:
    """The decision history from Postgres — the audit trail."""
    from .repository import read_recommendations

    try:
        async with database.session() as session:
            rows = await read_recommendations(
                session, limit=limit, status=status, station_id=station_id
            )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"could not read the decision history: {exc}",
        ) from exc

    return {
        "count": len(rows),
        "decisions": [
            {
                "recommendation_id": row.recommendation_id,
                "station_id": row.station_id,
                "fuel_type": row.fuel_type,
                "source_depot_id": row.source_depot_id,
                "route_id": row.route_id,
                "quantity": row.quantity,
                "status": row.status,
                "risk_level": row.risk_level,
                "confidence": row.confidence,
                "decided_by": row.decided_by,
                "decided_at": row.decided_at.isoformat() if row.decided_at else None,
                "allocation_id": row.allocation_id,
                "error_code": row.error_code,
                "rationale": row.rationale,
                "sim_tick": row.sim_tick,
                "created_at": row.created_at.isoformat() if row.created_at else None,
            }
            for row in rows
        ],
    }


@app.get("/api/audit", tags=["audit"])
async def search_audit(
    q: str | None = Query(default=None, description="Full-text search over the trail"),
    source: str | None = Query(default=None, description="Comma-separated: decision, event, alert"),
    kind: str | None = Query(default=None, description="Comma-separated kinds"),
    status: str | None = Query(default=None, description="Comma-separated statuses"),
    entity_id: str | None = Query(default=None),
    since: datetime | None = Query(default=None),
    until: datetime | None = Query(default=None),
    limit: int = Query(default=200, ge=1, le=1000),
    database: Database = Depends(get_database),
) -> dict[str, Any]:
    """Search the unified audit trail.

    **One search over three trails.** The platform writes decisions, domain
    events and alerts to three separate tables. This endpoint presents them
    as one stream, so an operator can ask "what happened to this station?"
    once instead of joining three tables by hand.

    Every filter is optional and they compose:

    ```bash
    # Everything about one station
    /api/audit?entity_id=station-karnaphuli

    # Every critical alert in the last hour
    /api/audit?source=alert&status=critical&since=2026-11-24T10:00:00Z

    # Every mention of "route" in any trail
    /api/audit?q=route
    ```

    The response carries the entries **and** a summary, so a search can be
    read at a glance ("312 alerts, 47 of them critical") before drilling
    into individual rows.
    """
    from .audit import read_audit_entries, summarise_audit_entries

    def _split(value: str | None) -> list[str] | None:
        if not value:
            return None
        parts = [part.strip() for part in value.split(",") if part.strip()]
        return parts or None

    try:
        async with database.session() as session:
            # The full match is read first, so the summary describes the
            # whole result set rather than the page that is returned.
            # Without this, a trail with 20,000 alerts and 300 decisions
            # would report "300 decisions" as zero, because the newest
            # page is all alerts.
            everything = await read_audit_entries(
                session,
                sources=_split(source),
                kinds=_split(kind),
                statuses=_split(status),
                entity_id=entity_id,
                text=q,
                since=since,
                until=until,
                limit=1000,
            )
            summary = await summarise_audit_entries(session, everything)
            entries = everything[:limit]
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"could not read the audit trail: {exc}",
        ) from exc

    return {
        "count": len(entries),
        "total_matched": summary["count"],
        "summary": summary,
        "entries": [entry.to_dict() for entry in entries],
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


# ---------------------------------------------------------------------------
# Simulation control and crisis injection.
#
# These drive the *simulator*, not the platform. They exist so an operator
# can run the crisis drills from the dashboard. Every response is labelled
# `simulation_only` so the dashboard can say so on screen.
# ---------------------------------------------------------------------------


class CrisisRequest(BaseModel):
    """The body of a crisis-injection request."""

    #: Which crisis to inject. One of the simulator's six event types.
    type: str = Field(min_length=1, max_length=40)
    #: How long the crisis lasts, in ticks.
    duration_ticks: int = Field(default=8, ge=1, le=200)
    #: The crisis's parameters, passed through to the simulator.
    parameters: dict[str, Any] = Field(default_factory=dict)


@app.get("/api/simulation", tags=["simulation"])
async def get_simulation_control(
    client: SimulatorClient = Depends(get_simulator),
    cache: StateCache = Depends(get_cache),
) -> dict[str, Any]:
    """Report what the simulation can currently be told to do.

    The dashboard's controls need to know whether the simulation is
    running before it offers a "Run" button, and what tick it is at.
    """
    state = cache.state
    instance = state.instance if state else None
    return {
        "simulation_only": True,
        "status": instance.status if instance else "UNKNOWN",
        "tick": instance.tick if instance else None,
        "sim_time": instance.sim_time.isoformat() if instance and instance.sim_time else None,
        "tick_minutes": instance.tick_minutes if instance else None,
        "scenario_id": instance.scenario_id if instance else None,
        "actions": ["run", "pause", "toggle", "step", "reset"],
    }


@app.post("/api/simulation/crisis", tags=["simulation"])
async def inject_crisis(
    request: CrisisRequest,
    client: SimulatorClient = Depends(get_simulator),
) -> dict[str, Any]:
    """Inject a crisis event into the simulation.

    The six types are the simulator's own (guide §7.8): demand spike, route
    disruption, station outage, depot constraint, shipment delay and supply
    shortfall.
    """
    from .simulator_models import EventCreate

    try:
        body = EventCreate.model_validate(
            {
                "type": request.type,
                "start_tick": await _current_tick(client),
                "duration_ticks": request.duration_ticks,
                "parameters": request.parameters,
            }
        )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"invalid crisis: {exc}",
        ) from exc

    try:
        event = await client.admin_inject_event(body)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"the simulator refused the crisis: {exc}",
        ) from exc

    return {
        "simulation_only": True,
        "event": event.model_dump(mode="json") if hasattr(event, "model_dump") else dict(event),
    }


@app.post("/api/simulation/{action}", tags=["simulation"])
async def control_simulation(
    action: str,
    client: SimulatorClient = Depends(get_simulator),
) -> dict[str, Any]:
    """Run, pause, toggle, step or reset the simulation.

    This is the *simulator's* control surface, reached through the backend
    so the dashboard never talks to the simulator directly. It changes the
    simulated world and nothing else — the platform's own state is
    re-synced from the simulator on the next cycle.
    """
    actions = {
        "run": client.admin_run,
        "pause": client.admin_pause,
        "toggle": client.admin_toggle,
        "step": client.admin_step,
        "reset": client.admin_reset,
    }
    handler = actions.get(action)
    if handler is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"unknown simulation action: {action}",
        )

    try:
        result = await handler()
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"the simulator refused to {action}: {exc}",
        ) from exc

    payload = result.model_dump(mode="json") if hasattr(result, "model_dump") else dict(result)
    return {"simulation_only": True, "action": action, "result": payload}


async def _current_tick(client: SimulatorClient) -> int:
    """The simulator's current tick, or 0 when it cannot be read."""
    try:
        instance = await client.get_instance()
    except Exception:
        return 0
    return instance.tick


def _cached_simulator_health(
    cache: StateCache,
    sync_status: dict[str, Any],
) -> dict[str, Any]:
    """The simulator's state, read from the sync cache rather than probed.

    The cached instance still says which tick the world is at and whether
    it is running, which is what the panel needs; the sync status says
    whether the last refresh actually reached the simulator.
    """
    state = cache.state
    instance = state.instance if state else None

    if sync_status.get("degraded"):
        return {
            "status": "unreachable",
            "error": cache.degraded_reason or "the simulator is not answering",
            "simulation": {
                "status": str(instance.status) if instance else "UNKNOWN",
                "tick": instance.tick if instance else 0,
            },
        }

    return {
        "status": "ok",
        "database": "ok",
        "simulation": {
            "status": str(instance.status) if instance else "UNKNOWN",
            "tick": instance.tick if instance else 0,
        },
    }


@app.get("/api/service-health", tags=["ops"])
async def service_health(
    request: Request,
    client: SimulatorClient = Depends(get_simulator),
    db: Database = Depends(get_database),
    cache: StateCache = Depends(get_cache),
) -> dict[str, Any]:
    """The service health panel: every dependency, in one payload.

    The dashboard shows this as its "service health" panel: the backend,
    the database, the simulator, the prediction service, the decision
    engine, plus the request latency and error rate the Prometheus
    registry holds.

    The simulator's state is read from the **sync cache**, not from a
    fresh probe. The dashboard polls this endpoint every few seconds; a
    live probe would block for the whole simulator timeout each time
    the simulator is slow, and the panel would be the slowest thing on
    the page. The cache already knows whether the last sync succeeded.
    """
    sync = get_sync(request)
    sync_status = sync.status()
    simulator_status = _cached_simulator_health(cache, sync_status)
    database_status = await _database_health(db)

    decision: DecisionService | None = None
    try:
        decision = request.app.state.decision
    except AttributeError:
        decision = None

    intelligence = (
        request.app.state.intelligence if hasattr(request.app.state, "intelligence") else None
    )
    last_result = intelligence.last_result if intelligence is not None else None
    active_model = last_result.active_model.value if last_result is not None else "unknown"

    counters = decision.counters if decision is not None else {}
    proposed = counters.get("proposed", 0)
    approved = counters.get("approved", 0)

    return {
        "components": {
            "backend": {"status": "ok"},
            "database": database_status,
            "simulator": simulator_status,
            "prediction_service": {
                "status": "ok" if last_result is not None else "degraded",
                "active_model": active_model,
                "ml_kill_switch": intelligence.kill_switch if intelligence is not None else False,
                "evaluated_at": last_result.evaluated_at.isoformat()
                if last_result is not None and last_result.evaluated_at
                else None,
                "forecast_count": len(last_result.forecasts) if last_result is not None else 0,
            },
            "decision_engine": {
                "status": "ok" if decision is not None else "degraded",
                "auto_mode": decision.auto_mode if decision is not None else False,
                "fallback_active": decision.fallback_reason is not None
                if decision is not None
                else False,
                "fallback_reason": decision.fallback_reason if decision is not None else None,
                "counters": counters,
                "approval_rate": round(approved / proposed, 4) if proposed else 0.0,
                "decisions_per_minute": decision.decisions_per_minute
                if decision is not None
                else 0.0,
            },
        },
        # The circuit breaker is not a dependency — it is the mechanism the
        # platform uses to protect one. It sits beside the components rather
        # than among them, so "which things do we depend on" stays a
        # clean, five-item answer.
        "circuit_breaker": _circuit_breaker_snapshot(request),
        "http": _http_metrics(),
    }


def _circuit_breaker_snapshot(request: Request) -> dict[str, Any]:
    """The simulator circuit breaker's state, for the health panel.

    An open breaker is not an outage by itself — it is the platform
    *protecting* the simulator from a retry storm. The panel shows it so an
    operator can tell "the simulator is down" apart from "the platform has
    stopped calling because the simulator was down".
    """
    try:
        client = request.app.state.simulator
    except AttributeError:
        return {"status": "unknown"}
    breaker = client.breaker
    snapshot = breaker.snapshot()
    snapshot["status"] = breaker.state.value
    return snapshot


def _http_metrics() -> dict[str, Any]:
    """The request latency and error rate, read from the Prometheus registry.

    The dashboard shows p95 latency and the error rate; both come from the
    same registry Prometheus scrapes, so the panel and the Grafana
    dashboard cannot disagree.
    """
    from .metrics import HTTP_REQUEST_DURATION, HTTP_REQUESTS

    # The histogram's buckets are *cumulative counts* per upper bound, so
    # the 95th percentile is the largest boundary whose cumulative count
    # reaches 95% of the observations. Summing the buckets instead would
    # give the observation count, not a latency.
    buckets: list[tuple[float, float]] = []
    observations = 0
    for metric in HTTP_REQUEST_DURATION.collect():
        for sample in metric.samples:
            if sample.name.endswith("_count"):
                observations += sample.value
            elif sample.name.endswith("_bucket"):
                try:
                    boundary = float(sample.labels.get("le", "+Inf"))
                except ValueError:
                    continue
                buckets.append((boundary, sample.value))

    p95 = 0.0
    if buckets and observations > 0:
        buckets.sort()
        target = observations * 0.95
        cumulative = 0.0
        for boundary, count in buckets:
            cumulative += count
            if cumulative >= target:
                p95 = boundary
                break

    errors = 0
    total = 0
    for metric in HTTP_REQUESTS.collect():
        for sample in metric.samples:
            if sample.name.endswith("_total"):
                total += sample.value
                if sample.labels.get("status", "").startswith("5"):
                    errors += sample.value

    return {
        "p95_latency_seconds": round(p95, 6),
        "request_count": int(total),
        "error_count": int(errors),
        "error_rate": round(errors / total, 6) if total else 0.0,
    }
