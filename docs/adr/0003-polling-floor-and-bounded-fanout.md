# ADR 0003 — Polling with a minimum interval and a bounded fan-out

**Status:** Accepted
**Date:** Day 6 of 7
**Deciders:** the team

---

## Context

[ADR 0002](0002-rest-source-of-truth-sse-as-hint.md) settled that the poller
is the source of truth and that SSE events trigger re-reads. It left the
*rate* open, and the rate turned out to matter more than expected.

The organizer's simulator keeps its state in SQLite behind a SQLAlchemy
`QueuePool` of **five connections plus ten overflow**. The platform's own
event stream was knocking it over.

Three things happened at once:

1. The platform read the world on **every** `simulation.tick` event. The
   simulator fires roughly 6.7 ticks a second.
2. Each read was a fan-out of **nine** endpoints taken at once — nine
   connections out of fifteen, most of the pool, for a single cycle.
3. The simulator's per-subscriber queue is 200 deep. The platform was so slow
   to consume the stream that it fell behind, and was **silently dropped from
   the bus** — while its stream stayed open and kept sending keepalives.

The container was already unhealthy at the start of Day 5, before any of this
work: *"This was true before Day 5 — the container was already unhealthy when
the day started."*

## Decision

**Two settings bound how often the platform reads the world.**

| Setting | Value | What it does |
|---|---:|---|
| `SYNC_MIN_INTERVAL` | 3 s | A floor on the gap between two reads of the world |
| `SYNC_MAX_CONCURRENCY` | 3 | A bounded fan-out within a single cycle |

The floor is on **time, not on events**. The platform still reacts to an SSE
hint immediately when it has been idle — it simply does not read the world
again until the floor has passed. A pure rate limit would have delayed the
response to a real event.

The **reconnect catch-up read is deliberately not throttled**. The stream has
no replay, so the read that follows a reconnect is the only way to learn what
happened while the platform was disconnected. Delaying it would leave the
cache quietly wrong, which is worse than the extra read.

## Alternatives considered

| Alternative | Why not |
|---|---|
| **No floor — read on every event** | This is what caused the incident: ~70 requests a second against a fifteen-connection pool, and the platform fell more than 200 events behind and was dropped from the bus. |
| **A pure rate limit on events** | Would delay the platform's reaction to a genuine event that arrived after a quiet period. The floor is on elapsed time so an idle platform still reacts at once. |
| **Throttle the reconnect catch-up read too** | The catch-up read is the only way to recover missed state. Throttling it would trade a transient extra request for a permanently stale cache. |
| **Cache aggressively and read rarely** | The platform already caches. The issue was the *rate of re-reading*, not the presence of a cache. |
| **Ask the organisers to raise the pool limit** | The pool is in the organizer's image and cannot be changed from here. The request rate is ours, so the request rate is what we fixed. |

## Consequences

**Good.** Measured against the running stack over 60 seconds, same workload:

| | Sync cycles | Simulator requests | Per cycle |
|---|---|---|---|
| Before | 42 | 425 | 10.1 |
| **After** | **20** | **204** | 10.2 |
| | **−52 %** | **−52 %** | — |

The per-cycle cost is unchanged, which is the point: *"The platform still
reads everything, it just reads it half as often."* Through the whole run
after the fix there were **no simulator errors, no 500s, and the circuit
breaker closed**.

**Good.** The simulator's container was healthy for the rest of the run, and
the platform stopped being the reason it was unhealthy.

**Bad.** The cache can now be up to 3 s behind the world. That is the price
paid for not exhausting the simulator's pool, and it is small next to the
5 s poll interval the floor was built on.

**Bad.** The fan-out cap means a cycle that needs nine endpoints reads them in
three at a time, so a cycle takes longer in wall-clock terms than it would
unbounded.

**Bad.** The platform is still a **polling** client. The Day 6 report names
the consequence and the fix that was not taken: *"The simulator's faults
surface on the polling interval — detection takes up to 12 s for the fault
scenarios... A faster path would be an SSE-driven fault signal."*

## Evidence

- [`docs/progress/day6.md`](../progress/day6.md) — the measured before/after
  table, the 52 % figure, and *The simulator's connection pool leaks under
  sustained polling* in the risk table
- [`docs/postmortems/04-simulator-connection-pool-exhaustion.md`](../postmortems/04-simulator-connection-pool-exhaustion.md)
  — the full account, including the drill that reproduced it
- [`docs/bug-catalog.md`](../bug-catalog.md) B50
- [`backend/app/config.py`](../../backend/app/config.py) — `SYNC_MIN_INTERVAL`,
  `SYNC_MAX_CONCURRENCY`
