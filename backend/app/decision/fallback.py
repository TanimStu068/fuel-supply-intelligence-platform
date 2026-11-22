"""The rule-based fallback policy: refill the lowest days-of-cover first.

This is what runs when the intelligence layer cannot produce forecasts —
the model is not trained, the history is too short, or the forecasting
code itself is unavailable. It is deliberately the simplest policy that
could work, because a rule you can read in one glance is a rule you can
trust when the clever machinery is broken.

## The rule

1. **Rank** the (station, fuel) pairs by **days of cover** — how long the
   station's stock would last at the demand rate it is actually drawing.
   The lowest days of cover gets the fuel.
2. **Size** the shipment to reach a **24-hour target cover**, net of what
   is on hand and what is already in transit.
3. **Route** by arrival time, fastest first, falling back to the backup
   route when the fast one is disrupted.

## Where the demand rate comes from

Without a forecast there is no learned estimate, so the rate is derived
from the **published profile** — the same daily litres the simulator
itself uses — scaled by the hour-of-day factor and the station's own
demand multiplier. That is the simulator's own model, minus the noise.

The station's demand profile is not on the world snapshot, so it is
passed in alongside it. That keeps the snapshot honest: it holds what the
decision engine needs for the *allocation* rules, and the profile is
context about the *demand*.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from ..intelligence.profiles import (
    TICKS_PER_DAY,
    baseline_demand_per_tick,
)
from ..intelligence.risk import RiskLevel, StockSignal
from .models import Alternative, Recommendation, RecommendationStatus, RiskProjection
from .optimizer import OptimizerConfig
from .validator import AllocationValidator
from .world import WorldSnapshot

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class StationProfile:
    """The demand context of one station, for the fallback policy."""

    #: The station's published demand profile (``urban_high``, …).
    demand_profile: str
    #: The region's demand factor.
    region_demand_factor: float = 1.0
    #: The station's own demand multiplier.
    demand_multiplier: float = 1.0


@dataclass(slots=True)
class FallbackRisk:
    """A rule-based risk assessment, built without any model.

    It has the same shape as the intelligence layer's assessment, so the
    optimizer and the API do not have to care which policy produced it.
    """

    station_id: str
    fuel_type: str
    inventory: float
    capacity: float
    #: The daily demand rate, in litres.
    daily_demand: float
    #: Hours until the station runs dry at that rate.
    hours_to_stockout: float | None
    #: The risk band, from the same thresholds the risk engine uses.
    risk_level: RiskLevel
    #: How many days of cover the station has.
    days_of_cover: float
    signals: list[StockSignal]


class FallbackPolicy:
    """The rule-based allocation policy used when the model is unavailable."""

    def __init__(
        self,
        *,
        validator: AllocationValidator | None = None,
        config: OptimizerConfig | None = None,
    ) -> None:
        self._validator = validator or AllocationValidator()
        self._config = config or OptimizerConfig()

    def assess(
        self,
        *,
        world: WorldSnapshot,
        profiles: dict[str, StationProfile],
        station_id: str,
        fuel_type: str,
    ) -> FallbackRisk:
        """Assess one (station, fuel) pair with the rule, not a model."""
        station = world.stations.get(station_id)
        profile = profiles.get(station_id, StationProfile(demand_profile="regional"))
        inventory = station.inventory.get(fuel_type, 0.0) if station else 0.0
        capacity = station.capacity.get(fuel_type, 0.0) if station else 0.0

        # The fuel already on its way counts towards the cover.
        incoming = sum(
            allocation.quantity
            for allocation in world.in_transit_to(station_id, fuel_type)
            if allocation.expected_arrival_tick is not None
        )
        available = inventory + incoming

        daily_demand = self._daily_demand(world, profile, fuel_type)
        hours = self._hours_to_stockout(available, daily_demand)
        days_of_cover = available / daily_demand if daily_demand > 0 else 999.0

        if hours is None:
            level = RiskLevel.OK
        elif hours <= 8.0:
            level = RiskLevel.CRITICAL
        else:
            level = RiskLevel.WATCH

        signals = [
            StockSignal(
                name="inventory_liters",
                value=inventory,
                detail=f"{inventory:,.0f} L on hand",
            ),
            StockSignal(
                name="available_liters",
                value=available,
                detail=f"{available:,.0f} L available (on hand + in transit)",
            ),
            StockSignal(
                name="daily_demand_liters",
                value=daily_demand,
                detail=(
                    f"{daily_demand:,.0f} L/day from the {profile.demand_profile} "
                    f"profile (the forecast model is unavailable)"
                ),
            ),
            StockSignal(
                name="days_of_cover",
                value=days_of_cover,
                detail=f"{days_of_cover:.1f} days of cover at the current rate",
            ),
            StockSignal(
                name="hours_to_stockout",
                value=hours or 0.0,
                detail=(
                    f"{hours:.1f} simulated hours of cover"
                    if hours is not None
                    else "stock survives the target window"
                ),
            ),
        ]

        return FallbackRisk(
            station_id=station_id,
            fuel_type=fuel_type,
            inventory=inventory,
            capacity=capacity,
            daily_demand=daily_demand,
            hours_to_stockout=hours,
            risk_level=level,
            days_of_cover=days_of_cover,
            signals=signals,
        )

    def _daily_demand(self, world: WorldSnapshot, profile: StationProfile, fuel_type: str) -> float:
        """The daily demand rate, from the published profile.

        The simulator's demand varies with the hour of day, so the rate is
        averaged over a whole day rather than taken from the current hour.
        """
        hour = world.sim_time.hour if world.sim_time else 0
        total = 0.0
        for tick in range(TICKS_PER_DAY):
            tick_hour = (hour + tick * world.tick_minutes // 60) % 24
            total += baseline_demand_per_tick(
                profile_name=profile.demand_profile,
                fuel=fuel_type,
                hour=tick_hour,
                region_demand_factor=profile.region_demand_factor,
                demand_multiplier=profile.demand_multiplier,
            )
        return total

    def _hours_to_stockout(self, available: float, daily_demand: float) -> float | None:
        """How long until the stock runs out at the current rate."""
        if daily_demand <= 0:
            return None
        if available <= 0:
            return 0.0
        hours = (available / daily_demand) * 24.0
        # The stockout is beyond a simulated day, which is as far as the
        # fallback policy looks.
        return round(hours, 2) if hours <= 24.0 else None

    def propose(
        self,
        *,
        world: WorldSnapshot,
        profiles: dict[str, StationProfile],
    ) -> list[Recommendation]:
        """Propose allocations using the rule, lowest days-of-cover first.

        The returned recommendations are shaped exactly like the optimizer's,
        so the operator sees the same thing either way — except that they
        are marked as having used the fallback policy and carry a
        confidence that reflects it.
        """
        assessments = [
            self.assess(
                world=world,
                profiles=profiles,
                station_id=station_id,
                fuel_type=fuel_type,
            )
            for station_id, station in world.stations.items()
            for fuel_type in station.inventory
        ]

        actionable = [
            assessment
            for assessment in assessments
            if assessment.risk_level in self._config.actionable_levels
        ]

        # The rule: lowest days of cover first.
        actionable.sort(key=lambda item: (item.days_of_cover, item.station_id))

        recommendations: list[Recommendation] = []
        spent: dict[str, float] = {}

        for assessment in actionable:
            recommendation = self._propose_for(world=world, assessment=assessment, spent=spent)
            if recommendation is not None:
                recommendations.append(recommendation)
                spent[recommendation.source_depot_id] = (
                    spent.get(recommendation.source_depot_id, 0.0) + recommendation.quantity
                )

        return recommendations

    def _propose_for(
        self,
        *,
        world: WorldSnapshot,
        assessment: FallbackRisk,
        spent: dict[str, float],
    ) -> Recommendation | None:
        """Build one rule-based recommendation."""
        target = min(assessment.daily_demand, assessment.capacity)
        needed = target - assessment.inventory
        needed -= self._incoming(world, assessment.station_id, assessment.fuel_type)
        if needed < self._config.min_shipment_liters:
            return None

        candidates: list[tuple[float, Recommendation]] = []
        rejected: list[Alternative] = []

        for route in world.routes_to(assessment.station_id):
            depot_id = route.source_depot_id
            cap = self._max_quantity(world, depot_id, route, assessment.fuel_type, spent)
            if cap <= 0:
                rejected.append(
                    Alternative(
                        route_id=route.id,
                        source_depot_id=depot_id,
                        transit_ticks=route.transit_ticks,
                        quantity=0.0,
                        reason=self._why_empty(world, depot_id, route, assessment.fuel_type),
                    )
                )
                continue

            quantity = round(min(needed, cap), 3)
            if quantity <= 0:
                continue

            report = self._validator.validate(
                world=world,
                source_depot_id=depot_id,
                destination_station_id=assessment.station_id,
                route_id=route.id,
                fuel_type=assessment.fuel_type,
                quantity=quantity,
            )
            if not report.is_valid:
                violation = report.primary_violation
                rejected.append(
                    Alternative(
                        route_id=route.id,
                        source_depot_id=depot_id,
                        transit_ticks=route.transit_ticks,
                        quantity=0.0,
                        reason=(
                            f"{violation.code.value}: {violation.detail}"
                            if violation
                            else "invalid"
                        ),
                    )
                )
                continue

            projection = self._project(
                world=world, assessment=assessment, route=route, quantity=quantity
            )
            recommendation = Recommendation(
                id=f"fb-{assessment.station_id}-{assessment.fuel_type}-{world.tick}-{int(quantity)}",
                station_id=assessment.station_id,
                fuel_type=assessment.fuel_type,
                source_depot_id=depot_id,
                route_id=route.id,
                quantity=quantity,
                status=RecommendationStatus.PROPOSED,
                risk_level=assessment.risk_level.value,
                hours_to_stockout=assessment.hours_to_stockout,
                stockout_probability=0.5,
                projection=projection,
                signals=[signal.to_payload() for signal in assessment.signals],
                constraints=report.to_payload(),
                # The rule has no model behind it, so it does not claim
                # more confidence than it deserves.
                confidence=0.4,
                needs_human_review=True,
                used_fallback_policy=True,
                idempotency_key=(
                    f"fb-{assessment.station_id}-{assessment.fuel_type}"
                    f"-{world.tick}-{int(quantity)}"
                ),
                rationale=self._rationale(assessment, route, quantity),
                created_at=datetime.now(UTC),
                sim_tick=world.tick,
            )
            score = projection.hours_to_stockout_after or 48.0
            candidates.append((score - route.transit_ticks * 0.5, recommendation))

        if not candidates:
            return None

        candidates.sort(key=lambda item: item[0], reverse=True)
        _, best = candidates[0]

        alternatives = [
            Alternative(
                route_id=recommendation.route_id,
                source_depot_id=recommendation.source_depot_id,
                transit_ticks=self._transit_ticks(world, recommendation.route_id),
                quantity=recommendation.quantity,
                reason="a slower arrival than the recommended route",
                hours_to_stockout_after=(
                    recommendation.projection.hours_to_stockout_after
                    if recommendation.projection
                    else None
                ),
                score=score,
            )
            for score, recommendation in candidates[1:]
        ]
        alternatives.extend(rejected)
        best.alternatives = alternatives[: self._config.max_alternatives]

        return best

    def _incoming(self, world: WorldSnapshot, station_id: str, fuel_type: str) -> float:
        """The fuel already on its way to the station."""
        return sum(
            allocation.quantity
            for allocation in world.in_transit_to(station_id, fuel_type)
            if allocation.expected_arrival_tick is not None
        )

    def _max_quantity(
        self,
        world: WorldSnapshot,
        depot_id: str,
        route: object,
        fuel_type: str,
        spent: dict[str, float],
    ) -> float:
        """The largest quantity this route can carry right now."""
        cap = self._validator.max_quantity(
            world=world,
            source_depot_id=depot_id,
            destination_station_id=route.destination_station_id,
            route_id=route.id,
            fuel_type=fuel_type,
        )
        return max(0.0, cap - spent.get(depot_id, 0.0))

    def _why_empty(self, world: WorldSnapshot, depot_id: str, route: object, fuel_type: str) -> str:
        """Why a route could not carry anything."""
        report = self._validator.validate(
            world=world,
            source_depot_id=depot_id,
            destination_station_id=route.destination_station_id,
            route_id=route.id,
            fuel_type=fuel_type,
            quantity=1.0,
        )
        violation = report.primary_violation
        if violation is None:
            return "the depot's dispatch allowance for this tick is used up"
        return f"{violation.code.value}: {violation.detail}"

    def _transit_ticks(self, world: WorldSnapshot, route_id: str) -> int:
        """How many ticks a route takes."""
        for route in world.routes:
            if route.id == route_id:
                return route.transit_ticks
        return 0

    def _project(
        self,
        *,
        world: WorldSnapshot,
        assessment: FallbackRisk,
        route: object,
        quantity: float,
    ) -> RiskProjection:
        """Project the cover with and without the shipment.

        The projection uses the *constant* demand rate rather than a
        forecast, because there is no forecast. The fuel only helps from
        its arrival tick.
        """
        available = assessment.inventory + self._incoming(
            world, assessment.station_id, assessment.fuel_type
        )
        arrival_tick = world.tick + route.transit_ticks
        tick_hours = world.tick_minutes / 60.0

        before_hours = self._hours_to_stockout(available, assessment.daily_demand)
        after_hours = self._hours_to_stockout(available + quantity, assessment.daily_demand)

        # The cover only improves once the fuel has arrived.
        if after_hours is not None:
            travel_hours = route.transit_ticks * tick_hours
            after_hours = round(min(after_hours, 24.0) + travel_hours, 2)

        return RiskProjection(
            risk_level_before=self._level(before_hours),
            risk_level_after=self._level(after_hours),
            hours_to_stockout_before=before_hours,
            hours_to_stockout_after=after_hours,
            stockout_probability_before=0.5 if before_hours is not None else 0.0,
            stockout_probability_after=0.5 if after_hours is not None else 0.0,
            inventory_before=assessment.inventory,
            inventory_after=assessment.inventory + quantity,
            available_before=available,
            available_after=available + quantity,
            arrival_tick=arrival_tick,
            hours_until_arrival=route.transit_ticks * tick_hours,
        )

    def _level(self, hours: float | None) -> str:
        """The risk band, from the same thresholds the risk engine uses."""
        if hours is None:
            return RiskLevel.OK.value
        if hours <= 8.0:
            return RiskLevel.CRITICAL.value
        return RiskLevel.WATCH.value

    def _rationale(self, assessment: FallbackRisk, route: object, quantity: float) -> str:
        """One line explaining the recommendation."""
        cover = (
            f"{assessment.days_of_cover:.1f} days of cover"
            if assessment.days_of_cover < 999
            else "ample cover"
        )
        return (
            f"Refill {assessment.station_id} ({assessment.fuel_type.lower()}): it has "
            f"the lowest days of cover ({cover}) and the forecast model is "
            f"unavailable, so the rule-based policy applies. Ship "
            f"{quantity:,.0f} L from {route.source_depot_id} via {route.id}, "
            f"arriving in {route.transit_ticks} ticks."
        )
