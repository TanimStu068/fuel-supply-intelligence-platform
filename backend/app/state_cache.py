"""The cached view of the simulated fuel network.

The simulator is the source of truth, but it can be slow, faulted or down.
This module holds the last successfully fetched state plus the metadata that
tells an operator how much to trust it:

``stale``     the simulator served the data with ``X-Simulator-Stale: true``
``degraded``  the simulator is unreachable and this is last-known state
``age``       how long ago the state was fetched

Every read path in the API goes through :class:`StateCache`, so the degraded
flag and the age always travel with the payload.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from .simulator_models import (
    Allocation,
    Depot,
    Instance,
    Metrics,
    Region,
    Route,
    SimulatorEvent,
    Station,
    SupplyArrival,
)


@dataclass(slots=True)
class CachedState:
    """One complete snapshot of the simulated world."""

    instance: Instance
    regions: list[Region]
    depots: list[Depot]
    stations: list[Station]
    routes: list[Route]
    supply_arrivals: list[SupplyArrival]
    events: list[SimulatorEvent]
    allocations: list[Allocation]
    metrics: Metrics
    #: The simulator marked this snapshot stale.
    stale: bool = False
    #: When the snapshot was fetched (wall clock, UTC).
    fetched_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def age_seconds(self, now: float | None = None) -> float:
        """Seconds since the snapshot was fetched."""
        reference = now if now is not None else time.time()
        return max(0.0, reference - self.fetched_at.timestamp())

    def to_payload(self) -> dict[str, Any]:
        """Render the snapshot as the JSON body the API returns."""
        return {
            "instance": self.instance.model_dump(mode="json"),
            "regions": [r.model_dump(mode="json") for r in self.regions],
            "depots": [d.model_dump(mode="json") for d in self.depots],
            "stations": [s.model_dump(mode="json") for s in self.stations],
            "routes": [r.model_dump(mode="json") for r in self.routes],
            "supply_arrivals": [a.model_dump(mode="json") for a in self.supply_arrivals],
            "events": [e.model_dump(mode="json") for e in self.events],
            "allocations": [a.model_dump(mode="json") for a in self.allocations],
            "metrics": self.metrics.model_dump(mode="json"),
        }


class StateCache:
    """Holds the last known state and its freshness metadata.

    The cache is deliberately simple: one slot, replaced wholesale on every
    successful sync. A partial update would let a consumer see a snapshot
    that mixes two ticks, which is worse than a slightly older consistent one.
    """

    def __init__(self) -> None:
        self._state: CachedState | None = None
        #: Set when the simulator is unreachable; cleared on a successful sync.
        self._degraded = False
        #: Human-readable reason for the degraded state.
        self._degraded_reason: str | None = None
        #: Wall-clock time of the last successful sync.
        self._last_sync_at: float | None = None
        #: Wall-clock time of the last *attempted* sync, successful or not.
        self._last_attempt_at: float | None = None

    # -- reads --------------------------------------------------------------

    @property
    def state(self) -> CachedState | None:
        """The cached snapshot, if any."""
        return self._state

    @property
    def degraded(self) -> bool:
        """True when the simulator is unreachable and we serve last-known state."""
        return self._degraded

    @property
    def degraded_reason(self) -> str | None:
        """Why the cache is degraded, when it is."""
        return self._degraded_reason

    @property
    def has_state(self) -> bool:
        """True once a snapshot has been stored at least once."""
        return self._state is not None

    def last_sync_age(self, now: float | None = None) -> float | None:
        """Seconds since the last successful sync, or ``None`` if never synced."""
        if self._last_sync_at is None:
            return None
        reference = now if now is not None else time.time()
        return max(0.0, reference - self._last_sync_at)

    def metadata(self, now: float | None = None) -> dict[str, Any]:
        """The freshness metadata every API response carries."""
        reference = now if now is not None else time.time()
        age = self.last_sync_age(reference)
        return {
            "stale": bool(self._state.stale) if self._state else False,
            "degraded": self._degraded,
            "age_seconds": round(age, 3) if age is not None else None,
            "degraded_reason": self._degraded_reason,
            "last_sync_at": (
                datetime.fromtimestamp(self._last_sync_at, tz=UTC).isoformat()
                if self._last_sync_at is not None
                else None
            ),
        }

    # -- writes -------------------------------------------------------------

    def store(self, state: CachedState) -> None:
        """Replace the cached snapshot and clear the degraded flag."""
        self._state = state
        self._degraded = False
        self._degraded_reason = None
        self._last_sync_at = time.time()
        self._last_attempt_at = self._last_sync_at

    def mark_degraded(self, reason: str) -> None:
        """Record that the simulator could not be reached.

        The cached snapshot is kept: serving slightly old state beats serving
        nothing, as long as the response says how old it is.
        """
        self._degraded = True
        self._degraded_reason = reason
        self._last_attempt_at = time.time()

    def mark_attempt(self) -> None:
        """Record that a sync was attempted, without changing the outcome."""
        self._last_attempt_at = time.time()

    def invalidate(self) -> None:
        """Drop the cached snapshot.

        Used when the simulator serves stale data: the cached copy must not be
        presented as fresh, so it is discarded rather than relabelled.
        """
        self._state = None
