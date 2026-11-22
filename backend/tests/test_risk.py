"""Tests for the risk engine.

Note the units: a tick is 15 simulated minutes, so 100 L per tick against
500 L of stock is five ticks of cover — that is 1.25 simulated hours.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from app.intelligence.forecasting import Forecast, ForecastPoint, ModelKind
from app.intelligence.risk import InTransitShipment, RiskEngine, RiskLevel

BASE_TIME = datetime(2026, 1, 1, 8, 0, tzinfo=UTC)

#: Minutes in a tick.
TICK_MINUTES = 15


def _forecast(
    *,
    per_tick: float = 100.0,
    ticks: int = 24,
    spread: float = 0.0,
    model: ModelKind = ModelKind.BASELINE,
    confidence: float = 0.9,
) -> Forecast:
    """A forecast with a constant demand per tick."""
    points = []
    for offset in range(1, ticks + 1):
        points.append(
            ForecastPoint(
                tick=10 + offset,
                sim_time=BASE_TIME,
                predicted_demand=per_tick,
                lower_bound=max(0.0, per_tick - spread),
                upper_bound=per_tick + spread,
                confidence=confidence,
            )
        )
    return Forecast(
        station_id="station-mirpur",
        fuel_type="DIESEL",
        model=model,
        horizon_ticks=ticks,
        points=points,
        confidence=confidence,
        needs_human_review=False,
    )


def _assess(
    *,
    inventory: float,
    per_tick: float = 100.0,
    capacity: float = 15000.0,
    in_transit: list[InTransitShipment] | None = None,
    spread: float = 0.0,
    ticks: int = 24,
):
    """Assess one station/fuel pair."""
    engine = RiskEngine()
    return engine.assess(
        station_id="station-mirpur",
        fuel_type="DIESEL",
        inventory=inventory,
        capacity=capacity,
        forecast=_forecast(per_tick=per_tick, spread=spread, ticks=ticks),
        in_transit=in_transit or [],
        tick_minutes=TICK_MINUTES,
    )


def test_a_well_stocked_station_is_ok() -> None:
    assessment = _assess(inventory=14000.0, per_tick=100.0)

    assert assessment.risk_level is RiskLevel.OK
    assert assessment.hours_to_stockout is None
    assert assessment.stockout_probability == 0.0
    assert assessment.needs_human_review is False


def test_a_station_that_will_run_dry_is_critical() -> None:
    """500 L of stock against 100 L per tick is five ticks of cover."""
    assessment = _assess(inventory=500.0, per_tick=100.0)

    assert assessment.risk_level is RiskLevel.CRITICAL
    assert assessment.hours_to_stockout is not None
    assert assessment.hours_to_stockout == pytest.approx(5 * TICK_MINUTES / 60, abs=0.3)
    assert assessment.stockout_probability == 1.0
    assert "Dispatch" in assessment.recommendation


def test_a_station_with_moderate_cover_is_on_watch() -> None:
    """64 ticks of cover is 16 simulated hours — inside the WATCH band."""
    assessment = _assess(inventory=6400.0, per_tick=100.0, ticks=96)

    assert assessment.hours_to_stockout == pytest.approx(16.0, abs=0.3)
    assert assessment.risk_level is RiskLevel.WATCH
    assert "Watch" in assessment.recommendation


def test_the_hours_to_stockout_is_reported_in_simulated_hours() -> None:
    """Eight ticks of 15 minutes is two simulated hours."""
    assessment = _assess(inventory=800.0, per_tick=100.0)

    assert assessment.hours_to_stockout == pytest.approx(2.0, abs=0.3)


def test_incoming_fuel_extends_the_cover() -> None:
    """Fuel already in transit will arrive, so it counts."""
    without = _assess(inventory=500.0, per_tick=100.0)
    with_arrival = _assess(
        inventory=500.0,
        per_tick=100.0,
        in_transit=[
            InTransitShipment(
                allocation_id=1,
                destination_station_id="station-mirpur",
                fuel_type="DIESEL",
                quantity=1000.0,
                expected_arrival_tick=30,
                route_id="route-gazipur-mirpur",
            )
        ],
    )

    assert without.hours_to_stockout is not None
    assert with_arrival.hours_to_stockout is not None
    assert with_arrival.hours_to_stockout > without.hours_to_stockout


def test_a_shipment_for_another_fuel_does_not_count() -> None:
    assessment = _assess(
        inventory=500.0,
        per_tick=100.0,
        in_transit=[
            InTransitShipment(
                allocation_id=1,
                destination_station_id="station-mirpur",
                fuel_type="PETROL",
                quantity=5000.0,
                expected_arrival_tick=30,
                route_id="route-gazipur-mirpur",
            )
        ],
    )

    assert assessment.hours_to_stockout == pytest.approx(5 * TICK_MINUTES / 60, abs=0.3)


def test_a_shipment_arriving_after_the_horizon_does_not_count() -> None:
    assessment = _assess(
        inventory=500.0,
        per_tick=100.0,
        in_transit=[
            InTransitShipment(
                allocation_id=1,
                destination_station_id="station-mirpur",
                fuel_type="DIESEL",
                quantity=5000.0,
                expected_arrival_tick=500,
                route_id="route-gazipur-mirpur",
            )
        ],
    )

    assert assessment.hours_to_stockout == pytest.approx(5 * TICK_MINUTES / 60, abs=0.3)


def test_a_stockout_that_cannot_happen_has_zero_probability() -> None:
    """The forecast's upper bound never reaches the stock."""
    assessment = _assess(inventory=100000.0, per_tick=100.0)

    assert assessment.stockout_probability == 0.0
    assert assessment.risk_level is RiskLevel.OK


def test_a_wide_interval_lowers_the_probability() -> None:
    """Uncertainty must not be reported as certainty."""
    tight = _assess(inventory=2000.0, per_tick=100.0, spread=0.0)
    wide = _assess(inventory=2000.0, per_tick=100.0, spread=500.0)

    # A tight forecast that overruns the stock is certain.
    assert tight.stockout_probability == 1.0
    # A wide one is not.
    assert 0.0 < wide.stockout_probability < 1.0
    assert wide.stockout_probability < tight.stockout_probability


def test_a_certain_stockout_is_reported_as_certain() -> None:
    """Even the lower bound of the forecast exceeds the stock."""
    assessment = _assess(inventory=100.0, per_tick=100.0, spread=10.0)

    assert assessment.stockout_probability == 1.0


def test_the_assessment_carries_the_signals_that_drove_it() -> None:
    assessment = _assess(inventory=500.0, per_tick=100.0)

    names = {signal.name for signal in assessment.signals}
    assert "inventory_liters" in names
    assert "forecast_demand_liters" in names
    assert "stockout_probability" in names
    assert "hours_to_stockout" in names


def test_a_low_confidence_forecast_is_flagged_for_review() -> None:
    engine = RiskEngine()

    assessment = engine.assess(
        station_id="station-mirpur",
        fuel_type="DIESEL",
        inventory=1000.0,
        capacity=15000.0,
        forecast=_forecast(confidence=0.2),
        in_transit=[],
        tick_minutes=TICK_MINUTES,
    )

    assert assessment.needs_human_review is True


def test_the_risk_score_rises_as_the_cover_falls() -> None:
    comfortable = _assess(inventory=14000.0, per_tick=100.0)
    tight = _assess(inventory=1000.0, per_tick=100.0)

    assert tight.risk_score > comfortable.risk_score


def test_a_shipment_for_another_station_does_not_count() -> None:
    """Fuel in transit to another station is not cover for this one.

    Counting it as cover would hide the very shortage the risk engine
    exists to surface, and it would do so for every station at once.
    """
    assessment = _assess(
        inventory=500.0,
        per_tick=100.0,
        in_transit=[
            InTransitShipment(
                allocation_id=1,
                destination_station_id="station-tongi",
                fuel_type="DIESEL",
                quantity=50000.0,
                expected_arrival_tick=30,
                route_id="route-gazipur-tongi",
            )
        ],
    )

    assert assessment.hours_to_stockout == pytest.approx(5 * TICK_MINUTES / 60, abs=0.3)
    assert assessment.risk_level is RiskLevel.CRITICAL


def test_a_shipment_for_another_station_does_not_hide_a_stockout() -> None:
    """The probability must not fall because fuel is going elsewhere."""
    without = _assess(inventory=500.0, per_tick=100.0)
    elsewhere = _assess(
        inventory=500.0,
        per_tick=100.0,
        in_transit=[
            InTransitShipment(
                allocation_id=1,
                destination_station_id="station-coxsbazar",
                fuel_type="DIESEL",
                quantity=50000.0,
                expected_arrival_tick=30,
                route_id="route-patiya-coxsbazar",
            )
        ],
    )

    assert elsewhere.stockout_probability == without.stockout_probability
    assert elsewhere.hours_to_stockout == without.hours_to_stockout


def test_the_payload_is_json_safe() -> None:
    assessment = _assess(inventory=500.0, per_tick=100.0)

    payload = assessment.to_payload()
    json.dumps(payload)
    assert payload["risk_level"] == "CRITICAL"
    assert payload["signals"]
