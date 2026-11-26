# Postmortem: the platform believed it had a live event feed

**Incident:** the SSE reader stayed "connected" after the simulator was stopped
**Day:** 6 of 7 (Part 2 — the crisis drill)
**Severity:** high
**Status:** resolved and verified

---

## Summary

With the simulator stopped, the platform's `sse_connections_total` stayed at
**1** and `sse_reconnects_total` never moved — while the REST poller correctly
went degraded. The platform believed it had a live event feed long after the
feed was gone.

The cause was **three separate bugs**, and all three had to be fixed for the
platform to notice a dead simulator at all.

---

## Impact

| | Before | After |
|---|---|---|
| SSE connections opened in 25 minutes | 17 | **1** |
| SSE events received | 15 per connection | **742 in 3 minutes** |
| Stale-stream drops on a *healthy* simulator | one every ~50 s | **0** |
| Time to notice a stopped simulator | never | **~40 s** |

The simulator's event queue is two hundred deep, and the simulator
**unsubscribes** a client whose queue overflows. The platform was therefore
dropped from the bus entirely, while the stream stayed open, kept sending
keepalives, and delivered nothing — a live-looking feed that was permanently
silent.

The REST poller is the platform's source of truth, so **no wrong number was
served**; the platform degraded correctly. The loss was the *fast path*: the
platform was reacting on a five-second poll interval instead of on the event
that had just happened, and it did not know the difference.

---

## Timeline

The documented order of events. No timestamps are invented here; the drill
records per-scenario timings in [`docs/resilience-report.md`](../resilience-report.md),
which was generated `2026-11-24T19:45:15.381050+00:00`.

1. The platform ran with the simulator healthy, reading the world on every
   `simulation.tick` event.
2. Each event triggered a full sync cycle, and the reader **awaited** it. The
   simulator runs eight ticks a second and a cycle reads nine endpoints, so the
   reader spent its life inside a sync cycle.
3. The reader's throughput fell to about **fifteen events per fifty
   seconds**.
4. The simulator's per-subscriber queue (depth 200) overflowed, and the
   simulator **unsubscribed** the platform from the bus.
5. The stream stayed open and kept sending keepalives. The platform read the
   keepalives as a live feed.
6. In the crisis drill, the simulator was stopped. The REST poller went
   degraded — correctly. The SSE reader did not notice: the connection count
   stayed at 1 and the reconnect counter never moved.
7. The drill surfaced the contradiction: a degraded poller beside a
   "connected" stream.
8. The three causes were separated and fixed in turn.

---

## Root cause

### 1. The reader blocked on the sync

Every `simulation.tick` event triggered a full sync cycle, and the reader
*awaited* it. The simulator runs eight ticks a second and a cycle reads nine
endpoints, so the reader spent its life inside a sync cycle and read about
**fifteen events per fifty seconds**. The simulator's event queue is two
hundred deep, and the simulator **unsubscribes** a client whose queue
overflows — so the platform was dropped from the bus entirely. The stream
stayed open, kept sending keepalives, and delivered nothing.

The refreshes are now *scheduled* rather than awaited, so the reader never
stops reading.

### 2. The read timeout was unbounded

`httpx.Timeout(read=None)` means a socket whose far end has gone away produces
neither data nor an error: the reader waits forever on a connection that will
never answer. The simulator sends a keepalive every fifteen seconds, so a
stream silent for longer than that is *dead*, not quiet.

The read timeout is now a bounded `SSE_READ_TIMEOUT` (30 s), configured in
[`backend/app/config.py`](../../backend/app/config.py).

### 3. A live stream can still be lying

The dead simulator's queue keeps delivering buffered events for minutes, each
one resetting every read timeout — so *"bytes are still arriving"* proves
nothing. The stream's own tick is the evidence: if the platform's independent
REST view says the world has moved on and the stream has not, the stream is
describing a world that no longer exists.

[`_sse_stream_is_stale()`](../../backend/app/sync_service.py) compares the two
and drops the stream when it has fallen behind for longer than
`SSE_STALE_GRACE` (45 s).

---

## How it was detected

By the **crisis drill** — [`scripts/crisis_drill.py`](../../scripts/crisis_drill.py),
run as Day 6 Part 2. The drill stopped the simulator and read the backend's own
`/metrics`. The contradiction was visible in two counters side by side: the
poller was degraded while `sse_connections_total` was pinned at 1 and
`sse_reconnects_total` had never moved.

Nothing in the unit suite could have found it: the reader behaves correctly
against a simulator that is alive.

---

## Resolution

| # | Fix | Where |
|---|---|---|
| 1 | Refreshes are scheduled, not awaited | `backend/app/sync_service.py` |
| 2 | `SSE_READ_TIMEOUT` = 30 s (was `read=None`) | `backend/app/config.py` |
| 3 | `_sse_stream_is_stale()` drops a stream that has fallen behind the world for longer than `SSE_STALE_GRACE` = 45 s | `backend/app/sync_service.py` |

The stale-stream detector also needed a clock it could trust: a stream that
briefly falls behind and then catches up must not be punished later for a lag
it has already recovered from, so the "behind" clock is reset the moment the
stream catches up.

`ML_KILL_SWITCH_FALLBACKS` was found in the same pass — **defined but never
incremented**, so the counter that says how much of the platform's output the
kill-switch pushed onto the rule-based predictor always read zero. It is now
incremented where the fallback actually happens.

---

## What went well

- **The drill was designed to break the platform on purpose.** The scenario
  that found this was not a lucky accident; it was the point of the exercise.
- **The REST poller was already the source of truth.** The architecture
  decision from Day 2 — the SSE stream is a *hint*, the values always come from
  REST — meant no wrong number reached the operator.
- **The platform's own metrics exposed the contradiction.** Both counters
  already existed; nobody had looked at them together.
- **The fix is measured, not asserted.** Before and after numbers were taken
  on the running stack and are in [`day6.md`](../progress/day6.md).

## What went badly

- **A connected socket was treated as proof of life.** The reader's only
  evidence of a working feed was bytes arriving, and the dead simulator's
  queue kept sending bytes for minutes.
- **The unbounded read timeout was invisible in review.** `read=None` looks
  like a reasonable "wait forever" setting; it is only wrong for a stream.
- **The damage was silent and slow.** Nothing errored. The symptom was only a
  counter that did not move.
- **Three independent bugs sat behind one symptom.** Fixing the first one
  (the blocking reader) would have made the platform *look* better while the
  other two remained. Only the drill's measurement separated them.

---

## Action items

| # | Action | Status |
|---|---|---|
| 1 | Schedule refreshes instead of awaiting them in the SSE reader | **Done** |
| 2 | Bound the SSE read timeout (`SSE_READ_TIMEOUT`, 30 s) | **Done** |
| 3 | Detect a stale stream against the independent REST view | **Done** |
| 4 | Reset the "behind" clock when the stream catches up | **Done** |
| 5 | Increment `ML_KILL_SWITCH_FALLBACKS` where the fallback happens | **Done** |
| 6 | Regression tests for the reader and the stale-stream detector | **Done** — `backend/tests/test_sync_service.py::test_the_sse_reader_does_not_wait_for_the_sync`, `::test_a_stream_that_falls_behind_the_world_is_dropped`, `::test_a_stream_that_catches_up_is_not_dropped`, `::test_a_stream_at_the_world_tick_is_never_stale` |
| 7 | Add a fault-injection scenario that kills the simulator *without* stopping the container, so the pool-exhaustion path and the stream path are distinguished | **Not done** — the two are still distinguished only by reading the counters |
| 8 | An SSE-driven fault signal, so a simulator fault surfaces immediately instead of on the polling interval | **Not done** — recorded as a risk in [`day6.md`](../progress/day6.md): *"A faster path would be an SSE-driven fault signal"* |
