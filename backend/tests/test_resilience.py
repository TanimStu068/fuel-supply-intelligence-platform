"""Tests for the circuit breaker and the ML kill-switch.

Both are operator-facing safety devices, so the tests are about behaviour an
operator would rely on: the breaker must *stop* the hammering, the probe
must find the recovery, and the kill-switch must actually stop the learned
model from being used.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest

from app.circuit_breaker import CircuitBreaker, CircuitState
from app.config import Settings
from app.intelligence.forecasting import (
    BaselinePredictor,
    ForecastSelector,
    ForecastSelectorConfig,
    HistoryPoint,
    LearnedPredictor,
    StationContext,
)
from app.simulator_client import SimulatorClient
from app.simulator_errors import SimulatorAPIError, SimulatorConnectionError

# -- circuit breaker --------------------------------------------------------


def test_the_breaker_starts_closed() -> None:
    breaker = CircuitBreaker(name="simulator")
    assert breaker.state is CircuitState.CLOSED
    assert breaker.allows()


def test_the_breaker_opens_after_consecutive_failures() -> None:
    breaker = CircuitBreaker(name="simulator", failure_threshold=3)
    for _ in range(3):
        breaker.record_failure()
    assert breaker.state is CircuitState.OPEN
    assert not breaker.allows()


def test_a_success_resets_the_failure_run() -> None:
    breaker = CircuitBreaker(name="simulator", failure_threshold=3)
    breaker.record_failure()
    breaker.record_failure()
    breaker.record_success()
    breaker.record_failure()
    breaker.record_failure()
    # The run was reset by the success, so two failures are not enough.
    assert breaker.state is CircuitState.CLOSED


def test_the_breaker_refuses_calls_while_open() -> None:
    breaker = CircuitBreaker(name="simulator", failure_threshold=1)
    breaker.record_failure()
    assert not breaker.allows()
    breaker.refuse()
    assert breaker.refused_count == 1


def test_the_breaker_probes_after_the_recovery_timeout() -> None:
    breaker = CircuitBreaker(
        name="simulator",
        failure_threshold=1,
        recovery_timeout=0.01,
    )
    breaker.record_failure()
    assert breaker.state is CircuitState.OPEN
    assert not breaker.allows()

    import time

    time.sleep(0.02)

    # The probe is allowed through, and the breaker moves to half-open.
    assert breaker.allows()
    assert breaker.state is CircuitState.HALF_OPEN


def test_a_successful_probe_closes_the_breaker() -> None:
    breaker = CircuitBreaker(
        name="simulator",
        failure_threshold=1,
        recovery_timeout=0.01,
    )
    breaker.record_failure()
    import time

    time.sleep(0.02)
    assert breaker.allows()
    breaker.record_success()
    assert breaker.state is CircuitState.CLOSED
    assert breaker.failure_count == 0


def test_a_failed_probe_reopens_the_breaker() -> None:
    breaker = CircuitBreaker(
        name="simulator",
        failure_threshold=1,
        recovery_timeout=0.01,
    )
    breaker.record_failure()
    import time

    time.sleep(0.02)
    assert breaker.allows()
    breaker.record_failure()
    assert breaker.state is CircuitState.OPEN


def test_the_breaker_snapshot_describes_its_state() -> None:
    breaker = CircuitBreaker(name="simulator", failure_threshold=2)
    breaker.record_failure()
    snapshot = breaker.snapshot()
    assert snapshot["name"] == "simulator"
    assert snapshot["state"] == "closed"
    assert snapshot["failure_count"] == 1
    assert snapshot["refused_calls"] == 0


def test_the_breaker_rejects_nonsense_configuration() -> None:
    with pytest.raises(ValueError):
        CircuitBreaker(failure_threshold=0)
    with pytest.raises(ValueError):
        CircuitBreaker(recovery_timeout=0)
    with pytest.raises(ValueError):
        CircuitBreaker(success_threshold=0)


# -- the breaker inside the simulator client --------------------------------


@pytest.mark.asyncio
async def test_an_open_breaker_refuses_the_call_without_sending_it() -> None:
    """The whole point of the breaker: the request never leaves the process."""
    sent = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request.url.path)
        raise httpx.ConnectError("connection refused")

    settings = Settings(
        SIMULATOR_BASE_URL="http://simulator.test",
        SIMULATOR_REQUEST_TIMEOUT=1.0,
        SIMULATOR_CONNECT_TIMEOUT=0.5,
        SIMULATOR_MAX_RETRIES=0,
        SIMULATOR_RETRY_BASE_DELAY=0.001,
        SIMULATOR_RETRY_MAX_DELAY=0.01,
        CIRCUIT_BREAKER_FAILURES=2,
    )
    client = SimulatorClient(
        settings=settings,
        client=httpx.AsyncClient(
            base_url=settings.simulator_base_url,
            transport=httpx.MockTransport(handler),
        ),
    )

    # Two failures open the breaker.
    for _ in range(2):
        with pytest.raises(SimulatorConnectionError):
            await client.get_instance()

    assert client.breaker.state is CircuitState.OPEN
    sent_before = len(sent)

    # The next call is refused without being sent.
    with pytest.raises(SimulatorConnectionError):
        await client.get_instance()

    assert len(sent) == sent_before
    assert client.breaker.refused_count >= 1


@pytest.mark.asyncio
async def test_a_conflict_does_not_trip_the_breaker() -> None:
    """A 409 is a domain answer: the simulator is healthy."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            409,
            json={"detail": {"code": "ROUTE_MISMATCH", "message": "no such route"}},
        )

    settings = Settings(
        SIMULATOR_BASE_URL="http://simulator.test",
        SIMULATOR_REQUEST_TIMEOUT=1.0,
        SIMULATOR_CONNECT_TIMEOUT=0.5,
        SIMULATOR_MAX_RETRIES=0,
        CIRCUIT_BREAKER_FAILURES=2,
    )
    client = SimulatorClient(
        settings=settings,
        client=httpx.AsyncClient(
            base_url=settings.simulator_base_url,
            transport=httpx.MockTransport(handler),
        ),
    )

    for _ in range(4):
        with pytest.raises(SimulatorAPIError):
            await client.get_instance()

    assert client.breaker.state is CircuitState.CLOSED


@pytest.mark.asyncio
async def test_an_injected_fault_does_not_trip_the_breaker() -> None:
    """An injected 503 is the simulator misbehaving on purpose, not being broken."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            503,
            json={"error": {"code": "FAULT_INJECTED", "message": "latency fault"}},
        )

    settings = Settings(
        SIMULATOR_BASE_URL="http://simulator.test",
        SIMULATOR_REQUEST_TIMEOUT=1.0,
        SIMULATOR_CONNECT_TIMEOUT=0.5,
        SIMULATOR_MAX_RETRIES=0,
        CIRCUIT_BREAKER_FAILURES=2,
    )
    client = SimulatorClient(
        settings=settings,
        client=httpx.AsyncClient(
            base_url=settings.simulator_base_url,
            transport=httpx.MockTransport(handler),
        ),
    )

    for _ in range(4):
        with pytest.raises(SimulatorAPIError):
            await client.get_instance()

    assert client.breaker.state is CircuitState.CLOSED


# -- the ML kill-switch -----------------------------------------------------


def _history(rows: int = 60) -> list[HistoryPoint]:
    return [
        HistoryPoint(
            station_id="station-mirpur",
            fuel_type="DIESEL",
            tick=index,
            sim_time=datetime(2026, 1, 1, tzinfo=UTC) + timedelta(minutes=15 * index),
            demand_liters=100.0 + index,
            served_liters=100.0 + index,
            unmet_liters=0.0,
        )
        for index in range(rows)
    ]


def _context() -> StationContext:
    return StationContext(
        station_id="station-mirpur",
        profile_name="urban_high",
        region_demand_factor=1.0,
        demand_multiplier=1.0,
        current_tick=100,
        current_sim_time=datetime(2026, 1, 1, tzinfo=UTC),
        tick_minutes=15,
    )


def test_the_kill_switch_starts_off() -> None:
    selector = ForecastSelector()
    assert selector.kill_switch is False


def test_the_kill_switch_forces_the_baseline() -> None:
    selector = ForecastSelector(
        learned=LearnedPredictor(),
        baseline=BaselinePredictor(),
        config=ForecastSelectorConfig(min_training_rows=1, min_confidence=0.0),
    )
    selector.set_kill_switch(True)
    assert selector.kill_switch is True

    forecast = selector.forecast(
        context=_context(),
        fuel="DIESEL",
        horizon_ticks=4,
        history=_history(),
    )

    # The forecast came from the rule-based predictor, and it says so.
    assert forecast.model.value == "baseline"
    assert forecast.fallback_reason is not None
    assert "kill-switch" in forecast.fallback_reason
    # And it is flagged for a human, because there is no model behind it.
    assert forecast.needs_human_review is True


def test_the_kill_switch_can_be_turned_back_off() -> None:
    selector = ForecastSelector()
    selector.set_kill_switch(True)
    assert selector.kill_switch is True
    selector.set_kill_switch(False)
    assert selector.kill_switch is False


def test_the_kill_switch_overrides_a_healthy_model() -> None:
    """A model that looks healthy must still be stopped by the switch."""
    selector = ForecastSelector(
        learned=LearnedPredictor(),
        baseline=BaselinePredictor(),
        config=ForecastSelectorConfig(min_training_rows=1, min_confidence=0.0),
    )
    selector.set_kill_switch(True)
    # Even with a trained model present, the switch wins.
    assert selector.active_model.value == "baseline"
