"""The intelligence engine: the one place the whole layer is driven from.

It takes the current world state and the stored demand history, and produces

* a forecast per (station, fuel),
* a risk assessment per (station, fuel),
* the demand and inventory anomalies,
* the network bottlenecks,

plus the metadata that says **which model produced each number** and
whether a human should look at it before it is acted on.

The fallback rule is the important part. The learned model is used when it
is trained, has enough data and is confident enough. Otherwise the
rule-based baseline answers, the forecast is flagged
``needs_human_review``, and the reason is recorded. The platform never
silently produces a number it cannot stand behind.

**The anomaly reference.** Demand anomalies are scored against the
station's *pre-event* demand level, not against a baseline computed with the
current demand multiplier. That is deliberate: once a ``demand_spike``
raises the multiplier, a baseline that knows about the spike reproduces it
perfectly and would never flag it. Comparing against the level the station
had before the event is what makes the spike visible.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime

from ..simulator_models import Allocation, Depot, Route, Station, SupplyArrival
from .detection import (
    Anomaly,
    AnomalyKind,
    Bottleneck,
    BottleneckDetector,
    DemandAnomalyDetector,
    InventoryAnomalyDetector,
)
from .forecasting import (
    Forecast,
    ForecastSelector,
    HistoryPoint,
    ModelKind,
    StationContext,
)
from .profiles import baseline_shape, noise_bound
from .risk import InTransitShipment, RiskAssessment, RiskEngine, RiskLevel

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class IntelligenceResult:
    """Everything the intelligence layer produced for one evaluation."""

    forecasts: list[Forecast] = field(default_factory=list)
    risks: list[RiskAssessment] = field(default_factory=list)
    anomalies: list[Anomaly] = field(default_factory=list)
    bottlenecks: list[Bottleneck] = field(default_factory=list)
    #: Which model was active for each forecast.
    models_used: dict[str, int] = field(default_factory=dict)
    #: How many forecasts need a human to look at them.
    needs_human_review_count: int = 0
    #: The fallback activations, by reason.
    fallback_activations: dict[str, int] = field(default_factory=dict)
    #: When the evaluation ran.
    evaluated_at: datetime = field(default_factory=datetime.now)

    @property
    def critical_risks(self) -> list[RiskAssessment]:
        """The assessments at CRITICAL risk."""
        return [risk for risk in self.risks if risk.risk_level is RiskLevel.CRITICAL]

    @property
    def watch_risks(self) -> list[RiskAssessment]:
        """The assessments at WATCH risk."""
        return [risk for risk in self.risks if risk.risk_level is RiskLevel.WATCH]

    @property
    def active_model(self) -> ModelKind:
        """The model that produced most of the forecasts."""
        if not self.models_used:
            return ModelKind.BASELINE
        return ModelKind(max(self.models_used, key=lambda key: self.models_used[key]))

    def to_payload(self) -> dict[str, object]:
        """Render the result as a JSON-safe dictionary."""
        return {
            "evaluated_at": self.evaluated_at.isoformat(),
            "models_used": self.models_used,
            "active_model": self.active_model.value,
            "needs_human_review_count": self.needs_human_review_count,
            "fallback_activations": self.fallback_activations,
            "forecasts": [forecast.to_payload() for forecast in self.forecasts],
            "risks": [risk.to_payload() for risk in self.risks],
            "anomalies": [anomaly.to_payload() for anomaly in self.anomalies],
            "bottlenecks": [bottleneck.to_payload() for bottleneck in self.bottlenecks],
        }


class IntelligenceEngine:
    """Drives forecasting, risk scoring and detection over one world state."""

    def __init__(
        self,
        *,
        selector: ForecastSelector | None = None,
        risk_engine: RiskEngine | None = None,
        demand_detector: DemandAnomalyDetector | None = None,
        inventory_detector: InventoryAnomalyDetector | None = None,
        bottleneck_detector: BottleneckDetector | None = None,
        horizon_ticks: int = 24,
    ) -> None:
        self._selector = selector or ForecastSelector()
        self._risk = risk_engine or RiskEngine()
        self._demand_detector = demand_detector or DemandAnomalyDetector()
        self._inventory_detector = inventory_detector or InventoryAnomalyDetector()
        self._bottleneck_detector = bottleneck_detector or BottleneckDetector()
        self._horizon_ticks = horizon_ticks
        #: The inventory we saw last time, for the inventory-anomaly check.
        self._previous_inventory: dict[tuple[str, str], float] = {}
        #: The tick that inventory belonged to, so the anomaly check knows
        #: how many ticks of demand the change has to account for.
        self._previous_inventory_tick: dict[tuple[str, str], int] = {}
        #: The selector's fallback counters as of the last evaluation, so
        #: the Prometheus series counts activations and not their running
        #: total.
        self._previous_fallback_counts: dict[str, int] = {}

    @property
    def selector(self) -> ForecastSelector:
        """The forecast selector, so callers can inspect the active model."""
        return self._selector

    @property
    def horizon_ticks(self) -> int:
        """How many ticks ahead the forecast reaches."""
        return self._horizon_ticks

    def evaluate(
        self,
        *,
        stations: list[Station],
        depots: list[Depot],
        routes: list[Route],
        allocations: list[Allocation],
        supply_arrivals: list[SupplyArrival],
        region_demand_factors: dict[str, float],
        current_tick: int,
        current_sim_time: datetime | None,
        tick_minutes: int,
        history: list[HistoryPoint],
    ) -> IntelligenceResult:
        """Evaluate the whole network and return everything we found."""
        result = IntelligenceResult()

        # The fallback counters are per evaluation, so a reason that keeps
        # firing is one ongoing condition rather than one event per
        # forecast.
        self._selector.begin_evaluation()

        in_transit = self._in_transit(allocations)
        contexts = self._build_contexts(
            stations=stations,
            region_demand_factors=region_demand_factors,
            current_tick=current_tick,
            current_sim_time=current_sim_time,
            tick_minutes=tick_minutes,
        )

        for station in stations:
            context = contexts[station.id]
            for fuel in station.inventory:
                forecast = self._selector.forecast(
                    context=context,
                    fuel=fuel,
                    horizon_ticks=self._horizon_ticks,
                    history=history,
                )
                result.forecasts.append(forecast)
                result.models_used[forecast.model.value] = (
                    result.models_used.get(forecast.model.value, 0) + 1
                )
                if forecast.needs_human_review:
                    result.needs_human_review_count += 1

                assessment = self._risk.assess(
                    station_id=station.id,
                    fuel_type=fuel,
                    inventory=station.inventory[fuel],
                    capacity=station.capacity.get(fuel, 0.0),
                    forecast=forecast,
                    in_transit=in_transit,
                    tick_minutes=tick_minutes,
                )
                result.risks.append(assessment)

        result.anomalies.extend(
            self._detect_demand_anomalies(stations=stations, history=history, contexts=contexts)
        )
        result.anomalies.extend(
            self._detect_inventory_anomalies(
                stations=stations,
                allocations=allocations,
                current_tick=current_tick,
                history=history,
            )
        )
        result.bottlenecks.extend(
            self._bottleneck_detector.detect(
                depots=[depot.model_dump(mode="json") for depot in depots],
                routes=[route.model_dump(mode="json") for route in routes],
                supply_arrivals=[arrival.model_dump(mode="json") for arrival in supply_arrivals],
                stations=[station.model_dump(mode="json") for station in stations],
                allocations_in_transit=[
                    allocation.model_dump(mode="json") for allocation in allocations
                ],
                tick_minutes=tick_minutes,
            )
        )

        result.fallback_activations = self._fallback_delta()
        return result

    def _fallback_delta(self) -> dict[str, int]:
        """How many times each fallback reason fired in this evaluation.

        The selector's counters are cumulative, but the Prometheus series
        is a counter too — incrementing it by the cumulative total on
        every cycle would count the same activation over and over. The
        delta is what actually happened since the last evaluation.
        """
        delta: dict[str, int] = {}
        for reason, total in self._selector.fallback_counts.items():
            previous = self._previous_fallback_counts.get(reason, 0)
            if total > previous:
                delta[reason] = total - previous
        self._previous_fallback_counts = dict(self._selector.fallback_counts)
        return delta

    def _forecast_map(self, result: IntelligenceResult) -> dict[tuple[str, str], Forecast]:
        """Index the forecasts by (station, fuel)."""
        return {
            (forecast.station_id, forecast.fuel_type): forecast for forecast in result.forecasts
        }

    def build_contexts(
        self,
        *,
        stations: list[Station],
        region_demand_factors: dict[str, float],
        current_tick: int,
        current_sim_time: datetime | None,
        tick_minutes: int,
    ) -> dict[str, StationContext]:
        """Build the per-station forecasting context.

        Public so the trainer can build the same contexts the engine uses.
        """
        return self._build_contexts(
            stations=stations,
            region_demand_factors=region_demand_factors,
            current_tick=current_tick,
            current_sim_time=current_sim_time,
            tick_minutes=tick_minutes,
        )

    def _build_contexts(
        self,
        *,
        stations: list[Station],
        region_demand_factors: dict[str, float],
        current_tick: int,
        current_sim_time: datetime | None,
        tick_minutes: int,
    ) -> dict[str, StationContext]:
        """Build the per-station forecasting context."""
        return {
            station.id: StationContext(
                station_id=station.id,
                profile_name=station.demand_profile,
                region_demand_factor=region_demand_factors.get(station.region_id, 1.0),
                demand_multiplier=station.demand_multiplier,
                current_tick=current_tick,
                tick_minutes=tick_minutes,
                current_sim_time=current_sim_time,
            )
            for station in stations
        }

    def _in_transit(self, allocations: list[Allocation]) -> list[InTransitShipment]:
        """The allocations that are on their way to a station.

        Both PENDING and IN_TRANSIT count: a pending allocation has already
        left the depot's inventory, so the fuel is committed and will arrive.

        The destination is kept on every shipment. Fuel that is in transit to
        one station is not cover for another, and counting it as such would
        hide exactly the shortages the risk engine exists to surface.
        """
        return [
            InTransitShipment(
                allocation_id=allocation.id,
                destination_station_id=allocation.destination_station_id,
                fuel_type=allocation.fuel_type.value,
                quantity=allocation.quantity,
                expected_arrival_tick=allocation.expected_arrival_tick,
                route_id=allocation.route_id,
            )
            for allocation in allocations
            if allocation.status.value in {"PENDING", "IN_TRANSIT"}
        ]

    def _detect_demand_anomalies(
        self,
        *,
        stations: list[Station],
        history: list[HistoryPoint],
        contexts: dict[str, StationContext],
    ) -> list[Anomaly]:
        """Compare the most recent observation against the pre-event level.

        Only the newest observation per (station, fuel) is checked: the
        forecast is for the *next* tick, and comparing older observations
        against it would be meaningless.
        """
        anomalies: list[Anomaly] = []
        latest: dict[tuple[str, str], HistoryPoint] = {}

        for point in history:
            key = (point.station_id, point.fuel_type)
            current = latest.get(key)
            if current is None or point.tick > current.tick:
                latest[key] = point

        for (station_id, fuel), point in latest.items():
            context = contexts.get(station_id)
            if context is None:
                continue

            expected = self._reference_expectation(context, fuel, point)
            uncertainty = max(abs(expected) * noise_bound(context.profile_name), 1.0)

            anomaly = self._demand_detector.detect(
                station_id=station_id,
                fuel_type=fuel,
                expected=expected,
                actual=point.demand_liters,
                uncertainty=uncertainty,
            )
            if anomaly is not None:
                anomalies.append(anomaly)

        return anomalies

    def _reference_expectation(
        self, context: StationContext, fuel: str, point: HistoryPoint
    ) -> float:
        """The demand the station would show if no crisis event were active.

        This is the **published profile** — the shape alone, with no demand
        multiplier. The simulator raises a station's multiplier during a
        ``demand_spike``, so comparing the observation against the current
        multiplier would explain the spike away with the very thing that
        caused it.

        The profile is the honest reference: it is what the station was
        drawing before anything unusual happened, and a deviation from it is
        exactly what an operator needs to know about.
        """
        shape = baseline_shape(
            profile_name=context.profile_name,
            fuel=fuel,
            hour=point.sim_time.hour,
            region_demand_factor=context.region_demand_factor,
        )
        return shape

    def _detect_inventory_anomalies(
        self,
        *,
        stations: list[Station],
        allocations: list[Allocation],
        current_tick: int,
        history: list[HistoryPoint],
    ) -> list[Anomaly]:
        """Compare each station's inventory against the previous evaluation.

        The comparison has to be made over the *same window* the inventory
        actually changed in. Two evaluations are typically several ticks
        apart, and fuel is consumed every tick, so a change is only
        "unexplained" relative to the demand over that many ticks — not to
        a single tick's worth.

        Deliveries are counted too: an allocation that arrived inside the
        window explains a rise in the tank, and without it every refuelled
        station would raise a false critical anomaly.

        The consumption the change is measured against is the **observed**
        fuel actually drawn over the window, not the forecast and not the
        demand. A demand spike is a real draw on the tank, and comparing a
        spike against the calm forecast would report every affected station
        as a critical inventory anomaly — the detector would be describing
        the event it exists to detect.
        """
        anomalies: list[Anomaly] = []

        for station in stations:
            for fuel, amount in station.inventory.items():
                key = (station.id, fuel)
                previous = self._previous_inventory.get(key)

                if previous is not None:
                    previous_tick = self._previous_inventory_tick.get(key)
                    consumption, window_covered = self._observed_consumption(
                        history=history,
                        station_id=station.id,
                        fuel_type=fuel,
                        from_tick=previous_tick,
                        to_tick=current_tick,
                    )

                    # A window the stored history does not fully cover
                    # cannot be judged: the fuel drawn over the missing
                    # ticks is unknown, so any comparison would report a
                    # change that never happened.
                    if window_covered:
                        expected_deliveries = self._delivered_in_window(
                            allocations=allocations,
                            station_id=station.id,
                            fuel_type=fuel,
                            from_tick=previous_tick,
                            to_tick=current_tick,
                        )

                        anomaly = self._inventory_detector.detect(
                            entity_id=station.id,
                            fuel_type=fuel,
                            previous_inventory=previous,
                            current_inventory=amount,
                            expected_consumption=consumption,
                            expected_deliveries=expected_deliveries,
                        )
                        if anomaly is not None:
                            anomalies.append(anomaly)

                self._previous_inventory[key] = amount
                self._previous_inventory_tick[key] = current_tick

        return anomalies

    def _observed_consumption(
        self,
        *,
        history: list[HistoryPoint],
        station_id: str,
        fuel_type: str,
        from_tick: int | None,
        to_tick: int,
    ) -> tuple[float, bool]:
        """The fuel actually drawn from the tank over a window of ticks.

        The window is ``[from_tick, to_tick)``. A tank reading taken at tick
        T is the tank as it stood *before* tick T was served, so the fuel
        drawn between two readings is what was served over the ticks in
        between them — the later reading excluded.

        It is the **served** litres, not the demand. A station that could
        not meet its demand did not draw that fuel from the tank, so
        charging the tank for it would report a loss that never happened.

        Returns the litres drawn and whether the window is fully covered by
        the stored history.
        """
        if from_tick is None:
            return 0.0, False

        drawn = 0.0
        covered_ticks = 0
        for point in history:
            if point.station_id != station_id or point.fuel_type != fuel_type:
                continue
            if not from_tick <= point.tick < to_tick:
                continue
            drawn += point.served_liters
            covered_ticks += 1

        window_is_covered = covered_ticks == to_tick - from_tick
        return drawn, window_is_covered

    def _delivered_in_window(
        self,
        *,
        allocations: list[Allocation],
        station_id: str,
        fuel_type: str,
        from_tick: int | None,
        to_tick: int,
    ) -> float:
        """How much fuel arrived at a station inside a window of ticks.

        An allocation that arrived in the window explains the inventory
        rise, so it must not be reported as an unexplained change.

        The window is ``[from_tick, to_tick)``, matching the reading of the
        tank: a delivery that arrives on the later tick is already in the
        later reading, not in the change between the two.
        """
        if from_tick is None:
            return 0.0

        delivered = 0.0
        for allocation in allocations:
            if allocation.destination_station_id != station_id:
                continue
            if allocation.fuel_type.value != fuel_type:
                continue
            if allocation.status.value != "ARRIVED":
                continue
            arrival_tick = allocation.actual_arrival_tick
            if arrival_tick is None:
                continue
            if from_tick <= arrival_tick < to_tick:
                delivered += allocation.quantity

        return delivered


def risk_summary(risks: list[RiskAssessment]) -> dict[str, int]:
    """Count the assessments in each risk band."""
    summary = {level.value: 0 for level in RiskLevel}
    for risk in risks:
        summary[risk.risk_level.value] += 1
    return summary


def anomaly_summary(anomalies: list[Anomaly]) -> dict[str, int]:
    """Count the anomalies of each kind."""
    summary = {kind.value: 0 for kind in AnomalyKind}
    for anomaly in anomalies:
        summary[anomaly.kind.value] += 1
    return summary
