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
