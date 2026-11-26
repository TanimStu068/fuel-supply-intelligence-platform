# Postmortem: the platform exhausted the simulator's connection pool

**Incident:** the platform's own event stream knocked the simulator over; the
simulator answered 500 until it was restarted
**Day:** 5 of 7 (observed) and 6 of 7 (diagnosed and fixed)
**Severity:** high
**Status:** resolved and measured

---

## Summary

The organizer's simulator keeps its state in SQLite behind a SQLAlchemy pool of
**five connections plus ten overflow**. The platform read the world on every
SSE tick event — nine endpoints at a time, and the simulator fires ticks
several times a second.

That is roughly **seventy requests a second** against a fifteen-connection
pool. The platform's own event stream knocked the simulator over: it answered
with 500s until it was restarted.

---

## Impact

| | Before | After |
|---|---|---|
| Sync cycles per minute | 42 | **20** |
| Simulator requests per minute | 425 | **204** |
| Requests per cycle | 10.1 | 10.2 |
| **Total reduction** | | **−52 %** |

Throughout the run after the fix: **no simulator errors, no 500s, the circuit
breaker closed**.

The platform itself degraded correctly — it kept serving the last-known state
with an age, marked degraded — so no wrong number reached the operator. The
damage was to the **simulator**, which is the world the whole platform depends
on.

The simulator's container was already unhealthy at the start of Day 5:
*"This was true before Day 5 — the container was already unhealthy when the day
started."*

---

## Timeline

The documented order of events.

1. The simulator's image was found to leak database connections: it exhausts
   its SQLAlchemy connection pool (`QueuePool limit of size 5 overflow 10`)
   under the backend's sustained polling, and then answers every request with
   a 500 until it is restarted.
2. The platform was reading the world on **every** `simulation.tick` event.
3. Each read was a fan-out of **nine** endpoints taken at once.
4. The simulator fires ticks several times a second, so the platform's own
   event stream produced roughly **70 requests a second** against a
   fifteen-connection pool.
5. The simulator's pool was exhausted and it began answering 500.
6. On Day 6 the crisis drill reproduced it deliberately: *"fired 30 concurrent
   GET /v1/demand-history?limit=2000 at the simulator to take its pool"*.
   Detection took **18.1 s** — the backend's own request timeout fired and the
   circuit breaker opened. Recovery took **111.4 s**. Outcome: RECOVERED.
7. The platform's polling was measured against the running stack over 60
   seconds, before and after the fix.
8. Two changes were made: a floor on the gap between two reads of the world,
   and a bounded fan-out.

---

## Root cause

### The platform had no floor on its own read rate

The platform reacted to *every* event, and the simulator emits events at its
tick rate. There was no minimum interval between two reads of the world, so a
busy simulator produced a read per tick.

Two changes were made, both measured:

| Change | What it does |
|---|---|
| `SYNC_MIN_INTERVAL` (default 3 s) | A floor on the gap between two reads of the world. The floor is on *time*, not on events — the platform still reacts to a hint at once when it has been idle, and simply does not read the world again until the floor has passed. |
| `SYNC_MAX_CONCURRENCY` (default 3) | A bounded fan-out. Nine connections taken at once from a pool of fifteen is most of it. |

The floor is on **time**, not on events, and that distinction matters: the
platform must still be able to react immediately to a hint it has not acted on
for a while. A pure rate limit would have delayed the response to a real
event.

### The catch-up read is deliberately *not* throttled

The read that follows a **stream reconnect** is not throttled. The stream has
no replay, so that read is the only way to learn what happened while the
platform was disconnected. Delaying it would leave the cache quietly wrong,
which is worse than the extra read. This is pinned by
`backend/tests/test_sync_service.py::test_a_reconnect_catch_up_is_not_throttled`.

### The dashboard made it worse, once

The first version of `GET /api/service-health` probed the simulator directly.
The dashboard polls that endpoint every ten seconds, and a live probe blocks
for the whole simulator timeout when the simulator is slow — so the health
panel became the slowest thing on the page, and the panel's own timeout left
the dashboard with *no* health information at all.

The panel now reads the **sync cache**: the cached instance says which tick the
world is at, and the sync service's own status says whether the last refresh
reached the simulator. The endpoint answers in about 5 ms instead of blocking,
and it cannot fail when the simulator is down — which is exactly when an
operator most wants to see it.

---

## How it was detected

Two separate routes, on two separate days.

**Observed (Day 5).** The simulator's container was unhealthy at the start of
the day, before any new work. The team's own reading of the logs was that the
dashboard did not make it worse: *"Every dashboard read goes to the backend's
cache, never to the simulator. The one place the dashboard used to add load was
the health panel's live probe, and that was removed in Day 5 for exactly this
reason."*

**Reproduced (Day 6).** The crisis drill has a dedicated scenario for it, with
a measured detection time, a measured recovery time and a measured memory
reading before and after. The drill fired 30 concurrent
`GET /v1/demand-history?limit=2000` at the simulator to take its pool.

---

## Resolution

| # | Fix | Where |
|---|---|---|
| 1 | `SYNC_MIN_INTERVAL` = 3 s floor on the gap between two reads | `backend/app/config.py` |
| 2 | `SYNC_MAX_CONCURRENCY` = 3 bounded fan-out | `backend/app/config.py` |
| 3 | The health panel reads the sync cache instead of probing the simulator | `backend/app/main.py` |
| 4 | The reconnect catch-up read is deliberately not throttled | `backend/app/sync_service.py` |

The circuit breaker was already in place from Day 6 Part 1: it opened after 5
consecutive failures and probes after 20 s, and during the drill it was the
thing that stopped the platform from hammering a simulator that could not
answer.

---

## What went well

- **The platform degraded correctly.** The simulator was unreachable, and the
  API kept serving the last-known state with an age and a degraded flag. The
  circuit breaker opened on its own.
- **The scenario was reproduced on purpose.** The drill has a dedicated
  scenario with measured detection (18.1 s) and recovery (111.4 s), rather
  than a paragraph describing what might happen.
- **The fix was measured on both sides.** 42 cycles / 425 requests a minute
  before, 20 / 204 after — the same workload, the same running stack.
- **The per-cycle cost was left alone.** 10.1 before, 10.2 after: *"The
  per-cycle cost is unchanged, which is the point: the platform still reads
  everything, it just reads it half as often."*
- **The organisers' simulator was left untouched.** *"The simulator itself
  unmodified"* — the leak is in the organizer's image, and the fix was on our
  side of the boundary.

## What went badly

- **The platform was reading the world at the simulator's tick rate.** There
  was no floor on the read rate at all, and the fan-out was unbounded.
- **The health panel turned an occasional problem into a constant load.** Ten
  probes a minute, each blocking for the full simulator timeout when the
  simulator was slow.
- **The container was already unhealthy before the day started.** The
  condition was inherited, not created that day, and it was still costing
  time.
- **The leak was in someone else's image, and the temptation was to treat it
  as someone else's problem.** The pool is the organizer's, but the *request
  rate* is ours.

---

## Action items

| # | Action | Status |
|---|---|---|
| 1 | `SYNC_MIN_INTERVAL` floor on the gap between two reads of the world | **Done** |
| 2 | `SYNC_MAX_CONCURRENCY` bounded fan-out | **Done** |
| 3 | The health panel reads the sync cache, not the simulator | **Done** — `backend/tests/test_dashboard_api.py::test_the_service_health_panel_does_not_block_on_the_simulator` |
| 4 | The reconnect catch-up read stays un-throttled | **Done** — `backend/tests/test_sync_service.py::test_a_reconnect_catch_up_is_not_throttled` |
| 5 | The circuit breaker opens on consecutive failures and probes after a recovery timeout | **Done** — `backend/tests/test_resilience.py` |
| 6 | Regression tests for the throttle and the fan-out cap | **Done** — `backend/tests/test_sync_service.py::test_two_cycles_in_a_row_are_spaced_by_the_minimum_interval`, `::test_the_fan_out_never_exceeds_the_configured_concurrency` |
| 7 | Report the simulator's connection-pool exhaustion to the organisers | **Not done** — the pool limit (`QueuePool limit of size 5 overflow 10`) is in the organizer's image and cannot be changed from here. The platform now fits inside it |
| 8 | A simulator-side connection metric, so pool exhaustion is visible before requests start failing | **Not done** — the platform can only observe it as failed requests and an open breaker |
