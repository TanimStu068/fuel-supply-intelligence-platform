"""Tests for the rule-based fallback policy.

The fallback is what runs when the intelligence layer cannot produce
forecasts, so these tests check that it still produces *valid* proposals,
that it ranks by days of cover, and that it never claims confidence it
does not have.
"""

from __future__ import annotations

from decision_fixtures import build_world

from app.decision.fallback import FallbackPolicy, StationProfile
from app.decision.models import RecommendationStatus
from app.decision.world import DepotSnapshot, StationSnapshot
from app.intelligence.risk import RiskLevel

PROFILES = {
    "station-mirpur": StationProfile(
        demand_profile="urban_high",
        region_demand_factor=1.0,
        demand_multiplier=1.0,
    )
}


def test_the_fallback_assesses_a_station_without_a_model() -> None:
    policy = FallbackPolicy()

    assessment = policy.assess(
        world=build_world(),
        profiles=PROFILES,
        station_id="station-mirpur",
        fuel_type="DIESEL",
    )

    # The urban_high profile draws 8,500 L of diesel a day.
    assert 8_000 < assessment.daily_demand < 9_000
    assert assessment.risk_level in {RiskLevel.WATCH, RiskLevel.CRITICAL}
    assert assessment.days_of_cover > 0


def test_the_fallback_refills_the_lowest_cover_first() -> None:
    """The rule the brief asks for, and it is the only rule."""
    policy = FallbackPolicy()
    world = build_world(
        stations={
            "station-mirpur": StationSnapshot(
                id="station-mirpur",
                status="OPEN",
                inventory={"DIESEL": 500.0, "PETROL": 9_000.0, "OCTANE": 5_000.0},
                capacity={"DIESEL": 15_000.0, "PETROL": 14_000.0, "OCTANE": 9_000.0},
            ),
            "station-tongi": StationSnapshot(
                id="station-tongi",
                status="OPEN",
                inventory={"DIESEL": 12_000.0, "PETROL": 9_000.0, "OCTANE": 5_000.0},
                capacity={"DIESEL": 18_000.0, "PETROL": 9_000.0, "OCTANE": 3_500.0},
            ),
        }
    )
    profiles = {
        "station-mirpur": StationProfile(demand_profile="urban_high"),
        "station-tongi": StationProfile(demand_profile="industrial"),
    }

    recommendations = policy.propose(world=world, profiles=profiles)

    # The nearly-empty station is served first.
    assert recommendations
    assert recommendations[0].station_id == "station-mirpur"


def test_a_fallback_recommendation_is_marked_as_such() -> None:
    """The operator must be able to see the rule produced it."""
    policy = FallbackPolicy()

    recommendations = policy.propose(world=build_world(), profiles=PROFILES)

    assert recommendations
    recommendation = recommendations[0]
    assert recommendation.used_fallback_policy is True
    assert recommendation.needs_human_review is True
    assert recommendation.confidence <= 0.4
    assert recommendation.status is RecommendationStatus.PROPOSED
    assert "forecast model is unavailable" in recommendation.rationale


def test_every_fallback_recommendation_passes_every_constraint() -> None:
    """The fallback must not propose anything the simulator would refuse."""
    policy = FallbackPolicy()

    recommendations = policy.propose(world=build_world(), profiles=PROFILES)

    for recommendation in recommendations:
        assert all(check["satisfied"] for check in recommendation.constraints)


def test_a_fallback_shipment_respects_the_route_limit() -> None:
    policy = FallbackPolicy()
    world = build_world()

    recommendations = policy.propose(world=world, profiles=PROFILES)

    for recommendation in recommendations:
        route = next(route for route in world.routes if route.id == recommendation.route_id)
        assert recommendation.quantity <= route.max_shipment


def test_the_fallback_ignores_a_closed_station() -> None:
    """Shipping to a station in outage would fill a tank nobody can draw from."""
    policy = FallbackPolicy()
    world = build_world()
    station = world.stations["station-mirpur"]
    world.stations["station-mirpur"] = StationSnapshot(
        id=station.id,
        status="OUTAGE",
        inventory=station.inventory,
        capacity=station.capacity,
    )

    recommendations = policy.propose(world=world, profiles=PROFILES)

    assert recommendations == []


def test_the_fallback_uses_the_backup_route_when_the_fast_one_is_disrupted() -> None:
    policy = FallbackPolicy()
    world = build_world()
    world.routes = (
        world.routes[0].__class__(
            id="route-gazipur-mirpur",
            source_depot_id="depot-gazipur",
            destination_station_id="station-mirpur",
            transit_ticks=2,
            max_shipment=7_000.0,
            status="DISRUPTED",
        ),
        world.routes[1].__class__(
            id="route-patiya-mirpur",
            source_depot_id="depot-patiya",
            destination_station_id="station-mirpur",
            transit_ticks=4,
            max_shipment=5_000.0,
            status="AVAILABLE",
        ),
    )

    recommendations = policy.propose(world=world, profiles=PROFILES)

    assert recommendations
    assert recommendations[0].route_id == "route-patiya-mirpur"


def test_the_fallback_does_not_ship_to_an_empty_depot() -> None:
    policy = FallbackPolicy()
    world = build_world()
    depot = world.depots["depot-gazipur"]
    world.depots["depot-gazipur"] = DepotSnapshot(
        id=depot.id,
        status="OPEN",
        dispatch_capacity_per_tick=depot.dispatch_capacity_per_tick,
        inventory={"DIESEL": 0.0, "PETROL": 0.0, "OCTANE": 0.0},
        capacity=depot.capacity,
    )

    recommendations = policy.propose(world=world, profiles=PROFILES)

    # The Gazipur route has nothing to carry, so the Patiya backup is used.
    for recommendation in recommendations:
        assert recommendation.source_depot_id != "depot-gazipur"


def test_the_fallback_idempotency_key_is_deterministic() -> None:
    """A retried fallback approval must be a replay, not a second shipment."""
    policy = FallbackPolicy()

    first = policy.propose(world=build_world(), profiles=PROFILES)
    second = policy.propose(world=build_world(), profiles=PROFILES)

    assert first[0].idempotency_key == second[0].idempotency_key
    assert first[0].idempotency_key.startswith("fb-")
