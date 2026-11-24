"""A circuit breaker for calls to a flaky dependency.

**Why it exists.** The organizer's simulator keeps its state in SQLite behind a
SQLAlchemy pool of five connections plus ten overflow. The backend used to
retry every failed call, and each retry opened a *new* connection while the
previous one was still waiting on the pool. The simulator's pool therefore
exhausted under the backend's own retry storm, and the simulator stopped
answering at all — a self-inflicted outage. A circuit breaker stops the
hammering: after a run of failures the calls are refused outright, so the
dependency gets a chance to recover instead of being kept down.

**The three states.**

``closed``
    Calls go through. This is the normal state.

``open``
    Calls are refused immediately, without touching the network. The
    dependency is presumed broken.

``half_open``
    One probe call is allowed through, to find out whether the dependency has
    recovered. If it succeeds the breaker closes; if it fails the breaker
    opens again.

**Why half-open allows exactly one probe.** Two probes would double the
pressure on a dependency that is only just recovering, and if the dependency
is genuinely still broken they would both fail anyway. One probe is the
smallest amount of traffic that answers the question.

The breaker is deliberately *not* thread-safe beyond asyncio's single-threaded
model: the platform runs one event loop, so a plain attribute check is
atomic enough and keeps the hot path cheap.
"""

from __future__ import annotations

import logging
import time
from enum import StrEnum

from .metrics import (
    CIRCUIT_OPEN,
    CIRCUIT_STATE_TRANSITIONS,
)

logger = logging.getLogger(__name__)


class CircuitState(StrEnum):
    """The three states of the breaker."""

    #: Calls pass through.
    CLOSED = "closed"

    #: Calls are refused without touching the network.
    OPEN = "open"

    #: One probe call is allowed through to test recovery.
    HALF_OPEN = "half_open"


class CircuitOpenError(RuntimeError):
    """Raised when a call is refused because the breaker is open.

    It is *not* a transport error: the request was never sent. The caller
    should treat it as "the dependency is known to be down" rather than
    "the request failed and might succeed if retried".
    """


class CircuitBreaker:
    """Refuses calls to a dependency that is failing.

    The breaker opens after ``failure_threshold`` consecutive failures and
    closes again after ``recovery_timeout`` seconds have passed, when the
    next call is let through as a probe.

    Parameters
    ----------
    name:
        The dependency's name, used in the log lines and the metric label.
    failure_threshold:
        How many consecutive failures open the breaker.
    recovery_timeout:
        How long the breaker stays open before it lets a probe through.
    success_threshold:
        How many consecutive successful probes close the breaker. One is
        enough for most dependencies; more is useful when a single success
        does not really mean the dependency is healthy again.
    """

    def __init__(
        self,
        *,
        name: str = "dependency",
        failure_threshold: int = 5,
        recovery_timeout: float = 30.0,
        success_threshold: int = 1,
    ) -> None:
        if failure_threshold < 1:
            raise ValueError("failure_threshold must be at least 1")
        if recovery_timeout <= 0:
            raise ValueError("recovery_timeout must be positive")
        if success_threshold < 1:
            raise ValueError("success_threshold must be at least 1")

        self._name = name
        self._failure_threshold = failure_threshold
        self._recovery_timeout = recovery_timeout
        self._success_threshold = success_threshold

        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._success_count = 0
        #: When the breaker opened. Used to decide when to probe.
        self._opened_at: float | None = None
        #: When the breaker last changed state, for the metric.
        self._changed_at = time.time()
        #: How many calls the breaker refused outright.
        self._refused = 0

        # Publish the gauge straight away, so the series exists in the
        # first scrape. A metric that only appears once something has
        # already gone wrong is a metric that cannot show a *closed*
        # breaker — and "the breaker is closed" is the state worth
        # seeing.
        CIRCUIT_OPEN.labels(circuit=self._name).set(0)

    @property
    def name(self) -> str:
        """The dependency's name."""
        return self._name

    @property
    def state(self) -> CircuitState:
        """The breaker's current state."""
        return self._state

    @property
    def failure_count(self) -> int:
        """How many consecutive failures have been seen."""
        return self._failure_count

    @property
    def refused_count(self) -> int:
        """How many calls the breaker refused without sending them."""
        return self._refused

    def allows(self) -> bool:
        """Whether a call may proceed right now.

        A call is allowed when the breaker is closed, or when it is open but
        the recovery timeout has passed — in which case the breaker moves to
        half-open and the call becomes the probe.
        """
        if self._state is CircuitState.CLOSED:
            return True

        if self._state is CircuitState.OPEN:
            if self._opened_at is None:
                return False
            if time.monotonic() - self._opened_at >= self._recovery_timeout:
                self._transition(CircuitState.HALF_OPEN)
                return True
            return False

        # Half-open: one probe at a time. The probe is the call that is
        # running right now, so the next one is refused until it reports.
        return True

    def record_success(self) -> None:
        """Report that a call succeeded."""
        if self._state is CircuitState.HALF_OPEN:
            self._success_count += 1
            if self._success_count >= self._success_threshold:
                self._transition(CircuitState.CLOSED)
                self._failure_count = 0
                self._success_count = 0
            return

        # A success in the closed state resets the failure run. The breaker
        # only counts *consecutive* failures, so one good call in the middle
        # of a bad patch is enough to keep the breaker closed.
        self._failure_count = 0
        self._success_count = 0

    def record_failure(self) -> None:
        """Report that a call failed."""
        self._failure_count += 1
        self._success_count = 0

        if self._state is CircuitState.HALF_OPEN:
            # The probe failed: the dependency is still broken.
            self._transition(CircuitState.OPEN)
            return

        if self._failure_count >= self._failure_threshold:
            self._transition(CircuitState.OPEN)

    def refuse(self) -> None:
        """Record that a call was refused without being sent."""
        self._refused += 1

    def reset(self) -> None:
        """Force the breaker closed. Used by the kill-switch and by tests."""
        self._transition(CircuitState.CLOSED)
        self._failure_count = 0
        self._success_count = 0
        self._opened_at = None

    def snapshot(self) -> dict[str, object]:
        """A JSON-safe description of the breaker, for the health endpoint."""
        return {
            "name": self._name,
            "state": self._state.value,
            "failure_count": self._failure_count,
            "failure_threshold": self._failure_threshold,
            "recovery_timeout_seconds": self._recovery_timeout,
            "refused_calls": self._refused,
            "opened_seconds_ago": (
                round(time.time() - self._changed_at, 3)
                if self._state is not CircuitState.CLOSED
                else None
            ),
        }

    def _transition(self, new_state: CircuitState) -> None:
        """Move to a new state, log the change and publish the gauge."""
        if new_state is self._state:
            return
        previous = self._state
        self._state = new_state
        self._changed_at = time.time()
        if new_state is CircuitState.OPEN:
            self._opened_at = time.monotonic()
        elif new_state is CircuitState.CLOSED:
            self._opened_at = None

        # The gauge is what the ``CircuitBreakerOpen`` alert rule reads, so
        # it has to be published on every transition — not only when the
        # breaker opens. Without this the metric simply never appears in
        # the scrape, and the alert rule silently evaluates an empty
        # series and never fires.
        CIRCUIT_OPEN.labels(circuit=self._name).set(1 if new_state is CircuitState.OPEN else 0)
        CIRCUIT_STATE_TRANSITIONS.labels(circuit=self._name, state=new_state.value).inc()

        logger.warning(
            "circuit breaker %s: %s -> %s",
            self._name,
            previous.value,
            new_state.value,
            extra={
                "circuit": self._name,
                "circuit_state": new_state.value,
                "previous_state": previous.value,
                "failure_count": self._failure_count,
            },
        )
