"""Tests for the request instrumentation.

The load test found three problems in the metrics middleware, and all three
are invisible in a response body — they are visible in the metric series
the platform exports. A test that only checks the endpoint answers 200
cannot see any of them.

1. The ``path`` label used the raw URL, so every station in
   ``/api/history/{station_id}`` was its own time series. The metric's
   cardinality grew with the number of stations instead of the number of
   endpoints.
2. The middleware read the container's cgroup files on every request.
3. The middleware was a ``BaseHTTPMiddleware``, which runs every request
   through a task group and two memory object streams.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.metrics import (
    HTTP_REQUESTS,
    _route_template,
    install_metrics_middleware,
    publish_container_memory,
)


@pytest.fixture
def instrumented_app() -> FastAPI:
    """A small app with the platform's own metrics middleware."""
    app = FastAPI()
    install_metrics_middleware(app)

    @app.get("/api/history/{station_id}")
    async def history(station_id: str) -> dict[str, str]:
        return {"station_id": station_id}

    @app.get("/api/stations")
    async def stations() -> dict[str, list[str]]:
        return {"stations": []}

    @app.get("/boom")
    async def boom() -> dict[str, str]:
        return {"ok": "no"}

    return app


def _paths_for(method: str, path_prefix: str) -> set[str]:
    """The ``path`` label values the counter holds for one method."""
    paths = set()
    for label_values, _ in HTTP_REQUESTS._metrics.items():
        labels = dict(zip(HTTP_REQUESTS._labelnames, label_values, strict=True))
        if labels.get("method") == method:
            paths.add(labels.get("path", ""))
    assert path_prefix  # the caller says what it expects to find
    return paths


def test_the_path_label_is_the_route_template_not_the_raw_url(
    instrumented_app: FastAPI,
) -> None:
    """Two requests to the same endpoint are one time series, not two.

    ``/api/history/station-mirpur`` and ``/api/history/station-uttara``
    are the same endpoint. Labelling them by the raw URL made the
    dashboard's "request rate by path" panel draw one line per station,
    and made the metric's cardinality grow with the size of the network
    rather than with the number of endpoints.
    """
    with TestClient(instrumented_app) as client:
        client.get("/api/history/station-mirpur")
        client.get("/api/history/station-uttara")

    paths = _paths_for("GET", "/api/history")
    assert "/api/history/{station_id}" in paths
    assert "/api/history/station-mirpur" not in paths
    assert "/api/history/station-uttara" not in paths


def test_the_route_template_falls_back_to_the_raw_path() -> None:
    """A request that matched no route has no template.

    The raw path is the honest answer for a 404: it says which URL was
    asked for, which is what an operator wants to see.
    """
    assert _route_template({"path": "/nope"}) == "/nope"
    assert _route_template({}) == "unknown"


def test_the_container_memory_is_still_published(instrumented_app: FastAPI) -> None:
    """The memory gauges are published on a timer, so they still exist.

    The cgroup read moved off the request path, but the numbers the
    "how close is this container to being killed" alert depends on must
    still be exported.
    """
    publish_container_memory()
    with TestClient(instrumented_app) as client:
        response = client.get("/metrics")

    assert response.status_code == 200
    assert "container_memory_limit_bytes" in response.text


def test_the_middleware_does_not_read_the_cgroup_on_every_request(
    instrumented_app: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The request path must not open the cgroup files.

    Four file opens per request, on the path the dashboard polls every
    five seconds, is waste: the memory numbers answer a question that
    does not change between two requests a millisecond apart.
    """
    import app.metrics as metrics

    calls = {"n": 0}
    original = metrics._read_cgroup_value

    def counting_read(path: str) -> int | None:
        calls["n"] += 1
        return original(path)

    monkeypatch.setattr(metrics, "_read_cgroup_value", counting_read)

    with TestClient(instrumented_app) as client:
        client.get("/api/stations")

    assert calls["n"] == 0
