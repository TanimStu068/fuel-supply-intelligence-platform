"""Shortage and stockout risk.

For every (station, fuel) pair the engine answers three questions:

1. **How long until this station runs dry?** ``hours_to_stockout`` — the
   simulated hours until the forecast demand exceeds the stock on hand plus
   the fuel already in transit.
2. **How likely is a stockout inside the horizon?** ``stockout_probability``
   — derived from the forecast's confidence interval: if the lower bound of
   the cumulative demand exceeds the available stock, a stockout is certain.
3. **What level of risk is that?** ``OK`` / ``WATCH`` / ``CRITICAL``.

The score is explainable: every risk entry carries the signals that drove
it, so an operator sees *why* a station is red rather than a bare number.

The available stock is deliberately generous to the station, not the depot:
it counts what is on hand **plus** what is already in transit to that
station, because an allocation that has departed will arrive.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from .forecasting import Forecast


class RiskLevel(StrEnum):
    """The risk band a station/fuel pair sits in."""

    OK = "OK"
    WATCH = "WATCH"
    CRITICAL = "CRITICAL"


@dataclass(slots=True)
class StockSignal:
    """One input to a risk score, with its value and a plain reading."""

    name: str
    value: float
    detail: str

    def to_payload(self) -> dict[str, object]:
        return {
            "name": self.name,
            "value": round(self.value, 3),
            "detail": self.detail,
        }


@dataclass(slots=True)
class RiskAssessment:
    """The risk assessment for one (station, fuel) pair."""

    station_id: str
    fuel_type: str
    risk_level: RiskLevel
    #: Simulated hours until the station runs out. ``None`` when it will not.
    hours_to_stockout: float | None
    #: Probability of a stockout inside the forecast horizon, 0..1.
    stockout_probability: float
    #: 0..1 — how severe the risk is, independent of the band.
    risk_score: float
    #: The forecast the assessment is based on.
    forecast: Forecast
    #: The signals that drove the score.
    signals: list[StockSignal] = field(default_factory=list)
    #: True when a human should confirm before acting.
    needs_human_review: bool = False
    #: The recommendation, in one line.
    recommendation: str = ""
    #: When the assessment was made.
    assessed_at: datetime | None = None

    def to_payload(self) -> dict[str, object]:
        """Render the assessment as a JSON-safe dictionary."""
        return {
            "station_id": self.station_id,
            "fuel_type": self.fuel_type,
            "risk_level": self.risk_level.value,
            "hours_to_stockout": (
                round(self.hours_to_stockout, 2) if self.hours_to_stockout is not None else None
            ),
            "stockout_probability": round(self.stockout_probability, 4),
            "risk_score": round(self.risk_score, 4),
            "needs_human_review": self.needs_human_review,
            "recommendation": self.recommendation,
            "model": self.forecast.model.value,
            "confidence": round(self.forecast.confidence, 4),
            "forecast_total_liters": round(self.forecast.total_demand, 3),
            "forecast_upper_liters": round(self.forecast.upper_total, 3),
            "signals": [signal.to_payload() for signal in self.signals],
        }


@dataclass(slots=True)
class InTransitShipment:
    """An allocation that is on its way to a station."""

    allocation_id: int
    #: The station the fuel is destined for. Fuel in transit to *another*
    #: station is not cover for this one.
    destination_station_id: str
    fuel_type: str
    quantity: float
    expected_arrival_tick: int | None
    route_id: str


class RiskEngine:
    """Turns forecasts and stock levels into risk assessments.

    The thresholds are deliberately conservative: a station is only CRITICAL
    when it is genuinely about to run dry, not merely busy.
    """

    #: Hours of cover below which a station is CRITICAL.
    CRITICAL_HOURS = 8.0
    #: Hours of cover below which a station is WATCH.
    WATCH_HOURS = 24.0
    #: Stockout probability above which a station is CRITICAL.
    CRITICAL_PROBABILITY = 0.6
    #: Stockout probability above which a station is WATCH.
    WATCH_PROBABILITY = 0.25

    def assess(
        self,
        *,
        station_id: str,
        fuel_type: str,
        inventory: float,
        capacity: float,
        forecast: Forecast,
        in_transit: list[InTransitShipment],
        tick_minutes: int = 15,
    ) -> RiskAssessment:
        """Assess the risk for one (station, fuel) pair."""
        tick_hours = tick_minutes / 60.0

        horizon_tick = forecast.points[-1].tick if forecast.points else None
        incoming = sum(
            shipment.quantity
            for shipment in in_transit
            if shipment.destination_station_id == station_id
            and shipment.fuel_type == fuel_type
            and shipment.expected_arrival_tick is not None
            and horizon_tick is not None
            and shipment.expected_arrival_tick <= horizon_tick
        )
        available = inventory + incoming

        cumulative: list[float] = []
        running = 0.0
        for point in forecast.points:
            running += point.predicted_demand
            cumulative.append(running)

        hours_to_stockout = self._hours_to_stockout(
            available=available,
            cumulative=cumulative,
            tick_hours=tick_hours,
        )
        probability = self._stockout_probability(
            available=available,
            cumulative=cumulative,
            forecast=forecast,
        )

        signals = self._build_signals(
            inventory=inventory,
            capacity=capacity,
            incoming=incoming,
            available=available,
            hours_to_stockout=hours_to_stockout,
            probability=probability,
            forecast=forecast,
        )

        score = self._risk_score(
            hours_to_stockout=hours_to_stockout,
            probability=probability,
            capacity_ratio=(inventory / capacity) if capacity > 0 else 0.0,
        )
        level = self._risk_level(hours_to_stockout, probability)

        needs_review = forecast.needs_human_review or forecast.confidence < 0.5

        return RiskAssessment(
            station_id=station_id,
            fuel_type=fuel_type,
            risk_level=level,
            hours_to_stockout=hours_to_stockout,
            stockout_probability=probability,
            risk_score=score,
            forecast=forecast,
            signals=signals,
            needs_human_review=needs_review,
            recommendation=self._recommendation(level, hours_to_stockout, fuel_type),
        )

    def _hours_to_stockout(
        self,
        *,
        available: float,
        cumulative: list[float],
        tick_hours: float,
    ) -> float | None:
        """Simulated hours until cumulative demand exhausts the stock.

        ``None`` when the stock survives the whole forecast horizon.
        """
        if available <= 0:
            return 0.0
        for index, consumed in enumerate(cumulative):
            if consumed >= available:
                # Interpolate within the tick for a smoother number.
                previous = cumulative[index - 1] if index > 0 else 0.0
                span = consumed - previous
                fraction = (available - previous) / span if span > 0 else 1.0
                return round((index + fraction) * tick_hours, 2)
        return None

    def _stockout_probability(
        self,
        *,
        available: float,
        cumulative: list[float],
        forecast: Forecast,
    ) -> float:
        """How likely a stockout is inside the forecast horizon.

        The forecast's confidence interval is the honest source of
        uncertainty: if even the *lower* bound of cumulative demand exceeds
        the stock, the stockout is certain; if even the *upper* bound stays
        below it, it cannot happen.
        """
        if not cumulative:
            return 0.0

        upper_cumulative = sum(point.upper_bound for point in forecast.points)
        lower_cumulative = sum(point.lower_bound for point in forecast.points)

        if lower_cumulative >= available:
            return 1.0
        if upper_cumulative <= available:
            return 0.0

        # Somewhere in between: scale with how far the upper bound overshoots.
        overshoot = upper_cumulative - available
        headroom = max(1e-6, upper_cumulative - lower_cumulative)
        return round(max(0.0, min(1.0, overshoot / headroom)), 4)

    def _risk_score(
        self,
        *,
        hours_to_stockout: float | None,
        probability: float,
        capacity_ratio: float,
    ) -> float:
        """Combine the signals into a single 0..1 severity score."""
        if hours_to_stockout is None:
            coverage_score = 0.0
        else:
            coverage_score = max(0.0, min(1.0, 1.0 - hours_to_stockout / 48.0))

        inventory_score = max(0.0, min(1.0, 1.0 - capacity_ratio))
        score = 0.5 * coverage_score + 0.35 * probability + 0.15 * inventory_score
        return round(max(0.0, min(1.0, score)), 4)

    def _risk_level(self, hours_to_stockout: float | None, probability: float) -> RiskLevel:
        """Band the score into OK / WATCH / CRITICAL.

        **Time to stockout decides CRITICAL, not probability.** A certain
        stockout that is sixteen hours away is not the same emergency as
        one that is two hours away, and a rule that cannot tell them apart
        produces alerts nobody can act on. So a station is CRITICAL only
        when it is genuinely about to run dry.

        Probability still separates OK from WATCH: it is what says whether
        the stock survives the forecast horizon at all.
        """
        if hours_to_stockout is None:
            # The stock survives the horizon. The probability says how
            # comfortably.
            if probability >= self.WATCH_PROBABILITY:
                return RiskLevel.WATCH
            return RiskLevel.OK

        if hours_to_stockout <= self.CRITICAL_HOURS:
            return RiskLevel.CRITICAL

        # It will run out, but not imminently. Worth watching.
        return RiskLevel.WATCH

    def _build_signals(
        self,
        *,
        inventory: float,
        capacity: float,
        incoming: float,
        available: float,
        hours_to_stockout: float | None,
        probability: float,
        forecast: Forecast,
    ) -> list[StockSignal]:
        """Collect the signals that explain the score."""
        signals = [
            StockSignal(
                name="inventory_liters",
                value=inventory,
                detail=f"{inventory:,.0f} L on hand",
            ),
            StockSignal(
                name="capacity_liters",
                value=capacity,
                detail=f"{capacity:,.0f} L capacity",
            ),
            StockSignal(
                name="incoming_liters",
                value=incoming,
                detail=f"{incoming:,.0f} L already in transit",
            ),
            StockSignal(
                name="available_liters",
                value=available,
                detail=f"{available:,.0f} L available (on hand + in transit)",
            ),
            StockSignal(
                name="forecast_demand_liters",
                value=forecast.total_demand,
                detail=(
                    f"{forecast.total_demand:,.0f} L forecast over "
                    f"{forecast.horizon_ticks} ticks by the {forecast.model.value} model"
                ),
            ),
            StockSignal(
                name="forecast_upper_liters",
                value=forecast.upper_total,
                detail=f"{forecast.upper_total:,.0f} L at the upper confidence bound",
            ),
            StockSignal(
                name="stockout_probability",
                value=probability,
                detail=f"{probability:.0%} chance of a stockout in the horizon",
            ),
        ]

        if hours_to_stockout is not None:
            signals.append(
                StockSignal(
                    name="hours_to_stockout",
                    value=hours_to_stockout,
                    detail=f"{hours_to_stockout:.1f} simulated hours of cover",
                )
            )
        else:
            signals.append(
                StockSignal(
                    name="hours_to_stockout",
                    value=0.0,
                    detail="stock survives the forecast horizon",
                )
            )

        return signals

    def _recommendation(
        self, level: RiskLevel, hours_to_stockout: float | None, fuel_type: str
    ) -> str:
        """One line of plain-English advice."""
        if level is RiskLevel.OK:
            return f"No action needed; {fuel_type.lower()} cover is comfortable."
        if level is RiskLevel.WATCH:
            if hours_to_stockout is None:
                return f"Monitor {fuel_type.lower()}; the forecast is close to the limit."
            return (
                f"Watch {fuel_type.lower()}: about {hours_to_stockout:.0f} simulated "
                f"hours of cover left."
            )
        if hours_to_stockout is None:
            return f"Dispatch {fuel_type.lower()} now; a stockout is likely in the horizon."
        return (
            f"Dispatch {fuel_type.lower()} now: about {hours_to_stockout:.0f} simulated "
            f"hours of cover left."
        )
