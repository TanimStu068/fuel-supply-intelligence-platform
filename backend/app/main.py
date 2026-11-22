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

import json
import logging
import os
from contextlib import asynccontextmanager
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
    """Write one recommendation to the audit trail."""
    from .repository import write_recommendation

    database = default_database
    try:
        async with database.session() as session:
            await write_recommendation(
                session,
                recommendation=recommendation,
                simulator_response=json.dumps(recommendation.simulator_response or {}, default=str),
            )
            await session.commit()
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


@app.get("/api/recommendations", tags=["decision"])
async def get_recommendations(
    status: str | None = Query(default=None, description="Filter by status."),
    station_id: str | None = Query(default=None, description="Filter by station."),
    service: DecisionService = Depends(get_decision),
) -> dict[str, Any]:
    """Return the allocation recommendations, newest first.

    Each recommendation carries everything an operator needs to judge it:
    the station, fuel, depot and route, the quantity, the projected
    stockout before and after, the signals that drove it, the constraints
    that were considered, a confidence, and the alternatives that were
    ranked and rejected.
    """
    recommendations = service.list_recommendations(status=status, station_id=station_id)
    return {
        "count": len(recommendations),
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
