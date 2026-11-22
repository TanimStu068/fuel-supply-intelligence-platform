"""The recommendation model: what the engine proposes, and why.

A recommendation is not a number — it is an argument. Every one of them
carries the signals that drove it, the constraints that were considered,
the projected effect on the station's risk, a confidence, and the
alternatives that were ranked and rejected.

The lifecycle is deliberately human-first:

``PROPOSED`` → ``APPROVED`` → ``SUBMITTED`` → (``ARRIVED`` | ``FAILED``)
             ↘ ``REJECTED``

Nothing is sent to the simulator until a human approves it (or the
auto-mode — which is **off** by default — approves a strictly limited
class of low-risk, high-confidence action).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum


class RecommendationStatus(StrEnum):
    """Where a recommendation is in its lifecycle."""

    PROPOSED = "PROPOSED"
    APPROVED = "APPROVED"
    SUBMITTED = "SUBMITTED"
    ARRIVED = "ARRIVED"
    FAILED = "FAILED"
    REJECTED = "REJECTED"
    STALE = "STALE"

    @property
    def is_terminal(self) -> bool:
        """True when no further action will change the outcome."""
        return self in {
            RecommendationStatus.ARRIVED,
            RecommendationStatus.FAILED,
            RecommendationStatus.REJECTED,
        }


@dataclass(slots=True)
class RiskProjection:
    """The station's risk before and after a proposed allocation.

    The "after" is a **local projection**: the engine replays the forecast
    against the fuel the shipment would deliver, at the tick it would
    arrive. It never asks the simulator what would happen.
    """

    risk_level_before: str
    risk_level_after: str
    hours_to_stockout_before: float | None
    hours_to_stockout_after: float | None
    stockout_probability_before: float
    stockout_probability_after: float
    inventory_before: float
    inventory_after: float
    available_before: float
    available_after: float
    #: The tick the fuel would arrive at.
    arrival_tick: int
    #: Simulated hours until arrival.
    hours_until_arrival: float

    def to_payload(self) -> dict[str, object]:
        return {
            "risk_level_before": self.risk_level_before,
            "risk_level_after": self.risk_level_after,
            "hours_to_stockout_before": self.hours_to_stockout_before,
            "hours_to_stockout_after": self.hours_to_stockout_after,
            "stockout_probability_before": round(self.stockout_probability_before, 4),
            "stockout_probability_after": round(self.stockout_probability_after, 4),
            "inventory_before": round(self.inventory_before, 1),
            "inventory_after": round(self.inventory_after, 1),
            "available_before": round(self.available_before, 1),
            "available_after": round(self.available_after, 1),
            "arrival_tick": self.arrival_tick,
            "hours_until_arrival": round(self.hours_until_arrival, 2),
        }


@dataclass(slots=True)
class Alternative:
    """A route the engine considered but did not recommend."""

    route_id: str
    source_depot_id: str
    transit_ticks: int
    quantity: float
    #: Why this one lost, in one line.
    reason: str
    #: The projected cover this alternative would have delivered.
    hours_to_stockout_after: float | None = None
    #: The score the alternative achieved, for ranking.
    score: float = 0.0

    def to_payload(self) -> dict[str, object]:
        return {
            "route_id": self.route_id,
            "source_depot_id": self.source_depot_id,
            "transit_ticks": self.transit_ticks,
            "quantity": round(self.quantity, 1),
            "reason": self.reason,
            "hours_to_stockout_after": (
                round(self.hours_to_stockout_after, 2)
                if self.hours_to_stockout_after is not None
                else None
            ),
            "score": round(self.score, 4),
        }


@dataclass(slots=True)
class Recommendation:
    """One proposed allocation, with everything needed to judge it."""

    #: A stable identifier for this recommendation.
    id: str
    station_id: str
    fuel_type: str
    source_depot_id: str
    route_id: str
    quantity: float
    status: RecommendationStatus = RecommendationStatus.PROPOSED

    #: The risk that prompted the recommendation.
    risk_level: str = "OK"
    hours_to_stockout: float | None = None
    stockout_probability: float = 0.0

    #: The projected effect of this allocation.
    projection: RiskProjection | None = None

    #: The signals from the intelligence layer that drove the decision.
    signals: list[dict[str, object]] = field(default_factory=list)
    #: The constraint checks that were made, and whether they passed.
    constraints: list[dict[str, object]] = field(default_factory=list)
    #: The alternatives that were considered and ranked.
    alternatives: list[Alternative] = field(default_factory=list)

    #: How confident the engine is, 0..1.
    confidence: float = 0.0
    #: True when a human must look at this before it is sent.
    needs_human_review: bool = True
    #: Whether the policy that produced this was the primary one or the
    #: fallback used when the intelligence layer was unavailable.
    used_fallback_policy: bool = False

    #: The idempotency key that will be used to submit this allocation.
    idempotency_key: str = ""
    #: One line explaining the recommendation.
    rationale: str = ""

    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    #: When a human approved or rejected it, and who.
    decided_at: datetime | None = None
    decided_by: str | None = None
    #: The simulator's response to the submission.
    simulator_response: dict[str, object] | None = None
    #: The simulator's error code, when it rejected the allocation.
    error_code: str | None = None
    error_message: str | None = None
    #: The simulator's allocation id, once it has one.
    allocation_id: int | None = None
    #: The tick the recommendation was made against.
    sim_tick: int = 0

    def to_payload(self) -> dict[str, object]:
        return {
            "id": self.id,
            "station_id": self.station_id,
            "fuel_type": self.fuel_type,
            "source_depot_id": self.source_depot_id,
            "route_id": self.route_id,
            "quantity": round(self.quantity, 1),
            "status": self.status.value,
            "risk_level": self.risk_level,
            "hours_to_stockout": (
                round(self.hours_to_stockout, 2) if self.hours_to_stockout is not None else None
            ),
            "stockout_probability": round(self.stockout_probability, 4),
            "projection": self.projection.to_payload() if self.projection else None,
            "signals": self.signals,
            "constraints": self.constraints,
            "alternatives": [alternative.to_payload() for alternative in self.alternatives],
            "confidence": round(self.confidence, 4),
            "needs_human_review": self.needs_human_review,
            "used_fallback_policy": self.used_fallback_policy,
            "idempotency_key": self.idempotency_key,
            "rationale": self.rationale,
            "created_at": self.created_at.isoformat(),
            "decided_at": self.decided_at.isoformat() if self.decided_at else None,
            "decided_by": self.decided_by,
            "simulator_response": self.simulator_response,
            "error_code": self.error_code,
            "error_message": self.error_message,
            "allocation_id": self.allocation_id,
            "sim_tick": self.sim_tick,
        }
