"""The background sync service: REST polling plus an SSE hint listener.

Two loops run side by side:

**The poller** is the source of truth. On a fixed interval it re-reads the
whole world over REST, writes it to Postgres, and replaces the cached state.

**The SSE listener** is a hint channel. Every event it receives triggers a
targeted re-read of the affected resource, so the cache reacts quickly
without ever trusting the event payload itself.

Failure handling is the interesting part:

* The simulator can vanish. The poller then serves last-known state marked
  ``degraded`` with an age, and keeps retrying until it comes back.
* The SSE stream has **no replay**. After every reconnect the poller does a
  full REST refetch, because anything that happened while we were away is
  gone from the stream.
* **Silence is not a disconnect.** The simulator only emits a keepalive
  every 15 seconds, so a quiet stream is normal.
* A ``stream_disconnect`` fault makes ``GET /v1/stream`` answer ``503``
  immediately; that is a reconnect, not a crash.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
import time
from datetime import UTC, datetime
from typing import Any

from .config import Settings, get_settings
from .db import Database
from .logging_config import bind_correlation_id, new_correlation_id
from .metrics import (
    DEGRADED_MODE,
    DEGRADED_MODE_ACTIVATIONS,
    DEMAND_INGESTED,
    LAST_SYNC_AGE,
    SSE_CONNECTIONS,
    SSE_EVENTS,
    SSE_RECONNECTS,
    STALE_DATA,
    SYNC_CYCLE_DURATION,
    SYNC_CYCLES,
)
from .repository import (
    write_alert,
    write_allocations,
    write_demand_observations,
    write_domain_events,
    write_inventory_snapshots,
    write_state_snapshot,
    write_system_health,
)
from .simulator_client import SimulatorClient
from .simulator_errors import (
    SimulatorAPIError,
    SimulatorConnectionError,
    SimulatorTimeoutError,
    SimulatorValidationError,
)
from .simulator_models import Station
from .sse import StreamEvent
from .state_cache import CachedState, StateCache

logger = logging.getLogger(__name__)


class SyncService:
    """Keeps the cached state and the Postgres mirror in step with the simulator."""

    def __init__(
        self,
        client: SimulatorClient,
        database: Database,
        cache: StateCache,
        settings: Settings | None = None,
    ) -> None:
        self._client = client
        self._database = database
        self._cache = cache
        self._settings = settings or get_settings()
        self._tasks: list[asyncio.Task[None]] = []
        self._stopping = asyncio.Event()
        #: Set by the SSE loop so the poller can shorten its interval when
        #: something just changed.
        self._dirty = asyncio.Event()

    # -- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        """Start the poller and the SSE listener."""
        self._stopping.clear()
        self._tasks = [
            asyncio.create_task(self._poll_loop(), name="sync-poller"),
            asyncio.create_task(self._sse_loop(), name="sync-sse"),
        ]
        logger.info("sync service started")

    async def stop(self) -> None:
        """Stop both loops and wait for them to finish."""
        self._stopping.set()
        self._dirty.set()
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._tasks.clear()
        logger.info("sync service stopped")

    # -- REST polling ------------------------------------------------------

    async def _poll_loop(self) -> None:
        """Poll the simulator on a fixed interval.

        The first cycle runs immediately so the platform has data before the
        first scheduled tick.
        """
        while not self._stopping.is_set():
            await self.run_sync_cycle()
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=self._poll_interval())
            except TimeoutError:
                continue

    def _poll_interval(self) -> float:
        """The delay between two poll cycles, with a little jitter.

        The jitter keeps several backend replicas from hitting the simulator
        in lockstep after a restart.
        """
        base = self._settings.sync_poll_interval
        return max(0.5, base + random.uniform(0, base * 0.1))

    async def run_sync_cycle(self) -> bool:
        """Fetch the whole world, store it, and mirror it into Postgres.

        Returns True when the cycle succeeded. A failed cycle never raises:
        the service must keep running while the simulator is down.
        """
        correlation_id = bind_correlation_id(new_correlation_id("sync"))
        started = time.perf_counter()
        self._cache.mark_attempt()

        logger.info("sync cycle started", extra={"correlation_id": correlation_id})

        try:
            snapshot = await self._fetch_world()
        except (SimulatorConnectionError, SimulatorTimeoutError) as exc:
            await self._handle_simulator_unreachable(str(exc), correlation_id)
            SYNC_CYCLES.labels(outcome="failed").inc()
            SYNC_CYCLE_DURATION.observe(time.perf_counter() - started)
            return False
        except SimulatorValidationError as exc:
            logger.error(
                "sync cycle rejected malformed simulator data",
                extra={"correlation_id": correlation_id, "error": str(exc)},
            )
            await self._write_health("simulator", "degraded", str(exc), correlation_id)
            SYNC_CYCLES.labels(outcome="invalid").inc()
            SYNC_CYCLE_DURATION.observe(time.perf_counter() - started)
            return False
        except SimulatorAPIError as exc:
            logger.error(
                "sync cycle failed with a simulator error",
                extra={
                    "correlation_id": correlation_id,
                    "status_code": exc.status_code,
                    "code": exc.code,
                    "error": str(exc),
                },
            )
            await self._write_health("simulator", "error", str(exc), correlation_id)
            SYNC_CYCLES.labels(outcome="failed").inc()
            SYNC_CYCLE_DURATION.observe(time.perf_counter() - started)
            return False

        stale = snapshot.stale
        if stale:
            # The simulator explicitly says its data is stale. We keep the
            # values but mark them, and we drop the cached copy so nothing
            # downstream mistakes it for a fresh reading.
            logger.warning(
                "simulator served stale data",
                extra={"correlation_id": correlation_id},
            )
            self._cache.invalidate()

        self._cache.store(snapshot)
        await self._persist(snapshot, correlation_id)
        await self._ingest_demand_history(correlation_id)
        await self._raise_alerts(snapshot, correlation_id)

        age = self._cache.last_sync_age() or 0.0
        LAST_SYNC_AGE.set(age)
        STALE_DATA.set(1 if stale else 0)
        DEGRADED_MODE.set(0)

        SYNC_CYCLES.labels(outcome="success").inc()
        SYNC_CYCLE_DURATION.observe(time.perf_counter() - started)
        logger.info(
            "sync cycle completed",
            extra={
                "correlation_id": correlation_id,
                "sim_tick": snapshot.instance.tick,
                "stale": stale,
                "duration_seconds": round(time.perf_counter() - started, 4),
            },
        )
        return True

    async def _fetch_world(self) -> CachedState:
        """Read every section of the world in parallel."""
        import asyncio

        self._client.reset_stale()
        (
            instance,
            regions,
            depots,
            stations,
            routes,
            arrivals,
            events,
            allocations,
            metrics,
        ) = await asyncio.gather(
            self._client.get_instance(),
            self._client.get_regions(),
            self._client.get_depots(),
            self._client.get_stations(),
            self._client.get_routes(),
            self._client.get_supply_arrivals(),
            self._client.get_events(),
            self._client.get_allocations(),
            self._client.get_metrics(),
        )

        return CachedState(
            instance=instance,
            regions=regions,
            depots=depots,
            stations=stations,
            routes=routes,
            supply_arrivals=arrivals,
            events=events,
            allocations=allocations,
            metrics=metrics,
            stale=self._client.stale,
        )

    async def _handle_simulator_unreachable(self, reason: str, correlation_id: str) -> None:
        """Record that the simulator is gone and serve last-known state."""
        if not self._cache.degraded:
            DEGRADED_MODE_ACTIVATIONS.inc()
            logger.warning(
                "simulator unreachable; serving last-known state",
                extra={"correlation_id": correlation_id, "error": reason},
            )
        self._cache.mark_degraded(reason)
        DEGRADED_MODE.set(1)
        await self._write_health("simulator", "unreachable", reason, correlation_id)

    async def _persist(self, snapshot: CachedState, correlation_id: str) -> None:
        """Mirror the snapshot into Postgres. Never raises."""
        try:
            async with self._database.session() as session:
                await write_state_snapshot(
                    session,
                    sim_tick=snapshot.instance.tick,
                    sim_time=snapshot.instance.sim_time,
                    sim_status=snapshot.instance.status.value,
                    scenario_id=snapshot.instance.scenario_id,
                    metrics=snapshot.metrics,
                    stale=snapshot.stale,
                    payload={
                        "stations": len(snapshot.stations),
                        "depots": len(snapshot.depots),
                        "routes": len(snapshot.routes),
                        "events": len(snapshot.events),
                        "allocations": len(snapshot.allocations),
                    },
                )
                await write_inventory_snapshots(
                    session,
                    sim_tick=snapshot.instance.tick,
                    sim_time=snapshot.instance.sim_time,
                    stations=snapshot.stations,
                    depots=snapshot.depots,
                )
                await write_allocations(session, snapshot.allocations)
                await write_domain_events(session, snapshot.events)
                await write_system_health(
                    session,
                    component="simulator",
                    status="stale" if snapshot.stale else "ok",
                    detail=None if not snapshot.stale else "X-Simulator-Stale header present",
                    correlation_id=correlation_id,
                )
                await write_system_health(
                    session,
                    component="backend",
                    status="ok",
                    correlation_id=correlation_id,
                )
                await session.commit()
        except Exception as exc:
            logger.warning(
                "could not persist snapshot to postgres",
                extra={"correlation_id": correlation_id, "error": str(exc)},
            )

    async def _write_health(
        self, component: str, status: str, detail: str | None, correlation_id: str
    ) -> None:
        """Write one component's health row. Never raises."""
        try:
            async with self._database.session() as session:
                await write_system_health(
                    session,
                    component=component,
                    status=status,
                    detail=detail,
                    correlation_id=correlation_id,
                )
                await session.commit()
        except Exception as exc:
            logger.warning(
                "could not write system health",
                extra={"component": component, "error": str(exc)},
            )

    # -- SSE listener ------------------------------------------------------

    async def _sse_loop(self) -> None:
        """Listen to the event stream, reconnecting forever.

        Every reconnect is followed by a full REST refetch, because the
        stream has no replay and anything that happened while we were
        disconnected is lost.
        """
        attempt = 0

        while not self._stopping.is_set():
            correlation_id = bind_correlation_id(new_correlation_id("sse"))
            reason = "initial"

            try:
                attempt += 1
                SSE_CONNECTIONS.inc()
                logger.info(
                    "opening simulator event stream",
                    extra={"correlation_id": correlation_id, "attempt": attempt},
                )

                async for event in self._client.stream():
                    SSE_EVENTS.labels(event=event.event).inc()
                    logger.info(
                        "sse event received",
                        extra={
                            "correlation_id": correlation_id,
                            "event": event.event,
                            "entity_id": event.entity_id,
                        },
                    )
                    await self._handle_event(event)

                # The stream ended normally: the server closed it.
                reason = "closed"
                logger.warning(
                    "simulator event stream closed; reconnecting",
                    extra={"correlation_id": correlation_id},
                )
            except SimulatorAPIError as exc:
                # A ``stream_disconnect`` fault answers 503 on connect.
                reason = "fault"
                logger.warning(
                    "simulator event stream failed; reconnecting",
                    extra={
                        "correlation_id": correlation_id,
                        "status_code": exc.status_code,
                        "code": exc.code,
                        "error": str(exc),
                    },
                )
            except (SimulatorConnectionError, SimulatorTimeoutError) as exc:
                reason = "unreachable"
                logger.warning(
                    "simulator event stream unreachable; reconnecting",
                    extra={"correlation_id": correlation_id, "error": str(exc)},
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                reason = "error"
                logger.error(
                    "unexpected error in the simulator event stream",
                    extra={"correlation_id": correlation_id, "error": str(exc)},
                )

            if self._stopping.is_set():
                break

            SSE_RECONNECTS.labels(reason=reason).inc()
            await asyncio.sleep(self._reconnect_delay(attempt))
            attempt = 0

            # No Last-Event-ID replay: re-read everything after a reconnect.
            await self.run_sync_cycle()

    def _reconnect_delay(self, attempt: int) -> float:
        """Exponential backoff with jitter between reconnect attempts."""
        base = self._settings.sse_reconnect_base_delay
        ceiling = min(base * (2 ** min(attempt - 1, 6)), self._settings.sse_reconnect_max_delay)
        return max(0.1, random.uniform(0, ceiling))

    async def _handle_event(self, event: StreamEvent) -> None:
        """React to an SSE event by re-reading the affected resource.

        The event payload is only a hint. The simulator explicitly documents
        that the queue can drop events, so the payload is never written to
        the database directly — it decides *what to re-read*.
        """
        try:
            if event.is_tick:
                await self.run_sync_cycle()
                return

            if event.is_allocation:
                await self._refresh_allocations()
                return

            if event.is_inventory:
                await self._refresh_inventory(event.entity_id)
                return

            if event.is_notice:
                # A notice means the simulator itself did something (a reset,
                # or a background-runner exception). Re-read the whole world.
                await self.run_sync_cycle()
        except SimulatorAPIError as exc:
            logger.warning(
                "could not refresh after an sse event",
                extra={"event": event.event, "error": str(exc)},
            )
        except (SimulatorConnectionError, SimulatorTimeoutError) as exc:
            logger.warning(
                "simulator unreachable while refreshing after an sse event",
                extra={"event": event.event, "error": str(exc)},
            )

    async def _refresh_allocations(self) -> None:
        """Re-read the allocation ledger and mirror it."""
        allocations = await self._client.get_allocations()
        try:
            async with self._database.session() as session:
                await write_allocations(session, allocations)
                await session.commit()
        except Exception as exc:
            logger.warning("could not persist allocations", extra={"error": str(exc)})

    async def _refresh_inventory(self, entity_id: str | None) -> None:
        """Re-read one depot or station and mirror its inventory."""
        if entity_id is None:
            await self.run_sync_cycle()
            return

        try:
            if entity_id.startswith("depot"):
                depots = [await self._client.get_depot(entity_id)]
                stations: list[Station] = []
            else:
                stations = [await self._client.get_station(entity_id)]
                depots = []
        except SimulatorAPIError:
            # The entity may have been removed; fall back to a full sync.
            await self.run_sync_cycle()
            return

        instance = await self._client.get_instance()
        try:
            async with self._database.session() as session:
                await write_inventory_snapshots(
                    session,
                    sim_tick=instance.tick,
                    sim_time=instance.sim_time,
                    stations=stations,
                    depots=depots,
                )
                await session.commit()
        except Exception as exc:
            logger.warning(
                "could not persist inventory refresh",
                extra={"entity_id": entity_id, "error": str(exc)},
            )

    # -- demand history ----------------------------------------------------

    async def ingest_demand_history(self, limit: int | None = None) -> tuple[int, int]:
        """Pull demand history incrementally and de-duplicate it.

        Returns ``(rows_touched, rows_skipped_as_duplicate)``. The simulator
        returns rows newest-first, so a repeat run mostly re-sends rows we
        already hold; the unique constraint on ``(station, fuel, tick)`` keeps
        the table clean.
        """
        correlation_id = bind_correlation_id(new_correlation_id("demand"))
        effective_limit = limit or self._settings.demand_history_limit

        try:
            observations = await self._client.get_demand_history(limit=effective_limit)
        except (SimulatorConnectionError, SimulatorTimeoutError, SimulatorAPIError) as exc:
            logger.warning(
                "could not ingest demand history",
                extra={"correlation_id": correlation_id, "error": str(exc)},
            )
            return (0, 0)

        if not observations:
            return (0, 0)

        try:
            async with self._database.session() as session:
                written, _ = await write_demand_observations(session, observations)
                await session.commit()
        except Exception as exc:
            logger.warning(
                "could not persist demand history",
                extra={"correlation_id": correlation_id, "error": str(exc)},
            )
            return (0, 0)

        DEMAND_INGESTED.labels(outcome="upserted").inc(written)
        logger.info(
            "demand history ingested",
            extra={
                "correlation_id": correlation_id,
                "rows": written,
                "limit": effective_limit,
            },
        )
        return (written, 0)

    async def _ingest_demand_history(self, correlation_id: str) -> None:
        """Pull the demand time series into Postgres.

        The simulator's table grows without bound, so the pull is always
        limited and de-duplicated on ``(station, fuel, tick)``.
        """
        try:
            observations = await self._client.get_demand_history(
                limit=self._settings.demand_history_limit
            )
        except (SimulatorConnectionError, SimulatorTimeoutError, SimulatorAPIError) as exc:
            logger.warning(
                "could not ingest demand history",
                extra={"correlation_id": correlation_id, "error": str(exc)},
            )
            return

        if not observations:
            return

        try:
            async with self._database.session() as session:
                await write_demand_observations(session, observations)
                await session.commit()
        except Exception as exc:
            logger.warning(
                "could not persist demand history",
                extra={"correlation_id": correlation_id, "error": str(exc)},
            )
            return

        DEMAND_INGESTED.labels(outcome="upserted").inc(len(observations))
        logger.info(
            "demand history ingested",
            extra={
                "correlation_id": correlation_id,
                "rows": len(observations),
                "limit": self._settings.demand_history_limit,
            },
        )

    async def _raise_alerts(self, snapshot: CachedState, correlation_id: str) -> None:
        """Raise the alerts the current state implies.

        Three conditions, all cheap to evaluate and all worth surfacing to an
        operator: a station in outage, a route that is not available, and a
        depot that has been marked constrained.
        """
        candidates: list[tuple[str, str, str, str, dict[str, Any]]] = []

        for station in snapshot.stations:
            if station.status.value == "OUTAGE":
                candidates.append(
                    (
                        "station_outage",
                        "critical",
                        "station",
                        station.id,
                        {"name": station.name},
                    )
                )

        for route in snapshot.routes:
            if route.status.value == "DISRUPTED":
                candidates.append(
                    (
                        "route_disrupted",
                        "high",
                        "route",
                        route.id,
                        {
                            "source_depot_id": route.source_depot_id,
                            "destination_station_id": route.destination_station_id,
                        },
                    )
                )

        for depot in snapshot.depots:
            if depot.status.value == "CONSTRAINED":
                candidates.append(
                    (
                        "depot_constrained",
                        "warning",
                        "depot",
                        depot.id,
                        {"name": depot.name},
                    )
                )

        if not candidates:
            return

        tick = snapshot.instance.tick
        try:
            async with self._database.session() as session:
                for kind, severity, entity_type, entity_id, payload in candidates:
                    created = await write_alert(
                        session,
                        kind=kind,
                        severity=severity,
                        entity_type=entity_type,
                        entity_id=entity_id,
                        sim_tick=tick,
                        message=f"{entity_type} {entity_id} reported {kind}",
                        payload=payload,
                    )
                    if created:
                        logger.warning(
                            "alert raised",
                            extra={
                                "correlation_id": correlation_id,
                                "kind": kind,
                                "severity": severity,
                                "entity_id": entity_id,
                                "sim_tick": tick,
                            },
                        )
                await session.commit()
        except Exception as exc:
            logger.warning(
                "could not write alerts",
                extra={"correlation_id": correlation_id, "error": str(exc)},
            )

    # -- introspection -----------------------------------------------------

    def status(self) -> dict[str, Any]:
        """A snapshot of the sync service's own state, for the health endpoint."""
        age = self._cache.last_sync_age()
        return {
            "polling": True,
            "poll_interval_seconds": self._settings.sync_poll_interval,
            "last_sync_age_seconds": round(age, 3) if age is not None else None,
            "degraded": self._cache.degraded,
            "stale": bool(self._cache.state.stale) if self._cache.state else False,
            "has_state": self._cache.has_state,
            "checked_at": datetime.now(UTC).isoformat(),
        }
