"""Tests for the human approval flow and the 409 handling.

Every conflict code the simulator can return (guide §9) is reproduced
here, and the service has to turn each one into a clear reason rather
than an exception the operator cannot act on.
"""

from __future__ import annotations

import pytest
from decision_fixtures import build_risk, build_world

from app.decision.models import RecommendationStatus
from app.decision.optimizer import AllocationOptimizer
from app.decision.service import CONFLICT_EXPLANATIONS, DecisionError, DecisionService
from app.simulator_errors import (
    CANNOT_CANCEL,
    DEPOT_CLOSED,
    DESTINATION_CAPACITY_EXCEEDED,
    DISPATCH_CAPACITY_EXCEEDED,
    IDEMPOTENCY_KEY_MISMATCH,
    INSUFFICIENT_INVENTORY,
    ROUTE_CAPACITY_EXCEEDED,
    ROUTE_DISRUPTED,
    ROUTE_MISMATCH,
    STATION_CLOSED,
    CannotCancelError,
    ConflictError,
    DepotClosedError,
    DestinationCapacityExceededError,
    DispatchCapacityExceededError,
    IdempotencyKeyMismatchError,
    InsufficientInventoryError,
    RouteCapacityExceededError,
    RouteDisruptedError,
    RouteMismatchError,
    SimulatorConnectionError,
    StationClosedError,
)


class FakeAllocation:
    """The shape of the simulator's allocation response."""

    def __init__(self, allocation_id: int = 7) -> None:
        self.id = allocation_id
        self.status = type("Status", (), {"value": "PENDING"})()
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
    """A simulator client that records the allocations it was asked for."""

    def __init__(self, error: Exception | None = None) -> None:
        self.submitted: list[dict] = []
        self.error = error

    async def create_allocation(self, allocation: dict) -> FakeAllocation:
        if self.error is not None:
            raise self.error
        self.submitted.append(allocation)
        return FakeAllocation()


async def _service(client: FakeClient | None = None) -> DecisionService:
    """A decision service with one proposal already in it."""
    service = DecisionService(client or FakeClient())
    optimizer = AllocationOptimizer()
    recommendations = optimizer.propose(world=build_world(), risks=[build_risk()])
    await service.propose(recommendations)
    return service


# --- the lifecycle ---------------------------------------------------------


@pytest.mark.asyncio
async def test_a_recommendation_starts_as_proposed() -> None:
    service = await _service()

    recommendation = service.list_recommendations()[0]

    assert recommendation.status is RecommendationStatus.PROPOSED
    assert recommendation.decided_by is None
    assert recommendation.allocation_id is None


@pytest.mark.asyncio
async def test_nothing_is_submitted_without_an_approval() -> None:
    """The whole point: the engine proposes, the human disposes."""
    client = FakeClient()
    service = await _service(client)

    service.list_recommendations()

    assert client.submitted == []


@pytest.mark.asyncio
async def test_approving_submits_the_allocation() -> None:
    client = FakeClient()
    service = await _service(client)
    recommendation = service.list_recommendations()[0]

    await service.approve(recommendation.id, decided_by="alice")

    assert len(client.submitted) == 1
    assert client.submitted[0]["idempotency_key"] == recommendation.idempotency_key
    assert client.submitted[0]["destination_station_id"] == "station-mirpur"
    assert recommendation.status is RecommendationStatus.SUBMITTED
    assert recommendation.allocation_id == 7
    assert recommendation.decided_by == "alice"
    assert recommendation.decided_at is not None


@pytest.mark.asyncio
async def test_rejecting_sends_nothing() -> None:
    client = FakeClient()
    service = await _service(client)
    recommendation = service.list_recommendations()[0]

    service.reject(recommendation.id, decided_by="alice", reason="not yet")

    assert client.submitted == []
    assert recommendation.status is RecommendationStatus.REJECTED
    assert recommendation.decided_by == "alice"
    assert recommendation.error_message == "not yet"


@pytest.mark.asyncio
async def test_a_rejected_recommendation_cannot_be_approved() -> None:
    service = await _service()
    recommendation = service.list_recommendations()[0]

    service.reject(recommendation.id, decided_by="alice")

    with pytest.raises(DecisionError, match="not PROPOSED"):
        await service.approve(recommendation.id, decided_by="bob")


@pytest.mark.asyncio
async def test_approving_an_unknown_recommendation_fails() -> None:
    service = await _service()

    with pytest.raises(DecisionError, match="no recommendation"):
        await service.approve("does-not-exist", decided_by="alice")


@pytest.mark.asyncio
async def test_the_submission_uses_the_deterministic_idempotency_key() -> None:
    """A retried approval must be a replay, not a second shipment."""
    client = FakeClient()
    service = await _service(client)
    recommendation = service.list_recommendations()[0]

    await service.approve(recommendation.id, decided_by="alice")

    key = client.submitted[0]["idempotency_key"]
    assert key.startswith("rec-station-mirpur")
    # The key is ``rec-{station}-{fuel}-{tick}-{quantity}`` with an
    # optional ``-{leg}`` suffix for the parts of a split shipment.
    assert key == recommendation.idempotency_key
    assert str(int(recommendation.quantity)) in key


# --- every 409 the simulator can return ------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "code"),
    [
        (RouteMismatchError(409, "ROUTE_MISMATCH", "m"), ROUTE_MISMATCH),
        (DepotClosedError(409, "DEPOT_CLOSED", "m"), DEPOT_CLOSED),
        (StationClosedError(409, "STATION_CLOSED", "m"), STATION_CLOSED),
        (RouteDisruptedError(409, "ROUTE_DISRUPTED", "m"), ROUTE_DISRUPTED),
        (
            RouteCapacityExceededError(409, "ROUTE_CAPACITY_EXCEEDED", "m"),
            ROUTE_CAPACITY_EXCEEDED,
        ),
        (
            InsufficientInventoryError(409, "INSUFFICIENT_INVENTORY", "m"),
            INSUFFICIENT_INVENTORY,
        ),
        (
            DispatchCapacityExceededError(409, "DISPATCH_CAPACITY_EXCEEDED", "m"),
            DISPATCH_CAPACITY_EXCEEDED,
        ),
        (
            DestinationCapacityExceededError(409, "DESTINATION_CAPACITY_EXCEEDED", "m"),
            DESTINATION_CAPACITY_EXCEEDED,
        ),
        (
            IdempotencyKeyMismatchError(409, "IDEMPOTENCY_KEY_MISMATCH", "m"),
            IDEMPOTENCY_KEY_MISMATCH,
        ),
        (CannotCancelError(409, CANNOT_CANCEL, "m"), CANNOT_CANCEL),
    ],
)
async def test_every_conflict_is_handled_with_a_clear_reason(
    error: ConflictError, code: str
) -> None:
    """A 409 must become an explanation, not a crash."""
    service = await _service(FakeClient(error))
    recommendation = service.list_recommendations()[0]

    await service.approve(recommendation.id, decided_by="alice")

    assert recommendation.status is RecommendationStatus.FAILED
    assert recommendation.error_code == code
    assert recommendation.error_message
    assert recommendation.error_message != code
    assert service.counters["conflicts"] == 1
    assert service.conflicts_by_code[code] == 1


@pytest.mark.asyncio
async def test_an_unreachable_simulator_leaves_the_recommendation_retryable() -> None:
    """A network failure is not a rejection — the operator can retry."""
    service = await _service(FakeClient(SimulatorConnectionError("connection refused")))
    recommendation = service.list_recommendations()[0]

    await service.approve(recommendation.id, decided_by="alice")

    assert recommendation.status is RecommendationStatus.APPROVED
    assert recommendation.error_code == "SIMULATOR_UNREACHABLE"
    assert "idempotency key" in recommendation.error_message.lower()


@pytest.mark.asyncio
async def test_every_conflict_code_has_an_explanation() -> None:
    """No conflict code is left unexplained."""
    for code in (
        ROUTE_MISMATCH,
        DEPOT_CLOSED,
        STATION_CLOSED,
        ROUTE_DISRUPTED,
        ROUTE_CAPACITY_EXCEEDED,
        INSUFFICIENT_INVENTORY,
        DISPATCH_CAPACITY_EXCEEDED,
        DESTINATION_CAPACITY_EXCEEDED,
        CANNOT_CANCEL,
    ):
        assert code in CONFLICT_EXPLANATIONS
        assert len(CONFLICT_EXPLANATIONS[code]) > 20


# --- the what-if simulation ------------------------------------------------


@pytest.mark.asyncio
async def test_the_simulation_does_not_touch_the_simulator() -> None:
    """The "simulate" step must not "act"."""
    client = FakeClient()
    service = await _service(client)
    recommendation = service.list_recommendations()[0]

    result = service.simulate(recommendation.id)

    assert result["simulated"] is True
    assert result["writes_to_simulator"] is False
    assert client.submitted == []
    assert recommendation.status is RecommendationStatus.PROPOSED


@pytest.mark.asyncio
async def test_the_simulation_shows_the_improvement() -> None:
    service = await _service()
    recommendation = service.list_recommendations()[0]

    result = service.simulate(recommendation.id)

    assert "before" in result
    assert "after" in result
    assert result["after"]["stockout_probability"] <= (result["before"]["stockout_probability"])
    assert result["improvement"]["stockout_probability_reduction"] >= 0


@pytest.mark.asyncio
async def test_simulating_an_unknown_recommendation_fails() -> None:
    service = await _service()

    with pytest.raises(DecisionError):
        service.simulate("does-not-exist")


# --- the auto-mode ---------------------------------------------------------


@pytest.mark.asyncio
async def test_the_auto_mode_is_off_by_default() -> None:
    service = await _service()

    assert service.auto_mode is False


@pytest.mark.asyncio
async def test_the_auto_mode_does_nothing_while_it_is_off() -> None:
    client = FakeClient()
    await _service(client)

    assert client.submitted == []


@pytest.mark.asyncio
async def test_the_auto_mode_leaves_critical_stations_to_a_human() -> None:
    """CRITICAL is exactly where a human should look."""
    client = FakeClient()
    service = await _service(client)
    service.set_auto_mode(True)

    assert client.submitted == []
    assert service.counters["auto_approved"] == 0


@pytest.mark.asyncio
async def test_the_auto_mode_leaves_the_fallback_policy_to_a_human() -> None:
    """A recommendation made without a model is never auto-approved."""
    from app.decision.fallback import FallbackPolicy, StationProfile

    client = FakeClient()
    service = DecisionService(client)
    policy = FallbackPolicy()
    recommendations = policy.propose(
        world=build_world(),
        profiles={"station-mirpur": StationProfile(demand_profile="urban_high")},
    )
    service.set_auto_mode(True)

    await service.propose(recommendations, fallback=True)

    assert client.submitted == []
    assert service.counters["auto_approved"] == 0


# --- reconciliation --------------------------------------------------------


@pytest.mark.asyncio
async def test_a_submitted_allocation_is_reconciled_when_it_arrives() -> None:
    """The decision history closes the loop on the simulator's status."""
    client = FakeClient()
    service = await _service(client)
    recommendation = service.list_recommendations()[0]
    await service.approve(recommendation.id, decided_by="alice")

    arrived = FakeAllocation()
    arrived.status = type("Status", (), {"value": "ARRIVED"})()
    arrived.actual_arrival_tick = 12

    reconciled = service.reconcile([arrived])

    assert reconciled == 1
    assert recommendation.status is RecommendationStatus.ARRIVED


@pytest.mark.asyncio
async def test_a_failed_allocation_is_reconciled() -> None:
    client = FakeClient()
    service = await _service(client)
    recommendation = service.list_recommendations()[0]
    await service.approve(recommendation.id, decided_by="alice")

    failed = FakeAllocation()
    failed.status = type("Status", (), {"value": "FAILED"})()
    failed.failure_reason = "ROUTE_UNAVAILABLE"

    service.reconcile([failed])

    assert recommendation.status is RecommendationStatus.FAILED
    assert recommendation.error_code == "FAILED"
    assert recommendation.error_message == "ROUTE_UNAVAILABLE"
