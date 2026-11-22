"""Tests for the intelligence engine's inventory anomaly detection.

The inventory window is the subtle part. A tank reading taken at tick T is
the tank as it stood *before* tick T was served, so the fuel drawn between
two readings is what was served over the ticks in between them. The window
is ``[previous_tick, current_tick)`` and it is measured against the fuel
actually drawn, not against the demand and not against the forecast.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.intelligence.detection import AnomalyKind
from app.intelligence.engine import IntelligenceEngine
from app.intelligence.forecasting import ForecastSelector, HistoryPoint
from app.simulator_models import Allocation, AllocationStatus, Station

BASE_TIME = datetime(2026, 1, 1, 8, 0, tzinfo=UTC)


def _history(
    *,
    start_tick: int,
    end_tick: int,
    served_per_tick: float,
    demand_per_tick: float | None = None,
) -> list[HistoryPoint]:
    """One observation per tick over ``[start_tick, end_tick)``."""
    demand = served_per_tick if demand_per_tick is None else demand_per_tick
    return [
        HistoryPoint(
            station_id="station-mirpur",
            fuel_type="DIESEL",
            tick=tick,
            sim_time=BASE_TIME + timedelta(minutes=15 * tick),
            demand_liters=demand,
            served_liters=served_per_tick,
            unmet_liters=demand - served_per_tick,
        )
        for tick in range(start_tick, end_tick)
    ]


def _station(inventory: float) -> Station:
    return Station(
        id="station-mirpur",
        name="Mirpur",
        region_id="region-dhaka",
        status="OPEN",
        demand_profile="urban_high",
        demand_multiplier=1.0,
        inventory={"DIESEL": inventory},
        capacity={"DIESEL": 15000.0},
    )


def _allocation(*, arrival_tick: int, quantity: float) -> Allocation:
    return Allocation(
        id=1,
        idempotency_key="platform-station-mirpur-DIESEL-1",
        source_depot_id="depot-gazipur",
        destination_station_id="station-mirpur",
        route_id="route-gazipur-mirpur",
        fuel_type="DIESEL",
        quantity=quantity,
        created_tick=arrival_tick - 2,
        departure_tick=arrival_tick - 1,
        expected_arrival_tick=arrival_tick,
        actual_arrival_tick=arrival_tick,
        status=AllocationStatus.ARRIVED,
    )


def _engine() -> IntelligenceEngine:
    return IntelligenceEngine(selector=ForecastSelector(), horizon_ticks=24)


def test_a_tank_refilled_inside_the_window_is_not_an_anomaly() -> None:
    """A delivery inside the window explains the rise in the tank."""
    engine = _engine()
    history = _history(start_tick=10, end_tick=20, served_per_tick=100.0)

    # The first evaluation records the starting tank level.
    engine._detect_inventory_anomalies(
        stations=[_station(1000.0)],
        allocations=[],
        current_tick=10,
        history=history,
    )

    # Over ticks 10..20 the station draws 1000 L and a 7000 L truck arrives
    # at tick 15, so the tank ends at 1000 - 1000 + 7000 = 7000.
    anomalies = engine._detect_inventory_anomalies(
        stations=[_station(7000.0)],
        allocations=[_allocation(arrival_tick=15, quantity=7000.0)],
        current_tick=20,
        history=history,
    )

    assert anomalies == []


def test_a_demand_spike_is_not_an_inventory_anomaly() -> None:
    """A station that sold more than usual has not lost fuel.

    The spike is a real draw on the tank. Comparing it against the calm
    forecast would report every affected station as a critical inventory
    anomaly — the detector describing the event it exists to detect.
    """
    engine = _engine()
    history = _history(start_tick=10, end_tick=20, served_per_tick=100.0)

    engine._detect_inventory_anomalies(
        stations=[_station(5000.0)],
        allocations=[],
        current_tick=10,
        history=history,
    )

    # Ticks 15..20 ran at 500 L per tick instead of 100 L: the tank drops by
    # 2500 L, and 2500 L is exactly what was served.
    spiked = [
        point
        if point.tick < 15
        else HistoryPoint(
            station_id=point.station_id,
            fuel_type=point.fuel_type,
            tick=point.tick,
            sim_time=point.sim_time,
            demand_liters=500.0,
            served_liters=500.0,
            unmet_liters=0.0,
        )
        for point in history
    ]

    anomalies = engine._detect_inventory_anomalies(
        stations=[_station(2500.0)],
        allocations=[],
        current_tick=20,
        history=spiked,
    )

    assert anomalies == []


def test_fuel_that_was_never_served_is_not_charged_to_the_tank() -> None:
    """Unserved demand did not leave the tank.

    A station that could not meet its demand did not draw that fuel, so
    charging the tank for it would report a loss that never happened.
    """
    engine = _engine()
    # Ten ticks of 200 L demanded, but only 100 L served per tick.
    history = _history(
        start_tick=10,
        end_tick=20,
        served_per_tick=100.0,
        demand_per_tick=200.0,
    )

    engine._detect_inventory_anomalies(
        stations=[_station(5000.0)],
        allocations=[],
        current_tick=10,
        history=history,
    )

    # The tank dropped by 1000 L: exactly what was served.
    anomalies = engine._detect_inventory_anomalies(
        stations=[_station(4000.0)],
        allocations=[],
        current_tick=20,
        history=history,
    )

    assert anomalies == []


def test_a_window_the_history_does_not_cover_is_not_judged() -> None:
    """A partially covered window cannot be judged.

    The fuel drawn over the missing ticks is unknown, so any comparison
    would report a change that never happened.
    """
    engine = _engine()
    history = _history(start_tick=10, end_tick=15, served_per_tick=100.0)

    engine._detect_inventory_anomalies(
        stations=[_station(1000.0)],
        allocations=[],
        current_tick=10,
        history=history,
    )

    # The window 10..20 is only covered for half of its ticks.
    anomalies = engine._detect_inventory_anomalies(
        stations=[_station(5000.0)],
        allocations=[],
        current_tick=20,
        history=history,
    )

    assert anomalies == []


def test_a_genuine_loss_is_still_reported() -> None:
    """The detector still reports fuel that went missing."""
    engine = _engine()
    history = _history(start_tick=10, end_tick=20, served_per_tick=100.0)

    engine._detect_inventory_anomalies(
        stations=[_station(5000.0)],
        allocations=[],
        current_tick=10,
        history=history,
    )

    # The tank should hold 4000 L, but it holds 2000 L: 2000 L vanished.
    anomalies = engine._detect_inventory_anomalies(
        stations=[_station(2000.0)],
        allocations=[],
        current_tick=20,
        history=history,
    )

    assert len(anomalies) == 1
    assert anomalies[0].kind is AnomalyKind.INVENTORY
    assert "dropped" in anomalies[0].message
