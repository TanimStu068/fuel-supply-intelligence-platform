#!/usr/bin/env python3
"""Isolate the framework cost of one request.

The platform's read path serves cached state, and it spends about 1.2 ms of
CPU on a request that returns a kilobyte. This measures where that goes,
one layer at a time, on the same FastAPI and uvicorn the platform runs.
"""

from __future__ import annotations

import asyncio
import time

import httpx
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.middleware.base import BaseHTTPMiddleware


class Station(BaseModel):
    id: str
    name: str
    region: str
    fuel_type: str
    capacity_liters: float
    current_stock_liters: float
    status: str


STATIONS = [
    Station(
        id=f"station-{index}",
        name=f"Station {index}",
        region="dhaka",
        fuel_type="DIESEL",
        capacity_liters=50000.0,
        current_stock_liters=12345.0,
        status="ACTIVE",
    )
    for index in range(12)
]


def _payload() -> dict[str, object]:
    return {"stations": [s.model_dump(mode="json") for s in STATIONS]}


def build_app(with_middleware: bool, with_model_dump: bool) -> FastAPI:
    app = FastAPI()

    if with_middleware:

        class _Metrics(BaseHTTPMiddleware):
            async def dispatch(self, request, call_next):
                started = time.perf_counter()
                response = await call_next(request)
                response.headers["x-elapsed"] = str(time.perf_counter() - started)
                return response

        app.add_middleware(_Metrics)

    @app.get("/probe")
    async def probe() -> dict[str, object]:
        if with_model_dump:
            return _payload()
        return {"stations": [s.model_dump() for s in STATIONS]}

    @app.get("/probe-fast")
    async def probe_fast() -> JSONResponse:
        # Pre-serialised bytes, straight into the response.
        import json

        return JSONResponse(content=json.dumps(_payload()))

    return app


async def bench(app: FastAPI, path: str, seconds: float = 3.0) -> float:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://probe", timeout=10.0
    ) as client:
        for _ in range(5):
            await client.get(path)
        started = time.perf_counter()
        count = 0
        while time.perf_counter() - started < seconds:
            await client.get(path)
            count += 1
        elapsed = time.perf_counter() - started
    return count / elapsed


async def main() -> None:
    print(f"{'configuration':44s} {'rps':>9s} {'us/req':>9s}")
    print("-" * 64)

    cases = [
        ("plain dict, no middleware", build_app(False, False), "/probe"),
        ("+ BaseHTTPMiddleware", build_app(True, False), "/probe"),
        ("+ middleware, JSONResponse", build_app(True, False), "/probe-fast"),
    ]
    for label, app, path in cases:
        rps = await bench(app, path)
        print(f"{label:44s} {rps:9.1f} {1e6 / rps:9.1f}")


asyncio.run(main())
