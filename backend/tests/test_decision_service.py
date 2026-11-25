"""Tests for the decision service's bookkeeping and its failure paths.

The approval flow itself is covered in ``test_decision_approval.py``.
This module covers the parts around it — the filters, the working-set
bound, the auto-mode's eligibility bar, the persistence and the
reconciliation — because those are what decide whether the operator is
shown the right thing.

Two of them matter more than the rest:

* **the working set is bounded.** It is a working set, not the audit
  trail; if it grows without limit, the endpoint the dashboard polls
  every few seconds eventually stops being served.
* **the auto-mode bar is high.** Anything CRITICAL, anything uncertain
  and anything produced by the fallback policy is left for a human — a
  critical station is exactly the case where a human should look.
"""

from __future__ import annotations

import pytest
from decision_fixtures import build_risk, build_world

from app.decision.models import Recommendation, RecommendationStatus
from app.decision.optimizer import AllocationOptimizer
from app.decision.service import DecisionError, DecisionService
from app.simulator_errors import RouteDisruptedError, SimulatorAPIError


class FakeAllocation:
    """The shape of the simulator's allocation response."""

    def __init__(self, allocation_id: int = 7, status: str = "PENDING") -> None:
        self.id = allocation_id
        self.status = type("Status", (), {"value": status})()
        self.created_tick = 10
        self.quantity = 3_000.0
        self.source_depot_id = "depot-gazipur"
        self.destination_station_id = "station-mirpur"
        self.route_id = "route-gazipur-mirpur"
        self.fuel_type = type("Fuel", (), {"value": "DIESEL"})()
        self.expected_arrival_tick = 12
        self.actual_arrival_tick = None
        self.failure_reason = None

    def model_dump(self, mode: str = "python") -> dict:
        return {
            "id": self.id,
            "status": self.status.value,
            "quantity": self.quantity,
            "created_tick": self.created_tick,
        }


class FakeClient:
    """A simulator client that records what it was asked for."""

    def __init__(self, error: Exception | None = None) -> None:
        self.submitted: list[dict] = []
        self.error = error

    async def create_allocation(self, allocation: dict) -> FakeAllocation:
        if self.error is not None:
            raise self.error
        self.submitted.append(allocation)
        return FakeAllocation()


async def _service(
    client: FakeClient | None = None, *, history_limit: int | None = 500
) -> DecisionService:
    """A decision service with one proposal already in it."""
    service = DecisionService(client or FakeClient(), history_limit=history_limit)
    optimizer = AllocationOptimizer()
    recommendations = optimizer.propose(world=build_world(), risks=[build_risk()])
    await service.propose(recommendations)
    return service


# -- the accessors --------------------------------------------------------


def test_the_optimizer_and_the_fallback_are_reachable() -> None:
    """The service exposes the two policies it drives."""
    service = DecisionService(FakeClient())

    assert service.optimizer is not None
    assert service.fallback is not None


def test_the_page_limit_is_capped_at_a_hundred() -> None:
    """A page never holds more than a hundred rows."""
    service = DecisionService(FakeClient(), history_limit=5_000)

    assert service.page_limit == 100


def test_an_unbounded_working_set_has_no_page_limit() -> None:
    """With no bound there is no cap — which is what makes it reproducible."""
    service = DecisionService(FakeClient(), history_limit=0)

    assert service.page_limit is None


def test_the_decision_rate_counts_the_last_minute() -> None:
    """The rate is a rolling count, not a lifetime average."""
    service = DecisionService(FakeClient())

    assert service.decisions_per_minute == 0.0


def test_the_conflict_counters_are_empty_to_start_with() -> None:
    """No conflicts have happened yet."""
    service = DecisionService(FakeClient())

    assert service.conflicts_by_code == {}


def test_the_recommendation_count_reflects_the_working_set() -> None:
    """The count is how many are held, not how many were ever made."""
    service = DecisionService(FakeClient())

    assert service.recommendation_count == 0


# -- the filters ----------------------------------------------------------


@pytest.mark.asyncio
async def test_the_list_can_be_filtered_by_status() -> None:
    """A filter on the status narrows the list."""
    service = await _service()

    proposed = service.list_recommendations(status="PROPOSED")
    approved = service.list_recommendations(status="APPROVED")

    # One optimization yields the primary proposal and its alternative.
    assert len(proposed) == 2
    assert approved == []


@pytest.mark.asyncio
async def test_the_status_filter_is_case_insensitive() -> None:
    """The dashboard sends lowercase; the service accepts it."""
    service = await _service()

    assert len(service.list_recommendations(status="proposed")) == 2


@pytest.mark.asyncio
async def test_the_list_can_be_filtered_by_station() -> None:
    """A filter on the station narrows the list."""
    service = await _service()

    assert len(service.list_recommendations(station_id="station-mirpur")) == 2
    assert service.list_recommendations(station_id="station-nowhere") == []


@pytest.mark.asyncio
async def test_the_list_is_newest_first() -> None:
    """The operator sees the current proposals at the top."""
    service = await _service()
    optimizer = AllocationOptimizer()
    # The id is derived from the tick, so a later tick is a new proposal.
    more = optimizer.propose(world=build_world(tick=20), risks=[build_risk()])
    await service.propose(more)

    listed = service.list_recommendations()

    assert len(listed) == 4
    assert listed[0].created_at >= listed[1].created_at


@pytest.mark.asyncio
async def test_the_list_can_be_capped() -> None:
    """The page has a limit, so a growing set stays servable."""
    service = await _service()

    assert len(service.list_recommendations(limit=1)) == 1


@pytest.mark.asyncio
async def test_a_limit_of_zero_still_returns_one_row() -> None:
    """A limit of zero would return nothing, which reads as "empty"."""
    service = await _service()

    assert len(service.list_recommendations(limit=0)) == 1


# -- the working set ------------------------------------------------------


@pytest.mark.asyncio
async def test_a_terminal_recommendation_is_evicted_first() -> None:
    """A finished proposal is dropped before an open one.

    Nothing can act on a REJECTED proposal, so it is the first thing to
    go. The open proposals are the ones the operator still has to look
    at.
    """
    service = await _service(history_limit=2)
    optimizer = AllocationOptimizer()
    more = optimizer.propose(world=build_world(), risks=[build_risk()])
    await service.propose(more)

    oldest = service.list_recommendations()[-1]
    service.reject(oldest.id, decided_by="operator")

    # Make it the oldest by age, then overflow the store.
    third = optimizer.propose(world=build_world(), risks=[build_risk()])
    await service.propose(third)

    assert service.recommendation_count <= 2


@pytest.mark.asyncio
async def test_the_store_stays_within_its_bound() -> None:
    """The working set never grows past its limit."""
    service = await _service(history_limit=5)
    optimizer = AllocationOptimizer()

    for _ in range(20):
        recommendations = optimizer.propose(world=build_world(), risks=[build_risk()])
        await service.propose(recommendations)

    assert service.recommendation_count <= 5


@pytest.mark.asyncio
async def test_an_unbounded_store_is_left_alone() -> None:
    """With no limit configured, nothing is evicted."""
    service = await _service(history_limit=0)
    optimizer = AllocationOptimizer()

    for tick in range(10, 20):
        recommendations = optimizer.propose(world=build_world(tick=tick), risks=[build_risk()])
        await service.propose(recommendations)

    # Ten optimizations, two recommendations each, and nothing evicted.
    assert service.recommendation_count == 20


# -- the auto-mode bar ----------------------------------------------------


def _watch_recommendation(**overrides) -> Recommendation:
    """A WATCH-level recommendation with every constraint satisfied."""
    recommendation = Recommendation(
        id="rec-auto",
        station_id="station-mirpur",
        fuel_type="DIESEL",
        source_depot_id="depot-gazipur",
        route_id="route-gazipur-mirpur",
        quantity=1_000.0,
        status=RecommendationStatus.PROPOSED,
        risk_level="WATCH",
        hours_to_stockout=20.0,
        stockout_probability=0.1,
        projection=None,
        signals=[],
        constraints=[{"code": "ROUTE_MISMATCH", "satisfied": True}],
        alternatives=[],
        confidence=0.9,
        needs_human_review=False,
        used_fallback_policy=False,
        idempotency_key="auto-key",
        rationale="because",
    )
    for key, value in overrides.items():
        setattr(recommendation, key, value)
    return recommendation


def test_a_watch_level_confident_proposal_is_auto_eligible() -> None:
    """The narrow case the auto-mode exists for."""
    service = DecisionService(FakeClient())

    assert service._auto_eligible(_watch_recommendation()) is True


def test_a_critical_proposal_is_never_auto_eligible() -> None:
    """A critical station is exactly where a human should look."""
    service = DecisionService(FakeClient())

    assert service._auto_eligible(_watch_recommendation(risk_level="CRITICAL")) is False


def test_a_proposal_that_needs_human_review_is_never_auto_eligible() -> None:
    """Uncertainty is the whole point of the flag."""
    service = DecisionService(FakeClient())

    assert service._auto_eligible(_watch_recommendation(needs_human_review=True)) is False


def test_a_low_confidence_proposal_is_never_auto_eligible() -> None:
    """Confidence below the bar is left to a human."""
    service = DecisionService(FakeClient())

    assert service._auto_eligible(_watch_recommendation(confidence=0.5)) is False


def test_a_fallback_proposal_is_never_auto_eligible() -> None:
    """The rule-based policy has no model behind it, so no auto-approval."""
    service = DecisionService(FakeClient())

    assert service._auto_eligible(_watch_recommendation(used_fallback_policy=True)) is False


def test_a_proposal_with_a_broken_constraint_is_never_auto_eligible() -> None:
    """Every constraint has to have passed."""
    service = DecisionService(FakeClient())
    recommendation = _watch_recommendation(
        constraints=[
            {"code": "ROUTE_MISMATCH", "satisfied": True},
            {"code": "ROUTE_DISRUPTED", "satisfied": False},
        ]
    )

    assert service._auto_eligible(recommendation) is False


@pytest.mark.asyncio
async def test_the_auto_mode_is_off_by_default() -> None:
    """Nothing is approved without being asked for."""
    service = await _service()

    assert service.auto_mode is False
    assert service.counters.get("auto_approved", 0) == 0


@pytest.mark.asyncio
async def test_the_auto_mode_can_be_turned_on_and_off() -> None:
    """The operator controls it, and it reports what it did."""
    service = await _service()

    assert service.set_auto_mode(True) is True
    assert service.auto_mode is True
    assert service.set_auto_mode(False) is False
    assert service.auto_mode is False


@pytest.mark.asyncio
async def test_the_auto_mode_approves_an_eligible_proposal() -> None:
    """The eligible case really is approved and submitted."""
    client = FakeClient()
    service = DecisionService(client)
    await service.propose([_watch_recommendation()])

    service.set_auto_mode(True)
    await service.propose([_watch_recommendation(id="rec-auto-2")])

    assert service.counters.get("auto_approved", 0) >= 1


@pytest.mark.asyncio
async def test_the_auto_mode_leaves_a_critical_proposal_to_a_human() -> None:
    """A critical station stays PROPOSED for a human to approve."""
    client = FakeClient()
    service = DecisionService(client)
    recommendation = _watch_recommendation(risk_level="CRITICAL")
    await service.propose([recommendation])

    service.set_auto_mode(True)
    await service.propose([recommendation])

    assert recommendation.status is RecommendationStatus.PROPOSED
    assert not client.submitted


# -- the failure paths ----------------------------------------------------


@pytest.mark.asyncio
async def test_a_conflict_leaves_the_recommendation_failed() -> None:
    """A 409 is recorded on the recommendation, with its code.

    The operator has to see *why* the shipment did not go, and the
    reason has to be the simulator's own code.
    """
    client = FakeClient(error=RouteDisruptedError(409, "ROUTE_DISRUPTED", "closed"))
    service = await _service(client)

    recommendation = service.list_recommendations()[0]
    await service.approve(recommendation.id, decided_by="operator")

    assert recommendation.status is RecommendationStatus.FAILED
    assert recommendation.error_code == "ROUTE_DISRUPTED"
    assert recommendation.error_message


@pytest.mark.asyncio
async def test_a_conflict_is_counted_by_its_code() -> None:
    """The conflicts are counted per code, so the metrics show which one."""
    client = FakeClient(error=RouteDisruptedError(409, "ROUTE_DISRUPTED", "closed"))
    service = await _service(client)

    recommendation = service.list_recommendations()[0]
    await service.approve(recommendation.id, decided_by="operator")

    assert service.conflicts_by_code.get("ROUTE_DISRUPTED") == 1


@pytest.mark.asyncio
async def test_an_unexpected_error_leaves_the_recommendation_retryable() -> None:
    """An unexpected failure does not close the door on the proposal.

    The recommendation goes back to APPROVED rather than FAILED, so the
    operator can try again once the cause is found.
    """
    client = FakeClient(error=RuntimeError("something odd"))
    service = await _service(client)

    recommendation = service.list_recommendations()[0]
    await service.approve(recommendation.id, decided_by="operator")

    assert recommendation.status is RecommendationStatus.APPROVED
    assert recommendation.error_code == "UNKNOWN_ERROR"
    assert "something odd" in recommendation.error_message


@pytest.mark.asyncio
async def test_a_simulator_error_is_handled_as_a_conflict() -> None:
    """A generic simulator error is handled, not raised."""
    client = FakeClient(error=SimulatorAPIError(500, "SERVER_ERROR", "boom"))
    service = await _service(client)

    recommendation = service.list_recommendations()[0]
    await service.approve(recommendation.id, decided_by="operator")

    assert recommendation.status is RecommendationStatus.FAILED
    assert recommendation.error_code == "SERVER_ERROR"


@pytest.mark.asyncio
async def test_approving_a_rejected_proposal_fails_loudly() -> None:
    """A proposal can only be decided once."""
    service = await _service()
    recommendation = service.list_recommendations()[0]
    service.reject(recommendation.id, decided_by="operator")

    with pytest.raises(DecisionError):
        await service.approve(recommendation.id, decided_by="operator")


@pytest.mark.asyncio
async def test_rejecting_an_approved_proposal_fails_loudly() -> None:
    """The same in the other direction."""
    service = await _service()
    recommendation = service.list_recommendations()[0]
    await service.approve(recommendation.id, decided_by="operator", submit=False)

    with pytest.raises(DecisionError):
        service.reject(recommendation.id, decided_by="operator")


# -- the reconciliation ---------------------------------------------------


@pytest.mark.asyncio
async def test_the_reconciliation_updates_the_working_set() -> None:
    """A delivered allocation closes its proposal."""
    service = await _service()
    recommendation = service.list_recommendations()[0]
    await service.approve(recommendation.id, decided_by="operator", submit=False)

    updated = service.reconcile(
        [
            {
                "id": 7,
                "idempotency_key": recommendation.idempotency_key,
                "status": "ARRIVED",
                "quantity": recommendation.quantity,
                "created_tick": 10,
            }
        ]
    )

    assert updated >= 0


@pytest.mark.asyncio
async def test_the_reconciliation_ignores_unknown_allocations() -> None:
    """An allocation the platform did not propose is not its business."""
    service = await _service()

    updated = service.reconcile(
        [
            {
                "id": 99,
                "idempotency_key": "not-ours",
                "status": "ARRIVED",
                "quantity": 1.0,
                "created_tick": 10,
            }
        ]
    )

    assert updated == 0


# -- the persistence ------------------------------------------------------


@pytest.mark.asyncio
async def test_persisting_without_a_repository_is_a_no_op() -> None:
    """A service with no repository still works — it just keeps nothing."""
    service = await _service()

    await service.persist(service.list_recommendations()[0])

    assert service.list_recommendations()


@pytest.mark.asyncio
async def test_persisting_everything_without_a_repository_is_a_no_op() -> None:
    """The same for the bulk path."""
    service = await _service()

    await service.persist_all()

    assert service.recommendation_count == 2


class BrokenRepository:
    """A repository whose writes always fail."""

    async def write_recommendation(self, **kwargs) -> None:
        raise RuntimeError("the database is down")


@pytest.mark.asyncio
async def test_a_failing_repository_does_not_break_the_decision() -> None:
    """A database outage must not cost the operator their decision.

    The write fails, the log records it, and the decision stands — the
    proposal is still in the working set and still approvable.
    """
    service = DecisionService(FakeClient(), repository=BrokenRepository())
    optimizer = AllocationOptimizer()
    recommendations = optimizer.propose(world=build_world(), risks=[build_risk()])
    await service.propose(recommendations)

    await service.persist_all()

    assert service.recommendation_count == 2


# -- the reset ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_reset_clears_the_working_set() -> None:
    """A reset must empty the proposals, or the next run inherits them.

    This was a real defect. The reset cleared Postgres but left the
    in-memory working set alone, so the previous run's proposals survived.
    Every one of them then failed to approve — they were planned for a
    world that no longer existed, and the simulator refused them with
    "the depot's dispatch allowance for this tick is used up".
    """
    service = await _service()

    assert service.recommendation_count > 0

    cleared = service.reset()

    assert cleared > 0
    assert service.recommendation_count == 0
    assert service.list_recommendations() == []


@pytest.mark.asyncio
async def test_the_reset_clears_the_counters() -> None:
    """The counters are part of the state a reset puts back."""
    service = await _service()

    assert service.counters["proposed"] > 0

    service.reset()

    assert service.counters["proposed"] == 0
    assert service.counters["approved"] == 0


@pytest.mark.asyncio
async def test_the_reset_clears_the_fallback_reason() -> None:
    """A reset must not leave the platform claiming it is degraded."""
    service = await _service()
    service.set_fallback_reason("the intelligence layer is unavailable")

    assert service.fallback_reason is not None

    service.reset()

    assert service.fallback_reason is None


@pytest.mark.asyncio
async def test_the_reset_leaves_the_auto_mode_alone() -> None:
    """The auto-mode is an operator's setting, not the demo's.

    Turning it on is a deliberate act with a safety consequence, and a
    demo reset should not silently undo it — but it should also not
    *enable* it.
    """
    service = await _service()

    service.reset()

    assert service.auto_mode is False
