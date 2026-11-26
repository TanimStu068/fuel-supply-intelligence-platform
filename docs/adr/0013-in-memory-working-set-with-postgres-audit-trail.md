# ADR 0013 — An in-memory working set, with Postgres as the audit trail

**Status:** Accepted
**Date:** Day 5 of 7
**Deciders:** the team

---

## Context

The dashboard's recommendations page needs the **live** recommendations —
the ones an operator can act on right now. The platform also needs a durable
**history** — every decision ever made, for audit and for the history page.

Those are different lifetimes. A live recommendation is meaningful until it
is acted on or superseded by the next sync cycle. A decision record is
meaningful forever.

The first implementation kept **both** in one in-memory store. It had no
bound.

The consequence was measured. The Day 5 log records it plainly: *"The
decision history grows without bound → The recommendations page grows without
bound."* The store reached **three thousand recommendations and twenty-two
megabytes**, and every call to `/api/recommendations` serialised **all of
it** — an endpoint the dashboard polls every five seconds.

The root cause is structural. The background sync **proposes on every
cycle**, and each proposal carries a full projection, its signals, its
constraints and its alternatives. That is a rich object, and it is rich on
every cycle.

## Decision

**Two stores with two lifetimes.**

| | Live working set | Audit trail |
|---|---|---|
| **Where** | In-memory, in the decision service | Postgres |
| **Bound** | `DECISION_HISTORY_LIMIT` recommendations | Unbounded (no retention job yet) |
| **Served by** | `/api/recommendations` | `/api/decision-history` |
| **Purpose** | What the operator can act on now | What happened, and when |

The working set is **bounded**. The source says the limit plainly: *"A limit
of zero means unbounded, which is how the 'before' side of the load test's
second bottleneck is reproduced."* — so the unbounded path is still in the
code, deliberately, as a reproducible baseline.

The recommendations page is **also** paginated. The **frontend** renders
**twelve cards** at a time (`PAGE_SIZE = 12` in
`frontend/src/views/RecommendationsView.tsx`) with a count in the header and a
"Show 12 more" button — *"the dashboard draws a dozen cards at a time, and
the rest are one click away"*. The **backend** has a separate page cap of
`min(100, DECISION_HISTORY_LIMIT)`, and no cap at all when the working set
is unbounded.

## Alternatives considered

| Alternative | Why not |
|---|---|
| **One unbounded in-memory store** | This is what it did. 3,000 recommendations and 22 MB, serialised in full on every dashboard poll, every five seconds. |
| **Everything in Postgres, read the live set from there** | The live recommendations are read on the dashboard's five-second poll. Reading them from the database means a query and a serialization per poll, for data that is by definition short-lived. |
| **Keep only the latest recommendation per station/fuel** | Tempting, and it bounds the store naturally. Rejected because the working set's purpose is to show the operator what has been proposed — and an operator looking at the page needs to see the proposals in context, not one per station overwritten. |
| **No bound, but paginate the API** | Pagination bounds the *response*, not the *store*. The 22 MB was still resident, and the store still grew for the life of the process. Both are needed: the bound stops the growth, and the pagination stops the response. |
| **Push the working set to Redis** | A third store on a host with a 1.5 GB budget, for data that is short-lived and single-process. The Day 4 risk table already noted *"Postgres is a single point of failure"*, and the platform's answer to that was to serve from the cache — not to add a dependency. |

## Consequences

**Good.** The store's memory is bounded, and the bound is **configurable** —
`DECISION_HISTORY_LIMIT` can be turned up or down without a code change. A
limit of zero reproduces the unbounded behaviour for comparison.

**Good.** The dashboard's response is bounded at **twelve cards** by the
frontend, so the five-second poll does not grow with the store. The first
live run had **243 open recommendations** and rendered every one of them —
a 6 MB screenshot — and the fix brought it to 484 KB.

**Good.** The **audit trail is durable and complete**. Every decision ever
proposed, approved, rejected or submitted is in Postgres, indexed on status,
station and tick, and served from `/api/decision-history`. The working set
can be cleared without losing the record — and there is an endpoint that does
exactly that.

**Good.** The clear endpoint returns how many recommendations went, which
makes the boundary between the two stores observable rather than implicit.

**Bad.** The working set is **per-process**. It does not survive a restart,
and it is not shared between replicas. The platform runs a single backend
process, so this is currently correct — but it is a constraint, not a
property.

**Bad.** Clearing the working set **loses the live recommendations** that an
operator might have been about to act on. The endpoint is explicit about it
and returns the count, so it is a deliberate act.

**Bad.** The audit trail **still grows without bound**. The Day 4 log
recorded *"Pruning is Day 7 work"*, and no retention job was written. It is
indexed, so the cost is disk rather than latency, but it is open.

**Bad.** The limit is a **count**, not a size. 3,000 recommendations reached
22 MB, so the count is a proxy for memory; the actual resident size depends
on how rich each proposal is. The source says so: *"Left alone it reached
three thousand recommendations and twenty-two megabytes."*

**Bad.** A recommendation that is evicted from the working set is still
approved by its id, because the approval path reads from the audit trail —
but it is no longer visible in the live list. The two views of a
recommendation can therefore differ.

## Evidence

- [`backend/app/decision/service.py`](../../backend/app/decision/service.py)
  — the working-set docstring, `_history_limit`, and the clear endpoint
- [`docs/progress/day5.md`](../progress/day5.md) — *The decision history grows
  without bound*, and the 3,000 / 22 MB figures
- [`docs/progress/day4.md`](../progress/day4.md) — the risk table on storage
  growth, and *"Pruning is Day 7 work"*
- [`docs/load-test-report.md`](../load-test-report.md) — the unbounded path as
  the reproducible "before" of the second bottleneck
- [`docs/architecture.md`](../architecture.md) §3, §6
