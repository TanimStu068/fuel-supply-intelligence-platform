"""Tests for the fallback policy's *activation* behaviour.

The rule-based policy itself is tested in ``test_decision_fallback.py``.
What matters here is the plumbing around it: that the platform notices the
intelligence layer is down, that it says so, that the metric counts
*activations* rather than poll cycles, and that the operator's own request
for recommendations goes through the same policy selection as the
background sync.
"""

from __future__ import annotations

from typing import ClassVar

import pytest
from decision_fixtures import build_world

from app.decision.fallback import FallbackPolicy, StationProfile
from app.decision.models import RecommendationStatus
from app.decision.service import DecisionService

PROFILES = {
    "station-mirpur": StationProfile(demand_profile="urban_high"),
}


class FakeClient:
    """A simulator client that records what it was asked to do."""

    def __init__(self) -> None:
        self.submitted: list[object] = []

    async def create_allocation(self, allocation: object) -> object:
        self.submitted.append(allocation)
        return object()

    async def aclose(self) -> None:
        return None


def _fallback_recommendations():
    policy = FallbackPolicy()
    return policy.propose(world=build_world(), profiles=PROFILES)


def _populated_cache():
    """A cache holding one realistic snapshot of the network."""
    from app.simulator_models import Depot, Instance, Metrics, Region, Route, Station
    from app.state_cache import CachedState, StateCache

    cache = StateCache()
    cache.store(
        CachedState(
            instance=Instance.model_validate(
                {
                    "id": 1,
                    "scenario_id": "baseline",
                    "scenario_version": "1.0",
                    "seed": 1,
                    "sim_time": "2026-01-01T08:00:00+00:00",
                    "tick": 10,
                    "tick_minutes": 15,
                    "status": "PAUSED",
                }
            ),
            regions=[
                Region.model_validate({"id": "region-dhaka", "name": "Dhaka", "demand_factor": 1.0})
            ],
            depots=[
                Depot.model_validate(
                    {
                        "id": "depot-gazipur",
                        "name": "Gazipur",
                        "region_id": "region-dhaka",
                        "status": "OPEN",
                        "dispatch_capacity_per_tick": 12000,
                        "capacity": {"DIESEL": 90000},
                        "inventory": {"DIESEL": 60000},
                    }
                )
            ],
            stations=[
                Station.model_validate(
                    {
                        "id": "station-mirpur",
                        "name": "Mirpur",
                        "region_id": "region-dhaka",
                        "status": "OPEN",
                        "demand_profile": "urban_high",
                        "demand_multiplier": 1.0,
                        "capacity": {"DIESEL": 15000},
                        "inventory": {"DIESEL": 2000},
                    }
                )
            ],
            routes=[
                Route.model_validate(
                    {
                        "id": "route-gazipur-mirpur",
                        "source_depot_id": "depot-gazipur",
                        "destination_station_id": "station-mirpur",
                        "transit_ticks": 2,
                        "max_shipment": 7000,
                        "status": "AVAILABLE",
                    }
                )
            ],
            supply_arrivals=[],
            events=[],
            allocations=[],
            metrics=Metrics.model_validate(
                {
                    "served_demand_liters": 100.0,
                    "unmet_demand_liters": 0.0,
                    "service_level": 1.0,
                    "allocation_liters": 0.0,
                    "allocation_failures": 0,
                }
            ),
        )
    )
    return cache


# --- the activation lifecycle -----------------------------------------------


@pytest.mark.asyncio
async def test_the_fallback_reason_is_recorded_when_the_policy_engages() -> None:
    service = DecisionService(FakeClient())

    await service.propose(_fallback_recommendations(), fallback=True, reason="the model is down")

    assert service.fallback_reason == "the model is down"


@pytest.mark.asyncio
async def test_the_fallback_reason_survives_until_the_normal_policy_returns() -> None:
    """The operator must be able to see the platform is degraded.

    The reason used to be cleared as soon as it had been counted, which
    meant ``/api/decision-settings`` could never report that the fallback
    was in use.
    """
    service = DecisionService(FakeClient())

    await service.propose(_fallback_recommendations(), fallback=True, reason="the model is down")
    # A second cycle while the outage is still on.
    await service.propose(_fallback_recommendations(), fallback=True, reason="the model is down")

    assert service.fallback_reason == "the model is down"


@pytest.mark.asyncio
async def test_the_fallback_reason_is_cleared_when_the_normal_policy_returns() -> None:
    service = DecisionService(FakeClient())

    await service.propose(_fallback_recommendations(), fallback=True, reason="the model is down")
    await service.propose(_fallback_recommendations(), fallback=False)

    assert service.fallback_reason is None


def test_an_outage_is_counted_once_not_once_per_poll_cycle() -> None:
    """One outage is one activation, however many cycles it spans."""
    service = DecisionService(FakeClient())

    service.set_fallback_reason("the model is down")
    first = service.consume_fallback_activation()
    second = service.consume_fallback_activation()

    assert first == "the model is down"
    assert second is None


def test_a_new_outage_is_counted_again() -> None:
    """The counter re-arms once the normal policy comes back."""
    service = DecisionService(FakeClient())

    service.set_fallback_reason("the model is down")
    assert service.consume_fallback_activation() == "the model is down"

    service.set_fallback_reason(None)
    service.set_fallback_reason("the model is down again")

    assert service.consume_fallback_activation() == "the model is down again"


def test_an_outage_is_counted_once_even_when_the_reason_changes() -> None:
    """The outage is one activation, however the failure is worded.

    The engine failure and the missing-forecast path word the reason
    differently, but they are the same outage.
    """
    service = DecisionService(FakeClient())

    service.set_fallback_reason("the forecasting model is unavailable (drill)")
    first = service.consume_fallback_activation()
    service.set_fallback_reason("the intelligence layer has not evaluated the network yet")
    second = service.consume_fallback_activation()

    assert first is not None
    assert second is None


def test_no_reason_means_no_activation() -> None:
    service = DecisionService(FakeClient())

    assert service.consume_fallback_activation() is None


# --- the recommendation payload ---------------------------------------------


@pytest.mark.asyncio
async def test_a_fallback_recommendation_is_marked_and_kept_for_a_human() -> None:
    service = DecisionService(FakeClient())

    recommendations = await service.propose(
        _fallback_recommendations(), fallback=True, reason="the model is down"
    )

    assert recommendations
    recommendation = recommendations[0]
    assert recommendation.used_fallback_policy is True
    assert recommendation.needs_human_review is True
    assert recommendation.confidence <= 0.4
    assert recommendation.status is RecommendationStatus.PROPOSED
    assert "forecast model is unavailable" in recommendation.rationale


@pytest.mark.asyncio
async def test_the_fallback_recommendations_are_counted() -> None:
    """The audit trail has to say *why* the rule was used."""
    service = DecisionService(FakeClient())
    recommendations = _fallback_recommendations()

    await service.propose(recommendations, fallback=True, reason="the model is down")

    assert service.counters.get("fallback_recommendations") == len(recommendations)
    assert service.counters.get("fallback_recommendations", 0) > 0


# --- the timezone ------------------------------------------------------------


def test_every_recommendation_carries_a_timezone_aware_timestamp() -> None:
    """A naive and an aware datetime cannot be compared.

    The optimizer made aware timestamps and the fallback made naive ones,
    so sorting a mixed list of recommendations raised ``TypeError``.
    """
    from datetime import UTC

    fallback = _fallback_recommendations()[0]

    assert fallback.created_at.tzinfo is not None
    assert fallback.created_at.utcoffset() == UTC.utcoffset(None)


def test_recommendations_from_both_policies_can_be_sorted_together() -> None:
    """The mixed list is the one the decision history builds."""
    from decision_fixtures import build_risk

    from app.decision.optimizer import AllocationOptimizer

    optimizer = AllocationOptimizer()
    model_driven = optimizer.propose(world=build_world(), risks=[build_risk()])
    rule_based = _fallback_recommendations()

    ordered = sorted(model_driven + rule_based, key=lambda item: item.created_at)

    assert len(ordered) == len(model_driven) + len(rule_based)


# --- the shared policy selection --------------------------------------------


@pytest.mark.asyncio
async def test_the_operator_proposal_uses_the_fallback_when_there_is_no_forecast() -> None:
    """The dashboard's "propose" button must behave like the background sync.

    Both go through ``IntelligenceService.propose_now``, so both pick the
    same policy. With no evaluation result there is no forecast, so the
    rule-based policy has to take over.
    """
    from app.intelligence_service import IntelligenceService

    service = IntelligenceService(
        database=None,
        cache=_populated_cache(),
        engine=None,
        decision=DecisionService(FakeClient()),
    )
    # No evaluation has run, so there is no forecast to act on.
    recommendations = await service.propose_now()

    assert service.decision is not None
    assert service.decision.fallback_reason == (
        "the intelligence layer has not evaluated the network yet"
    )
    assert all(item.used_fallback_policy for item in recommendations)


@pytest.mark.asyncio
async def test_a_failed_evaluation_drops_the_stale_result() -> None:
    """A broken engine must not leave yesterday's forecast in play.

    The previous result describes a world that has moved on. Acting on it
    would mean acting on a forecast the platform could not refresh, so the
    fallback policy has to take over instead.
    """
    from app.intelligence_service import IntelligenceService

    service = IntelligenceService(
        database=None,
        cache=_populated_cache(),
        engine=None,
        decision=DecisionService(FakeClient()),
    )

    # A healthy evaluation, so there is a result to go stale.
    service._last_result = _healthy_result()
    assert service.last_result is not None

    def _broken(*args, **kwargs):
        raise RuntimeError("the forecasting model is unavailable (drill)")

    service._engine = _BrokenEngine(_broken)

    result = await service.evaluate_and_store()

    check_result = service.last_result
    assert result is None
    assert check_result is None


class _BrokenEngine:
    """An engine whose evaluation always fails."""

    def __init__(self, boom) -> None:
        self._boom = boom
        self.selector = _StubSelector()

    def evaluate(self, **kwargs):
        return self._boom(**kwargs)


class _StubSelector:
    """The selector the service reads when it looks for a reason."""

    fallback_counts: ClassVar[dict[str, int]] = {}

    def _fallback_reason(self, history):  # pragma: no cover - not reached
        return None


def _healthy_result():
    """A minimal, usable intelligence result."""
    from app.intelligence.engine import IntelligenceResult

    return IntelligenceResult(forecasts=[], risks=[])


def test_the_metric_label_is_bounded() -> None:
    """A failure whose text changes must not create a new time series."""
    from app.intelligence_service import _fallback_label

    assert _fallback_label("no_forecast") == "no_forecast"
    assert _fallback_label("boom at tick 42") == "intelligence_evaluation_failed"
    assert _fallback_label("no_forecast: the model is down") == "no_forecast"
