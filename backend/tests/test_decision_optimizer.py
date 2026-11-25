"""Tests for the decision engine's optimizer, validator and split logic.

The decision engine is the part of the platform that decides *what to
ship*. Everything it produces is a proposal a human approves, so the
tests here are about two things:

* **it never proposes something the simulator would refuse**, and
* **it says why** when it proposes nothing.

The cases that matter are the edges: a route that cannot carry the
deficit, a depot that is closed, a tank with no room, a deficit bigger
than any single shipment. A decision engine is only as trustworthy as
its handling of the cases where the answer is "no, and here is why".
"""

from __future__ import annotations

import pytest
from decision_fixtures import build_forecast, build_risk, build_world

from app.decision.constraints import ConstraintCode, ConstraintReport
from app.decision.optimizer import AllocationOptimizer, OptimizerConfig
from app.decision.validator import AllocationValidator
from app.decision.world import DepotSnapshot, RouteSnapshot, StationSnapshot
from app.intelligence.risk import RiskLevel


def _optimizer(**config_kwargs) -> AllocationOptimizer:
    """An optimizer with the production configuration."""
    return AllocationOptimizer(config=OptimizerConfig(**config_kwargs))


def _critical_risk(**overrides):
    """A critical risk for Mirpur diesel."""
    risk = build_risk(**overrides)
    return risk


# -- the validator --------------------------------------------------------


def test_a_valid_allocation_passes_every_check() -> None:
    """A good allocation has no violations at all."""
    validator = AllocationValidator()
    report = validator.validate(
        world=build_world(),
        source_depot_id="depot-gazipur",
        destination_station_id="station-mirpur",
        route_id="route-gazipur-mirpur",
        fuel_type="DIESEL",
        quantity=1_000.0,
    )

    assert report.is_valid
    assert report.violations == []
    assert report.primary_violation is None


def test_a_route_that_does_not_exist_is_a_mismatch() -> None:
    """Naming a route that is not there is a route mismatch."""
    validator = AllocationValidator()
    report = validator.validate(
        world=build_world(),
        source_depot_id="depot-gazipur",
        destination_station_id="station-mirpur",
        route_id="route-nope",
        fuel_type="DIESEL",
        quantity=1_000.0,
    )

    assert not report.is_valid
    assert report.primary_violation.code == ConstraintCode.ROUTE_MISMATCH


def test_a_route_between_other_endpoints_is_a_mismatch() -> None:
    """A route that connects elsewhere does not connect here."""
    validator = AllocationValidator()
    report = validator.validate(
        world=build_world(),
        source_depot_id="depot-patiya",
        destination_station_id="station-mirpur",
        route_id="route-gazipur-mirpur",
        fuel_type="DIESEL",
        quantity=1_000.0,
    )

    assert not report.is_valid
    assert report.primary_violation.code == ConstraintCode.ROUTE_MISMATCH


def test_a_closed_depot_cannot_ship() -> None:
    """A closed depot is a violation."""
    world = build_world(
        depots={
            "depot-gazipur": DepotSnapshot(
                id="depot-gazipur",
                status="CLOSED",
                dispatch_capacity_per_tick=12_000.0,
                inventory={"DIESEL": 60_000.0},
                capacity={"DIESEL": 90_000.0},
            )
        }
    )
    validator = AllocationValidator()
    report = validator.validate(
        world=world,
        source_depot_id="depot-gazipur",
        destination_station_id="station-mirpur",
        route_id="route-gazipur-mirpur",
        fuel_type="DIESEL",
        quantity=1_000.0,
    )

    assert not report.is_valid
    assert report.primary_violation.code == ConstraintCode.DEPOT_CLOSED


def test_a_constrained_depot_can_still_ship() -> None:
    """A CONSTRAINED depot is still shippable — that is the point of it.

    A depot under constraint still has stock and still has a route, so
    refusing to ship from it would turn a soft problem into a hard one.
    """
    world = build_world(
        depots={
            "depot-gazipur": DepotSnapshot(
                id="depot-gazipur",
                status="CONSTRAINED",
                dispatch_capacity_per_tick=12_000.0,
                inventory={"DIESEL": 60_000.0},
                capacity={"DIESEL": 90_000.0},
            )
        }
    )
    validator = AllocationValidator()
    report = validator.validate(
        world=world,
        source_depot_id="depot-gazipur",
        destination_station_id="station-mirpur",
        route_id="route-gazipur-mirpur",
        fuel_type="DIESEL",
        quantity=1_000.0,
    )

    assert report.is_valid


def test_a_closed_station_cannot_receive() -> None:
    """A closed station is a violation."""
    world = build_world(
        stations={
            "station-mirpur": StationSnapshot(
                id="station-mirpur",
                status="CLOSED",
                inventory={"DIESEL": 2_000.0},
                capacity={"DIESEL": 15_000.0},
            )
        }
    )
    validator = AllocationValidator()
    report = validator.validate(
        world=world,
        source_depot_id="depot-gazipur",
        destination_station_id="station-mirpur",
        route_id="route-gazipur-mirpur",
        fuel_type="DIESEL",
        quantity=1_000.0,
    )

    assert not report.is_valid
    assert report.primary_violation.code == ConstraintCode.STATION_CLOSED


def test_a_disrupted_route_is_a_violation() -> None:
    """A route the simulator has closed cannot be used."""
    world = build_world(
        routes=(
            RouteSnapshot(
                id="route-gazipur-mirpur",
                source_depot_id="depot-gazipur",
                destination_station_id="station-mirpur",
                transit_ticks=2,
                max_shipment=7_000.0,
                status="DISRUPTED",
            ),
        )
    )
    validator = AllocationValidator()
    report = validator.validate(
        world=world,
        source_depot_id="depot-gazipur",
        destination_station_id="station-mirpur",
        route_id="route-gazipur-mirpur",
        fuel_type="DIESEL",
        quantity=1_000.0,
    )

    assert not report.is_valid
    assert report.primary_violation.code == ConstraintCode.ROUTE_DISRUPTED


def test_the_max_quantity_is_bounded_by_every_cap() -> None:
    """The maximum is the smallest of the route, depot, tank and allowance."""
    validator = AllocationValidator()
    world = build_world()

    quantity = validator.max_quantity(
        world=world,
        source_depot_id="depot-gazipur",
        destination_station_id="station-mirpur",
        route_id="route-gazipur-mirpur",
        fuel_type="DIESEL",
    )

    # The route carries 7,000 L per shipment, the station has 13,000 L of
    # room, the depot has 60,000 L and a 12,000 L allowance.
    assert quantity == 7_000.0


def test_the_max_quantity_is_zero_for_a_route_that_does_not_exist() -> None:
    """A route that is not there can carry nothing."""
    validator = AllocationValidator()

    quantity = validator.max_quantity(
        world=build_world(),
        source_depot_id="depot-gazipur",
        destination_station_id="station-mirpur",
        route_id="route-nope",
        fuel_type="DIESEL",
    )

    assert quantity == 0.0


def test_the_report_carries_every_check_it_made() -> None:
    """The report is the explanation, so it holds every check."""
    validator = AllocationValidator()
    report = validator.validate(
        world=build_world(),
        source_depot_id="depot-gazipur",
        destination_station_id="station-mirpur",
        route_id="route-gazipur-mirpur",
        fuel_type="DIESEL",
        quantity=1_000.0,
    )

    assert isinstance(report, ConstraintReport)
    codes = {check.code for check in report.checks}
    assert ConstraintCode.ROUTE_MISMATCH in codes
    assert ConstraintCode.DEPOT_CLOSED in codes
    assert ConstraintCode.STATION_CLOSED in codes


# -- the optimizer's ordering ---------------------------------------------


def test_the_fallback_order_refills_the_lowest_cover_first() -> None:
    """The rule-based policy ranks by days of cover, lowest first.

    With no forecast to reason from, "who needs fuel soonest" is the
    only honest question, and the answer is the station with the least
    cover.
    """
    optimizer = _optimizer()
    urgent = _critical_risk(
        hours_to_stockout=1.0,
        inventory=500.0,
        forecast=build_forecast(hourly_demand=1_000.0),
    )
    comfortable = _critical_risk(
        hours_to_stockout=20.0,
        inventory=12_000.0,
        forecast=build_forecast(hourly_demand=100.0),
    )

    ordered = optimizer._order_by_fallback_policy([comfortable, urgent])

    assert ordered[0] is urgent


def test_a_station_with_no_forecast_has_infinite_cover() -> None:
    """Without a forecast there is nothing to rank by, so it sorts last."""
    optimizer = _optimizer()
    risk = _critical_risk()
    risk.forecast.points = []

    assert optimizer._days_of_cover(risk) == 999.0


def test_a_forecast_with_no_demand_has_infinite_cover() -> None:
    """A forecast predicting nothing is not a station in trouble."""
    optimizer = _optimizer()
    risk = _critical_risk(forecast=build_forecast(hourly_demand=0.0))

    assert optimizer._days_of_cover(risk) == 999.0


def test_the_days_of_cover_uses_the_available_liters() -> None:
    """Cover is what the station has against what it burns."""
    optimizer = _optimizer()
    risk = _critical_risk(inventory=2_000.0, forecast=build_forecast(hourly_demand=400.0))

    # 96 ticks at 100 L each is 2,400 L a day; the station has 2,000 L.
    cover = optimizer._days_of_cover(risk)
    assert 0 < cover < 1.5


def test_the_available_liters_fall_back_to_zero() -> None:
    """A risk with no availability signal is treated as having nothing."""
    optimizer = _optimizer()
    risk = _critical_risk()
    risk.signals = []

    assert optimizer._available_from_risk(risk) == 0.0


# -- the proposal ---------------------------------------------------------


def test_a_critical_station_gets_a_recommendation() -> None:
    """The happy path: a short station gets a proposal."""
    optimizer = _optimizer()
    recommendations = optimizer.propose(
        world=build_world(),
        risks=[_critical_risk()],
    )

    assert recommendations
    proposal = recommendations[0]
    assert proposal.station_id == "station-mirpur"
    assert proposal.quantity > 0
    assert proposal.status == "PROPOSED"


def test_a_healthy_station_gets_nothing() -> None:
    """A station that is fine is not shipped fuel."""
    optimizer = _optimizer()
    recommendations = optimizer.propose(
        world=build_world(),
        risks=[_critical_risk(level=RiskLevel.OK, hours_to_stockout=None)],
    )

    assert recommendations == []


def test_a_station_that_is_not_in_the_world_gets_nothing() -> None:
    """A risk for an unknown station produces nothing, not a crash."""
    optimizer = _optimizer()
    risk = _critical_risk()
    risk.station_id = "station-nowhere"

    recommendations = optimizer.propose(world=build_world(), risks=[risk])

    assert recommendations == []


def test_a_deficit_bigger_than_the_route_is_split() -> None:
    """A deficit above ``max_shipment`` goes out as several shipments.

    The simulator refuses any single allocation above the route's limit,
    so one leg would simply be rejected.
    """
    optimizer = _optimizer()
    world = build_world()
    risk = _critical_risk(
        inventory=0.0,
        forecast=build_forecast(hourly_demand=4_000.0),
    )

    recommendations = optimizer.propose(world=world, risks=[risk])

    assert len(recommendations) > 1
    assert all(rec.quantity <= 7_000.0 for rec in recommendations)
    assert sum(rec.quantity for rec in recommendations) > 7_000.0


def test_every_leg_of_a_split_has_its_own_idempotency_key() -> None:
    """The legs are separate approvals, so they need separate keys.

    Two legs with the same key would be treated by the simulator as the
    same allocation, and the second would be silently dropped.
    """
    optimizer = _optimizer()
    world = build_world()
    risk = _critical_risk(inventory=0.0, forecast=build_forecast(hourly_demand=4_000.0))

    recommendations = optimizer.propose(world=world, risks=[risk])

    keys = [rec.idempotency_key for rec in recommendations]
    assert len(set(keys)) == len(keys)


def test_the_split_stops_at_the_tank_room() -> None:
    """Splitting past the tank's room would promise fuel it cannot take.

    The simulator caps what a tank actually receives on arrival, so a
    proposal larger than the free space is a promise that cannot be
    kept.
    """
    optimizer = _optimizer()
    world = build_world(
        stations={
            "station-mirpur": StationSnapshot(
                id="station-mirpur",
                status="OPEN",
                inventory={"DIESEL": 14_000.0},
                capacity={"DIESEL": 15_000.0},
            )
        }
    )
    risk = _critical_risk(inventory=14_000.0, forecast=build_forecast(hourly_demand=40_000.0))

    recommendations = optimizer.propose(world=world, risks=[risk])

    # The tank has 1,000 L of room, so the proposal cannot exceed that.
    assert sum(rec.quantity for rec in recommendations) <= 1_000.0


def test_a_route_that_cannot_carry_anything_is_explained() -> None:
    """When nothing can be shipped, the reason is recorded.

    An operator who gets a silent empty list cannot tell "the station
    is fine" from "the platform could not work out a route".
    """
    optimizer = _optimizer()
    world = build_world(
        routes=(
            RouteSnapshot(
                id="route-gazipur-mirpur",
                source_depot_id="depot-gazipur",
                destination_station_id="station-mirpur",
                transit_ticks=2,
                max_shipment=7_000.0,
                status="DISRUPTED",
            ),
        )
    )
    risk = _critical_risk()

    recommendations = optimizer.propose(world=world, risks=[risk])

    # Nothing can be shipped, so nothing is proposed...
    assert recommendations == []

    # ...but the reason is available to the caller.
    reason = optimizer._why_empty(
        world=world,
        depot_id="depot-gazipur",
        route=world.routes[0],
        fuel_type="DIESEL",
    )
    assert "ROUTE_DISRUPTED" in reason


def test_the_needed_liters_never_exceed_the_tank_capacity() -> None:
    """Asking for more than the tank holds is how a station gets nowhere."""
    optimizer = _optimizer()
    world = build_world()
    risk = _critical_risk(inventory=0.0, forecast=build_forecast(hourly_demand=100_000.0))

    needed = optimizer._needed_liters(risk, world)

    assert needed <= world.stations["station-mirpur"].capacity["DIESEL"]


def test_the_needed_liters_are_zero_without_a_forecast() -> None:
    """Without a forecast there is no way to know what is needed."""
    optimizer = _optimizer()
    risk = _critical_risk()
    risk.forecast.points = []

    assert optimizer._needed_liters(risk, build_world()) == 0.0


def test_the_needed_liters_extrapolate_a_short_forecast() -> None:
    """A forecast shorter than the cover window is extrapolated.

    Shipping only the horizon's demand would leave the station short
    again every cycle.
    """
    optimizer = _optimizer()
    world = build_world()
    risk = _critical_risk(
        inventory=0.0,
        forecast=build_forecast(hourly_demand=400.0, ticks=8),
    )

    needed = optimizer._needed_liters(risk, world)

    assert needed > 0


def test_the_transit_ticks_are_read_from_the_route() -> None:
    """The transit time is the route's own."""
    optimizer = _optimizer()
    world = build_world()

    assert optimizer._transit_ticks(world, "route-gazipur-mirpur") == 2
    assert optimizer._transit_ticks(world, "route-nope") == 0


def test_the_recommendation_carries_the_signals_and_the_impact() -> None:
    """A proposal has to explain itself, or it cannot be approved."""
    optimizer = _optimizer()
    recommendations = optimizer.propose(world=build_world(), risks=[_critical_risk()])

    proposal = recommendations[0]
    assert proposal.signals
    assert proposal.constraints
    assert proposal.rationale
    assert proposal.confidence > 0
    assert proposal.risk_level == RiskLevel.CRITICAL
    assert proposal.hours_to_stockout == 4.0
    assert proposal.projection is not None


def test_the_alternatives_are_the_routes_that_were_considered() -> None:
    """The rejected routes are recorded, so the operator can see them."""
    optimizer = _optimizer()
    recommendations = optimizer.propose(world=build_world(), risks=[_critical_risk()])

    proposal = recommendations[0]
    # Both routes were considered; the faster one won.
    assert proposal.alternatives


def test_a_station_with_only_one_route_has_no_alternatives() -> None:
    """There is nothing else to offer when there is one way in."""
    optimizer = _optimizer()
    world = build_world(
        routes=(
            RouteSnapshot(
                id="route-gazipur-mirpur",
                source_depot_id="depot-gazipur",
                destination_station_id="station-mirpur",
                transit_ticks=2,
                max_shipment=7_000.0,
                status="AVAILABLE",
            ),
        )
    )

    recommendations = optimizer.propose(world=world, risks=[_critical_risk()])

    assert recommendations
    assert recommendations[0].alternatives == []


def test_the_dispatch_allowance_is_shared_across_the_recommendations() -> None:
    """The allowance is a per-tick total, not a per-shipment one.

    Two stations served from one depot cannot both be given the depot's
    whole allowance.
    """
    optimizer = _optimizer()
    world = build_world(
        depots={
            "depot-gazipur": DepotSnapshot(
                id="depot-gazipur",
                status="OPEN",
                dispatch_capacity_per_tick=3_000.0,
                inventory={"DIESEL": 60_000.0},
                capacity={"DIESEL": 90_000.0},
            )
        },
        stations={
            "station-mirpur": StationSnapshot(
                id="station-mirpur",
                status="OPEN",
                inventory={"DIESEL": 0.0},
                capacity={"DIESEL": 15_000.0},
            ),
            "station-karnaphuli": StationSnapshot(
                id="station-karnaphuli",
                status="OPEN",
                inventory={"DIESEL": 0.0},
                capacity={"DIESEL": 15_000.0},
            ),
        },
        routes=(
            RouteSnapshot(
                id="route-gazipur-mirpur",
                source_depot_id="depot-gazipur",
                destination_station_id="station-mirpur",
                transit_ticks=2,
                max_shipment=7_000.0,
                status="AVAILABLE",
            ),
            RouteSnapshot(
                id="route-gazipur-karnaphuli",
                source_depot_id="depot-gazipur",
                destination_station_id="station-karnaphuli",
                transit_ticks=3,
                max_shipment=7_000.0,
                status="AVAILABLE",
            ),
        ),
    )

    first = _critical_risk(inventory=0.0)
    second = _critical_risk(inventory=0.0)
    second.station_id = "station-karnaphuli"

    recommendations = optimizer.propose(world=world, risks=[first, second])

    total = sum(rec.quantity for rec in recommendations)
    assert total <= 3_000.0


def test_the_proposal_is_stable_for_the_same_inputs() -> None:
    """The same world and the same risk produce the same proposal.

    The idempotency key is derived from the inputs, so a proposal that
    changed between two calls would send two different allocations for
    one decision.
    """
    optimizer = _optimizer()
    world = build_world()
    risk = _critical_risk()

    first = optimizer.propose(world=world, risks=[risk])
    second = optimizer.propose(world=world, risks=[risk])

    assert first[0].idempotency_key == second[0].idempotency_key
    assert first[0].quantity == second[0].quantity


@pytest.mark.parametrize("limit", [1, 2, 5])
def test_the_split_never_exceeds_the_configured_leg_count(limit: int) -> None:
    """A split is bounded, so a huge deficit cannot produce a hundred legs."""
    optimizer = _optimizer(max_split_legs=limit)
    world = build_world()
    risk = _critical_risk(inventory=0.0, forecast=build_forecast(hourly_demand=100_000.0))

    recommendations = optimizer.propose(world=world, risks=[risk])

    assert len(recommendations) <= limit
