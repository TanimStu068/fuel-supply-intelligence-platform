# ADR 0007 — Deterministic idempotency keys derived from the recommendation

**Status:** Accepted
**Date:** Day 4 of 7
**Deciders:** the team

---

## Context

The simulator's only domain write is `POST /v1/allocations`, and it takes an
`idempotency_key` in the **body** — not in a header. The guide's rules are
strict:

- Same key, same body → return the existing allocation, HTTP **201**. Safe
  retry.
- Same key, **different** body → **409 `IDEMPOTENCY_KEY_MISMATCH`**. The
  first submission wins.
- *"Cancellation does NOT free the key. Once used, an idempotency_key is
  permanently occupied."*

The platform has a real need for safe retries. The approval endpoint writes to
the simulator, the network between them can fail, and the load test found
**2,619 conflicts** in a single decision run — the approve endpoint refusing
to dispatch the same allocation twice, which is correct behaviour but which
the harness was counting as a failure.

A recommendation's body can change between proposing and approving, because
the world moves. The Day 4 risk table names this: *"A recommendation goes
stale between proposing and approving → The world moves; the approval can hit
a 409."*

## Decision

**The idempotency key is derived from the recommendation's own identity and
its body.**

```
rec-{station}-{fuel}-{tick}-{quantity}
```

A retried approval is therefore a **replay** rather than a second shipment —
the simulator returns the original allocation.

**The quantity is part of the key.** The simulator's key is permanently
occupied once used, so a changed body needs a **new** key; reusing the old
one with a different quantity would come back as `IDEMPOTENCY_KEY_MISMATCH`.

A deficit above the route's `max_shipment` is split into several shipments,
**each with its own idempotency key** — a `-{leg}` suffix distinguishes the
parts, because two legs can carry the same quantity and would otherwise
collide.

The fallback policy has its **own** key namespace — `fb-{station}-{fuel}-{tick}-{quantity}`
against the optimizer's `rec-` prefix — so a fallback-proposed allocation and
a model-proposed one cannot collide.

## Alternatives considered

| Alternative | Why not |
|---|---|
| **A random UUID per attempt** | A retry after a network failure would create a **second** shipment — the exact thing idempotency exists to prevent. |
| **A timestamp or counter** | Same problem: two attempts produce two keys and two shipments. |
| **The recommendation's id alone** | The body can change between proposing and approving — the world moves, and the quantity is recomputed. A changed body on an occupied key returns `IDEMPOTENCY_KEY_MISMATCH`. Including the quantity means a changed body gets a new key. |
| **A hash of the whole body** | Would work, but it is opaque. A key a judge can read in the simulator's allocation list — `rec-station-mirpur-DIESEL-42-3000` — is self-describing, and it makes the replay behaviour demonstrable rather than something to take on trust. |
| **One key for the whole recommendation, including the split legs** | The legs have different quantities, so they need different keys anyway. |

## Consequences

**Good.** A retried approval is safe by construction, and the platform can
retry without tracking what it has already sent. The load test's
**2,619 conflicts** became visible as their own column — they are the
simulator correctly refusing a duplicate dispatch, not a platform failure.

**Good.** The key is readable. When a judge looks at `GET /v1/allocations` in
the simulator's own admin console, the key tells them which recommendation
produced the shipment.

**Good.** The key's components are exactly the things that define the
shipment — station, fuel, tick and quantity — so two allocations with the
same key are genuinely the same shipment.

**Bad.** A changed quantity means a **new** allocation, not an amendment. If
the world moves between proposing and approving and the optimizer recomputes
a different quantity, the original key is occupied and the new shipment is a
second allocation. The platform handles this by recording the simulator's
error code on the recommendation and letting the operator re-propose — the
Day 4 risk table's mitigation.

**Bad.** The key embeds the tick, so a recommendation proposed at tick *N* and
approved at tick *N+1* carries a key from *N*. That is deliberate — the tick
is part of the shipment's identity — but it means the key does not describe
the moment of submission.

**Bad.** The key is **not** a random value, so it is guessable. That is
acceptable here because the simulator is single-tenant and local, and there
is no shared surface to guess at.

## Evidence

- [`docs/progress/day4.md`](../progress/day4.md) — the key format and the
  reasoning about the permanently-occupied key; §5 on the lifecycle; the risk
  table on stale recommendations
- [`docs/load-test-report.md`](../load-test-report.md) — the 2,619 conflicts
  and the harness correction
- [`docs/postmortems/08-fallback-never-engaged-stale-forecast.md`](../postmortems/08-fallback-never-engaged-stale-forecast.md)
  — the fallback's own key namespace
- [`backend/app/decision/service.py`](../../backend/app/decision/service.py) —
  the key construction
