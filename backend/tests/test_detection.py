"""Tests for the anomaly detectors and the bottleneck detector."""

from __future__ import annotations

import pytest

from app.intelligence.detection import (
    AnomalyKind,
    BottleneckDetector,
    BottleneckKind,
    DemandAnomalyDetector,
    InventoryAnomalyDetector,
    combine_ewma,
    is_significant,
    percentage_change,
    z_score,
)

# --- demand anomalies ------------------------------------------------------


def _message_agrees_with_itself(message: str) -> bool:
    """Whether the percentage and the litre figures tell the same story.

    The message quotes a percentage and two litre figures. If the
    percentage is derived from the smoothed level, the litres must be the
    smoothed level too — quoting the raw observation beside a drift
    percentage produces a sentence that argues with itself.
    """
    import re

    match = re.search(
        r"is (?P<pct>-?\d+)% (?P<dir>above|below) the expected level"
        r"(?P<smoothed> on average)? \((?P<actual>[\d,]+) L"
        r"(?P<kind> smoothed)? against (?P<expected>[\d,]+) L",
        message,
    )
    if match is None:
        return False

    actual = float(match.group("actual").replace(",", ""))
    expected = float(match.group("expected").replace(",", ""))
    if expected == 0:
        return False

    deviation = abs(actual - expected) / expected
    # The message rounds the percentage to whole numbers.
    return abs(deviation * 100 - abs(int(match.group("pct")))) <= 1.0


def test_a_normal_observation_is_not_an_anomaly() -> None:
    detector = DemandAnomalyDetector()

    anomaly = detector.detect(
        station_id="station-mirpur",
        fuel_type="DIESEL",
        expected=100.0,
        actual=102.0,
        uncertainty=12.0,
    )

    assert anomaly is None


def test_a_large_demand_spike_is_an_anomaly() -> None:
    detector = DemandAnomalyDetector()

    anomaly = detector.detect(
        station_id="station-mirpur",
        fuel_type="DIESEL",
        expected=100.0,
        actual=200.0,
        uncertainty=12.0,
    )

    assert anomaly is not None
    assert anomaly.kind is AnomalyKind.DEMAND
    assert anomaly.severity == "critical"
    assert "above the expected level" in anomaly.message
    assert anomaly.detail["z_score"] > 3


def test_a_demand_drop_is_an_anomaly() -> None:
    detector = DemandAnomalyDetector()

    anomaly = detector.detect(
        station_id="station-mirpur",
        fuel_type="DIESEL",
        expected=100.0,
        actual=10.0,
        uncertainty=12.0,
    )

    assert anomaly is not None
    assert "below the expected level" in anomaly.message


def test_a_sustained_shift_within_the_noise_bound_is_not_an_anomaly() -> None:
    """A 15% sustained shift is inside the published noise bound.

    Flagging it would be a false positive, and a detector that cries wolf
    is worse than no detector at all.
    """
    detector = DemandAnomalyDetector()

    anomalies = []
    for _ in range(40):
        anomaly = detector.detect(
            station_id="station-mirpur",
            fuel_type="DIESEL",
            expected=100.0,
            actual=115.0,
            uncertainty=25.0,
        )
        if anomaly is not None:
            anomalies.append(anomaly)

    assert anomalies == []


def test_a_sustained_large_shift_is_caught_by_the_ewma() -> None:
    """The EWMA catches a level shift the z-score alone would miss.

    Here the per-tick uncertainty is wide enough that no single tick looks
    extreme, but the level has clearly moved: the EWMA notices.
    """
    detector = DemandAnomalyDetector()

    anomalies = []
    for _ in range(40):
        anomaly = detector.detect(
            station_id="station-mirpur",
            fuel_type="DIESEL",
            expected=100.0,
            actual=140.0,
            uncertainty=60.0,
        )
        if anomaly is not None:
            anomalies.append(anomaly)

    assert anomalies, "a sustained 40% shift must be flagged"
    # The z-score alone would not have fired: 40 L over a 60 L spread.
    assert anomalies[-1].detail["z_score"] < 3.0
    assert anomalies[-1].detail["relative_drift"] >= 0.25


def test_a_zero_width_interval_does_not_produce_an_infinite_z_score() -> None:
    detector = DemandAnomalyDetector()

    anomaly = detector.detect(
        station_id="station-mirpur",
        fuel_type="DIESEL",
        expected=100.0,
        actual=100.0,
        uncertainty=0.0,
    )

    assert anomaly is None


def test_the_anomaly_is_scoped_to_the_station_and_fuel() -> None:
    detector = DemandAnomalyDetector()

    detector.detect(
        station_id="station-mirpur",
        fuel_type="DIESEL",
        expected=100.0,
        actual=300.0,
        uncertainty=10.0,
    )

    # A different fuel at the same station must not inherit the level.
    other = detector.detect(
        station_id="station-mirpur",
        fuel_type="PETROL",
        expected=100.0,
        actual=101.0,
        uncertainty=10.0,
    )

    assert other is None


def test_the_anomaly_message_does_not_contradict_itself() -> None:
    """The percentage and the litre figures must describe one comparison.

    The EWMA drift and the raw observation are different numbers. Quoting
    the drift beside the raw litres produced messages like "demand is -4%
    below the expected level (259 L against 128 L)" — a sentence that
    argues with itself.
    """
    detector = DemandAnomalyDetector()

    # Prime the smoothed level low, then report a much higher observation.
    # The drift stays small while the observation is far above expectation,
    # which is exactly the combination that produced the contradiction.
    detector.detect(
        station_id="station-mirpur",
        fuel_type="DIESEL",
        expected=128.0,
        actual=64.7,
        uncertainty=10.0,
    )

    anomaly = detector.detect(
        station_id="station-mirpur",
        fuel_type="DIESEL",
        expected=128.0,
        actual=259.0,
        uncertainty=10.0,
    )

    assert anomaly is not None
    assert _message_agrees_with_itself(anomaly.message)


def test_a_single_observation_quotes_the_observation() -> None:
    detector = DemandAnomalyDetector()

    anomaly = detector.detect(
        station_id="station-mirpur",
        fuel_type="DIESEL",
        expected=100.0,
        actual=259.0,
        uncertainty=10.0,
    )

    assert anomaly is not None
    assert "259 L against 100 L" in anomaly.message
    assert _message_agrees_with_itself(anomaly.message)


def test_a_sustained_shift_quotes_the_smoothed_level() -> None:
    """A sustained shift is reported against the smoothed level."""
    detector = DemandAnomalyDetector()

    anomaly = None
    for _ in range(40):
        anomaly = detector.detect(
            station_id="station-mirpur",
            fuel_type="DIESEL",
            expected=100.0,
            actual=160.0,
            uncertainty=80.0,
        )

    assert anomaly is not None
    assert "on average" in anomaly.message
    assert _message_agrees_with_itself(anomaly.message)


def test_a_delivery_inside_the_window_explains_the_rise() -> None:
    """Fuel that arrived is not an unexplained change."""
    detector = InventoryAnomalyDetector()

    anomaly = detector.detect(
        entity_id="station-mirpur",
        fuel_type="DIESEL",
        previous_inventory=1000.0,
        current_inventory=3000.0,
        expected_consumption=100.0,
        expected_deliveries=2000.0,
    )

    assert anomaly is None


def test_consumption_over_several_ticks_is_not_an_anomaly() -> None:
    """The window is as many ticks as actually elapsed.

    Two evaluations are typically several ticks apart, so a change has to
    be compared against the demand over that whole window — not against a
    single tick's worth. Comparing against one tick turns every normal
    refill into a false critical alert.
    """
    detector = InventoryAnomalyDetector()

    # Ten ticks at 100 L each: the tank drops by 1000 L, which is exactly
    # what the demand over those ten ticks explains.
    anomaly = detector.detect(
        entity_id="station-mirpur",
        fuel_type="DIESEL",
        previous_inventory=5000.0,
        current_inventory=4000.0,
        expected_consumption=1000.0,
    )

    assert anomaly is None


# --- inventory anomalies ---------------------------------------------------


def test_a_consumption_matching_change_is_not_an_anomaly() -> None:
    detector = InventoryAnomalyDetector()

    anomaly = detector.detect(
        entity_id="station-mirpur",
        fuel_type="DIESEL",
        previous_inventory=1000.0,
        current_inventory=900.0,
        expected_consumption=100.0,
    )

    assert anomaly is None


def test_an_unexplained_inventory_drop_is_an_anomaly() -> None:
    detector = InventoryAnomalyDetector()

    anomaly = detector.detect(
        entity_id="station-mirpur",
        fuel_type="DIESEL",
        previous_inventory=1000.0,
        current_inventory=500.0,
        expected_consumption=100.0,
    )

    assert anomaly is not None
    assert anomaly.kind is AnomalyKind.INVENTORY
    assert "dropped" in anomaly.message


def test_an_unexplained_inventory_rise_is_an_anomaly() -> None:
    detector = InventoryAnomalyDetector()

    anomaly = detector.detect(
        entity_id="station-mirpur",
        fuel_type="DIESEL",
        previous_inventory=1000.0,
        current_inventory=1500.0,
        expected_consumption=100.0,
    )

    assert anomaly is not None
    assert "rose" in anomaly.message


def test_a_small_absolute_change_is_ignored() -> None:
    """A busy station must not be flagged for a small wobble."""
    detector = InventoryAnomalyDetector()

    anomaly = detector.detect(
        entity_id="station-mirpur",
        fuel_type="DIESEL",
        previous_inventory=1000.0,
        current_inventory=990.0,
        expected_consumption=100.0,
    )

    assert anomaly is None


def test_a_delivery_that_matches_expectation_is_not_an_anomaly() -> None:
    detector = InventoryAnomalyDetector()

    anomaly = detector.detect(
        entity_id="station-mirpur",
        fuel_type="DIESEL",
        previous_inventory=1000.0,
        current_inventory=1900.0,
        expected_consumption=100.0,
        expected_deliveries=1000.0,
    )

    assert anomaly is None


# --- bottlenecks -----------------------------------------------------------


def test_a_disrupted_route_is_a_bottleneck() -> None:
    detector = BottleneckDetector()

    bottlenecks = detector.detect(
        depots=[],
        routes=[{"id": "route-gazipur-mirpur", "status": "DISRUPTED", "transit_ticks": 2}],
        supply_arrivals=[],
        stations=[],
        allocations_in_transit=[],
    )

    assert len(bottlenecks) == 1
    assert bottlenecks[0].kind is BottleneckKind.ROUTE_DISRUPTED
    assert bottlenecks[0].severity == "critical"


def test_an_available_route_is_not_a_bottleneck() -> None:
    detector = BottleneckDetector()

    bottlenecks = detector.detect(
        depots=[],
        routes=[{"id": "route-gazipur-mirpur", "status": "AVAILABLE", "transit_ticks": 2}],
        supply_arrivals=[],
        stations=[],
        allocations_in_transit=[],
    )

    assert bottlenecks == []


def test_a_depot_near_its_dispatch_limit_is_a_bottleneck() -> None:
    detector = BottleneckDetector()

    bottlenecks = detector.detect(
        depots=[{"id": "depot-gazipur", "dispatch_capacity_per_tick": 1000.0}],
        routes=[],
        supply_arrivals=[],
        stations=[],
        allocations_in_transit=[
            {"source_depot_id": "depot-gazipur", "quantity": 900.0, "status": "PENDING"}
        ],
    )

    assert len(bottlenecks) == 1
    assert bottlenecks[0].kind is BottleneckKind.DISPATCH_CAPACITY
    assert bottlenecks[0].detail["ratio"] == 0.9


def test_a_depot_with_room_to_dispatch_is_not_a_bottleneck() -> None:
    detector = BottleneckDetector()

    bottlenecks = detector.detect(
        depots=[{"id": "depot-gazipur", "dispatch_capacity_per_tick": 1000.0}],
        routes=[],
        supply_arrivals=[],
        stations=[],
        allocations_in_transit=[
            {"source_depot_id": "depot-gazipur", "quantity": 100.0, "status": "PENDING"}
        ],
    )

    assert bottlenecks == []


def test_a_low_stock_depot_is_a_bottleneck() -> None:
    detector = BottleneckDetector()

    bottlenecks = detector.detect(
        depots=[
            {
                "id": "depot-gazipur",
                "dispatch_capacity_per_tick": 1000.0,
                "inventory": {"DIESEL": 500.0, "PETROL": 50000.0},
                "capacity": {"DIESEL": 90000.0, "PETROL": 70000.0},
            }
        ],
        routes=[],
        supply_arrivals=[],
        stations=[],
        allocations_in_transit=[],
    )

    assert len(bottlenecks) == 1
    assert bottlenecks[0].kind is BottleneckKind.DEPOT_LOW_STOCK
    assert bottlenecks[0].entity_id == "depot-gazipur"


def test_a_delayed_supply_arrival_is_a_bottleneck() -> None:
    detector = BottleneckDetector()

    bottlenecks = detector.detect(
        depots=[],
        routes=[],
        supply_arrivals=[
            {"id": "supply-1", "status": "DELAYED", "delay_ticks": 3, "quantity": 5000.0}
        ],
        stations=[],
        allocations_in_transit=[],
    )

    assert len(bottlenecks) == 1
    assert bottlenecks[0].kind is BottleneckKind.SUPPLY_DELAYED
    assert bottlenecks[0].detail["delay_hours"] == 0.75


def test_an_outage_station_is_a_bottleneck() -> None:
    detector = BottleneckDetector()

    bottlenecks = detector.detect(
        depots=[],
        routes=[],
        supply_arrivals=[],
        stations=[{"id": "station-mirpur", "status": "OUTAGE"}],
        allocations_in_transit=[],
    )

    assert len(bottlenecks) == 1
    assert bottlenecks[0].kind is BottleneckKind.STATION_OUTAGE


def test_a_healthy_network_has_no_bottlenecks() -> None:
    detector = BottleneckDetector()

    bottlenecks = detector.detect(
        depots=[
            {
                "id": "depot-gazipur",
                "dispatch_capacity_per_tick": 1000.0,
                "inventory": {"DIESEL": 50000.0},
                "capacity": {"DIESEL": 90000.0},
            }
        ],
        routes=[{"id": "route-gazipur-mirpur", "status": "AVAILABLE", "transit_ticks": 2}],
        supply_arrivals=[{"id": "supply-1", "status": "SCHEDULED", "quantity": 5000.0}],
        stations=[{"id": "station-mirpur", "status": "OPEN"}],
        allocations_in_transit=[],
    )

    assert bottlenecks == []


# --- the small helpers -----------------------------------------------------


def test_combine_ewma_weights_the_recent_values_more() -> None:
    result = combine_ewma([0.0, 0.0, 0.0, 100.0], alpha=0.3)

    assert result == pytest.approx(30.0)


def test_combine_ewma_of_nothing_is_zero() -> None:
    assert combine_ewma([]) == 0.0


def test_z_score_is_safe_against_zero_standard_deviation() -> None:
    assert z_score(10.0, 0.0, 0.0) > 0


def test_percentage_change_handles_a_zero_base() -> None:
    assert percentage_change(0.0, 5.0) == 0.0


def test_is_significant_compares_against_the_threshold() -> None:
    assert is_significant(3.5)
    assert not is_significant(2.9)
