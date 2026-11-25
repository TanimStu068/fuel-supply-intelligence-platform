"""Tests for the state cache: staleness, degradation and age."""

from __future__ import annotations

from datetime import UTC, datetime

from app.simulator_models import (
    Allocation,
    Depot,
    Instance,
    Metrics,
    Region,
    Route,
    Station,
    SupplyArrival,
)
from app.state_cache import CachedState, StateCache


def _state(stale: bool = False, tick: int = 0) -> CachedState:
    return CachedState(
        instance=Instance.model_validate(
            {
                "id": 1,
                "scenario_id": "baseline",
                "scenario_version": "1.0",
                "seed": 1,
                "sim_time": "2026-01-01T00:00:00+00:00",
                "tick": tick,
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
        supply_arrivals=[
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
        ],
        events=[],
        allocations=[
            Allocation.model_validate(
                {
                    "id": 1,
                    "idempotency_key": "demo-001",
                    "source_depot_id": "depot-gazipur",
                    "destination_station_id": "station-mirpur",
                    "route_id": "route-gazipur-mirpur",
                    "fuel_type": "DIESEL",
                    "quantity": 3000,
                    "created_tick": 5,
                    "departure_tick": 6,
                    "expected_arrival_tick": 8,
                    "actual_arrival_tick": None,
                    "status": "IN_TRANSIT",
                    "failure_reason": None,
                }
            )
        ],
        metrics=Metrics.model_validate(
            {
                "served_demand_liters": 100.0,
                "unmet_demand_liters": 0.0,
                "service_level": 1.0,
                "allocation_liters": 3000.0,
                "allocation_failures": 0,
            }
        ),
        stale=stale,
    )


def test_cache_starts_empty() -> None:
    cache = StateCache()

    assert cache.has_state is False
    assert cache.state is None
    assert cache.degraded is False
    assert cache.last_sync_age() is None


def test_store_makes_state_available() -> None:
    cache = StateCache()

    cache.store(_state())

    assert cache.has_state is True
    assert cache.degraded is False
    assert cache.state is not None
    assert cache.state.stale is False


def test_store_clears_the_degraded_flag() -> None:
    cache = StateCache()
    cache.mark_degraded("connection refused")

    cache.store(_state())

    assert cache.degraded is False
    assert cache.degraded_reason is None


def test_mark_degraded_keeps_the_last_known_state() -> None:
    cache = StateCache()
    cache.store(_state())

    cache.mark_degraded("connection refused")

    assert cache.degraded is True
    assert cache.degraded_reason == "connection refused"
    assert cache.has_state is True
    assert cache.state is not None


def test_metadata_reports_degraded_and_age() -> None:
    cache = StateCache()
    cache.store(_state())
    cache.mark_degraded("connection refused")

    metadata = cache.metadata()

    assert metadata["degraded"] is True
    assert metadata["degraded_reason"] == "connection refused"
    assert metadata["age_seconds"] is not None
    assert metadata["age_seconds"] >= 0


def test_metadata_reports_stale() -> None:
    cache = StateCache()

    cache.store(_state(stale=True))

    assert cache.metadata()["stale"] is True


def test_invalidate_drops_the_snapshot() -> None:
    cache = StateCache()
    cache.store(_state())

    cache.invalidate()

    assert cache.state is None
    assert cache.has_state is False


# --- the clock going backwards ------------------------------------------------


def test_a_rewound_clock_is_recorded() -> None:
    """A snapshot from a rewound clock is noticed.

    ``POST /admin/reset`` on the simulator starts the world again at tick
    0, so the tick numbers of the old run and the new one overlap. The
    cache has to be able to say "the world was reset", because the event
    stream's tick tracker depends on it.
    """
    cache = StateCache()
    cache.store(_state(tick=900))

    assert cache.rewound is False

    cache.store(_state(tick=0))

    assert cache.rewound is True


def test_a_forward_snapshot_is_not_a_rewind() -> None:
    """The ordinary case: the clock moved on, and nothing was flagged."""
    cache = StateCache()
    cache.store(_state(tick=10))

    cache.store(_state(tick=11))

    assert cache.rewound is False
    assert cache.state is not None
    assert cache.state.instance.tick == 11


def test_the_same_tick_is_not_a_rewind() -> None:
    """A re-read of the same tick is not a reset."""
    cache = StateCache()
    cache.store(_state(tick=42))

    cache.store(_state(tick=42))

    assert cache.rewound is False
    assert cache.state is not None
    assert cache.state.instance.tick == 42


def test_a_rewound_snapshot_still_becomes_the_cached_state() -> None:
    """The new world's snapshot is the current world, so it is kept.

    The cache must not throw the new world's numbers away — they are the
    truth now. What matters is that the rewind is *recorded*, so the
    caller can reset the tick trackers that still refer to the old world.
    """
    cache = StateCache()
    cache.store(_state(tick=900))

    cache.store(_state(tick=3))

    assert cache.rewound is True
    assert cache.state is not None
    assert cache.state.instance.tick == 3
    assert cache.degraded is False
    assert cache.last_sync_age() is not None


def test_the_first_snapshot_is_never_a_rewind() -> None:
    """A cache that has never held anything cannot be rewound."""
    cache = StateCache()

    cache.store(_state(tick=0))

    assert cache.rewound is False
    assert cache.has_state is True


def test_age_grows_over_time() -> None:
    cache = StateCache()
    cache.store(_state())

    now = datetime.now(UTC).timestamp()
    first = cache.last_sync_age(now)
    later = cache.last_sync_age(now + 30)

    assert first is not None
    assert later is not None
    assert later > first


def test_to_payload_carries_every_section() -> None:
    payload = _state().to_payload()

    assert set(payload) == {
        "instance",
        "regions",
        "depots",
        "stations",
        "routes",
        "supply_arrivals",
        "events",
        "allocations",
        "metrics",
    }
    assert payload["stations"][0]["id"] == "station-mirpur"
    assert payload["metrics"]["service_level"] == 1.0
