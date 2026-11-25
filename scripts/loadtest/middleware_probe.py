#!/usr/bin/env python3
"""Compare the metrics middleware's cost, BaseHTTPMiddleware against pure ASGI.

The platform's own profiler showed the metrics middleware's plumbing in the
request path's self-time. ``BaseHTTPMiddleware`` runs every request through
a task group and two memory object streams; a pure ASGI middleware does
neither.
"""

from __future__ import annotations

import asyncio
import time

import httpx
from fastapi import FastAPI
from prometheus_client import CollectorRegistry, Counter, Histogram
from starlette.middleware.base import BaseHTTPMiddleware

REQUESTS = Counter(
    "bench_requests_total", "requests", ["method", "path", "status"]
)
DURATION = Histogram(
    "bench_request_duration_seconds", "duration", ["method", "path"]
)


def build(with_base_middleware: bool) -> FastAPI:
    app = FastAPI()

    if with_base_middleware:

        class _Metrics(BaseHTTPMiddleware):
            async def dispatch(self, request, call_next):
                route = request.scope.get("route")
                path = getattr(route, "path", request.url.path)
                started = time.perf_counter()
                status = "500"
                try:
                    response = await call_next(request)
                    status = str(response.status_code)
                    return response
                finally:
                    REQUESTS.labels(
                        method=request.method, path=path, status=status
                    ).inc()
                    DURATION.labels(method=request.method, path=path).observe(
                        time.perf_counter() - started
                    )

        app.add_middleware(_Metrics)
    else:

        class _MetricsASGI:
            """The same counters, with no task group and no streams."""

            def __init__(self, app):
                self.app = app

            async def __call__(self, scope, receive, send):
                if scope["type"] != "http":
                    await self.app(scope, receive, send)
                    return
                path = scope.get("path", "unknown")
                method = scope.get("method", "GET")
                started = time.perf_counter()
                status = "500"

                async def send_wrapper(message):
                    nonlocal status
                    if message["type"] == "http.response.start":
                        status = str(message["status"])
                    await send(message)

                try:
                    await self.app(scope, receive, send_wrapper)
                finally:
                    REQUESTS.labels(
                        method=method, path=path, status=status
                    ).inc()
                    DURATION.labels(method=method, path=path).observe(
                        time.perf_counter() - started
                    )

        app.add_middleware(_MetricsASGI)

    @app.get("/api/stations")
    async def stations() -> dict:
        return {"stations": [{"id": f"s{i}", "name": f"S{i}"} for i in range(12)]}

    return app


async def bench(app: FastAPI, seconds: float = 4.0) -> float:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://probe", timeout=10.0
    ) as client:
        for _ in range(20):
            await client.get("/api/stations")
        started = time.perf_counter()
        count = 0
        while time.perf_counter() - started < seconds:
            await client.get("/api/stations")
            count += 1
        return count / (time.perf_counter() - started)


async def main() -> None:
    print(f"{'middleware':32s} {'rps':>9s} {'us/req':>9s}")
    print("-" * 54)
    for label, use_base in (("BaseHTTPMiddleware", True), ("pure ASGI", False)):
        rps = await bench(build(use_base))
        print(f"{label:32s} {rps:9.1f} {1e6 / rps:9.1f}")

    _ = CollectorRegistry


asyncio.run(main())
