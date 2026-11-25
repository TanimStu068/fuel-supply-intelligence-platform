"""The decision service: propose, simulate, approve, submit, reconcile.

This is the human-in-the-loop boundary. The engine's output is a
*proposal*; nothing reaches the simulator until a human approves it.

The service owns:

* the **recommendation store** — every proposal, its status, who decided
  it, and what the simulator said;
* the **what-if simulation** — a local projection that never touches the
  simulator;
* the **approval flow** — approve, reject, and submit to the simulator
  with a deterministic idempotency key;
* the **reconciliation** — matching a submitted allocation to its later
  ``ARRIVED`` / ``FAILED`` status;
* the **auto-mode** — off by default, and even when on it only approves a
  narrow class of low-risk, high-confidence action.
"""

from __future__ import annotations

import json
import logging
import time
from collections import deque
from datetime import UTC, datetime
from typing import Any

from ..simulator_client import SimulatorClient
from ..simulator_errors import (
    CANNOT_CANCEL,
    DEPOT_CLOSED,
    DESTINATION_CAPACITY_EXCEEDED,
    DISPATCH_CAPACITY_EXCEEDED,
    INSUFFICIENT_INVENTORY,
    ROUTE_CAPACITY_EXCEEDED,
    ROUTE_DISRUPTED,
    ROUTE_MISMATCH,
    STATION_CLOSED,
    ConflictError,
    SimulatorAPIError,
    SimulatorConnectionError,
)
from .fallback import FallbackPolicy
from .models import Recommendation, RecommendationStatus
from .optimizer import AllocationOptimizer
from .validator import AllocationValidator

logger = logging.getLogger(__name__)

#: What each 409 code means for the operator, and what to do about it.
CONFLICT_EXPLANATIONS: dict[str, str] = {
    ROUTE_MISMATCH: (
        "The route does not connect that depot and station. The world changed "
        "since the proposal was made."
    ),
    DEPOT_CLOSED: (
        "The depot is closed and cannot ship. Wait for the depot constraint to "
        "resolve, or use another depot."
    ),
    STATION_CLOSED: ("The station is closed. It cannot receive fuel until the outage resolves."),
    ROUTE_DISRUPTED: (
        "The route is disrupted. Use an alternative route, or wait for the disruption to clear."
    ),
    ROUTE_CAPACITY_EXCEEDED: (
        "The shipment is larger than the route allows. Split it into smaller shipments."
    ),
    INSUFFICIENT_INVENTORY: (
        "The depot no longer holds that much fuel. Wait for the next supply arrival, or ship less."
    ),
    DISPATCH_CAPACITY_EXCEEDED: (
        "The depot's dispatch allowance for this tick is used up. Try again on the next tick."
    ),
    DESTINATION_CAPACITY_EXCEEDED: (
        "The station's tank is full. Wait for the station to draw fuel down."
    ),
    CANNOT_CANCEL: "The allocation is no longer pending and cannot be cancelled.",
}


class DecisionService:
    """Owns the recommendations and the human approval flow."""

    def __init__(
        self,
        client: SimulatorClient,
        *,
        optimizer: AllocationOptimizer | None = None,
        fallback: FallbackPolicy | None = None,
        validator: AllocationValidator | None = None,
        repository: Any | None = None,
        database: Any | None = None,
        history_limit: int = 500,
    ) -> None:
        self._client = client
        self._optimizer = optimizer or AllocationOptimizer()
        self._fallback = fallback or FallbackPolicy()
        self._validator = validator or AllocationValidator()
        self._repository = repository
        self._database = database
        #: How many recommendations the in-memory store keeps.
        #:
        #: The store is a **working set**, not the audit trail — the audit
        #: trail is Postgres. Without a bound it grows for the life of the
        #: process: the background sync proposes on every cycle, and each
        #: proposal carries a full projection, its signals, its
        #: constraints and its alternatives. Left alone it reached three
        #: thousand recommendations and twenty-two megabytes, and every
        #: call to ``/api/recommendations`` serialised all of it — an
        #: endpoint the dashboard polls every five seconds.
        #:
        #: A limit of zero means **unbounded**, which is how the "before"
        #: side of the load test's second bottleneck is reproduced.
        self._history_limit = max(0, history_limit)

        #: The live recommendations, keyed by their id.
        self._recommendations: dict[str, Recommendation] = {}
        #: Whether the auto-approve mode is on. **Off by default.**
        self._auto_mode = False
        #: The counters the metrics are built from.
        self._counters: dict[str, int] = {
            "proposed": 0,
            "approved": 0,
            "rejected": 0,
            "submitted": 0,
            "auto_approved": 0,
            "failed": 0,
            "conflicts": 0,
        }
        #: The 409 codes the simulator returned, by code.
        self._conflicts_by_code: dict[str, int] = {}
        #: Why the fallback policy is in use **right now**, if it is. This is
        #: sticky: it survives until the normal policy comes back, so the
        #: operator can see that the platform is degraded rather than
        #: guessing from a missing recommendation.
        self._fallback_reason: str | None = None
        #: Whether the current fallback engagement has been counted. A
        #: persistent outage is one activation, not one per poll cycle.
        self._fallback_counted = False
        #: The timestamps of the recent proposals, for the per-minute rate.
        self._proposal_times: deque[float] = deque(maxlen=600)

    # -- accessors -----------------------------------------------------------

    @property
    def optimizer(self) -> AllocationOptimizer:
        """The allocation optimizer."""
        return self._optimizer

    @property
    def fallback(self) -> FallbackPolicy:
        """The rule-based fallback policy."""
        return self._fallback

    @property
    def auto_mode(self) -> bool:
        """Whether recommendations are auto-approved. Off by default."""
        return self._auto_mode

    def set_auto_mode(self, enabled: bool) -> bool:
        """Turn the auto-approve mode on or off."""
        self._auto_mode = enabled
        logger.info("decision auto-mode %s", "enabled" if enabled else "disabled")
        return self._auto_mode

    @property
    def fallback_reason(self) -> str | None:
        """Why the fallback policy is in use right now, if it is."""
        return self._fallback_reason

    def set_fallback_reason(self, reason: str | None) -> None:
        """Record why the fallback policy is in use.

        The activation counter is keyed on the *engagement*, not on the
        reason text: an outage is one activation however many different
        words the failure produces, and however many poll cycles it spans.
        Setting ``None`` re-arms it, so the next outage counts again.
        """
        self._fallback_reason = reason
        if reason is None:
            self._fallback_counted = False

    def consume_fallback_activation(self) -> str | None:
        """The fallback reason, if this engagement has not been counted yet.

        The caller turns this into the ``decision_fallback_activations``
        metric. Returning it only once per engagement is what makes the
        metric count *activations* rather than poll cycles.
        """
        if self._fallback_reason is None or self._fallback_counted:
            return None
        self._fallback_counted = True
        return self._fallback_reason

    @property
    def counters(self) -> dict[str, int]:
        """The decision counters."""
        return dict(self._counters)

    @property
    def decisions_per_minute(self) -> float:
        """How many recommendations were proposed in the last minute.

        This is a rolling count over the last 60 seconds, not a lifetime
        average, so it shows the platform's current decision rate rather
        than its historical one.
        """
        now = time.monotonic()
        recent = [stamp for stamp in self._proposal_times if now - stamp <= 60.0]
        return float(len(recent))

    @property
    def conflicts_by_code(self) -> dict[str, int]:
        """The 409 codes the simulator returned, by code."""
        return dict(self._conflicts_by_code)

    @property
    def recommendation_count(self) -> int:
        """How many recommendations the working set holds."""
        return len(self._recommendations)

    @property
    def page_limit(self) -> int | None:
        """How many recommendations one page holds, or ``None`` for no cap.

        The page is smaller than the working set: the dashboard draws a
        dozen cards at a time, and the rest are one click away. When the
        working set is unbounded — the "before" side of the load test's
        second bottleneck — there is no cap either, which is what makes
        that state reproducible rather than remembered.
        """
        if not self._history_limit:
            return None
        return min(100, self._history_limit)

    def list_recommendations(
        self,
        *,
        status: str | None = None,
        station_id: str | None = None,
        limit: int | None = None,
    ) -> list[Recommendation]:
        """The recommendations, newest first.

        ``limit`` caps how many are returned. The endpoint uses it because
        the dashboard polls this list every few seconds, and a page that
        grows without bound is a page that eventually stops being served.
        """
        rows = list(self._recommendations.values())
        if status:
            wanted = status.upper()
            rows = [row for row in rows if row.status.value == wanted]
        if station_id:
            rows = [row for row in rows if row.station_id == station_id]
        rows.sort(key=lambda row: row.created_at, reverse=True)
        if limit is not None:
            rows = rows[: max(1, limit)]
        return rows

    def get(self, recommendation_id: str) -> Recommendation | None:
        """One recommendation by id."""
        return self._recommendations.get(recommendation_id)

    # -- proposing -----------------------------------------------------------

    async def propose(
        self,
        recommendations: list[Recommendation],
        *,
        fallback: bool = False,
        reason: str | None = None,
    ) -> list[Recommendation]:
        """Record the proposals from the optimizer.

        ``fallback=True`` marks the proposals as having come from the
        rule-based policy, and ``reason`` says why. The reason is kept
        until the normal policy comes back, so the operator can see *why*
        the platform fell back rather than only that it did.
        """
        if fallback:
            self.set_fallback_reason(reason or "the intelligence layer is unavailable")
        else:
            self.set_fallback_reason(None)

        for recommendation in recommendations:
            self._recommendations[recommendation.id] = recommendation
            self._counters["proposed"] += 1
            self._proposal_times.append(time.monotonic())
            if fallback:
                self._counters["fallback_recommendations"] = (
                    self._counters.get("fallback_recommendations", 0) + 1
                )

        self._evict_old_recommendations()

        if self._auto_mode:
            await self._auto_approve(recommendations)

        return recommendations

    def reset(self) -> int:
        """Clear the working set, and return how many recommendations went.

        The platform's decisions are cleared so a demo — or a drill — starts
        from a known state.

        This clears the **in-memory working set** as well as the counters.
        The in-memory set is the one that matters for the reset: it is what
        ``/api/recommendations`` serves and what an approval looks the
        recommendation up in. Clearing only Postgres would leave the
        previous run's proposals in memory, and every one of them would
        fail to approve — they were planned for a world that no longer
        exists, and the simulator would refuse them with *"the depot's
        dispatch allowance for this tick is used up"* or *"the station's
        tank is full"*. The audit trail is deliberately **not** touched:
        Postgres keeps the history, and ``/api/decision-history`` serves it.
        """
        cleared = len(self._recommendations)
        self._recommendations.clear()
        self._proposal_times.clear()
        self._conflicts_by_code.clear()
        for name in self._counters:
            self._counters[name] = 0
        self.set_fallback_reason(None)
        logger.info("the decision working set was cleared", extra={"cleared": cleared})
        return cleared

    def _evict_old_recommendations(self) -> None:
        """Keep the in-memory store within its bound.

        The store is a **working set**: the dashboard polls it, and the
        decision engine reads it to reconcile allocations. It is not the
        audit trail — Postgres keeps everything, and
        ``/api/decision-history`` serves the full list.

        What is dropped is chosen in two passes:

        1. **Terminal** recommendations first — one that has ARRIVED,
           FAILED or been REJECTED is finished, and nothing can act on it.
        2. Then the **oldest** of what is left, if the store is still
           over its limit.

        The second pass is what makes the bound a bound. A proposal that
        nobody acted on is still a proposal, and it is still in the store
        — but a proposal made forty ticks ago is about a world that has
        moved on, and the operator's page shows the current ones. Evicting
        the oldest first keeps the actionable ones: the most recent
        proposals are the last to go.
        """
        if not self._history_limit:
            return
        if len(self._recommendations) <= self._history_limit:
            return

        ordered = sorted(self._recommendations.values(), key=lambda row: row.created_at)

        for recommendation in ordered:
            if len(self._recommendations) <= self._history_limit:
                return
            if recommendation.status.is_terminal:
                self._recommendations.pop(recommendation.id, None)

        for recommendation in ordered:
            if len(self._recommendations) <= self._history_limit:
                return
            self._recommendations.pop(recommendation.id, None)

    async def _auto_approve(self, recommendations: list[Recommendation]) -> None:
        """Approve the narrow class of action the auto-mode allows.

        The bar is deliberately high: a WATCH-level station, a confident
        forecast, no human-review flag, and a route that is not already
        disrupted. Anything CRITICAL, anything uncertain, and anything
        that used the fallback policy is left for a human — a critical
        station is exactly the case where a human should look.
        """
        for recommendation in recommendations:
            if not self._auto_eligible(recommendation):
                continue
            self._counters["auto_approved"] += 1
            await self.approve(recommendation.id, decided_by="auto-mode", submit=True)

    def _auto_eligible(self, recommendation: Recommendation) -> bool:
        """Whether the auto-mode is allowed to approve this recommendation."""
        if recommendation.used_fallback_policy:
            return False
        if recommendation.needs_human_review:
            return False
        if recommendation.risk_level != "WATCH":
            return False
        if recommendation.confidence < 0.85:
            return False
        # Every constraint must have passed.
        return all(check.get("satisfied") for check in recommendation.constraints)

    # -- approving and rejecting ---------------------------------------------

    async def approve(
        self, recommendation_id: str, *, decided_by: str, submit: bool = True
    ) -> Recommendation:
        """Approve a recommendation, and optionally submit it.

        The submission uses the recommendation's deterministic idempotency
        key, so a retried approval is a **replay** rather than a second
        shipment — the simulator returns the original allocation.
        """
        recommendation = self._require(recommendation_id)

        if recommendation.status is not RecommendationStatus.PROPOSED:
            raise DecisionError(
                f"recommendation {recommendation_id} is {recommendation.status.value}, not PROPOSED"
            )

        recommendation.status = RecommendationStatus.APPROVED
        recommendation.decided_at = datetime.now(UTC)
        recommendation.decided_by = decided_by
        self._counters["approved"] += 1

        if submit:
            await self._submit(recommendation)
        return recommendation

    def reject(
        self, recommendation_id: str, *, decided_by: str, reason: str = ""
    ) -> Recommendation:
        """Reject a recommendation. Nothing is sent to the simulator."""
        recommendation = self._require(recommendation_id)

        if recommendation.status is not RecommendationStatus.PROPOSED:
            raise DecisionError(
                f"recommendation {recommendation_id} is {recommendation.status.value}, not PROPOSED"
            )

        recommendation.status = RecommendationStatus.REJECTED
        recommendation.decided_at = datetime.now(UTC)
        recommendation.decided_by = decided_by
        recommendation.error_message = reason or "rejected by the operator"
        self._counters["rejected"] += 1
        return recommendation

    def _require(self, recommendation_id: str) -> Recommendation:
        """Fetch a recommendation or fail loudly."""
        recommendation = self._recommendations.get(recommendation_id)
        if recommendation is None:
            raise DecisionError(f"no recommendation with id {recommendation_id}")
        return recommendation

    # -- submitting ----------------------------------------------------------

    async def _submit(self, recommendation: Recommendation) -> None:
        """Submit an approved recommendation to the simulator.

        Every documented 409 is handled and explained. The recommendation
        records the code and the explanation so the operator can see why
        the shipment did not go, and the counter is incremented so the
        failure mode is visible in the metrics.
        """
        body = {
            "idempotency_key": recommendation.idempotency_key,
            "source_depot_id": recommendation.source_depot_id,
            "destination_station_id": recommendation.station_id,
            "route_id": recommendation.route_id,
            "fuel_type": recommendation.fuel_type,
            "quantity": recommendation.quantity,
        }

        try:
            allocation = await self._client.create_allocation(body)
        except ConflictError as exc:
            recommendation.status = RecommendationStatus.FAILED
            recommendation.error_code = exc.code
            recommendation.error_message = CONFLICT_EXPLANATIONS.get(exc.code or "", exc.message)
            self._counters["conflicts"] += 1
            if exc.code:
                self._conflicts_by_code[exc.code] = self._conflicts_by_code.get(exc.code, 0) + 1
            logger.warning(
                "allocation rejected by the simulator",
                extra={
                    "recommendation_id": recommendation.id,
                    "error_code": exc.code,
                    "station_id": recommendation.station_id,
                },
            )
            return
        except SimulatorConnectionError as exc:
            # The simulator could not be reached. The recommendation stays
            # APPROVED so the operator can retry: the idempotency key
            # makes the retry safe.
            recommendation.status = RecommendationStatus.APPROVED
            recommendation.error_code = "SIMULATOR_UNREACHABLE"
            recommendation.error_message = (
                f"The simulator could not be reached: {exc}. Retry the approval — "
                f"the idempotency key makes a repeat safe."
            )
            self._counters["failed"] += 1
            logger.warning("could not submit allocation: %s", exc)
            return
        except SimulatorAPIError as exc:
            recommendation.status = RecommendationStatus.FAILED
            recommendation.error_code = exc.code
            recommendation.error_message = exc.message
            self._counters["failed"] += 1
            logger.warning("allocation failed: %s", exc)
            return
        except Exception as exc:
            recommendation.status = RecommendationStatus.APPROVED
            recommendation.error_code = "UNKNOWN_ERROR"
            recommendation.error_message = f"Unexpected error: {exc}"
            self._counters["failed"] += 1
            logger.warning("allocation submission failed: %s", exc)
            return

        recommendation.status = RecommendationStatus.SUBMITTED
        recommendation.allocation_id = allocation.id
        recommendation.simulator_response = allocation.model_dump(mode="json")
        recommendation.error_code = None
        recommendation.error_message = None
        self._counters["submitted"] += 1

        logger.info(
            "allocation submitted",
            extra={
                "recommendation_id": recommendation.id,
                "allocation_id": allocation.id,
                "station_id": recommendation.station_id,
                "quantity": recommendation.quantity,
            },
        )

    # -- what-if simulation --------------------------------------------------

    def simulate(self, recommendation_id: str) -> dict[str, Any]:
        """Project what would happen if this recommendation were approved.

        This is a **local** projection: it replays the forecast against
        the fuel the shipment would deliver. Nothing is written to the
        simulator, and no recommendation status changes.
        """
        recommendation = self._require(recommendation_id)
        projection = recommendation.projection

        if projection is None:
            return {
                "recommendation_id": recommendation.id,
                "simulated": False,
                "reason": "this recommendation has no projection to simulate",
            }

        before = {
            "risk_level": projection.risk_level_before,
            "hours_to_stockout": projection.hours_to_stockout_before,
            "stockout_probability": projection.stockout_probability_before,
            "available_liters": projection.available_before,
        }
        after = {
            "risk_level": projection.risk_level_after,
            "hours_to_stockout": projection.hours_to_stockout_after,
            "stockout_probability": projection.stockout_probability_after,
            "available_liters": projection.available_after,
        }

        return {
            "recommendation_id": recommendation.id,
            "simulated": True,
            "writes_to_simulator": False,
            "station_id": recommendation.station_id,
            "fuel_type": recommendation.fuel_type,
            "quantity": recommendation.quantity,
            "route_id": recommendation.route_id,
            "arrival_tick": projection.arrival_tick,
            "hours_until_arrival": projection.hours_until_arrival,
            "before": before,
            "after": after,
            "improvement": {
                "risk_level_changed": before["risk_level"] != after["risk_level"],
                "hours_to_stockout_gained": (
                    round(
                        (after["hours_to_stockout"] or 48.0) - (before["hours_to_stockout"] or 0.0),
                        2,
                    )
                    if before["hours_to_stockout"] is not None
                    else None
                ),
                "stockout_probability_reduction": round(
                    before["stockout_probability"] - after["stockout_probability"], 4
                ),
            },
            "constraints": recommendation.constraints,
            "alternatives": [
                alternative.to_payload() for alternative in recommendation.alternatives
            ],
        }

    # -- reconciliation ------------------------------------------------------

    def reconcile(self, allocations: list[Any]) -> int:
        """Match submitted allocations to their later status.

        The simulator is the source of truth for what happened to a
        shipment. This walks the simulator's ledger and closes the loop on
        the recommendations that are still open.
        """
        by_id = {
            allocation.id: allocation
            for allocation in allocations
            if getattr(allocation, "id", None) is not None
        }

        reconciled = 0
        for recommendation in self._recommendations.values():
            if recommendation.status not in {
                RecommendationStatus.SUBMITTED,
                RecommendationStatus.APPROVED,
            }:
                continue
            if recommendation.allocation_id is None:
                continue

            allocation = by_id.get(recommendation.allocation_id)
            if allocation is None:
                continue

            status = getattr(allocation.status, "value", str(allocation.status))
            if status == "ARRIVED":
                recommendation.status = RecommendationStatus.ARRIVED
                recommendation.simulator_response = allocation.model_dump(mode="json")
                reconciled += 1
            elif status in {"FAILED", "CANCELLED"}:
                recommendation.status = RecommendationStatus.FAILED
                recommendation.error_code = status
                reason = getattr(allocation, "failure_reason", None)
                recommendation.error_message = reason or f"the shipment was {status.lower()}"
                reconciled += 1

        return reconciled

    # -- persistence ---------------------------------------------------------

    async def persist(self, recommendation: Recommendation) -> None:
        """Write one recommendation to the audit trail."""
        if self._repository is None:
            return
        try:
            await self._repository.write_recommendation(
                recommendation=recommendation,
                simulator_response=json.dumps(recommendation.simulator_response or {}, default=str),
            )
        except Exception as exc:
            logger.warning("could not persist a recommendation: %s", exc)

    async def persist_all(self) -> None:
        """Write every recommendation to the audit trail."""
        for recommendation in self._recommendations.values():
            await self.persist(recommendation)


class DecisionError(Exception):
    """Something an operator did wrong, or a recommendation is not there."""
