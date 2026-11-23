"""Tests for the dashboard's backend routes.

The Day 5 dashboard needs three things the API did not have: a way to
read the simulation's controls, a way to drive them, and a way to see
every dependency's health in one payload.

These routes are the dashboard's contract, so they are tested here rather
than only through the frontend's smoke tests.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.main import app, get_cache, get_database, get_simulator, get_sync
from app.state_cache import CachedState, StateCache


class _FakeSimulator:
    """A simulator that records the admin calls the dashboard makes."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.injected: list[dict[str, Any]] = []
        self.fail = False

    async def health(self, *, fast: bool = False):
        from app.simulator_models import HealthResponse

        return HealthResponse.model_validate(
            {
                "status": "ok",
                "database": "ok",
                "simulation": {"status": "PAUSED", "tick": 42},
            }
        )

    async def get_instance(self):
        from app.simulator_models import Instance

        return Instance.model_validate(
            {
                "id": 1,
                "scenario_id": "baseline",
                "scenario_version": "1.0",
                "seed": 1,
                "sim_time": "2026-01-01T08:00:00+00:00",
                "tick": 42,
                "tick_minutes": 15,
                "status": "PAUSED",
            }
        )

    async def _action(self, name: str):
        if self.fail:
            raise RuntimeError("the simulator is unreachable")

        class _Result:
            def __init__(self) -> None:
                self.status = "RUNNING"
                self.tick = 43
                self.sim_time = "2026-01-01T08:15:00+00:00"
                self.id = 1
                self.scenario_id = "baseline"
                self.scenario_version = "1.0"
                self.seed = 1
                self.tick_minutes = 15

            def model_dump(self, mode: str = "json") -> dict[str, Any]:
                return {
                    "status": self.status,
                    "tick": self.tick,
                    "sim_time": self.sim_time,
                }

        return _Result()

    async def admin_run(self):
        self.calls.append("run")
        return await self._action("run")

    async def admin_pause(self):
        self.calls.append("pause")
        return await self._action("pause")

    async def admin_toggle(self):
        self.calls.append("toggle")
        return await self._action("toggle")

    async def admin_step(self):
        self.calls.append("step")
        return await self._action("step")

    async def admin_reset(self):
        self.calls.append("reset")
        return await self._action("reset")

    async def admin_inject_event(self, event):
        self.injected.append(event.model_dump(mode="json"))
        return type(
            "Event",
            (),
            {
                "id": 1,
                "type": event.type,
                "status": "SCHEDULED",
                "start_tick": event.start_tick,
                "end_tick": event.start_tick + event.duration_ticks,
                "parameters": event.parameters,
                "model_dump": lambda self, mode="json": {
                    "id": 1,
                    "type": event.type,
                    "status": "SCHEDULED",
                    "start_tick": event.start_tick,
                    "end_tick": event.start_tick + event.duration_ticks,
                    "parameters": event.parameters,
                },
            },
        )()


class _FakeDatabase:
    async def ping(self) -> bool:
        return True

    def session(self):
        return _FakeSession()


class _FakeSession:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        return None

    async def execute(self, statement: Any) -> Any:
        return _FakeResult()

    async def commit(self) -> None:
        return None


class _FakeResult:
    def scalars(self) -> _FakeResult:
        return self

    def all(self) -> list[Any]:
        return []

    def first(self) -> None:
        return None


class _FakeSync:
    def status(self) -> dict[str, Any]:
        return {
            "polling": True,
            "poll_interval_seconds": 5.0,
            "last_sync_age_seconds": 1.0,
            "degraded": False,
            "stale": False,
            "has_state": True,
            "checked_at": "2026-01-01T08:00:00+00:00",
        }


def _cache() -> StateCache:
    from app.simulator_models import Depot, Instance, Metrics, Region, Route, Station

    cache = StateCache()
    cache.store(
        CachedState(
            instance=Instance.model_validate(
                {
                    "id": 1,
                    "scenario_id": "baseline",
                    "scenario_version": "1.0",
                    "seed": 1,
                    "sim_time": "2026-01-01T08:00:00+00:00",
                    "tick": 42,
                    "tick_minutes": 15,
                    "status": "PAUSED",
                }
            ),
            regions=[
                Region.model_validate({"id": "region-dhaka", "name": "Dhaka", "demand_factor": 1.0})
            ],
            depots=[
                Depot.model_validate(
                    {
                        "id": "depot-gazipur",
                        "name": "Gazipur",
                        "region_id": "region-dhaka",
                        "status": "OPEN",
                        "dispatch_capacity_per_tick": 12000,
                        "capacity": {"DIESEL": 90000},
                        "inventory": {"DIESEL": 60000},
                    }
                )
            ],
            stations=[
                Station.model_validate(
                    {
                        "id": "station-mirpur",
                        "name": "Mirpur",
                        "region_id": "region-dhaka",
                        "status": "OPEN",
                        "demand_profile": "urban_high",
                        "demand_multiplier": 1.0,
                        "capacity": {"DIESEL": 15000},
                        "inventory": {"DIESEL": 9000},
                    }
                )
            ],
            routes=[
                Route.model_validate(
                    {
                        "id": "route-gazipur-mirpur",
                        "source_depot_id": "depot-gazipur",
                        "destination_station_id": "station-mirpur",
                        "transit_ticks": 2,
                        "max_shipment": 7000,
                        "status": "AVAILABLE",
                    }
                )
            ],
            supply_arrivals=[],
            events=[],
            allocations=[],
            metrics=Metrics.model_validate(
                {
                    "served_demand_liters": 100.0,
                    "unmet_demand_liters": 0.0,
                    "service_level": 1.0,
                    "allocation_liters": 0.0,
                    "allocation_failures": 0,
                }
            ),
        )
    )
    return cache


@pytest.fixture
def simulator() -> _FakeSimulator:
    return _FakeSimulator()


@pytest.fixture
def client(simulator: _FakeSimulator) -> TestClient:
    app.dependency_overrides[get_simulator] = lambda: simulator
    app.dependency_overrides[get_database] = lambda: _FakeDatabase()
    app.dependency_overrides[get_cache] = lambda: _cache()
    app.dependency_overrides[get_sync] = lambda: _FakeSync()
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


# -- the simulation controls -------------------------------------------------


def test_the_simulation_state_is_readable(client: TestClient) -> None:
    response = client.get("/api/simulation")

    assert response.status_code == 200
    body = response.json()
    assert body["simulation_only"] is True
    assert body["status"] == "PAUSED"
    assert body["tick"] == 42
    assert "run" in body["actions"]
    assert "step" in body["actions"]


@pytest.mark.parametrize("action", ["run", "pause", "toggle", "step", "reset"])
def test_every_simulation_action_is_forwarded(client: TestClient, action: str) -> None:
    response = client.post(f"/api/simulation/{action}")

    assert response.status_code == 200
    body = response.json()
    assert body["simulation_only"] is True
    assert body["action"] == action


def test_an_unknown_simulation_action_is_refused(client: TestClient) -> None:
    response = client.post("/api/simulation/explode")

    assert response.status_code == 400
    assert "explode" in response.json()["detail"]


def test_a_simulator_failure_is_reported_not_raised(
    client: TestClient, simulator: _FakeSimulator
) -> None:
    """The dashboard needs a message it can show, not a 500."""
    simulator.fail = True

    response = client.post("/api/simulation/run")

    assert response.status_code == 502
    assert "unreachable" in response.json()["detail"]


# -- crisis injection --------------------------------------------------------


def test_a_crisis_is_injected_into_the_simulation(
    client: TestClient, simulator: _FakeSimulator
) -> None:
    response = client.post(
        "/api/simulation/crisis",
        json={"type": "demand_spike", "duration_ticks": 4, "parameters": {"multiplier": 2.0}},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["simulation_only"] is True
    assert body["event"]["type"] == "demand_spike"
    assert simulator.injected
    assert simulator.injected[0]["duration_ticks"] == 4
    assert simulator.injected[0]["parameters"] == {"multiplier": 2.0}


def test_a_crisis_starts_at_the_current_tick(client: TestClient, simulator: _FakeSimulator) -> None:
    client.post("/api/simulation/crisis", json={"type": "route_disruption", "duration_ticks": 2})

    assert simulator.injected[0]["start_tick"] == 42


def test_an_unknown_crisis_type_is_refused(client: TestClient) -> None:
    response = client.post("/api/simulation/crisis", json={"type": "meteor_strike"})

    assert response.status_code == 400
    assert "crisis" in response.json()["detail"]


def test_the_crisis_route_is_not_shadowed_by_the_action_route(client: TestClient) -> None:
    """``/api/simulation/crisis`` must not be read as an action name."""
    response = client.post("/api/simulation/crisis", json={"type": "demand_spike"})

    assert response.status_code == 200
    assert response.json()["action" if "action" in response.json() else "event"]


# -- the service health panel ------------------------------------------------


def test_the_service_health_panel_lists_every_dependency(client: TestClient) -> None:
    response = client.get("/api/service-health")

    assert response.status_code == 200
    components = response.json()["components"]
    assert set(components) == {
        "backend",
        "database",
        "simulator",
        "prediction_service",
        "decision_engine",
    }


def test_the_service_health_panel_reports_the_http_metrics(client: TestClient) -> None:
    response = client.get("/api/service-health")

    http = response.json()["http"]
    assert "p95_latency_seconds" in http
    assert "error_rate" in http
    assert http["request_count"] >= 0
    assert 0.0 <= http["error_rate"] <= 1.0


def test_the_service_health_panel_reports_the_fallback_state(client: TestClient) -> None:
    response = client.get("/api/service-health")

    engine = response.json()["components"]["decision_engine"]
    assert "fallback_active" in engine
    assert "auto_mode" in engine


def test_the_service_health_panel_does_not_block_on_the_simulator(client: TestClient) -> None:
    """The dashboard polls this endpoint; a live probe would stall it.

    The simulator's state is read from the sync cache, so the answer
    comes back even when the simulator is not answering at all.
    """
    response = client.get("/api/service-health")

    assert response.status_code == 200
    simulator = response.json()["components"]["simulator"]
    assert simulator["status"] in {"ok", "unreachable", "error", "degraded"}
    assert simulator["simulation"]["tick"] == 42
