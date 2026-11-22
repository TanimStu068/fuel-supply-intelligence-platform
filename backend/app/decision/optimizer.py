"""The allocation optimizer: a priority-greedy heuristic over the network.

## The approach, stated plainly

A **priority-greedy heuristic**, not an LP.

The reason is not that LP is too hard. It is that the problem is not
really an optimisation problem — it is a *sequencing* problem, and the
scoring function is the judgement, not the solver.

For each (station, fuel) pair in order of urgency, the engine:

1. computes the **deficit** — the fuel needed to reach a target cover,
   net of what is on hand and what is already in transit;
2. walks the candidate routes **fastest first**, because a station that
   runs dry in two hours does not care that the backup route is cheaper;
3. for each route, takes the largest quantity that passes every simulator
   constraint, and projects what that shipment does to the station's risk;
4. keeps the route with the best projected outcome, and records the others
   as ranked alternatives;
5. caps each shipment at the route's ``max_shipment``, because the
   simulator refuses anything larger.

Why greedy rather than LP: the constraint set the simulator enforces is
small and *local* (eight rules, all checkable in O(1)), while the thing
that actually needs judgement — which station deserves fuel first — is a
priority judgement that an LP objective would only obscure. A reviewer
asking "why did this station get fuel and that one not?" gets an answer
here that points at a risk score and a cover calculation. With an LP they
would get a dual price and a tie-break rule.

The greedy *is* suboptimal in the classic sense: it can spend a depot's
dispatch allowance on a station that a later, more urgent station needed.
That is mitigated, not solved, and the mitigation is stated below.

## The mitigations

* **Urgency ordering.** Stations are served in order of projected hours
  to stockout, so the ones closest to running dry get first claim on the
  scarce per-tick dispatch allowance.
* **Pre-validation.** Every proposal is checked against all eight
  simulator rules before it is offered, so the engine never proposes
  something that would come back as a 409.
* **Transit-aware cover.** A shipment only improves the station's cover
  from the tick it *arrives*, so a slow route is scored honestly rather
  than being credited with fuel the station cannot use yet.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from ..intelligence.forecasting import Forecast
from ..intelligence.risk import RiskAssessment, RiskLevel
from .constraints import ConstraintReport
from .models import Alternative, Recommendation, RecommendationStatus, RiskProjection
from .validator import AllocationValidator
from .world import WorldSnapshot

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class OptimizerConfig:
    """The tunables of the allocation policy."""

    #: How many simulated hours of cover a station should have.
    target_cover_hours: float = 24.0
    #: The smallest shipment worth proposing, in litres.
    min_shipment_liters: float = 500.0
    #: How many ranked alternatives to keep.
    max_alternatives: int = 3
    #: Only stations at this level or worse are considered.
    actionable_levels: frozenset[RiskLevel] = frozenset({RiskLevel.WATCH, RiskLevel.CRITICAL})


class AllocationOptimizer:
    """Turns risk assessments into valid, explained allocation proposals."""

    def __init__(
        self,
        *,
        validator: AllocationValidator | None = None,
        config: OptimizerConfig | None = None,
    ) -> None:
        self._validator = validator or AllocationValidator()
        self._config = config or OptimizerConfig()

    @property
    def config(self) -> OptimizerConfig:
        """The policy's tunables."""
        return self._config

    def propose(
        self,
        *,
        world: WorldSnapshot,
        risks: list[RiskAssessment],
        fallback: bool = False,
    ) -> list[Recommendation]:
        """Propose allocations for the stations that need them.

        ``fallback=True`` switches to the rule-based policy, used when the
        intelligence layer cannot produce forecasts.
        """
        actionable = [risk for risk in risks if risk.risk_level in self._config.actionable_levels]

        if fallback:
            ordered = self._order_by_fallback_policy(actionable)
        else:
            ordered = self._order_by_urgency(actionable)

        recommendations: list[Recommendation] = []
        # The depot's per-tick dispatch allowance is a shared, consumable
        # resource. Every proposal spends some of it, so the running
        # totals have to be tracked across the whole pass.
        spent: dict[str, float] = {}

        for risk in ordered:
            recommendation = self._propose_for(
                world=world, risk=risk, spent=spent, fallback=fallback
            )
            if recommendation is not None:
                recommendations.append(recommendation)
                spent[recommendation.source_depot_id] = (
                    spent.get(recommendation.source_depot_id, 0.0) + recommendation.quantity
                )

        return recommendations

    # -- ordering -----------------------------------------------------------

    def _order_by_urgency(self, risks: list[RiskAssessment]) -> list[RiskAssessment]:
        """The most urgent station gets first claim on the fuel.

        A station that is already empty outranks one with hours left, and
        within a band the lower projected cover wins.
        """
        return sorted(
            risks,
            key=lambda risk: (
                0 if risk.risk_level is RiskLevel.CRITICAL else 1,
                risk.hours_to_stockout if risk.hours_to_stockout is not None else 999.0,
                -risk.stockout_probability,
                risk.station_id,
                risk.fuel_type,
            ),
        )

    def _order_by_fallback_policy(self, risks: list[RiskAssessment]) -> list[RiskAssessment]:
        """The fallback ordering: refill the lowest days-of-cover first.

        This is the rule the brief asks for when the intelligence layer is
        down. Days of cover is estimated from the forecast's own daily
        demand, because without a model there is no better estimate
        available.
        """
        return sorted(
            risks,
            key=lambda risk: (
                self._days_of_cover(risk),
                risk.station_id,
                risk.fuel_type,
            ),
        )

    def _days_of_cover(self, risk: RiskAssessment) -> float:
        """How many days of cover a station has."""
        forecast = risk.forecast
        if not forecast.points:
            return 999.0
        daily = sum(point.predicted_demand for point in forecast.points)
        if daily <= 0:
            return 999.0
        return self._available_from_risk(risk) / daily

    # -- the per-station proposal -------------------------------------------

    def _propose_for(
        self,
        *,
        world: WorldSnapshot,
        risk: RiskAssessment,
        spent: dict[str, float],
        fallback: bool,
    ) -> Recommendation | None:
        """Build the recommendation for one station and fuel."""
        station = world.stations.get(risk.station_id)
        if station is None:
            return None

        needed = self._needed_liters(risk, world)
        if needed < self._config.min_shipment_liters:
            return None

        candidates: list[tuple[float, Recommendation]] = []
        rejected: list[Alternative] = []

        for route in world.routes_to(risk.station_id):
            depot_id = route.source_depot_id

            cap = self._max_quantity(world, depot_id, route, risk.fuel_type, spent)
            if cap <= 0:
                rejected.append(
                    Alternative(
                        route_id=route.id,
                        source_depot_id=depot_id,
                        transit_ticks=route.transit_ticks,
                        quantity=0.0,
                        reason=self._why_empty(world, depot_id, route, risk.fuel_type),
                    )
                )
                continue

            quantity = round(min(needed, cap), 3)
            if quantity <= 0:
                continue

            report = self._validator.validate(
                world=world,
                source_depot_id=depot_id,
                destination_station_id=risk.station_id,
                route_id=route.id,
                fuel_type=risk.fuel_type,
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

            projection = self._project(world=world, risk=risk, route=route, quantity=quantity)
            recommendation = self._build_recommendation(
                world=world,
                risk=risk,
                route=route,
                quantity=quantity,
                projection=projection,
                report=report,
                fallback=fallback,
            )
            candidates.append((self._score(projection, route), recommendation))

        if not candidates:
            return None

        candidates.sort(key=lambda item: item[0], reverse=True)
        _, best = candidates[0]
        best_transit = self._transit_ticks(world, best.route_id)

        alternatives: list[Alternative] = []
        for score, recommendation in candidates[1:]:
            transit = self._transit_ticks(world, recommendation.route_id)
            alternatives.append(
                Alternative(
                    route_id=recommendation.route_id,
                    source_depot_id=recommendation.source_depot_id,
                    transit_ticks=transit,
                    quantity=recommendation.quantity,
                    reason=(
                        f"arrives {transit - best_transit} tick(s) later than {best.route_id}"
                        if transit > best_transit
                        else "a smaller improvement in projected cover"
                    ),
                    hours_to_stockout_after=(
                        recommendation.projection.hours_to_stockout_after
                        if recommendation.projection
                        else None
                    ),
                    score=score,
                )
            )
        alternatives.extend(rejected)
        alternatives.sort(key=lambda alternative: alternative.score, reverse=True)
        best.alternatives = alternatives[: self._config.max_alternatives]

        return best

    def _build_recommendation(
        self,
        *,
        world: WorldSnapshot,
        risk: RiskAssessment,
        route: object,
        quantity: float,
        projection: RiskProjection,
        report: ConstraintReport,
        fallback: bool,
    ) -> Recommendation:
        """Assemble the recommendation, with everything that explains it."""
        route_id = route.id
        depot_id = route.source_depot_id

        # The idempotency key is deterministic: the same station, fuel,
        # tick and quantity always produce the same key, so a retried
        # submission is a replay rather than a second shipment. The
        # simulator's key is permanently occupied once used, so the
        # quantity has to be part of it — a changed body needs a new key.
        idempotency_key = f"rec-{risk.station_id}-{risk.fuel_type}-{world.tick}-{int(quantity)}"

        confidence = self._confidence(risk, projection, fallback)

        return Recommendation(
            id=idempotency_key,
            station_id=risk.station_id,
            fuel_type=risk.fuel_type,
            source_depot_id=depot_id,
            route_id=route_id,
            quantity=quantity,
            status=RecommendationStatus.PROPOSED,
            risk_level=risk.risk_level.value,
            hours_to_stockout=risk.hours_to_stockout,
            stockout_probability=risk.stockout_probability,
            projection=projection,
            signals=[signal.to_payload() for signal in risk.signals],
            constraints=report.to_payload(),
            confidence=confidence,
            needs_human_review=(
                fallback
                or risk.needs_human_review
                or confidence < 0.6
                or risk.risk_level is RiskLevel.CRITICAL
            ),
            used_fallback_policy=fallback,
            idempotency_key=idempotency_key,
            rationale=self._rationale(risk, projection, route, quantity, fallback),
            created_at=datetime.now(UTC),
            sim_tick=world.tick,
        )

    # -- the numbers ---------------------------------------------------------

    def _needed_liters(self, risk: RiskAssessment, world: WorldSnapshot) -> float:
        """How much fuel the station needs to reach its target cover.

        The target is the demand over the cover window, but never more
        than the tank can physically hold — asking for more is how a
        station ends up being shipped fuel it has nowhere to put.
        """
        forecast = risk.forecast
        if not forecast.points:
            return 0.0

        tick_hours = world.tick_minutes / 60.0
        ticks_needed = max(1, int(self._config.target_cover_hours / tick_hours))
        covered = min(ticks_needed, len(forecast.points))
        expected = sum(point.predicted_demand for point in forecast.points[:covered])

        if 0 < covered < ticks_needed:
            # The forecast is shorter than the cover window, so the rate is
            # extrapolated. Shipping only the horizon's demand would leave
            # the station short again every cycle.
            expected = (expected / covered) * ticks_needed

        station = world.stations.get(risk.station_id)
        capacity = station.capacity.get(risk.fuel_type, 0.0) if station else 0.0
        target = min(expected, capacity)

        return max(0.0, target - self._available_from_risk(risk))

    def _available_from_risk(self, risk: RiskAssessment) -> float:
        """The fuel the station already has, plus what is on its way."""
        for signal in risk.signals:
            if signal.name == "available_liters":
                return signal.value
        return 0.0

    def _max_quantity(
        self,
        world: WorldSnapshot,
        depot_id: str,
        route: object,
        fuel_type: str,
        spent: dict[str, float],
    ) -> float:
        """The largest quantity this route can carry right now.

        This is the simulator's cap, minus whatever the earlier
        recommendations in this pass have already committed.
        """
        cap = self._validator.max_quantity(
            world=world,
            source_depot_id=depot_id,
            destination_station_id=route.destination_station_id,
            route_id=route.id,
            fuel_type=fuel_type,
        )
        return max(0.0, cap - spent.get(depot_id, 0.0))

    def _why_empty(
        self,
        world: WorldSnapshot,
        depot_id: str,
        route: object,
        fuel_type: str,
    ) -> str:
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

    # -- the projection ------------------------------------------------------

    def _project(
        self,
        *,
        world: WorldSnapshot,
        risk: RiskAssessment,
        route: object,
        quantity: float,
    ) -> RiskProjection:
        """Project the station's risk with and without this shipment.

        The projection is **local**: it replays the forecast against the
        fuel the shipment would deliver, from the tick the fuel would
        arrive. Nothing is written and the simulator is never consulted.
        """
        forecast = risk.forecast
        station = world.stations.get(risk.station_id)
        on_hand = station.inventory.get(risk.fuel_type, 0.0) if station else 0.0

        horizon_tick = forecast.points[-1].tick if forecast.points else world.tick
        incoming = sum(
            allocation.quantity
            for allocation in world.in_transit_to(risk.station_id, risk.fuel_type)
            if allocation.expected_arrival_tick is not None
            and allocation.expected_arrival_tick <= horizon_tick
        )
        available_before = on_hand + incoming
        arrival_tick = world.tick + route.transit_ticks
        tick_hours = world.tick_minutes / 60.0

        before = self._replay(
            forecast=forecast,
            available=available_before,
            arrival_tick=None,
            tick_hours=tick_hours,
        )
        after = self._replay(
            forecast=forecast,
            available=available_before,
            arrival_tick=arrival_tick,
            extra=quantity,
            tick_hours=tick_hours,
        )

        return RiskProjection(
            risk_level_before=self._level(before),
            risk_level_after=self._level(after),
            hours_to_stockout_before=before[0],
            hours_to_stockout_after=after[0],
            stockout_probability_before=before[1],
            stockout_probability_after=after[1],
            inventory_before=on_hand,
            inventory_after=on_hand + quantity,
            available_before=available_before,
            available_after=available_before + quantity,
            arrival_tick=arrival_tick,
            hours_until_arrival=route.transit_ticks * tick_hours,
        )

    def _replay(
        self,
        *,
        forecast: Forecast,
        available: float,
        arrival_tick: int | None,
        tick_hours: float,
        extra: float = 0.0,
    ) -> tuple[float | None, float]:
        """Replay the forecast and return ``(hours_to_stockout, probability)``.

        ``arrival_tick`` is when the extra fuel becomes usable. Before that
        tick the shipment cannot help, which is what makes a slow route
        score honestly.
        """
        if not forecast.points:
            return None, 0.0

        running = 0.0
        hours: float | None = None
        lower_total = 0.0
        upper_total = 0.0

        for index, point in enumerate(forecast.points):
            running += point.predicted_demand
            lower_total += point.lower_bound
            upper_total += point.upper_bound

            usable = available + (
                extra if arrival_tick is not None and point.tick >= arrival_tick else 0.0
            )
            if hours is None and running >= usable:
                previous = running - point.predicted_demand
                span = running - previous
                fraction = (usable - previous) / span if span > 0 else 1.0
                hours = round((index + fraction) * tick_hours, 2)

        final_available = available + extra
        if lower_total >= final_available:
            probability = 1.0
        elif upper_total <= final_available:
            probability = 0.0
        else:
            overshoot = upper_total - final_available
            headroom = max(1e-6, upper_total - lower_total)
            probability = round(max(0.0, min(1.0, overshoot / headroom)), 4)

        return hours, probability

    def _level(self, replay: tuple[float | None, float]) -> str:
        """The risk band a replayed projection falls into."""
        hours, probability = replay
        if hours is None:
            return RiskLevel.WATCH.value if probability >= 0.25 else RiskLevel.OK.value
        if hours <= 8.0:
            return RiskLevel.CRITICAL.value
        return RiskLevel.WATCH.value

    # -- scoring -------------------------------------------------------------

    def _score(self, projection: RiskProjection, route: object) -> float:
        """Rank the candidate routes.

        More cover is better; earlier arrival is better. The two are
        combined into a single number so the ranking is explicit.
        """
        cover = (
            projection.hours_to_stockout_after
            if projection.hours_to_stockout_after is not None
            else 48.0
        )
        return round(cover - route.transit_ticks * 0.5, 4)

    def _confidence(
        self, risk: RiskAssessment, projection: RiskProjection, fallback: bool
    ) -> float:
        """How much the engine trusts this recommendation."""
        if fallback:
            # The fallback policy has no model behind it.
            return 0.4
        confidence = risk.forecast.confidence
        if projection.hours_to_stockout_after is None and risk.hours_to_stockout is not None:
            # The shipment removes the stockout entirely.
            confidence = min(1.0, confidence + 0.05)
        return round(confidence, 4)

    def _rationale(
        self,
        risk: RiskAssessment,
        projection: RiskProjection,
        route: object,
        quantity: float,
        fallback: bool,
    ) -> str:
        """One line explaining the recommendation."""
        if fallback:
            return (
                f"Refill {risk.station_id} ({risk.fuel_type.lower()}): it has the "
                f"lowest days of cover and the forecast model is unavailable. "
                f"Ship {quantity:,.0f} L from {route.source_depot_id} via "
                f"{route.id}, arriving in {route.transit_ticks} ticks."
            )

        if risk.hours_to_stockout is not None:
            situation = (
                f"{risk.risk_level.value} risk at {risk.station_id}: "
                f"{risk.hours_to_stockout:.0f} h of cover left."
            )
        else:
            situation = (
                f"{risk.risk_level.value} risk at {risk.station_id}: the forecast "
                f"exceeds what is available."
            )

        cover = (
            f"{projection.hours_to_stockout_after:.0f} h"
            if projection.hours_to_stockout_after is not None
            else "beyond the forecast horizon"
        )
        return (
            f"{situation} Ship {quantity:,.0f} L from {route.source_depot_id} via "
            f"{route.id} (arrives in {route.transit_ticks} ticks), lifting cover "
            f"to {cover}."
        )
