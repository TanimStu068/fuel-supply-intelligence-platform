# ADR 0002 — REST is the source of truth; SSE is a hint

**Status:** Accepted
**Date:** Day 2 of 7
**Deciders:** the team

---

## Context

The simulator exposes two ways to learn about the world: REST endpoints, and
an SSE stream at `/v1/stream`.

The integration guide is explicit about the division: *"Treat REST as the
source of truth; treat SSE as a hint that something changed."* But the guide
also documents three properties of the stream that make it a poor
foundation for a cache:

1. **The subscriber queue is 200 deep.** A client that falls more than 200
   events behind is **silently dropped**, and the stream keeps flowing to
   other subscribers. A missed event is indistinguishable from a state change
   that never happened.
2. **There is no `Last-Event-ID` replay.** On reconnect the client gets
   `: connected` and then only events from that moment forward.
3. **A 15-second silence is normal** — the simulator sends `: keepalive` every
   fifteen seconds — so silence is not a disconnect signal.

The platform needs a cache, because the dashboard polls the backend every few
seconds and the simulator's connection pool is small. The question is what
fills that cache.

## Decision

**The REST poller is the source of truth. The SSE listener is a hint channel.**

- `/api/state` reads the platform's own cache and **never** calls the
  simulator.
- The poller reads the whole world on a fixed interval.
- Every SSE event triggers a re-read of **only the affected resource**, so a
  tick event refreshes the instance and the allocations rather than the world.
- Every reconnect triggers a **full REST refetch**, because the stream has no
  replay and anything that happened while the platform was away is only
  knowable from REST.
- The event payload is never trusted as state.

## Alternatives considered

| Alternative | Why not |
|---|---|
| **SSE as the source of truth** | The guide's own warning: the queue drops slow subscribers silently, and there is no replay. A platform that trusted the stream would have a cache with holes in it and no way to know. |
| **SSE only, no polling** | There would be no way to recover the state after a missed event or a silent drop, and the platform would have no independent view to check the stream against. |
| **Polling only, no SSE** | Rejected, because the simulator fires roughly 6.7 ticks a second and the poll interval is 5 s — the cache would be up to a tick behind the world, and an operator would watch the dashboard lag a crisis by several seconds. The SSE listener is what closes that gap. |
| **Trusting the event payload** | The payload is a hint about *what changed*, not the new state. Using it as state would mean acting on a value the platform had not read back. |

## Consequences

**Good.** The cache is always internally consistent — it is built from REST
reads only. There is a single way the world enters the platform, so there is
one place to look when something is wrong. The full-refetch-on-reconnect rule
means a silent SSE drop cannot corrupt the cache: it can only delay it.

**Good.** The design gave the platform an **independent view** of the world.
When the Day 6 drill stopped the simulator, the REST poller went degraded
while the SSE reader still reported a live connection — and the platform
could tell the difference, because it had a second source to compare against.
That comparison became the stale-stream detector.

**Bad.** The platform reads the world more often than it needs to. Reacting
to every tick event meant roughly **70 requests a second** against a
simulator with a fifteen-connection pool, exhausting it and making the
simulator answer 500 until it was restarted. This forced
[ADR 0003](0003-polling-floor-and-bounded-fanout.md).

**Bad.** The cache can be up to one poll interval behind. The Day 6 drill
records the consequence: *"The simulator's faults surface on the polling
interval — detection takes up to 12 s for the fault scenarios."* The report
notes that this is an upper bound, because the drill polls every 2 s and the
circuit breaker reacts faster, and it names the fix that was not taken: *"A
faster path would be an SSE-driven fault signal."*

**Bad.** The `X-Simulator-Stale: true` header has to be honoured on every
response, and the flag is sticky — a snapshot built from several concurrent
requests reports stale if *any* component was stale, because a per-response
flag would be racy.

## Evidence

- [`docs/progress/day2.md`](../progress/day2.md) — *Background sync: REST poll
  as source of truth*, *SSE treated as a hint, re-GET on every event*, and the
  risk table
- [`docs/postmortems/01-silent-dead-sse-feed.md`](../postmortems/01-silent-dead-sse-feed.md)
  — the silent dead feed this decision's independence made detectable
- [`docs/postmortems/04-simulator-connection-pool-exhaustion.md`](../postmortems/04-simulator-connection-pool-exhaustion.md)
  — the cost of reacting to every tick
- [`docs/simulator-notes.md`](../simulator-notes.md) D7 — the stale header is
  only on GET responses
