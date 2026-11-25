"""Tests for the fallback policy's edge cases.

The fallback is what answers when the intelligence layer is down, so it
has to be *correct on its own terms* — it is the only thing between a
broken model and a dry tank.

The cases covered here are the ones where the rule has to make a call
without a forecast:

* a station with no profile of its own falls back to the regional one;
* a station that is already full is ``OK``;
* fuel on its way counts towards the cover, so a station that is about
  to be refuelled is not reported as critical;
* a stockout beyond a simulated day is ``OK``, because the policy does
  not look that far ahead.
"""

from __future__ import annotations

from decision_fixtures import build_world

from app.decision.fallback import FallbackPolicy, StationProfile
from app.decision.world import AllocationSnapshot, RouteSnapshot, StationSnapshot
from app.intelligence.risk import RiskLevel

PROFILES = {
    "station-mirpur": StationProfile(
        demand_profile="urban_high",
        region_demand_factor=1.0,
        demand_multiplier=1.0,
    )
}


def _policy() -> FallbackPolicy:
    """A fallback policy with the production configuration."""
    return FallbackPolicy()


def test_a_station_with_no_profile_uses_the_regional_one() -> None:
    """An unknown station still gets an assessment.

    The rule has to work for a station the platform has never seen, or
    a new station in the network would be invisible exactly when the
    model is down.
    """
    policy = _policy()

    assessment = policy.assess(
        world=build_world(),
        profiles={},
        station_id="station-mirpur",
        fuel_type="DIESEL",
    )

    assert assessment.daily_demand > 0
    assert assessment.risk_level in {RiskLevel.OK, RiskLevel.WATCH, RiskLevel.CRITICAL}


def test_a_station_that_is_not_in_the_world_is_assessed_as_empty() -> None:
    """An unknown station has nothing in its tank, not a crash."""
    policy = _policy()

    assessment = policy.assess(
        world=build_world(),
        profiles=PROFILES,
        station_id="station-nowhere",
        fuel_type="DIESEL",
    )

    assert assessment.inventory == 0.0
    assert assessment.capacity == 0.0


def test_a_full_station_is_ok() -> None:
    """A tank that is full is not a station in trouble."""
    policy = _policy()
    world = build_world(
        stations={
            "station-mirpur": StationSnapshot(
                id="station-mirpur",
                status="OPEN",
                inventory={"DIESEL": 15_000.0},
                capacity={"DIESEL": 15_000.0},
            )
        }
    )

    assessment = policy.assess(
        world=world,
        profiles=PROFILES,
        station_id="station-mirpur",
        fuel_type="DIESEL",
    )

    assert assessment.risk_level == RiskLevel.OK


def test_an_empty_station_is_critical() -> None:
    """An empty tank with demand against it is critical."""
    policy = _policy()
    world = build_world(
        stations={
            "station-mirpur": StationSnapshot(
                id="station-mirpur",
                status="OPEN",
                inventory={"DIESEL": 0.0},
                capacity={"DIESEL": 15_000.0},
            )
        }
    )

    assessment = policy.assess(
        world=world,
        profiles=PROFILES,
        station_id="station-mirpur",
        fuel_type="DIESEL",
    )

    assert assessment.risk_level == RiskLevel.CRITICAL
    assert assessment.hours_to_stockout == 0.0


def test_fuel_on_its_way_counts_towards_the_cover() -> None:
    """A station that is about to be refuelled is not critical.

    The incoming shipment is the cover. Without counting it, every
    station in the middle of a refill would raise a critical alert.
    """
    policy = _policy()
    world = build_world(
        stations={
            "station-mirpur": StationSnapshot(
                id="station-mirpur",
                status="OPEN",
                inventory={"DIESEL": 0.0},
                capacity={"DIESEL": 15_000.0},
            )
        },
        allocations=(
            AllocationSnapshot(
                id=1,
                idempotency_key="incoming-1",
                source_depot_id="depot-gazipur",
                destination_station_id="station-mirpur",
                route_id="route-gazipur-mirpur",
                fuel_type="DIESEL",
                quantity=14_000.0,
                created_tick=10,
                expected_arrival_tick=12,
                status="IN_TRANSIT",
            ),
        ),
    )

    assessment = policy.assess(
        world=world,
        profiles=PROFILES,
        station_id="station-mirpur",
        fuel_type="DIESEL",
    )

    # 14,000 L incoming against 8,500 L/day is more than a day of cover.
    assert assessment.risk_level == RiskLevel.OK


def test_a_stockout_beyond_a_simulated_day_is_ok() -> None:
    """The policy does not look further than a day ahead.

    A station with two days of cover is not a crisis, and calling it
    one would train the operator to ignore the alert.
    """
    policy = _policy()
    world = build_world(
        stations={
            "station-mirpur": StationSnapshot(
                id="station-mirpur",
                status="OPEN",
                inventory={"DIESEL": 20_000.0},
                capacity={"DIESEL": 25_000.0},
            )
        }
    )

    assessment = policy.assess(
        world=world,
        profiles=PROFILES,
        station_id="station-mirpur",
        fuel_type="DIESEL",
    )

    assert assessment.hours_to_stockout is None
    assert assessment.risk_level == RiskLevel.OK


def test_a_fuel_with_no_demand_has_no_stockout() -> None:
    """A fuel nobody draws never runs out."""
    policy = _policy()

    assessment = policy.assess(
        world=build_world(),
        profiles={
            "station-mirpur": StationProfile(
                demand_profile="no_demand",
                region_demand_factor=1.0,
                demand_multiplier=0.0,
            )
        },
        station_id="station-mirpur",
        fuel_type="DIESEL",
    )

    assert assessment.hours_to_stockout is None
    assert assessment.risk_level == RiskLevel.OK


def test_the_signals_explain_the_assessment() -> None:
    """Every number the operator sees has to come with a reason."""
    policy = _policy()

    assessment = policy.assess(
        world=build_world(),
        profiles=PROFILES,
        station_id="station-mirpur",
        fuel_type="DIESEL",
    )

    names = {signal.name for signal in assessment.signals}
    assert "inventory_liters" in names
    assert "available_liters" in names
    assert "daily_demand_liters" in names
    assert "days_of_cover" in names
    assert "hours_to_stockout" in names


def test_the_daily_demand_signal_says_the_model_is_unavailable() -> None:
    """The operator has to know this number came from a rule.

    A number that looks like a forecast but was not would be the worst
    possible outcome — it would be trusted like a forecast.
    """
    policy = _policy()

    assessment = policy.assess(
        world=build_world(),
        profiles=PROFILES,
        station_id="station-mirpur",
        fuel_type="DIESEL",
    )

    demand_signal = next(
        signal for signal in assessment.signals if signal.name == "daily_demand_liters"
    )
    assert "model is unavailable" in demand_signal.detail


def test_the_risk_band_thresholds_match_the_risk_engine() -> None:
    """The rule uses the same bands as the model, or the bands mean nothing.

    An operator who sees ``CRITICAL`` from the rule and ``WATCH`` from
    the model for the same station would stop believing either.
    """
    policy = _policy()

    assert policy._level(None) == RiskLevel.OK.value
    assert policy._level(4.0) == RiskLevel.CRITICAL.value
    assert policy._level(8.0) == RiskLevel.CRITICAL.value
    assert policy._level(12.0) == RiskLevel.WATCH.value


def test_the_transit_ticks_are_read_from_the_route() -> None:
    """The transit time is the route's own."""
    policy = _policy()
    world = build_world()

    assert policy._transit_ticks(world, "route-gazipur-mirpur") == 2
    assert policy._transit_ticks(world, "route-nope") == 0


def test_the_proposal_explains_a_route_it_cannot_use() -> None:
    """A route that is closed is recorded as an alternative with a reason."""
    policy = _policy()
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

    recommendations = policy.propose(world=world, profiles=PROFILES)

    assert recommendations == []

    reason = policy._why_empty(
        world=world,
        depot_id="depot-gazipur",
        route=world.routes[0],
        fuel_type="DIESEL",
    )
    assert "ROUTE_DISRUPTED" in reason


def test_a_route_with_no_room_is_explained() -> None:
    """A full tank is a reason, not a silence."""
    policy = _policy()
    world = build_world(
        stations={
            "station-mirpur": StationSnapshot(
                id="station-mirpur",
                status="OPEN",
                inventory={"DIESEL": 15_000.0},
                capacity={"DIESEL": 15_000.0},
            )
        }
    )

    reason = policy._why_empty(
        world=world,
        depot_id="depot-gazipur",
        route=world.routes[0],
        fuel_type="DIESEL",
    )

    assert reason


def test_the_projection_says_what_the_shipment_would_change() -> None:
    """The what-if has to show the before and the after."""
    policy = _policy()
    world = build_world()
    assessment = policy.assess(
        world=world,
        profiles=PROFILES,
        station_id="station-mirpur",
        fuel_type="DIESEL",
    )

    projection = policy._project(
        world=world,
        assessment=assessment,
        route=world.routes[0],
        quantity=1_000.0,
    )

    assert projection.available_after > projection.available_before
    assert projection.arrival_tick is not None


def test_the_rationale_says_how_much_cover_is_left() -> None:
    """The one-line explanation names the cover."""
    policy = _policy()
    world = build_world()
    assessment = policy.assess(
        world=world,
        profiles=PROFILES,
        station_id="station-mirpur",
        fuel_type="DIESEL",
    )

    rationale = policy._rationale(assessment, world.routes[0], 1_000.0)

    assert "cover" in rationale


def test_a_station_with_ample_cover_is_not_proposed_to() -> None:
    """The rule does not ship fuel to a station that does not need it."""
    policy = _policy()
    world = build_world(
        stations={
            "station-mirpur": StationSnapshot(
                id="station-mirpur",
                status="OPEN",
                inventory={"DIESEL": 15_000.0},
                capacity={"DIESEL": 15_000.0},
            )
        }
    )

    recommendations = policy.propose(world=world, profiles=PROFILES)

    assert recommendations == []


def test_a_fallback_recommendation_never_claims_high_confidence() -> None:
    """A rule-based proposal must not look like a model-based one.

    The operator approves these. If a rule-based proposal carried the
    same confidence as a forecasted one, there would be no way to tell
    the two apart at the point of approval.
    """
    policy = _policy()
    world = build_world(
        stations={
            "station-mirpur": StationSnapshot(
                id="station-mirpur",
                status="OPEN",
                inventory={"DIESEL": 0.0},
                capacity={"DIESEL": 15_000.0},
            )
        }
    )

    recommendations = policy.propose(world=world, profiles=PROFILES)

    assert recommendations
    for recommendation in recommendations:
        assert recommendation.used_fallback_policy is True
        assert recommendation.confidence < 0.9
        assert recommendation.needs_human_review is True
