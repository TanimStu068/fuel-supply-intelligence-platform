"""Shared builders for the decision-layer tests.

The world, the forecast and the risk assessment are built here rather
than in each test module, so the validator, the optimizer and the
approval flow are all tested against the *same* network.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.decision.world import (
    AllocationSnapshot,
    DepotSnapshot,
    RouteSnapshot,
    StationSnapshot,
    WorldSnapshot,
)
from app.intelligence.forecasting import Forecast, ForecastPoint, ModelKind
from app.intelligence.risk import RiskAssessment, RiskLevel, StockSignal

BASE_TIME = datetime(2026, 1, 1, 8, 0, tzinfo=UTC)


def build_world(**overrides) -> WorldSnapshot:
    """A small world with two depots, one station and two routes.

    ``route-gazipur-mirpur`` is the fast route (2 ticks, 7,000 L) and
    ``route-patiya-mirpur`` is the slow backup (4 ticks, 5,000 L) — the
    same shape as the real network, where Patiya is the longer way to
    Mirpur.
    """
    depots = overrides.pop(
        "depots",
        {
            "depot-gazipur": DepotSnapshot(
                id="depot-gazipur",
                status="OPEN",
                dispatch_capacity_per_tick=12_000.0,
                inventory={"DIESEL": 60_000.0, "PETROL": 45_000.0, "OCTANE": 26_000.0},
                capacity={"DIESEL": 90_000.0, "PETROL": 70_000.0, "OCTANE": 45_000.0},
            ),
            "depot-patiya": DepotSnapshot(
                id="depot-patiya",
                status="OPEN",
                dispatch_capacity_per_tick=11_000.0,
                inventory={"DIESEL": 55_000.0, "PETROL": 42_000.0, "OCTANE": 24_000.0},
                capacity={"DIESEL": 85_000.0, "PETROL": 65_000.0, "OCTANE": 40_000.0},
            ),
        },
    )
    stations = overrides.pop(
        "stations",
        {
            "station-mirpur": StationSnapshot(
                id="station-mirpur",
                status="OPEN",
                inventory={"DIESEL": 2_000.0, "PETROL": 9_000.0, "OCTANE": 5_000.0},
                capacity={"DIESEL": 15_000.0, "PETROL": 14_000.0, "OCTANE": 9_000.0},
            )
        },
    )
    routes = overrides.pop(
        "routes",
        (
            RouteSnapshot(
                id="route-gazipur-mirpur",
                source_depot_id="depot-gazipur",
                destination_station_id="station-mirpur",
                transit_ticks=2,
                max_shipment=7_000.0,
                status="AVAILABLE",
            ),
            RouteSnapshot(
                id="route-patiya-mirpur",
                source_depot_id="depot-patiya",
                destination_station_id="station-mirpur",
                transit_ticks=4,
                max_shipment=5_000.0,
                status="AVAILABLE",
            ),
        ),
    )
    return WorldSnapshot(
        tick=overrides.pop("tick", 10),
        sim_time=BASE_TIME,
        tick_minutes=overrides.pop("tick_minutes", 15),
        depots=depots,
        stations=stations,
        routes=routes,
        allocations=overrides.pop("allocations", ()),
        supply_arrivals=overrides.pop("supply_arrivals", ()),
    )


def build_forecast(
    *,
    hourly_demand: float = 400.0,
    ticks: int = 96,
    start_tick: int = 11,
    spread: float = 0.1,
) -> Forecast:
    """A forecast with a flat, predictable demand."""
    points = []
    for index in range(ticks):
        demand = hourly_demand / 4.0
        points.append(
            ForecastPoint(
                tick=start_tick + index,
                sim_time=BASE_TIME + timedelta(minutes=15 * (start_tick + index)),
                predicted_demand=demand,
                lower_bound=demand * (1 - spread),
                upper_bound=demand * (1 + spread),
                confidence=0.95,
            )
        )
    return Forecast(
        station_id="station-mirpur",
        fuel_type="DIESEL",
        model=ModelKind.LEARNED,
        horizon_ticks=ticks,
        points=points,
        confidence=0.95,
        needs_human_review=False,
    )


def build_risk(
    *,
    hours_to_stockout: float | None = 4.0,
    inventory: float = 2_000.0,
    level: RiskLevel = RiskLevel.CRITICAL,
    forecast: Forecast | None = None,
) -> RiskAssessment:
    """A risk assessment for one station and fuel."""
    forecast = forecast or build_forecast()
    return RiskAssessment(
        station_id="station-mirpur",
        fuel_type="DIESEL",
        risk_level=level,
        hours_to_stockout=hours_to_stockout,
        stockout_probability=0.9 if hours_to_stockout is not None else 0.1,
        risk_score=0.9,
        forecast=forecast,
        signals=[
            StockSignal(
                name="inventory_liters",
                value=inventory,
                detail=f"{inventory:,.0f} L on hand",
            ),
            StockSignal(
                name="available_liters",
                value=inventory,
                detail=f"{inventory:,.0f} L available",
            ),
            StockSignal(
                name="capacity_liters",
                value=15_000.0,
                detail="15,000 L capacity",
            ),
        ],
        needs_human_review=False,
        recommendation="Dispatch diesel now.",
    )


def build_allocation(
    *,
    allocation_id: int = 1,
    depot_id: str = "depot-gazipur",
    station_id: str = "station-mirpur",
    fuel_type: str = "DIESEL",
    quantity: float = 1_000.0,
    created_tick: int = 10,
    status: str = "IN_TRANSIT",
) -> AllocationSnapshot:
    """One allocation already in the simulator's ledger."""
    return AllocationSnapshot(
        id=allocation_id,
        idempotency_key=f"existing-{allocation_id}",
        source_depot_id=depot_id,
        destination_station_id=station_id,
        route_id="route-gazipur-mirpur",
        fuel_type=fuel_type,
        quantity=quantity,
        created_tick=created_tick,
        expected_arrival_tick=created_tick + 2,
        status=status,
    )
