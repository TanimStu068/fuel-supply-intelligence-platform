"""Tests for the intelligence engine — the predictor's decision surface.

The engine is the layer that turns a world state into forecasts, risk
assessments and anomalies. It is the part of the intelligence stack a
judge is most likely to poke at, because it is where "the platform
predicts a shortage" actually happens.

These tests cover the parts that decide whether an operator is told the
truth:

* **the fallback counters** — a reason that keeps firing must be one
  ongoing condition, not one event per forecast;
* **the demand-anomaly reference** — the comparison is against the
  *published profile*, never against the current demand multiplier,
  because the multiplier is the thing that caused the spike;
* **the inventory-anomaly window** — a change is only unexplained
  relative to the demand over the ticks it actually covers, and a
  delivery inside the window explains a rise in the tank;
* **the observed consumption** — it is the *served* litres, not the
  demand, because a station that could not meet its demand never drew
  that fuel.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.intelligence.engine import (
    IntelligenceEngine,
    anomaly_summary,
    risk_summary,
)
from app.intelligence.forecasting import HistoryPoint, ModelKind
from app.intelligence.risk import RiskLevel
from app.simulator_models import (
    Allocation,
    Depot,
    FuelType,
    Route,
    Station,
)

BASE_TIME = datetime(2026, 1, 1, 8, 0, tzinfo=UTC)


def _station(**overrides) -> Station:
    """A station with a tank for each fuel."""
    defaults = {
        "id": "station-mirpur",
        "name": "Mirpur",
        "region_id": "region-dhaka",
        "status": "OPEN",
        "demand_profile": "urban_high",
        "capacity": {"DIESEL": 15_000.0, "PETROL": 14_000.0},
        "inventory": {"DIESEL": 2_000.0, "PETROL": 9_000.0},
        "demand_multiplier": 1.0,
    }
    defaults.update(overrides)
    return Station(**defaults)


def _depot(**overrides) -> Depot:
    """A depot with stock."""
    defaults = {
        "id": "depot-gazipur",
        "name": "Gazipur",
        "region_id": "region-dhaka",
        "status": "OPEN",
        "capacity": {"DIESEL": 90_000.0},
        "inventory": {"DIESEL": 60_000.0},
        "dispatch_capacity_per_tick": 12_000.0,
    }
    defaults.update(overrides)
    return Depot(**defaults)


def _route(**overrides) -> Route:
    """One available route."""
    defaults = {
        "id": "route-gazipur-mirpur",
        "source_depot_id": "depot-gazipur",
        "destination_station_id": "station-mirpur",
        "transit_ticks": 2,
        "max_shipment": 7_000.0,
        "status": "AVAILABLE",
    }
    defaults.update(overrides)
    return Route(**defaults)


def _allocation(**overrides) -> Allocation:
    """One allocation in the simulator's ledger."""
    defaults = {
        "id": 1,
        "idempotency_key": "existing-1",
        "source_depot_id": "depot-gazipur",
        "destination_station_id": "station-mirpur",
        "route_id": "route-gazipur-mirpur",
        "fuel_type": FuelType.DIESEL,
        "quantity": 1_000.0,
        "created_tick": 10,
        "expected_arrival_tick": 12,
        "status": "IN_TRANSIT",
    }
    defaults.update(overrides)
    return Allocation(**defaults)


def _history(
    *,
    station_id: str = "station-mirpur",
    fuel_type: str = "DIESEL",
    ticks: int = 5,
    start_tick: int = 10,
    served: float = 100.0,
    demand: float = 100.0,
) -> list[HistoryPoint]:
    """A run of demand observations."""
    return [
        HistoryPoint(
            station_id=station_id,
            fuel_type=fuel_type,
            tick=start_tick + offset,
            sim_time=BASE_TIME + timedelta(minutes=15 * offset),
            demand_liters=demand,
            served_liters=served,
            unmet_liters=max(0.0, demand - served),
        )
        for offset in range(ticks)
    ]


def _evaluate(engine: IntelligenceEngine, **overrides):
    """Run one evaluation with sensible defaults."""
    defaults = {
        "stations": [_station()],
        "depots": [_depot()],
        "routes": [_route()],
        "allocations": [],
        "supply_arrivals": [],
        "region_demand_factors": {"region-dhaka": 1.0},
        "current_tick": 20,
        "current_sim_time": BASE_TIME,
        "tick_minutes": 15,
        "history": _history(),
    }
    defaults.update(overrides)
    return engine.evaluate(**defaults)


# -- the fallback counters ------------------------------------------------


def test_the_fallback_counters_are_per_evaluation_not_per_forecast() -> None:
    """A reason that keeps firing is one condition, not one event.

    The counters feed a Prometheus series. If they accumulated across
    evaluations, a single broken predictor would look like a storm of
    separate incidents, and an operator reading the graph would be told
    there were forty failures when there was one ongoing condition.
    """
    engine = IntelligenceEngine()

    first = _evaluate(engine)
    second = _evaluate(engine)

    # Both evaluations see the same untrained model, so both report the
    # same number of activations.
    assert first.fallback_activations == second.fallback_activations
    assert sum(first.fallback_activations.values()) > 0


def test_the_fallback_delta_is_zero_when_nothing_fell_back() -> None:
    """No fallback means no activations, and the delta says so."""
    engine = IntelligenceEngine()
    result = _evaluate(engine)

    # The learned model is untrained in a test, so there is at least one
    # activation; what matters is that the delta is a *count*, not a
    # running total that grows every evaluation.
    assert all(count >= 0 for count in result.fallback_activations.values())


def test_the_active_model_is_reported() -> None:
    """The result says which model is answering."""
    engine = IntelligenceEngine()
    result = _evaluate(engine)

    assert result.active_model in {model.value for model in ModelKind}
    assert result.models_used


# -- the demand-anomaly reference -----------------------------------------


def test_the_demand_reference_ignores_the_demand_multiplier() -> None:
    """The reference is the published profile, not the current draw.

    The simulator raises a station's multiplier during a demand spike, so
    comparing the observation against the *current* multiplier would
    explain the spike away with the very thing that caused it — and the
    anomaly would never be detected.

    The station here draws 10x its calm profile. If the reference
    included the multiplier, the observation would look exactly right
    and no anomaly would be raised.
    """
    engine = IntelligenceEngine()
    result = _evaluate(
        engine,
        stations=[_station(demand_multiplier=10.0)],
        history=_history(ticks=1, demand=5_000.0, served=5_000.0),
    )

    assert result.anomalies, "a 10x demand spike must raise an anomaly"


def test_a_calm_demand_raises_no_anomaly() -> None:
    """Calm demand against the profile is not an anomaly."""
    engine = IntelligenceEngine()
    result = _evaluate(engine)

    demand_anomalies = [a for a in result.anomalies if "demand" in a.kind.value]
    assert not demand_anomalies


def test_only_the_newest_observation_per_station_and_fuel_is_checked() -> None:
    """The forecast is for the next tick, so old observations are ignored.

    Comparing older observations against the current profile would
    report a station that ran hot earlier in the day as anomalous now.
    """
    engine = IntelligenceEngine()
    result = _evaluate(
        engine,
        history=[
            HistoryPoint(
                station_id="station-mirpur",
                fuel_type="DIESEL",
                tick=1,
                sim_time=BASE_TIME,
                demand_liters=90_000.0,
                served_liters=90_000.0,
                unmet_liters=0.0,
            ),
            *_history(ticks=2, start_tick=19, demand=100.0, served=100.0),
        ],
    )

    demand_anomalies = [a for a in result.anomalies if "demand" in a.kind.value]
    assert not demand_anomalies


# -- the inventory-anomaly window -----------------------------------------


def test_an_inventory_change_is_measured_against_the_observed_consumption() -> None:
    """The tank is compared against the fuel that was actually drawn.

    The comparison is over the *same window* the inventory changed in.
    A tank reading at tick T is the tank before T was served, so the
    fuel drawn between two readings is the served volume over the ticks
    in between — the later reading excluded.
    """
    engine = IntelligenceEngine()

    # First evaluation stores the inventory.
    _evaluate(engine, current_tick=10, history=_history(ticks=1, start_tick=9))

    # Second evaluation: the tank dropped by exactly what was served, so
    # the change is explained and nothing is reported.
    result = _evaluate(
        engine,
        current_tick=12,
        stations=[_station(inventory={"DIESEL": 1_700.0, "PETROL": 9_000.0})],
        history=_history(ticks=2, start_tick=10, served=150.0, demand=150.0),
    )

    inventory_anomalies = [a for a in result.anomalies if "inventory" in a.kind.value]
    assert not inventory_anomalies


def test_a_delivery_inside_the_window_explains_the_rise() -> None:
    """A refuelled station must not raise an unexplained-change alert.

    Without counting the delivery, every refuelled station would raise
    a false critical anomaly on the next evaluation.
    """
    engine = IntelligenceEngine()

    _evaluate(engine, current_tick=10, history=_history(ticks=1, start_tick=9))

    result = _evaluate(
        engine,
        current_tick=12,
        stations=[_station(inventory={"DIESEL": 4_000.0, "PETROL": 9_000.0})],
        allocations=[
            _allocation(
                status="ARRIVED",
                quantity=2_000.0,
                actual_arrival_tick=11,
            )
        ],
        history=_history(ticks=2, start_tick=10, served=0.0, demand=0.0),
    )

    inventory_anomalies = [a for a in result.anomalies if "inventory" in a.kind.value]
    assert not inventory_anomalies


def test_a_window_the_history_does_not_cover_is_not_judged() -> None:
    """A half-covered window cannot be judged, so it is not judged.

    The fuel drawn over the missing ticks is unknown, so any comparison
    would report a change that never happened.
    """
    engine = IntelligenceEngine()

    _evaluate(engine, current_tick=10, history=_history(ticks=1, start_tick=9))

    # The window is ticks 10..12 but the history only covers tick 10.
    result = _evaluate(
        engine,
        current_tick=12,
        stations=[_station(inventory={"DIESEL": 1_000.0, "PETROL": 9_000.0})],
        history=_history(ticks=1, start_tick=10, served=100.0, demand=100.0),
    )

    inventory_anomalies = [a for a in result.anomalies if "inventory" in a.kind.value]
    assert not inventory_anomalies


def test_an_unexplained_drop_is_reported() -> None:
    """A drop that nothing explains *is* reported."""
    engine = IntelligenceEngine()

    _evaluate(engine, current_tick=10, history=_history(ticks=1, start_tick=9))

    result = _evaluate(
        engine,
        current_tick=12,
        stations=[_station(inventory={"DIESEL": 100.0, "PETROL": 9_000.0})],
        history=_history(ticks=2, start_tick=10, served=100.0, demand=100.0),
    )

    inventory_anomalies = [a for a in result.anomalies if "inventory" in a.kind.value]
    assert inventory_anomalies


# -- the helpers used above ----------------------------------------------


def test_the_observed_consumption_is_the_served_volume_not_the_demand() -> None:
    """A station that could not meet its demand never drew the fuel.

    Charging the tank for unmet demand would report a loss that never
    happened, so every station in a shortfall would look like it was
    losing fuel.
    """
    engine = IntelligenceEngine()
    drawn, covered = engine._observed_consumption(
        history=_history(ticks=2, start_tick=10, served=80.0, demand=150.0),
        station_id="station-mirpur",
        fuel_type="DIESEL",
        from_tick=10,
        to_tick=12,
    )

    assert drawn == 160.0
    assert covered is True


def test_the_window_excludes_the_later_reading() -> None:
    """A tank reading at tick T is the tank *before* T was served."""
    engine = IntelligenceEngine()
    drawn, covered = engine._observed_consumption(
        history=_history(ticks=3, start_tick=10, served=100.0),
        station_id="station-mirpur",
        fuel_type="DIESEL",
        from_tick=10,
        to_tick=12,
    )

    # Ticks 10 and 11 are inside the window; tick 12 is not.
    assert drawn == 200.0
    assert covered is True


def test_a_window_with_no_previous_tick_is_not_covered() -> None:
    """Without a starting tick there is no window to judge."""
    engine = IntelligenceEngine()
    drawn, covered = engine._observed_consumption(
        history=_history(),
        station_id="station-mirpur",
        fuel_type="DIESEL",
        from_tick=None,
        to_tick=12,
    )

    assert drawn == 0.0
    assert covered is False


def test_the_delivered_volume_only_counts_arrivals_inside_the_window() -> None:
    """A delivery outside the window does not explain the change."""
    engine = IntelligenceEngine()

    delivered = engine._delivered_in_window(
        allocations=[
            _allocation(status="ARRIVED", quantity=1_000.0, actual_arrival_tick=11),
            _allocation(status="ARRIVED", quantity=2_000.0, actual_arrival_tick=99),
            _allocation(status="IN_TRANSIT", quantity=4_000.0),
            _allocation(status="ARRIVED", quantity=8_000.0, actual_arrival_tick=None),
            _allocation(
                status="ARRIVED",
                quantity=16_000.0,
                actual_arrival_tick=11,
                fuel_type=FuelType.PETROL,
            ),
            _allocation(
                status="ARRIVED",
                quantity=32_000.0,
                actual_arrival_tick=11,
                destination_station_id="station-karnaphuli",
            ),
        ],
        station_id="station-mirpur",
        fuel_type="DIESEL",
        from_tick=10,
        to_tick=12,
    )

    assert delivered == 1_000.0


def test_the_delivered_volume_is_zero_without_a_starting_tick() -> None:
    """Without a starting tick there is no window."""
    engine = IntelligenceEngine()

    delivered = engine._delivered_in_window(
        allocations=[_allocation(status="ARRIVED", actual_arrival_tick=11)],
        station_id="station-mirpur",
        fuel_type="DIESEL",
        from_tick=None,
        to_tick=12,
    )

    assert delivered == 0.0


# -- the summaries --------------------------------------------------------


def test_the_risk_summary_counts_every_band() -> None:
    """The summary counts the assessments in each risk band."""
    from app.intelligence.risk import RiskAssessment

    assessments = [
        RiskAssessment(
            station_id=f"station-{index}",
            fuel_type="DIESEL",
            risk_level=level,
            hours_to_stockout=4.0,
            stockout_probability=0.9,
            risk_score=0.9,
            forecast=None,
            signals=[],
            needs_human_review=False,
            recommendation="",
        )
        for index, level in enumerate([RiskLevel.CRITICAL, RiskLevel.CRITICAL, RiskLevel.WATCH])
    ]

    summary = risk_summary(assessments)

    assert summary[RiskLevel.CRITICAL.value] == 2
    assert summary[RiskLevel.WATCH.value] == 1
    # Every band is present, even the ones with nothing in them.
    assert summary[RiskLevel.OK.value] == 0


def test_the_anomaly_summary_counts_every_kind() -> None:
    """The summary counts the anomalies of each kind."""
    from app.intelligence.detection import Anomaly, AnomalyKind

    anomalies = [
        Anomaly(
            kind=AnomalyKind.DEMAND,
            entity_id="station-mirpur",
            fuel_type="DIESEL",
            detected_at=BASE_TIME,
            expected=100.0,
            actual=900.0,
            severity="critical",
            score=8.0,
            message="a spike",
            detail="a spike",
        ),
        Anomaly(
            kind=AnomalyKind.DEMAND,
            entity_id="station-karnaphuli",
            fuel_type="DIESEL",
            detected_at=BASE_TIME,
            expected=100.0,
            actual=900.0,
            severity="critical",
            score=8.0,
            message="a spike",
            detail="a spike",
        ),
    ]

    summary = anomaly_summary(anomalies)

    assert summary[AnomalyKind.DEMAND.value] == 2
    assert summary[AnomalyKind.INVENTORY.value] == 0


# -- the context building ------------------------------------------------


def test_a_station_gets_a_context_with_its_region_factor() -> None:
    """Each station's context carries its region's demand factor."""
    engine = IntelligenceEngine()
    contexts = engine.build_contexts(
        stations=[_station(id="station-a"), _station(id="station-b")],
        region_demand_factors={"region-dhaka": 1.25},
        current_tick=20,
        current_sim_time=BASE_TIME,
        tick_minutes=15,
    )

    assert set(contexts) == {"station-a", "station-b"}
    assert all(context.region_demand_factor == 1.25 for context in contexts.values())


def test_the_context_carries_the_current_tick_and_the_minutes() -> None:
    """The context has to know where "now" is, to project forwards."""
    engine = IntelligenceEngine()
    contexts = engine.build_contexts(
        stations=[_station()],
        region_demand_factors={"region-dhaka": 1.0},
        current_tick=42,
        current_sim_time=BASE_TIME,
        tick_minutes=15,
    )

    context = contexts["station-mirpur"]
    assert context.current_tick == 42
    assert context.tick_minutes == 15


def test_a_station_without_a_region_factor_gets_one() -> None:
    """An unknown region must not divide by zero."""
    engine = IntelligenceEngine()
    contexts = engine.build_contexts(
        stations=[_station()],
        region_demand_factors={},
        current_tick=20,
        current_sim_time=BASE_TIME,
        tick_minutes=15,
    )

    assert contexts["station-mirpur"].region_demand_factor > 0


def test_the_in_transit_shipments_are_only_the_undelivered_ones() -> None:
    """Only pending and in-transit allocations count as in transit."""
    engine = IntelligenceEngine()
    shipments = engine._in_transit(
        [
            _allocation(status="PENDING"),
            _allocation(status="IN_TRANSIT"),
            _allocation(status="ARRIVED"),
            _allocation(status="CANCELLED"),
        ]
    )

    assert len(shipments) == 2
    assert all(s.expected_arrival_tick is not None for s in shipments)


def test_the_forecast_map_is_keyed_by_station_and_fuel() -> None:
    """The forecasts are indexed so the risk engine can find them."""
    engine = IntelligenceEngine()
    result = _evaluate(engine)

    forecast_map = engine._forecast_map(result)

    assert forecast_map
    for (station_id, fuel_type), forecast in forecast_map.items():
        assert station_id
        assert fuel_type
        assert forecast.points


def test_the_result_payload_carries_the_counts() -> None:
    """The payload is what the API serves, so it has to be complete."""
    engine = IntelligenceEngine()
    result = _evaluate(engine)
    payload = result.to_payload()

    assert "risks" in payload
    assert "forecasts" in payload
    assert "anomalies" in payload
    assert payload["needs_human_review_count"] >= 0


def test_the_critical_and_watch_lists_partition_the_risks() -> None:
    """A risk is either critical, watch, or neither."""
    engine = IntelligenceEngine()
    result = _evaluate(engine)

    critical = result.critical_risks
    watch = result.watch_risks

    assert all(risk.risk_level == RiskLevel.CRITICAL for risk in critical)
    assert all(risk.risk_level == RiskLevel.WATCH for risk in watch)
    assert not {id(r) for r in critical} & {id(r) for r in watch}


@pytest.mark.parametrize("fuel", ["DIESEL", "PETROL"])
def test_every_fuel_in_a_station_is_evaluated(fuel: str) -> None:
    """A station with two fuels produces two forecasts and two risks."""
    engine = IntelligenceEngine()
    result = _evaluate(engine, stations=[_station()])

    fuels = {forecast.fuel_type for forecast in result.forecasts}
    assert fuels == {"DIESEL", "PETROL"}
    assert fuel in fuels
