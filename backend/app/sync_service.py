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
    SYNC_CYCLES_COALESCED,
)
from .repository import (
    max_demand_tick,
    truncate_demand_history,
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
    FaultInjectedError,
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
        intelligence: Any | None = None,
    ) -> None:
        self._client = client
        self._database = database
        self._cache = cache
        self._settings = settings or get_settings()
        self._intelligence = intelligence
        self._tasks: list[asyncio.Task[None]] = []
        self._stopping = asyncio.Event()
        #: Set by the SSE loop so the poller can shorten its interval when
        #: something just changed.
        self._dirty = asyncio.Event()
        #: The lock that serialises sync cycles. Only one cycle may run at
        #: a time: two overlapping cycles would double the load on the
        #: simulator for no benefit, because the second one reads the same
        #: world the first one is already reading.
        self._sync_lock = asyncio.Lock()
        #: The highest simulator tick the event stream has carried.
        #:
        #: This is how a live stream is told apart from a dead one. A dead
        #: simulator's queue keeps delivering buffered events for minutes
        #: after the simulator has gone, so "bytes are still arriving"
        #: proves nothing. The stream's own tick is the evidence: if the
        #: platform's REST view says the world has moved on and the stream
        #: has not, the stream is describing a world that no longer
        #: exists.
        self._sse_highest_tick = 0
        #: When the stream was first seen to be behind the world.
        self._sse_behind_since: float | None = None
        #: The refreshes the SSE reader has scheduled but not waited for.
        #: The tasks are held so they cannot be garbage-collected while
        #: they are still running.
        self._refresh_tasks: set[asyncio.Task[None]] = set()
        #: How many sync requests have been made. A cycle settles every
        #: request that was outstanding when it started.
        self._sync_requests = 0
        #: The highest request number a completed cycle has settled.
        self._sync_completed = 0
        #: Set by the worker when it has settled the pending requests.
        self._sync_settled = asyncio.Event()
        #: The worker task that does the reading, so a burst of requests
        #: collapses into one cycle rather than one cycle each.
        self._sync_worker: asyncio.Task[None] | None = None
        #: When the last cycle started. The throttle measures the gap
        #: against this, so a burst of hints arriving back to back does
        #: not turn into a burst of full reads of the world.
        self._last_cycle_started: float = 0.0
        #: The cap on how many simulator calls may be in flight at once.
        self._semaphore = asyncio.Semaphore(self._settings.sync_max_concurrency)

    # -- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        """Start the poller, the SSE listener and the model trainer."""
        self._stopping.clear()
        self._tasks = [
            asyncio.create_task(self._poll_loop(), name="sync-poller"),
            asyncio.create_task(self._sse_loop(), name="sync-sse"),
            asyncio.create_task(self._trainer_loop(), name="model-trainer"),
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
            # The poller goes through the same coalescing path as the SSE
            # listener, so a poll cycle and an event-driven cycle can never
            # overlap. Two overlapping cycles would double the load on the
            # simulator for no benefit.
            await self._request_sync()
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

    async def _throttle(self) -> None:
        """Wait out the minimum gap between two sync cycles.

        **Why this exists.** The simulator publishes a ``simulation.tick``
        event several times a second. Every one of them triggers a read of
        the world, and a read is nine endpoints. Sustained polling like
        that exhausts the simulator's connection pool — and an exhausted
        pool is a simulator that answers with 500s until it is restarted.

        The throttle is a floor on *time*, not a filter on events: the
        platform still reacts to a hint at once when it has been idle, and
        simply does not read the world again until the floor has passed.
        The floor is what keeps the simulator's pool alive.
        """
        floor = self._settings.sync_min_interval
        if floor <= 0:
            return

        elapsed = time.monotonic() - self._last_cycle_started
        remaining = floor - elapsed
        if remaining <= 0:
            return

        logger.debug(
            "sync cycle throttled",
            extra={"floor_seconds": floor, "waited_seconds": round(remaining, 3)},
        )
        await asyncio.sleep(remaining)

    async def forget_the_previous_world(self) -> None:
        """Drop the cached state and the stream's tick trackers.

        The simulator's clock went backwards — the world was reset — and
        the platform is still holding the *previous* world's snapshot. Left
        alone it would serve that snapshot as fresh until the next sync
        cycle, which is up to fifteen seconds away: numbers from a run
        that has ended, presented as the current state.

        Dropping the cache is the honest answer. The API answers 503
        ("no simulator state available yet") until the next cycle fetches
        the new world, which is exactly what it says.

        The stream's tick tracker is reset for the same reason. The event
        stream carries the old world's ticks, and a tracker left at tick
        900 would call the new world's stream permanently stale — the
        platform would drop a healthy stream every few seconds and
        reconnect, for ever.
        """
        self._cache.invalidate()
        self._sse_highest_tick = 0
        self._sse_behind_since = None
        logger.info("the cached state was dropped; the world was reset")

    async def run_sync_cycle(self, *, throttled: bool = True) -> bool:
        """Fetch the whole world, store it, and mirror it into Postgres.

        Returns True when the cycle succeeded. A failed cycle never raises:
        the service must keep running while the simulator is down.

        ``throttled=False`` skips the minimum-gap wait. It is used for the
        read that follows a **stream reconnect**: the stream has no replay,
        so that read is the only way to learn what happened while the
        platform was disconnected. It is also rare, so it costs nothing to
        make it immediate — whereas delaying it would leave the cache
        quietly wrong.
        """
        # The throttle comes first: the gap is measured against the start
        # of the *previous* cycle, so the floor applies between two reads
        # of the world rather than between the request and the read.
        if throttled:
            await self._throttle()

        correlation_id = bind_correlation_id(new_correlation_id("sync"))
        started = time.perf_counter()
        self._last_cycle_started = time.monotonic()
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
        except FaultInjectedError as exc:
            # The simulator is deliberately misbehaving — a fault was
            # injected into it. It is not the same as the dependency
            # being broken, so it does not trip the circuit breaker, but
            # the platform must not pretend the data it is holding is
            # current: the sync did not reach the world, so the cached
            # state is served *degraded*, with its age, exactly as it
            # would be if the simulator had vanished.
            #
            # Without this, the sync cycle falls through to the generic
            # ``SimulatorAPIError`` branch, which records a health row
            # but leaves the cache looking fresh — so the dashboard
            # shows live-looking numbers that are minutes old and
            # nothing anywhere says so.
            logger.warning(
                "sync cycle hit an injected simulator fault; serving last-known state",
                extra={"correlation_id": correlation_id, "error": str(exc)},
            )
            await self._handle_simulator_unreachable(str(exc), correlation_id)
            SYNC_CYCLES.labels(outcome="faulted").inc()
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

        if self._cache.rewound:
            # The simulator's clock went backwards: the world was reset.
            # The event stream's tick tracker has to be reset too — it is
            # carrying the *old* world's ticks, and a tracker left at tick
            # 900 would call the new world's stream permanently stale and
            # drop it every few seconds.
            self._sse_highest_tick = 0
            self._sse_behind_since = None
            logger.warning(
                "the simulator was reset; the event stream's tick tracker was reset",
                extra={
                    "correlation_id": correlation_id,
                    "incoming_tick": snapshot.instance.tick,
                },
            )

        # Each step of the cycle is timed separately. The whole cycle's
        # duration is dominated by one step, and without this split there
        # is no way to tell which one — the cycle is simply "slow".
        step_started = time.perf_counter()
        await self._persist(snapshot, correlation_id)
        persist_seconds = time.perf_counter() - step_started

        step_started = time.perf_counter()
        await self._ingest_demand_history(correlation_id)
        history_seconds = time.perf_counter() - step_started

        step_started = time.perf_counter()
        await self._raise_alerts(snapshot, correlation_id)
        alerts_seconds = time.perf_counter() - step_started

        step_started = time.perf_counter()
        await self._evaluate_intelligence(correlation_id)
        intelligence_seconds = time.perf_counter() - step_started

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
                "persist_seconds": round(persist_seconds, 4),
                "history_seconds": round(history_seconds, 4),
                "alerts_seconds": round(alerts_seconds, 4),
                "intelligence_seconds": round(intelligence_seconds, 4),
            },
        )
        return True

    async def _guarded(self, call: Any) -> Any:
        """Run one simulator call inside the fan-out semaphore.

        The simulator's connection pool holds fifteen connections and a
        cycle reads nine endpoints. Taking all nine at once works right
        up until the day something else needs a connection — and then the
        pool is exhausted and the simulator answers with 500s. The
        semaphore keeps the platform inside the pool it is reading from.
        """
        async with self._semaphore:
            return await call

    async def _fetch_world(self) -> CachedState:
        """Read every section of the world, with a bounded fan-out.

        The nine reads are made concurrently because they are independent
        and each one is a round trip to the simulator. The concurrency is
        capped at ``sync_max_concurrency``: nine connections taken at once
        from a pool of fifteen is already most of it, and a second cycle
        starting before the first has finished would take the rest.
        """
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
            self._guarded(self._client.get_instance()),
            self._guarded(self._client.get_regions()),
            self._guarded(self._client.get_depots()),
            self._guarded(self._client.get_stations()),
            self._guarded(self._client.get_routes()),
            self._guarded(self._client.get_supply_arrivals()),
            self._guarded(self._client.get_events()),
            self._guarded(self._client.get_allocations()),
            self._guarded(self._client.get_metrics()),
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
        """Mirror the snapshot into Postgres. Never raises.

        This goes through the retrying write helper: it writes the
        allocation ledger and the domain events while an operator may be
        approving a recommendation at the same time, and that pair
        deadlocks on the allocation rows. Postgres kills one side of a
        deadlock with ``40001``, which the SQL standard says the
        application should retry.
        """

        async def work(session) -> None:
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

        try:
            await self._database.write(work)
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
                    # A tick event carries the simulator's own tick number.
                    #: Remember the highest one the stream has carried, so
                    #: the loop can tell a live stream from a dead one.
                    if event.is_tick:
                        tick = event.data.get("tick")
                        if isinstance(tick, int) and tick > self._sse_highest_tick:
                            self._sse_highest_tick = tick
                    await self._handle_event(event)

                    # The stream can be *alive* and still be telling lies.
                    # A dead simulator's event queue holds up to two
                    #: hundred buffered events, and they keep arriving for
                    #: minutes after the simulator has gone — each one
                    #: resetting every read timeout. The platform would
                    #: carry on processing events from a dependency that
                    #: had already died, and would never reconnect.
                    #:
                    #: So the stream is checked against the world it
                    #: claims to describe. If the simulator has moved on
                    #: but the stream's tick has not, the stream is stale
                    #: and is dropped so the loop reconnects.
                    if await self._sse_stream_is_stale():
                        raise SimulatorTimeoutError(
                            "the simulator event stream is behind the world; reconnecting"
                        )

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
            # This read is not throttled — it is the catch-up for the
            # window the platform was not listening for.
            await self.run_sync_cycle(throttled=False)

    def _reconnect_delay(self, attempt: int) -> float:
        """Exponential backoff with jitter between reconnect attempts."""
        base = self._settings.sse_reconnect_base_delay
        ceiling = min(base * (2 ** min(attempt - 1, 6)), self._settings.sse_reconnect_max_delay)
        return max(0.1, random.uniform(0, ceiling))

    async def _request_sync(self) -> None:
        """Ask for a sync, collapsing a burst of requests into one cycle.

        **Why this exists.** The simulator publishes a ``simulation.tick``
        event several times a second. The first version of the SSE handler
        ran a *full* sync cycle on every one of them, and a full cycle reads
        nine endpoints. At eight ticks a second that is roughly seventy
        requests a second against a simulator whose database pool holds
        fifteen connections — so the platform's own event stream exhausted
        the simulator's pool and took it down.

        **How the collapsing works.** A single background *worker* task does
        the reading. Callers only raise a counter and wait. The worker takes
        the counter's value when it starts a cycle, and that cycle settles
        every request outstanding at that moment. The requests that arrive
        *during* the cycle are settled by the next one — so a burst of ten
        ticks produces one cycle, and a burst that keeps arriving produces
        one cycle at a time, never a queue of them.

        The result is that the simulator sees at most one cycle in flight
        and one pending, whatever the tick rate.

        **Why the caller does not wait.** The caller is the SSE reader, and
        the reader is the one thing that must stay responsive. If it waited
        for the cycle to finish, it would stop reading the stream while the
        cycle ran — and the simulator's event queue is only two hundred deep.
        A reader that stops reading fills that queue, and the simulator then
        *unsubscribes* the platform: the stream stays open, keeps sending
        keepalives, and delivers nothing but the truth about a world that
        has moved on. The platform would be looking at a live-looking but
        permanently silent feed.
        """
        self._sync_requests += 1
        my_generation = self._sync_requests

        while self._sync_completed < my_generation:
            # Clear before starting the worker, so a worker that finishes
            # immediately cannot set the flag before we wait on it.
            self._sync_settled.clear()
            if self._sync_worker is None or self._sync_worker.done():
                self._sync_worker = asyncio.create_task(
                    self._sync_worker_loop(), name="sync-worker"
                )
            await self._sync_settled.wait()

    async def _sync_worker_loop(self) -> None:
        """Read the world until no further sync is wanted."""
        # One turn of the event loop first, so every request that is
        # already pending registers before the first cycle starts. This is
        # what turns "ten ticks at once" into one cycle rather than two.
        await asyncio.sleep(0)

        while not self._stopping.is_set():
            # Everything requested up to this moment is settled by the
            # cycle we are about to run.
            target = self._sync_requests
            await self.run_sync_cycle()
            self._sync_completed = max(self._sync_completed, target)
            self._sync_settled.set()

            # If nothing arrived while we were reading, we are done.
            if self._sync_requests <= target:
                return

            # Requests that arrived *during* the cycle were collapsed into
            # the next one instead of each getting their own.
            SYNC_CYCLES_COALESCED.inc(self._sync_requests - target)

    async def _sse_stream_is_stale(self) -> bool:
        """Whether the event stream has fallen behind the world it describes.

        The simulator's event stream is a *hint* channel, and the hint is
        only useful while it is current. A dead simulator's queue holds up
        to two hundred buffered events, and they keep arriving for minutes
        after the simulator has gone — so a stream can look perfectly
        alive (bytes are still flowing) while describing a world that no
        longer exists.

        The check compares the stream against the platform's own REST view
        of the world, which is read on its own schedule and is therefore
        independent of the stream:

        * If the REST view says the simulator is at tick *N* and the
          stream has not reached *N* after a grace period, the stream is
          behind and is dropped so the loop reconnects.
        * If the REST view cannot be read at all, the simulator is down
          and the stream cannot be trusted either.

        The grace period matters. The stream and the REST view are read at
        different moments, so they are briefly out of step by design; a
        stream that is a few ticks behind is normal.
        """
        state = self._cache.state
        if state is None:
            return False

        live_tick = state.instance.tick
        if live_tick <= self._sse_highest_tick:
            # The stream is at or ahead of the world the platform can see.
            # Any clock that was running stops here: a stream that caught up
            # has just proved it is alive, and it must not be punished later
            # for a lag it has already recovered from.
            self._sse_behind_since = None
            return False

        # The stream is behind. Give it a moment before calling it dead.
        if self._sse_behind_since is None:
            self._sse_behind_since = time.monotonic()
            return False
        if time.monotonic() - self._sse_behind_since < self._settings.sse_stale_grace:
            return False

        logger.warning(
            "the simulator event stream fell behind the world; dropping it",
            extra={
                "stream_tick": self._sse_highest_tick,
                "world_tick": live_tick,
            },
        )
        self._sse_behind_since = None
        self._sse_highest_tick = 0
        return True

    async def _handle_event(self, event: StreamEvent) -> None:
        """React to an SSE event by re-reading the affected resource.

        The event payload is only a hint. The simulator explicitly documents
        that the queue can drop events, so the payload is never written to
        the database directly — it decides *what to re-read*.

        **This must not block the reader.** The SSE reader is the one thing
        that has to stay responsive, because the simulator's event queue is
        only two hundred deep and the simulator *unsubscribes* a client
        whose queue overflows. A reader that waits for a sync cycle to
        finish stops reading while the cycle runs, the queue fills, and the
        feed goes silently dead — while still looking perfectly alive, because
        the socket stays open and the simulator keeps sending keepalives.

        So the refreshes are *scheduled*, not awaited: the request is raised
        and the reader moves straight on to the next event. The work still
        happens, in the same order, and it is still collapsed into one
        cycle — it just no longer holds the reader hostage while it runs.
        """
        try:
            if event.is_tick:
                self._schedule_refresh(self._request_sync())
                return

            if event.is_allocation:
                self._schedule_refresh(self._refresh_allocations())
                return

            if event.is_inventory:
                self._schedule_refresh(self._refresh_inventory(event.entity_id))
                return

            if event.is_notice:
                # A notice means the simulator itself did something (a reset,
                # or a background-runner exception). Re-read the whole world.
                #
                # The simulator announces a reset on the stream ("Simulation
                # reset"), and that announcement is the only *prompt* signal
                # the platform gets that the world's clock went backwards.
                # The cached state is dropped before the re-read, so there is
                # no window in which the previous world's numbers are
                # served as fresh — the platform says "no state yet" until
                # the new world is fetched.
                self._schedule_refresh(self._handle_notice())
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

    async def _handle_notice(self) -> None:
        """React to a simulator notice: drop the stale world, then re-read.

        A notice carries no payload the platform can act on — it only says
        the simulator did something to itself. The reset is the case that
        matters, because the world's clock went backwards and the cache is
        holding a snapshot of a world that has ended.
        """
        await self.forget_the_previous_world()
        await self._request_sync()

    def _schedule_refresh(self, coro) -> None:
        """Start a refresh without waiting for it, and keep it alive.

        The coroutine is scheduled on the running loop and its task is
        held until it finishes, so it cannot be garbage-collected
        half-way through — which would leave a sync cycle half-done and
        the cache half-written.

        A refresh that raises is logged and dropped. The reader is not
        allowed to die because a *refresh* failed: the refresh is the
        optional part, and the reader is the part that has to keep working.
        """
        task = asyncio.create_task(self._run_refresh(coro), name="sse-refresh")
        self._refresh_tasks.add(task)
        task.add_done_callback(self._refresh_tasks.discard)

    async def _run_refresh(self, coro) -> None:
        """Run one refresh, swallowing and logging whatever it raises."""
        try:
            await coro
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "an sse-triggered refresh failed",
                extra={"error": str(exc)},
            )

    async def _await_refreshes(self) -> None:
        """Wait for the scheduled refreshes to finish.

        Used when the service is stopping, and by the tests: a refresh is
        *scheduled* rather than awaited so the SSE reader is never held up
        by it, which means there is no single point at which "the refresh
        has happened" can be observed.
        """
        while self._refresh_tasks:
            pending = list(self._refresh_tasks)
            await asyncio.gather(*pending, return_exceptions=True)

    async def _refresh_allocations(self) -> None:
        """Re-read the allocation ledger and mirror it.

        This goes through the retrying write helper: it writes the same
        allocation rows an approving operator is writing, so the two
        deadlock on each other under load.
        """
        allocations = await self._client.get_allocations()

        async def work(session) -> None:
            await write_allocations(session, allocations)

        try:
            await self._database.write(work)
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

        # A simulator reset restarts the clock at tick 0, so the stored
        # history belongs to a different timeline.
        await self._drop_history_if_the_world_was_reset(
            correlation_id=correlation_id,
            current_tick=observations[0].tick,
        )

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

    async def _trainer_loop(self) -> None:
        """Retrain the learned demand model on a slow schedule.

        Training is not cheap and the history changes slowly, so the model
        is retrained every ``model_train_interval`` seconds rather than on
        every tick. Until the first training completes, the rule-based
        baseline answers every forecast.
        """
        interval = max(60, self._settings.model_train_interval)
        while not self._stopping.is_set():
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=interval)
                return
            except TimeoutError:
                pass

            if self._intelligence is None:
                continue

            try:
                outcome = await self._intelligence.train_model()
            except Exception as exc:
                logger.warning("model training failed: %s", exc)
                continue

            if outcome.get("trained"):
                logger.info(
                    "learned demand model retrained",
                    extra={
                        "training_rows": outcome.get("training_rows"),
                        "validation_mae": outcome.get("validation_mae"),
                    },
                )

    async def _evaluate_intelligence(self, correlation_id: str) -> None:
        """Run the intelligence layer over the state we just fetched.

        A failure here must never stop the sync: the platform still serves
        the data, it just does not have fresh forecasts.
        """
        if self._intelligence is None:
            return
        try:
            result = await self._intelligence.evaluate_and_store()
        except Exception as exc:
            logger.warning(
                "intelligence evaluation failed",
                extra={"correlation_id": correlation_id, "error": str(exc)},
            )
            return

        if result is None:
            return

        critical = len(result.critical_risks)
        if critical:
            logger.warning(
                "shortage risk detected",
                extra={
                    "correlation_id": correlation_id,
                    "critical": critical,
                    "watch": len(result.watch_risks),
                    "models_used": result.models_used,
                },
            )

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

        # A simulator reset restarts the clock at tick 0. The stored
        # history belongs to the old timeline, and keeping it would make
        # two different runs look like one continuous series — with the
        # new run's tick 1 sitting right next to the old run's tick 1.
        await self._drop_history_if_the_world_was_reset(
            correlation_id=correlation_id,
            current_tick=observations[0].tick,
        )

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

    async def _drop_history_if_the_world_was_reset(
        self,
        *,
        correlation_id: str,
        current_tick: int,
    ) -> None:
        """Wipe the stored history when the simulator's clock went backwards.

        ``POST /admin/reset`` deletes the simulator's world and starts the
        clock again at tick 0. The stored demand history is from the old
        run, and the tick numbers overlap, so the forecaster would read the
        two runs as one series with a discontinuity in the middle — and
        learn the wrong thing from it.

        The check is deliberately conservative: the history is only dropped
        when the stored maximum tick is *ahead* of the tick we are looking
        at, which is what a reset looks like from here.
        """
        try:
            async with self._database.session() as session:
                stored_max = await max_demand_tick(session)
                if stored_max is None or stored_max <= current_tick:
                    return

                removed = await truncate_demand_history(session)
                await session.commit()
        except Exception as exc:
            logger.warning(
                "could not check for a simulator reset",
                extra={"correlation_id": correlation_id, "error": str(exc)},
            )
            return

        logger.warning(
            "the simulator was reset; the stored demand history was dropped",
            extra={
                "correlation_id": correlation_id,
                "stored_max_tick": stored_max,
                "current_tick": current_tick,
                "rows_removed": removed,
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

        async def work(session) -> None:
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

        try:
            await self._database.write(work)
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
