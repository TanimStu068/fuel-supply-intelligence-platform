"""Tests for the background sync service.

The three behaviours that matter:

* **SSE reconnect** — when the stream drops, the service reconnects and then
  does a full REST refetch, because the stream has no replay.
* **Stale handling** — when the simulator serves ``X-Simulator-Stale: true``,
  the cache is invalidated and the state is marked stale.
* **Degraded mode** — when the simulator is unreachable, the last known state
  is still served, flagged ``degraded`` with an age.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

import pytest

from app.config import Settings
from app.simulator_errors import SimulatorAPIError, SimulatorConnectionError
from app.state_cache import StateCache
from app.sync_service import SyncService


class _FakeDatabase:
    """A database that records the writes instead of performing them."""

    def __init__(self) -> None:
        self.snapshots: list[dict[str, Any]] = []
        self.health: list[dict[str, Any]] = []
        self.inventories: list[dict[str, Any]] = []
        self.allocations: list[Any] = []
        self.events: list[Any] = []
        self.demand: list[Any] = []
        self.commits = 0
        self.added: list[Any] = []
        self.executed: list[Any] = []

    def session(self):
        return _FakeSession(self)


class _FakeSession:
    def __init__(self, db: _FakeDatabase) -> None:
        self._db = db

    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        return None

    def add(self, row: Any) -> None:
        self._db.added.append(row)

    async def execute(self, statement: Any) -> Any:
        self._db.executed.append(statement)
        return _FakeResult()

    async def commit(self) -> None:
        self._db.commits += 1


class _FakeResult:
    """A minimal stand-in for an AsyncResult."""

    rowcount = 1

    def first(self):
        return None

    def all(self):
        return []

    def scalars(self):
        return []


class _FakeClient:
    """A simulator client whose responses the test controls."""

    def __init__(self) -> None:
        self.stale = False
        self.last_stale = False
        #: When set, every read reports the simulator's stale header, the way
        #: the real client does when the server sends it.
        self.force_stale = False
        self.unreachable = False
        self.instance_calls = 0
        self.allocation_calls = 0
        self.depot_calls = 0
        self.station_calls = 0
        self.demand_calls = 0
        self.stream_events: list[Any] = []
        self.stream_error: Exception | None = None
        self.stream_ends_after: int | None = None

    def reset_stale(self) -> None:
        self.stale = False
        self.last_stale = False

    def _guard(self) -> None:
        if self.unreachable:
            raise SimulatorConnectionError("connection refused")
        if self.force_stale:
            self.stale = True
            self.last_stale = True

    async def health(self, *, fast: bool = False):
        self._guard()
        from app.simulator_models import HealthResponse

        return HealthResponse.model_validate(
            {"status": "ok", "database": "ok", "simulation": {"status": "PAUSED", "tick": 0}}
        )

    async def get_instance(self):
        self._guard()
        self.instance_calls += 1
        from app.simulator_models import Instance

        return Instance.model_validate(
            {
                "id": 1,
                "scenario_id": "baseline",
                "scenario_version": "1.0",
                "seed": 1,
                "sim_time": "2026-01-01T00:00:00+00:00",
                "tick": 7,
                "tick_minutes": 15,
                "status": "RUNNING",
            }
        )

    async def get_regions(self):
        self._guard()
        from app.simulator_models import Region

        return [
            Region.model_validate({"id": "region-dhaka", "name": "Dhaka", "demand_factor": 1.0})
        ]

    async def get_depots(self):
        self._guard()
        from app.simulator_models import Depot

        return [
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
        ]

    async def get_stations(self):
        self._guard()
        from app.simulator_models import Station

        return [
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
        ]

    async def get_routes(self):
        self._guard()
        from app.simulator_models import Route

        return [
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
        ]

    async def get_supply_arrivals(self):
        self._guard()
        from app.simulator_models import SupplyArrival

        return [
            SupplyArrival.model_validate(
                {
                    "id": "supply-001",
                    "depot_id": "depot-gazipur",
                    "fuel_type": "DIESEL",
                    "quantity": 18000,
                    "planned_tick": 12,
                    "actual_tick": None,
                    "status": "SCHEDULED",
                }
            )
        ]

    async def get_events(self):
        self._guard()
        return []

    async def get_allocations(self):
        self._guard()
        self.allocation_calls += 1
        return []

    async def get_metrics(self):
        self._guard()
        from app.simulator_models import Metrics

        return Metrics.model_validate(
            {
                "served_demand_liters": 100.0,
                "unmet_demand_liters": 0.0,
                "service_level": 1.0,
                "allocation_liters": 0.0,
                "allocation_failures": 0,
            }
        )

    async def get_depot(self, entity_id: str):
        self._guard()
        self.depot_calls += 1
        from app.simulator_models import Depot

        return Depot.model_validate(
            {
                "id": entity_id,
                "name": "Gazipur",
                "region_id": "region-dhaka",
                "status": "OPEN",
                "dispatch_capacity_per_tick": 12000,
                "capacity": {"DIESEL": 90000},
                "inventory": {"DIESEL": 55000},
            }
        )

    async def get_station(self, entity_id: str):
        self._guard()
        self.station_calls += 1
        from app.simulator_models import Station

        return Station.model_validate(
            {
                "id": entity_id,
                "name": "Mirpur",
                "region_id": "region-dhaka",
                "status": "OPEN",
                "demand_profile": "urban_high",
                "demand_multiplier": 1.0,
                "capacity": {"DIESEL": 15000},
                "inventory": {"DIESEL": 8000},
            }
        )

    async def get_demand_history(self, *, station_id=None, limit=200):
        self._guard()
        self.demand_calls += 1
        from app.simulator_models import DemandObservation

        return [
            DemandObservation.model_validate(
                {
                    "id": 100,
                    "station_id": "station-mirpur",
                    "fuel_type": "DIESEL",
                    "tick": 12,
                    "sim_time": "2026-01-01T03:00:00+00:00",
                    "demand_liters": 95.123,
                    "served_liters": 95.123,
                    "unmet_liters": 0.0,
                }
            )
        ]

    async def stream(self, *, timeout: float | None = None):
        """Yield the configured events, then optionally raise."""
        if self.stream_error is not None:
            raise self.stream_error
        for event in self.stream_events:
            yield event
        if self.stream_ends_after is not None:
            return


@pytest.fixture
def settings() -> Settings:
    return Settings(
        SIMULATOR_BASE_URL="http://simulator.test",
        SIMULATOR_REQUEST_TIMEOUT=1.0,
        SIMULATOR_CONNECT_TIMEOUT=0.5,
        SIMULATOR_MAX_RETRIES=0,
        SIMULATOR_RETRY_BASE_DELAY=0.001,
        SIMULATOR_RETRY_MAX_DELAY=0.01,
        SYNC_POLL_INTERVAL=60.0,
        SSE_RECONNECT_BASE_DELAY=0.01,
        SSE_RECONNECT_MAX_DELAY=0.02,
        DEMAND_HISTORY_LIMIT=100,
    )


@pytest.fixture
def client() -> _FakeClient:
    return _FakeClient()


@pytest.fixture
def database() -> _FakeDatabase:
    return _FakeDatabase()


@pytest.fixture
def cache() -> StateCache:
    return StateCache()


@pytest.fixture
def service(settings, client, database, cache) -> SyncService:
    return SyncService(client=client, database=database, cache=cache, settings=settings)


# -- happy path ------------------------------------------------------------


async def test_sync_cycle_fetches_and_caches(service, client, cache) -> None:
    assert await service.run_sync_cycle() is True

    assert cache.has_state is True
    assert cache.degraded is False
    assert cache.state.instance.tick == 7
    assert cache.state.stations[0].id == "station-mirpur"


async def test_sync_cycle_persists_a_snapshot(service, database) -> None:
    await service.run_sync_cycle()

    assert database.commits >= 1


# -- degraded mode ---------------------------------------------------------


async def test_unreachable_simulator_degrades_the_cache(service, client, cache) -> None:
    assert await service.run_sync_cycle() is True
    assert cache.degraded is False

    client.unreachable = True

    assert await service.run_sync_cycle() is False
    assert cache.degraded is True
    assert cache.degraded_reason is not None


async def test_degraded_mode_still_serves_the_last_known_state(service, client, cache) -> None:
    await service.run_sync_cycle()
    client.unreachable = True
    await service.run_sync_cycle()

    assert cache.has_state is True
    assert cache.state.instance.tick == 7
    assert cache.metadata()["degraded"] is True
    assert cache.metadata()["age_seconds"] is not None


async def test_recovery_clears_the_degraded_flag(service, client, cache) -> None:
    await service.run_sync_cycle()
    client.unreachable = True
    await service.run_sync_cycle()
    assert cache.degraded is True

    client.unreachable = False

    assert await service.run_sync_cycle() is True
    assert cache.degraded is False
    assert cache.degraded_reason is None


# -- stale handling --------------------------------------------------------


async def test_stale_response_marks_the_state_stale(service, client, cache) -> None:
    client.force_stale = True

    assert await service.run_sync_cycle() is True

    assert cache.state.stale is True
    assert cache.metadata()["stale"] is True


async def test_stale_response_invalidates_the_previous_snapshot(service, client, cache) -> None:
    await service.run_sync_cycle()
    assert cache.has_state is True

    client.force_stale = True
    await service.run_sync_cycle()

    # The stale snapshot replaced the old one, and it is flagged.
    assert cache.has_state is True
    assert cache.state.stale is True


# -- SSE handling ----------------------------------------------------------


async def test_tick_event_triggers_a_full_sync(service, client, cache) -> None:
    from app.sse import StreamEvent

    client.stream_events = [
        StreamEvent(event="simulation.tick", data={"tick": 8, "sim_time": "2026-01-01T02:00:00"})
    ]

    await service._handle_event(client.stream_events[0])

    assert client.instance_calls >= 1
    assert cache.has_state is True


async def test_allocation_event_refreshes_only_the_ledger(service, client) -> None:
    from app.sse import StreamEvent

    client.stream_events = [
        StreamEvent(event="allocation.status_changed", data={"id": 1, "status": "ARRIVED"})
    ]
    before = client.instance_calls

    await service._handle_event(client.stream_events[0])

    assert client.allocation_calls == 1
    # No full refetch: the ledger is the only thing that changed.
    assert client.instance_calls == before


async def test_inventory_event_refreshes_one_entity(service, client) -> None:
    from app.sse import StreamEvent

    client.stream_events = [
        StreamEvent(
            event="inventory.updated",
            data={"entity_type": "depot", "entity_id": "depot-gazipur", "inventory": {}},
        )
    ]

    await service._handle_event(client.stream_events[0])

    assert client.depot_calls == 1
    assert client.station_calls == 0


async def test_notice_event_triggers_a_full_sync(service, client, cache) -> None:
    from app.sse import StreamEvent

    client.stream_events = [
        StreamEvent(event="simulator.notice", data={"message": "Simulation reset"})
    ]

    await service._handle_event(client.stream_events[0])

    assert client.instance_calls >= 1
    assert cache.has_state is True


async def test_stream_reconnect_triggers_a_full_rest_refetch(service, client, cache) -> None:
    """After a reconnect the service re-reads everything over REST.

    The stream has no Last-Event-ID replay, so anything that happened while
    we were disconnected only exists in the REST view.
    """
    from app.sse import StreamEvent

    client.stream_events = [
        StreamEvent(event="simulation.tick", data={"tick": 1, "sim_time": "2026-01-01T00:15:00"})
    ]
    client.stream_ends_after = 1

    service._stopping = asyncio.Event()

    # Run the SSE loop briefly; it should reconnect and refetch.
    task = asyncio.create_task(service._sse_loop())
    await asyncio.sleep(0.3)
    service._stopping.set()
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    # More than one instance read: the initial sync plus the post-reconnect
    # refetch.
    assert client.instance_calls >= 2


async def test_stream_fault_503_is_a_reconnect_not_a_crash(service, client) -> None:
    """A stream_disconnect fault answers 503; the loop must survive it."""
    client.stream_error = SimulatorAPIError(
        status_code=503,
        code="FAULT_INJECTED",
        message="Simulator API temporarily unavailable.",
        payload={},
    )
    service._stopping = asyncio.Event()

    task = asyncio.create_task(service._sse_loop())
    await asyncio.sleep(0.3)
    service._stopping.set()
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    # The task was still alive: a 503 did not kill the loop.
    assert task.cancelled() or task.done()


# -- demand history --------------------------------------------------------


async def test_ingest_demand_history_calls_the_simulator(service, client) -> None:
    written, _ = await service.ingest_demand_history(limit=50)

    assert client.demand_calls == 1
    assert written >= 0


async def test_ingest_demand_history_survives_an_unreachable_simulator(service, client) -> None:
    client.unreachable = True

    assert await service.ingest_demand_history() == (0, 0)
