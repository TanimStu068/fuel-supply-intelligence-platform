"""Prometheus instrumentation for the backend.

Exposes request-rate, latency and error-rate series plus counters for
simulator outcomes (success / fault-injected / error), which is what the
Grafana "Fuel Platform Overview" dashboard consumes.
"""

from __future__ import annotations

import time

from fastapi import FastAPI, Request, Response
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest

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
