"""Prometheus instrumentation for the backend.

Two groups of series:

**Application** — request rate, latency histogram and error rate for the
backend's own HTTP surface.

**Integration** — what happens between the platform and the simulator:
call outcomes and latency, SSE reconnects, stale responses, degraded-mode
activations and the age of the last successful sync.

The Grafana "Fuel Platform Overview" dashboard consumes these.
"""

from __future__ import annotations

import time

from fastapi import FastAPI, Request, Response
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest

#: HTTP requests, labelled by method, path and status code.
HTTP_REQUESTS = Counter(
    "http_requests_total",
    "Total HTTP requests handled by the backend.",
    ["method", "path", "status"],
)

#: HTTP request latency in seconds.
HTTP_REQUEST_DURATION = Histogram(
    "http_request_duration_seconds",
    "Backend HTTP request latency in seconds.",
    ["method", "path"],
)

#: Calls made to the simulator, labelled by outcome.
SIMULATOR_REQUESTS = Counter(
    "simulator_requests_total",
    "Requests issued to the fuel supply simulator, by outcome.",
    ["outcome"],
)

#: Simulator call latency in seconds.
SIMULATOR_REQUEST_DURATION = Histogram(
    "simulator_request_duration_seconds",
    "Latency of calls to the fuel supply simulator.",
    ["endpoint"],
)

#: Number of responses the simulator marked stale.
SIMULATOR_STALE_RESPONSES = Counter(
    "simulator_stale_responses_total",
    "Responses carrying the X-Simulator-Stale header.",
)

#: Calls the circuit breaker refused without sending them.
CIRCUIT_CALLS_REFUSED = Counter(
    "circuit_calls_refused_total",
    "Calls refused by a circuit breaker without being sent.",
    ["circuit"],
)

#: Calls the circuit breaker let through.
CIRCUIT_CALLS_ALLOWED = Counter(
    "circuit_calls_allowed_total",
    "Calls a circuit breaker let through.",
    ["circuit"],
)

#: Transitions between the breaker's states, by the state it moved to.
CIRCUIT_STATE_TRANSITIONS = Counter(
    "circuit_state_transitions_total",
    "Circuit breaker state transitions, by the new state.",
    ["circuit", "state"],
)

#: Whether each circuit breaker is currently open.
CIRCUIT_OPEN = Gauge(
    "circuit_open",
    "1 when the circuit breaker is open, 0 otherwise.",
    ["circuit"],
)

#: Simulator payloads rejected because they did not match the contract.
SIMULATOR_VALIDATION_REJECTIONS = Counter(
    "simulator_validation_rejections_total",
    "Simulator responses rejected because they did not match the documented schema.",
    ["endpoint"],
)

#: Sync cycles, labelled by outcome (success / failed).
SYNC_CYCLES = Counter(
    "sync_cycles_total",
    "Background sync cycles, by outcome.",
    ["outcome"],
)

#: How long one full sync cycle takes.
SYNC_CYCLE_DURATION = Histogram(
    "sync_cycle_duration_seconds",
    "Duration of a full background sync cycle.",
)

#: Sync cycles that were skipped because another cycle was already
#: running. A high count means the simulator's tick rate is outrunning the
#: platform's ability to read the world, and the coalescing is what keeps
#: the simulator's connection pool alive.
SYNC_CYCLES_COALESCED = Counter(
    "sync_cycles_coalesced_total",
    "Sync requests collapsed into a cycle that was already running.",
)

#: SSE connections opened against the simulator stream.
SSE_CONNECTIONS = Counter(
    "sse_connections_total",
    "SSE connections opened to the simulator event stream.",
)

#: SSE reconnects after a drop or a failure.
SSE_RECONNECTS = Counter(
    "sse_reconnects_total",
    "SSE reconnect attempts, by reason.",
    ["reason"],
)

#: SSE events received, by event name.
SSE_EVENTS = Counter(
    "sse_events_total",
    "SSE events received from the simulator, by event name.",
    ["event"],
)

#: Times the cached state was served in degraded mode.
DEGRADED_MODE_ACTIVATIONS = Counter(
    "degraded_mode_activations_total",
    "Times the backend served last-known state because the simulator was unreachable.",
)

#: Whether the backend is currently in degraded mode.
DEGRADED_MODE = Gauge(
    "degraded_mode",
    "1 when the backend is serving last-known state, 0 otherwise.",
)

#: Whether the cached state is currently marked stale.
STALE_DATA = Gauge(
    "stale_data",
    "1 when the cached state is marked stale, 0 otherwise.",
)

#: Age in seconds of the last successful sync.
LAST_SYNC_AGE = Gauge(
    "last_sync_age_seconds",
    "Seconds since the last successful sync with the simulator.",
)

#: Demand observations ingested, by whether the row was new or a duplicate.
DEMAND_INGESTED = Counter(
    "demand_observations_ingested_total",
    "Demand observations written to Postgres, by outcome.",
    ["outcome"],
)

#: Mean absolute error of the demand forecast, in litres, by model.
PREDICTION_ERROR_MAE = Gauge(
    "prediction_error_mae_liters",
    "Mean absolute error of the demand forecast against the observed demand, by model.",
    ["model"],
)

#: How many predictions landed inside their confidence interval.
PREDICTION_INTERVAL_COVERAGE = Gauge(
    "prediction_interval_coverage_ratio",
    "Share of predictions whose actual demand fell inside the confidence interval.",
    ["model"],
)

#: The mean confidence of the forecasts that were produced.
MODEL_CONFIDENCE = Gauge(
    "model_confidence",
    "Mean confidence of the forecasts produced by the active model.",
    ["model"],
)

#: How many forecasts were produced, by the model that produced them.
FORECASTS_PRODUCED = Counter(
    "forecasts_produced_total",
    "Forecasts produced, by the model that produced them.",
    ["model"],
)

#: How many risk assessments landed in each band.
RISK_ASSESSMENTS = Counter(
    "risk_assessments_total",
    "Risk assessments produced, by risk level.",
    ["level"],
)

#: How many anomalies were detected, by kind.
ANOMALIES_DETECTED = Counter(
    "anomalies_detected_total",
    "Anomalies detected, by kind.",
    ["kind"],
)

#: How many bottlenecks were detected, by kind.
BOTTLENECKS_DETECTED = Counter(
    "bottlenecks_detected_total",
    "Bottlenecks detected, by kind.",
    ["kind"],
)

#: How many times the rule-based fallback engaged, by reason.
FALLBACK_ACTIVATIONS = Counter(
    "fallback_activations_total",
    "Times the rule-based predictor replaced the learned model, by reason.",
    ["reason"],
)

#: How many forecasts were flagged for human review.
NEEDS_HUMAN_REVIEW = Counter(
    "needs_human_review_total",
    "Forecasts and assessments flagged for human review, by kind.",
    ["kind"],
)

#: How many allocation recommendations the engine has proposed.
DECISIONS_PROPOSED = Gauge(
    "decisions_proposed_total",
    "Allocation recommendations proposed by the decision engine.",
)

#: How many recommendations a human has approved.
DECISIONS_APPROVED = Gauge(
    "decisions_approved_total",
    "Allocation recommendations approved by an operator.",
)

#: How many recommendations a human has rejected.
DECISIONS_REJECTED = Gauge(
    "decisions_rejected_total",
    "Allocation recommendations rejected by an operator.",
)

#: How many recommendations were submitted to the simulator.
DECISIONS_SUBMITTED = Gauge(
    "decisions_submitted_total",
    "Allocation recommendations submitted to the simulator.",
)

#: How many recommendations the auto-mode approved on its own.
DECISIONS_AUTO_APPROVED = Gauge(
    "decisions_auto_approved_total",
    "Allocation recommendations approved by the auto-mode, without a human.",
)

#: How many submissions the simulator refused with a 409.
ALLOCATION_CONFLICTS = Gauge(
    "allocation_conflicts_total",
    "Allocation submissions the simulator refused with a conflict.",
)

#: The 409 codes the simulator returned, by code.
ALLOCATION_CONFLICTS_BY_CODE = Gauge(
    "allocation_conflicts_by_code_total",
    "Allocation submissions the simulator refused, by error code.",
    ["code"],
)

#: The share of proposed recommendations that a human approved.
DECISION_APPROVAL_RATE = Gauge(
    "decision_approval_rate",
    "Share of proposed allocation recommendations that were approved.",
)

#: How many times the decision layer fell back to the rule-based policy.
DECISION_FALLBACK_ACTIVATIONS = Counter(
    "decision_fallback_activations_total",
    "Times the decision engine used the rule-based fallback policy, by reason.",
    ["reason"],
)

#: Whether the learned model is disabled by the kill-switch.
ML_MODEL_DISABLED = Gauge(
    "ml_model_disabled",
    "1 when the ML kill-switch has disabled the learned model, 0 otherwise.",
)

#: How many times the kill-switch was flipped.
ML_KILL_SWITCH_ACTIVATIONS = Counter(
    "ml_kill_switch_activations_total",
    "Times the ML kill-switch was flipped, by the new state.",
    ["enabled"],
)

#: How many forecasts the kill-switch pushed onto the rule-based fallback.
ML_KILL_SWITCH_FALLBACKS = Counter(
    "ml_kill_switch_fallbacks_total",
    "Forecasts served by the rule-based predictor because the kill-switch was on.",
)

#: How many decisions were made in the last minute.
DECISIONS_PER_MINUTE = Gauge(
    "decisions_per_minute",
    "Allocation recommendations proposed in the last minute.",
)


def install_metrics_middleware(app: FastAPI) -> None:
    """Attach the metrics middleware and the ``/metrics`` route to an app."""
    from starlette.middleware.base import BaseHTTPMiddleware

    class _MetricsMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next):
            route = request.scope.get("route")
            path = getattr(route, "path", request.url.path)
            method = request.method
            started = time.perf_counter()
            status = "500"
            try:
                response = await call_next(request)
                status = str(response.status_code)
                return response
            finally:
                HTTP_REQUESTS.labels(method=method, path=path, status=status).inc()
                HTTP_REQUEST_DURATION.labels(method=method, path=path).observe(
                    time.perf_counter() - started
                )

    app.add_middleware(_MetricsMiddleware)
    app.add_api_route(
        "/metrics",
        _metrics_response,
        methods=["GET"],
        include_in_schema=False,
    )


def _metrics_response() -> Response:
    """Return the Prometheus exposition payload."""
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)
