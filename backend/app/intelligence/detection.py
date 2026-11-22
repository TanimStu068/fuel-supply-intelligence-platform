"""Detection: demand anomalies, inventory anomalies and bottlenecks.

Three detectors, each answering a different operator question:

**Demand anomaly** — "is this station's demand behaving differently from
what we expected?" Scored as a z-score against the forecast's own residual
spread, with an EWMA for level shifts.

**Inventory anomaly** — "did this station's inventory just move in a way we
cannot explain?" A change larger than the forecast demand plus the expected
deliveries is a delivery or a loss, not consumption.

**Bottleneck** — "what is limiting the network right now?" Depot dispatch
capacity, disrupted routes, delayed supply arrivals and low depot stock.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum


class AnomalyKind(StrEnum):
    """What kind of anomaly was detected."""

    DEMAND = "demand"
    INVENTORY = "inventory"


class BottleneckKind(StrEnum):
    """What kind of bottleneck was detected."""

    DISPATCH_CAPACITY = "dispatch_capacity"
    ROUTE_DISRUPTED = "route_disrupted"
    SUPPLY_DELAYED = "supply_delayed"
    DEPOT_LOW_STOCK = "depot_low_stock"
    STATION_OUTAGE = "station_outage"


@dataclass(slots=True)
class Anomaly:
    """One detected anomaly."""

    kind: AnomalyKind
    entity_id: str
    fuel_type: str | None
    severity: str
    score: float
    message: str
    expected: float
    actual: float
    detected_at: datetime | None = None
    detail: dict[str, float] = field(default_factory=dict)

    def to_payload(self) -> dict[str, object]:
        return {
            "kind": self.kind.value,
            "entity_id": self.entity_id,
            "fuel_type": self.fuel_type,
            "severity": self.severity,
            "score": round(self.score, 4),
            "message": self.message,
            "expected": round(self.expected, 3),
            "actual": round(self.actual, 3),
            "detail": {key: round(value, 4) for key, value in self.detail.items()},
        }


@dataclass(slots=True)
class Bottleneck:
    """One detected bottleneck."""

    kind: BottleneckKind
    entity_id: str
    severity: str
    message: str
    impact: str = ""
    detail: dict[str, float] = field(default_factory=dict)

    def to_payload(self) -> dict[str, object]:
        return {
            "kind": self.kind.value,
            "entity_id": self.entity_id,
            "severity": self.severity,
            "message": self.message,
            "impact": self.impact,
            "detail": {key: round(value, 4) for key, value in self.detail.items()},
        }


# --- demand anomalies ------------------------------------------------------


class DemandAnomalyDetector:
    """Scores how far the observed demand is from what was expected.

    Two signals, because they catch different things:

    **The z-score** compares the observation against the *published
    profile* — the demand the station would draw if no crisis event were
    active. The simulator's demand multiplier rises during a
    ``demand_spike``, so the platform could compare against the current
    multiplier and explain the spike away with the very thing that caused
    it. Comparing against the published profile keeps the spike visible.

    **The EWMA drift** compares a smoothed level against the same
    expectation. A single tick that is only mildly high sits inside the
    profile's published noise bound and must not be flagged, but a
    *sustained* shift eventually drags the smoothed level far enough away
    to be worth reporting.

    The reference the caller supplies must therefore be the *profile*
    expectation, not one computed from the current event state.
    """

    #: Above this z-score a demand anomaly is reported.
    Z_THRESHOLD = 3.0
    #: The EWMA weight: how much of each new observation is kept.
    EWMA_ALPHA = 0.3
    #: How far the EWMA level may drift from the expectation before it counts.
    EWMA_DRIFT_THRESHOLD = 0.25

    def __init__(self, *, z_threshold: float | None = None) -> None:
        if z_threshold is not None:
            self.Z_THRESHOLD = z_threshold
        #: The EWMA level per (station, fuel) key.
        self._levels: dict[tuple[str, str], float] = {}

    def detect(
        self,
        *,
        station_id: str,
        fuel_type: str,
        expected: float,
        actual: float,
        uncertainty: float,
    ) -> Anomaly | None:
        """Detect an anomaly in one observation, or return ``None``."""
        key = (station_id, fuel_type)

        residual = actual - expected
        # Guard against a zero-width interval, which would make any tiny
        # residual an infinite z-score.
        scale = max(uncertainty, abs(expected) * 0.05, 1e-6)
        z_score = residual / scale

        previous_level = self._levels.get(key)
        ewma_level = (
            actual
            if previous_level is None
            else self.EWMA_ALPHA * actual + (1 - self.EWMA_ALPHA) * previous_level
        )
        self._levels[key] = ewma_level

        drift = 0.0
        if expected > 0:
            drift = (ewma_level - expected) / expected

        if abs(z_score) < self.Z_THRESHOLD and abs(drift) < self.EWMA_DRIFT_THRESHOLD:
            return None

        severity = "critical" if abs(z_score) >= 5.0 or abs(drift) >= 0.5 else "warning"

        # The message has to be internally consistent: the percentage and
        # the litre figures must describe the same comparison. The EWMA
        # drift and the raw observation are two different numbers, so
        # they cannot be quoted side by side.
        if previous_level is not None and abs(drift) >= self.EWMA_DRIFT_THRESHOLD:
            # The smoothed level is what moved, so the drift is the
            # headline and the smoothed level is quoted beside it.
            direction = "above" if drift > 0 else "below"
            message = (
                f"{station_id} {fuel_type} demand is {abs(drift):.0%} {direction} "
                f"the expected level on average "
                f"({ewma_level:,.0f} L smoothed against {expected:,.0f} L expected)"
            )
        else:
            # A single observation deviated, or the smoothed level has not
            # moved yet. The observation itself is the evidence.
            deviation = (actual - expected) / expected if expected else 0.0
            direction = "above" if deviation > 0 else "below"
            message = (
                f"{station_id} {fuel_type} demand is {abs(deviation):.0%} {direction} "
                f"the expected level ({actual:,.0f} L against {expected:,.0f} L)"
            )

        return Anomaly(
            kind=AnomalyKind.DEMAND,
            entity_id=station_id,
            fuel_type=fuel_type,
            severity=severity,
            score=round(max(abs(z_score), abs(drift) * 10), 4),
            message=message,
            expected=expected,
            actual=actual,
            detail={
                "z_score": z_score,
                "residual": residual,
                "ewma_level": ewma_level,
                "relative_drift": drift,
            },
        )


# --- inventory anomalies ---------------------------------------------------


class InventoryAnomalyDetector:
    """Detects inventory changes that consumption alone cannot explain.

    A station's inventory should fall by roughly the demand it served. If it
    falls by more, something arrived and was counted differently, or fuel
    went missing. If it *rises* by more than the expected deliveries, the
    ledger and the physical stock disagree.
    """

    #: The multiple of the expected change above which we call it anomalous.
    THRESHOLD = 2.0
    #: The smallest absolute change worth reporting, in litres.
    MIN_ABSOLUTE_CHANGE = 50.0

    def detect(
        self,
        *,
        entity_id: str,
        fuel_type: str,
        previous_inventory: float,
        current_inventory: float,
        expected_consumption: float,
        expected_deliveries: float = 0.0,
    ) -> Anomaly | None:
        """Detect an unexplained inventory change, or return ``None``."""
        change = current_inventory - previous_inventory
        expected_change = expected_deliveries - expected_consumption

        unexplained = change - expected_change
        magnitude = abs(unexplained)

        if magnitude < self.MIN_ABSOLUTE_CHANGE:
            return None

        # Scale the threshold by the size of the expected movement so a
        # busy station is not flagged for a small absolute wobble.
        scale = max(abs(expected_change), abs(expected_consumption) * 0.25, 1.0)
        if magnitude < self.THRESHOLD * scale:
            return None

        severity = "critical" if magnitude >= 3 * scale else "warning"
        direction = "dropped" if unexplained < 0 else "rose"

        return Anomaly(
            kind=AnomalyKind.INVENTORY,
            entity_id=entity_id,
            fuel_type=fuel_type,
            severity=severity,
            score=round(magnitude / scale, 4),
            message=(
                f"{entity_id} {fuel_type} inventory {direction} by {magnitude:,.0f} L "
                f"more than consumption explains"
            ),
            expected=expected_change,
            actual=change,
            detail={
                "unexplained_change": unexplained,
                "expected_change": expected_change,
                "previous_inventory": previous_inventory,
                "current_inventory": current_inventory,
            },
        )


# --- bottlenecks -----------------------------------------------------------


class BottleneckDetector:
    """Finds the constraints that limit the network right now.

    Each check looks at one kind of constraint and reports only when the
    constraint is actually binding — a bottleneck detector that reports
    every constraint is a noise generator.
    """

    #: Dispatch capacity below this fraction of the limit is a bottleneck.
    DISPATCH_CAPACITY_RATIO = 0.8
    #: Depot stock below this fraction of capacity is a bottleneck.
    DEPOT_STOCK_RATIO = 0.2
    #: A supply arrival delayed by this many ticks is a bottleneck.
    SUPPLY_DELAY_TICKS = 2

    def detect(
        self,
        *,
        depots: list[dict[str, object]],
        routes: list[dict[str, object]],
        supply_arrivals: list[dict[str, object]],
        stations: list[dict[str, object]],
        allocations_in_transit: list[dict[str, object]],
        tick_minutes: int = 15,
    ) -> list[Bottleneck]:
        """Return every bottleneck the current state implies."""
        bottlenecks: list[Bottleneck] = []

        bottlenecks.extend(self._dispatch_capacity(depots, allocations_in_transit))
        bottlenecks.extend(self._route_disruptions(routes))
        bottlenecks.extend(self._supply_delays(supply_arrivals, tick_minutes))
        bottlenecks.extend(self._depot_low_stock(depots))
        bottlenecks.extend(self._station_outages(stations))

        return bottlenecks

    def _dispatch_capacity(
        self, depots: list[dict[str, object]], allocations: list[dict[str, object]]
    ) -> list[Bottleneck]:
        """Depots that are close to their per-tick dispatch limit."""
        bottlenecks: list[Bottleneck] = []

        for depot in depots:
            depot_id = str(depot.get("id", ""))
            limit = float(depot.get("dispatch_capacity_per_tick", 0.0))
            if limit <= 0:
                continue

            committed = sum(
                float(allocation.get("quantity", 0.0))
                for allocation in allocations
                if allocation.get("source_depot_id") == depot_id
                and allocation.get("status") in {"PENDING", "IN_TRANSIT"}
            )

            # The simulator's limit is per tick, and a pending allocation
            # counts against the tick it was created in.
            ratio = committed / limit
            if ratio < self.DISPATCH_CAPACITY_RATIO:
                continue

            severity = "critical" if ratio >= 1.0 else "warning"
            bottlenecks.append(
                Bottleneck(
                    kind=BottleneckKind.DISPATCH_CAPACITY,
                    entity_id=depot_id,
                    severity=severity,
                    message=(
                        f"{depot_id} has committed {committed:,.0f} L of its "
                        f"{limit:,.0f} L per-tick dispatch capacity ({ratio:.0%})"
                    ),
                    impact="new allocations from this depot may be rejected",
                    detail={"committed": committed, "limit": limit, "ratio": ratio},
                )
            )

        return bottlenecks

    def _route_disruptions(self, routes: list[dict[str, object]]) -> list[Bottleneck]:
        """Routes that cannot be used."""
        return [
            Bottleneck(
                kind=BottleneckKind.ROUTE_DISRUPTED,
                entity_id=str(route.get("id", "")),
                severity="critical",
                message=f"Route {route.get('id')} is disrupted",
                impact="shipments on this route will fail at departure",
                detail={"transit_ticks": float(route.get("transit_ticks", 0))},
            )
            for route in routes
            if route.get("status") == "DISRUPTED"
        ]

    def _supply_delays(
        self, supply_arrivals: list[dict[str, object]], tick_minutes: int
    ) -> list[Bottleneck]:
        """Supply arrivals that are running late."""
        bottlenecks: list[Bottleneck] = []

        for arrival in supply_arrivals:
            if arrival.get("status") != "DELAYED":
                continue

            delay_ticks = float(arrival.get("delay_ticks", self.SUPPLY_DELAY_TICKS))
            if delay_ticks < self.SUPPLY_DELAY_TICKS:
                continue

            hours = delay_ticks * tick_minutes / 60.0
            bottlenecks.append(
                Bottleneck(
                    kind=BottleneckKind.SUPPLY_DELAYED,
                    entity_id=str(arrival.get("id", "")),
                    severity="warning" if delay_ticks < 4 else "critical",
                    message=(
                        f"Supply {arrival.get('id')} is delayed by about "
                        f"{hours:.1f} simulated hours"
                    ),
                    impact=f"{float(arrival.get('quantity', 0)):,.0f} L will reach the depot late",
                    detail={
                        "delay_ticks": delay_ticks,
                        "delay_hours": hours,
                        "quantity": float(arrival.get("quantity", 0)),
                    },
                )
            )

        return bottlenecks

    def _depot_low_stock(self, depots: list[dict[str, object]]) -> list[Bottleneck]:
        """Depots that are running low on a fuel."""
        bottlenecks: list[Bottleneck] = []

        for depot in depots:
            depot_id = str(depot.get("id", ""))
            inventory = depot.get("inventory", {})
            capacity = depot.get("capacity", {})
            if not isinstance(inventory, dict) or not isinstance(capacity, dict):
                continue

            for fuel, amount in inventory.items():
                limit = float(capacity.get(fuel, 0.0))
                if limit <= 0:
                    continue

                ratio = float(amount) / limit
                if ratio >= self.DEPOT_STOCK_RATIO:
                    continue

                bottlenecks.append(
                    Bottleneck(
                        kind=BottleneckKind.DEPOT_LOW_STOCK,
                        entity_id=depot_id,
                        severity="warning" if ratio >= 0.1 else "critical",
                        message=(
                            f"{depot_id} is low on {fuel}: {float(amount):,.0f} L of "
                            f"{limit:,.0f} L capacity ({ratio:.0%})"
                        ),
                        impact="dispatch from this depot may be constrained soon",
                        detail={"inventory": float(amount), "capacity": limit, "ratio": ratio},
                    )
                )

        return bottlenecks

    def _station_outages(self, stations: list[dict[str, object]]) -> list[Bottleneck]:
        """Stations that are out of service."""
        return [
            Bottleneck(
                kind=BottleneckKind.STATION_OUTAGE,
                entity_id=str(station.get("id", "")),
                severity="critical",
                message=f"Station {station.get('id')} is in outage",
                impact="no demand is served at this station until it reopens",
                detail={},
            )
            for station in stations
            if station.get("status") == "OUTAGE"
        ]


def combine_ewma(values: list[float], alpha: float = 0.3) -> float:
    """The exponentially weighted moving average of a series."""
    if not values:
        return 0.0
    level = values[0]
    for value in values[1:]:
        level = alpha * value + (1 - alpha) * level
    return level


def z_score(value: float, mean: float, stddev: float) -> float:
    """The z-score of a value, safe against a zero standard deviation."""
    return (value - mean) / max(stddev, 1e-9)


def percentage_change(old: float, new: float) -> float:
    """The relative change from ``old`` to ``new``."""
    if old == 0:
        return 0.0
    return (new - old) / old


def is_significant(z: float, threshold: float = 3.0) -> bool:
    """Whether a z-score clears the significance threshold."""
    return not math.isnan(z) and abs(z) >= threshold
