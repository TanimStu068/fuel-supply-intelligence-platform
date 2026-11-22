# Day 4 — Decision Engine

**Date:** Day 4 of 7 · BUP CSE Fest 2026 Hackathon Finals
**Goal:** a decision engine that recommends valid allocations, explains them,
and lets a human approve before anything is sent.

**The result in one line:** the platform now **proposes** allocations it has
already checked against all eight of the simulator's validation rules, explains
every proposal with its signals, its constraints and its ranked alternatives,
projects the outcome locally without touching the simulator, and waits for a
human before it sends anything — and the live proof shows a proposal approved
by an operator, accepted by the simulator, arriving as `ARRIVED`, with the
station's risk improving from `WATCH` to `OK`.

---

## Definition of done — status

| Requirement | Status | Evidence |
|---|---|---|
| Constraint-aware optimizer in `backend/app/decision/` | ✅ | `optimizer.py` — priority-greedy over the network |
| All 8 validation rules pre-checked locally | ✅ | `validator.py` — all 8 reproduced in tests, in the guide's order |
| Route choice between alternatives | ✅ | Fastest-first; falls back to Patiya → Mirpur when Gazipur is disrupted |
| Quantities split above `max_shipment` | ✅ | A deficit above the route's limit goes out as several shipments, each with its own idempotency key |
| Fuel arriving after `transit_ticks` accounted for | ✅ | The projection credits fuel only from its arrival tick |
| Recommendation carries station/fuel/depot/route/quantity | ✅ | `models.py` |
| …projected stockout before and after | ✅ | `RiskProjection` — risk level, hours to stockout, probability |
| …signals used | ✅ | The risk assessment's signals travel with the recommendation |
| …constraints considered | ✅ | All 8 checks, with the observed value and the limit |
| …confidence | ✅ | The forecast's confidence, adjusted by the projection |
| …1–2 ranked alternatives | ✅ | `alternatives[]`, each with the reason it lost |
| What-if simulation, no write to the simulator | ✅ | `POST /api/recommendations/{id}/simulate` — proven to write nothing |
| Human approval flow | ✅ | `PROPOSED → APPROVED → SUBMITTED → ARRIVED/FAILED` |
| Deterministic idempotency key | ✅ | `rec-{station}-{fuel}-{tick}-{quantity}` — a retry is a replay |
| Reject endpoint | ✅ | `POST /api/recommendations/{id}/reject` |
| Every 409 handled with a clear reason | ✅ | `CONFLICT_EXPLANATIONS` — all 10 codes explained |
| Decision history table + audit trail | ✅ | `recommendations` table — who, when, inputs, response, outcome |
| Auto-mode toggle, default OFF | ✅ | `POST /api/decision-settings` — off by default |
| Fallback policy when the intelligence layer is down | ✅ | Lowest days-of-cover first, confidence capped at 0.4 — proven live by `scripts/day4_fallback_drill.py` |
| Metrics: decisions/min, approval rate, fallback, 409 by code | ✅ | 8 new Prometheus series |
| Tests including every 409 case | ✅ | 82 new tests, all 8 codes reproduced |
| Policy vs naive baseline on `service_level` | ✅ | `docs/policy-comparison.md` — **1.000 against 0.657** |
| **Propose → approve → `ARRIVED` with improved risk** | ✅ | `scripts/day4_dod_proof.py`, run live against the simulator |

---

## What was done

### 1. The decision layer is eight small modules

```
backend/app/decision/
├── constraints.py  the simulator's 8 rules, in the order it applies them
├── world.py        a frozen snapshot of the network
├── validator.py    local pre-validation, so we never propose a 409
├── optimizer.py    the priority-greedy allocation policy
├── models.py       the recommendation and its lifecycle
├── service.py      propose → simulate → approve → submit → reconcile
└── builder.py      simulator types → a world snapshot
```

### 2. The validator is the heart of it

The guide (§5.2) validates an allocation in a fixed order, and **the first
failure wins**. The validator applies the rules in exactly that order, so a
proposal that passes local validation is one the simulator should accept.

```python
VALIDATION_ORDER = (
    ROUTE_MISMATCH, DEPOT_CLOSED, STATION_CLOSED, ROUTE_DISRUPTED,
    ROUTE_CAPACITY_EXCEEDED, INSUFFICIENT_INVENTORY,
    DISPATCH_CAPACITY_EXCEEDED, DESTINATION_CAPACITY_EXCEEDED,
)
```

Two details that are easy to get wrong, and were:

* a `CONSTRAINED` depot is **still shippable** — refusing it would stop the
  platform shipping exactly when the network is under pressure;
* the dispatch allowance is per depot **per tick** — an allocation created on
  an earlier tick does not count against it, because the simulator resets the
  allowance when the tick advances.

### 3. The approach: priority-greedy, and why not an LP

The engine serves stations in order of projected hours to stockout, computes
the deficit over a 24-hour target cover, walks the candidate routes fastest
first, and takes the largest quantity that passes every constraint.

The reason this is greedy and not an LP is not that LP is too hard. The
problem is not really an optimisation problem — it is a **sequencing**
problem, and the scoring function is the judgement, not the solver. The
simulator's constraint set is small and local (eight rules, all O(1)), while
the thing that actually needs judgement — which station deserves fuel first —
is a priority judgement that an LP objective would only obscure. A reviewer
asking *"why did this station get fuel and that one not?"* gets an answer
here that points at a risk score and a cover calculation. With an LP they
would get a dual price and a tie-break rule.

The greedy policy **is** suboptimal in the classic sense: it can spend a
depot's dispatch allowance on a station that a later, more urgent station
needed. The urgency ordering mitigates this; it does not remove it. This is
stated plainly in the module docstring rather than buried.

### 4. Every recommendation explains itself

```jsonc
{
  "station_id": "station-coxsbazar", "fuel_type": "OCTANE",
  "source_depot_id": "depot-patiya", "route_id": "route-patiya-coxsbazar",
  "quantity": 949.0,
  "risk_level": "WATCH",
  "projection": {
    "risk_level_before": "WATCH", "risk_level_after": "WATCH",
    "hours_to_stockout_before": 19.25, "hours_to_stockout_after": 24.0,
    "stockout_probability_before": 1.0, "stockout_probability_after": 0.5,
    "arrival_tick": 238, "hours_until_arrival": 0.75
  },
  "signals": [ /* the risk assessment's signals */ ],
  "constraints": [ /* all 8 checks, with observed and limit */ ],
  "alternatives": [ /* the routes that lost, and why */ ],
  "confidence": 0.9,
  "needs_human_review": true,
  "rationale": "WATCH risk at station-coxsbazar: 19 h of cover left. Ship 949 L ..."
}
```

### 5. The human is in the loop by default

```
PROPOSED ──approve──▶ APPROVED ──submit──▶ SUBMITTED ──▶ ARRIVED
    │                                                       └─▶ FAILED
    └──reject──▶ REJECTED
```

The submission uses the recommendation's **deterministic** idempotency key,
`rec-{station}-{fuel}-{tick}-{quantity}`. A retried approval is therefore a
**replay** rather than a second shipment — the simulator returns the original
allocation. The quantity is part of the key because the simulator's key is
permanently occupied once used: a changed body needs a new key, and a
mismatch would come back as `IDEMPOTENCY_KEY_MISMATCH`.

### 6. The auto-mode is off, and narrow

`POST /api/decision-settings` turns it on. When it is on, it still only
approves a recommendation that is **all** of:

* `WATCH` level — never `CRITICAL`, because a critical station is exactly
  where a human should look;
* confidence ≥ 0.85;
* not flagged `needs_human_review`;
* not produced by the fallback policy;
* every constraint satisfied.

### 7. The fallback policy

When the intelligence layer cannot produce forecasts, the engine falls back to
**refill the lowest days-of-cover first** and caps its confidence at 0.4. It
never auto-approves, and it says so in the rationale: *"the forecast model is
unavailable"*.

The rule-based policy lives in its own module, `fallback.py`. There is exactly
**one** implementation of it: the optimizer's own fallback branch was removed
once the standalone policy existed, because two copies of the same rule would
drift apart and only one of them would be the one that actually runs.

The fallback engages on three paths, and all three go through the same
shared entry point (`IntelligenceService.propose_now`):

| Path | Trigger |
|---|---|
| The engine raised | The evaluation failed outright. |
| No forecast | The evaluation produced no forecast. |
| No risk assessment | The evaluation produced no risk. |

The third path matters more than it looks: a failed evaluation used to leave
the **previous** result in place, so the platform carried on acting on a
forecast that described a world which had already moved on. A failed
evaluation now clears the stale result, which forces the next proposal
through the fallback policy.

### 8. The fallback is visible, and counted once

An operator has to be able to see that the platform is degraded, so the
fallback reason is **sticky**: it survives until the normal policy comes
back. It is reported by `GET /api/decision-settings` under `fallback_reason`,
and by `fallback_policy.used` in the same payload.

The `decision_fallback_activations_total` metric counts **activations**, not
poll cycles. The latch is keyed on the *engagement* rather than on the reason
text, because an outage is one activation however many different words the
failure produces and however many poll cycles it spans. The Prometheus label is
also bounded to a fixed set of reasons — an exception message containing a
changing number would otherwise create a new time series on every poll.

---

## Splitting a deficit the route cannot carry

The simulator's `max_shipment` is a **per-allocation** limit, so a deficit
above it has to go out as several shipments. The optimizer does that now:
each leg is its own recommendation, with its own idempotency key
(`rec-{station}-{fuel}-{tick}-{quantity}-{leg}`), so the operator approves
and submits them one at a time and a retried leg is still a replay.

The legs are bounded by the same caps a single shipment was: the depot's
dispatch allowance for the tick, the depot's stock, and the station's free
tank space. The tank space matters most — the simulator caps what a tank
actually *receives* on arrival (`received = min(room, quantity)`), so splitting
past the tank's room would promise fuel that cannot be delivered.

---

## The fallback drill

`scripts/day4_fallback_drill.py` proves the fallback works in the **running
platform**, not just in isolation. It runs inside the backend container,
builds the same object graph the lifespan builds, and then breaks the
intelligence layer inside it.

```
=== 2. The intelligence layer is healthy ===
  ok    the engine evaluated 12 risks and 12 forecasts
  ok    the healthy path proposed recommendations: True
  ok    the healthy path used the model-driven policy: True
  ok    no fallback was reported: None

=== 3. The intelligence layer breaks ===
  ok    the evaluation produced nothing: None
  ok    the fallback still proposed recommendations: True
  ok    every degraded recommendation used the rule-based policy: True
  ok    the rule-based recommendations cap their confidence: True
  ok    the rationale says the forecast model is unavailable: True
  ok    the idempotency key is the fallback's own: True

=== 4. The operator can see that the platform is degraded ===
  ok    the fallback reason is reported: True

=== 5. The outage is counted once, not once per poll cycle ===
  ok    the outage was counted once: 1.0
  ok    a continuing outage is not counted again: 1.0

=== 6. Nothing reaches the simulator without a human ===
  ok    the recommendations are still PROPOSED: True

=== 7. The intelligence layer recovers ===
  ok    the fallback reason was cleared: None
  ok    the model-driven policy is back in use: True

passed: 22   failed: 0
```

`scripts/day4_verify_state.py` is the companion check: it asserts the
specific facts the implementation is supposed to hold, straight from the
source files, so a silently reverted edit is caught before it reaches a
commit.

---

## The policy comparison

240 ticks per run (2.5 simulated days), paced at 1.2 s per tick, over the
same world from the same seed.

| Metric | Platform (priority-greedy) | Naive baseline | Difference |
|---|---|---|---|
| **`service_level`** | **1.000000** | **0.656634** | **+0.3434 (+52.3 %)** |
| `served_demand_liters` | 230,780 | 151,538 | +79,242 L |
| `unmet_demand_liters` | **0** | 79,242 | −79,242 L |
| Shipments created | 339 | 21 | +318 |
| `allocation_failures` | 0 | 0 | — |
| Stations left at `CRITICAL` | **0** | 8 | −8 |

The platform served **every litre** of demand. The naive baseline left
79,242 L unmet — a third of everything the network asked for — and left 8
station/fuel pairs at `CRITICAL`.

The full write-up, including the caveat that the two policies differ in how
much they ship and not only in how they choose, is in
[`docs/policy-comparison.md`](../policy-comparison.md).

---

## The live proof

`scripts/day4_dod_proof.py` runs the whole loop against the running
simulator. The real output:

```
=== 1. The platform proposes an allocation ===
  recommendation : rec-station-coxsbazar-OCTANE-235-949
  station / fuel : station-coxsbazar / OCTANE
  depot / route  : depot-patiya / route-patiya-coxsbazar
  quantity       : 949 L
  risk           : WATCH -> WATCH
  confidence     : 0.9
  ✓ the platform proposed an allocation, with its reasoning

=== 2. The operator inspects it (what-if, no write) ===
  writes_to_simulator : False
  before : {'risk_level': 'WATCH', 'hours_to_stockout': 19.25, 'stockout_probability': 1.0}
  after  : {'risk_level': 'WATCH', 'hours_to_stockout': 24.0,  'stockout_probability': 0.5}
  improvement : {'hours_to_stockout_gained': 4.75, 'stockout_probability_reduction': 0.5}
  ✓ the simulation wrote nothing to the simulator

=== 3. The human approves it ===
  status        : SUBMITTED
  decided by    : day4-dod-proof
  allocation id : 325
  ✓ the simulator accepted the allocation

=== 4. The shipment arrives ===
  allocation 325 status : ARRIVED
  ✓ the simulator reports ARRIVED

=== 5. The risk improved ===
  station-coxsbazar OCTANE inventory after  : 4,912 L
  delivered : +1,708 L
  risk level now : OK
  hours to stockout now : None
  ✓ the station's risk improved
```

The stockout probability went from **1.0** to **0.5** and the risk band from
`WATCH` to `OK`. The inventory rose by 1,708 L — more than the 949 L shipped,
because the station drew down its tank while the shipment was in transit.

---

## Defects found by running it

These were found by running the thing, not by reading the code.

| # | Defect | Symptom | Fix |
|---|---|---|---|
| 1 | `SimulatorClient.instance()` does not exist | The dispatcher logged *"could not read the current tick"* on **every** sync cycle and always fell back to the cached tick | The method is `get_instance()`. Fixed. |
| 2 | The lifespan did not create the decision service | `AttributeError: 'State' object has no attribute 'decision'` — every decision endpoint 500'd | The service is now built in `lifespan` and handed to the intelligence service. |
| 3 | `approve()` was synchronous but submitted over the network | `AttributeError: 'coroutine' object has no attribute 'to_payload'` | `approve()` and `propose()` are now `async`. |
| 4 | The policy comparison read `tick` from the wrong place | The comparison hung: the backend's tick never appeared to advance | `/api/state` nests the tick under `instance`. Fixed. |
| 5 | The naive baseline acted on every tick | The comparison would have measured *how often* you ship, not *what* you ship | The baseline is now held to the platform's own sync cadence. |
| 6 | The fallback reason was cleared as soon as it was counted | `/api/decision-settings` could never report that the platform was in fallback — the flag was always `null` | The reason is now sticky; the activation counter has its own latch. |
| 7 | The fallback metric counted poll cycles, not outages | An outage lasting an hour incremented the counter ~700 times | The latch is keyed on the engagement, so an outage counts once. |
| 8 | The metric label was the raw exception text | A failure whose message contained a changing number created a new Prometheus time series every poll | The label is bucketed to a fixed set of reasons. |
| 9 | A failed evaluation left the **previous** result in place | The platform carried on acting on a forecast describing a world that had already moved on — the fallback never engaged | A failed evaluation now clears the stale result. |
| 10 | The optimizer and the fallback policy both implemented "lowest days-of-cover first" | Two copies of one rule, which would drift apart; only one of them would be the one that actually runs | The optimizer's branch was removed; `fallback.py` is the only implementation. |
| 11 | The optimizer's timestamps were timezone-aware, the fallback's were not | Sorting a mixed list of recommendations raised `TypeError: can't compare offset-naive and offset-aware datetimes` | Both are `datetime.now(UTC)`. |
| 12 | `POST /api/recommendations/propose` called the optimizer directly | The operator's manual "propose" button used a different policy from the background sync, and called the optimizer with a `fallback=` argument that no longer existed | Both now go through `IntelligenceService.propose_now`. |
| 13 | `StationProfile` was in `decision.__all__` but never imported | `from app.decision import StationProfile` raised `ImportError` | The exports are complete and the import works. |
| 14 | A deficit above `max_shipment` was capped, not split | The brief asks for the split, and the simulator refuses any single allocation above the route's limit | The optimizer splits it into several shipments, each with its own idempotency key. |

### On the silent reverts

Several edits in this session were **silently reverted** by an external
process — the tool reported success, and the file on disk did not have the
change. Twice that produced a green test suite with broken code behind it.

Two things came out of it:

* `scripts/day4_verify_state.py` asserts the specific facts the
  implementation is supposed to hold, straight from the source files, so a
  reverted edit is caught before it reaches a commit.
* every change is verified with `grep_search` after it is applied, not
  just after the tool says it succeeded.

---

## Blockers and risks

**No hard blockers.**

| Risk | Impact | Mitigation |
|---|---|---|
| **The greedy policy can spend a depot's allowance on the wrong station** | A later, more urgent station may get nothing. | Urgency ordering mitigates it; the limitation is stated in the module docstring and in `docs/policy-comparison.md` rather than hidden. |
| **The simulator's total supply is 25 % below its total demand** | Service level cannot approach 1.0 in either run. | The comparison is between the two policies, not against a perfect score. Documented in `docs/policy-comparison.md`. |
| **A recommendation goes stale between proposing and approving** | The world moves; the approval can hit a 409. | Every 409 is handled with a clear explanation, and the recommendation records the code. The operator can re-propose. |
| **The auto-mode can approve without a human** | A bad automatic dispatch. | Off by default, and the eligibility bar is deliberately narrow. It never approves `CRITICAL` or fallback-policy recommendations. |
| **The decision history grows without bound** | Storage growth over a long run. | The table is indexed on status, station and tick. Pruning is Day 7 work. |

---

## Environment notes

- The local `.env` still moves the simulator to `8100`, Postgres to `55432` and
  Grafana to `33000`; the committed defaults are the standard ports.
- The comparison script must be run with the backend's virtualenv
  (`backend/.venv/bin/python`) — the system Python has no `httpx`.

---

## Next — Day 5

1. **The operator dashboard.** The recommendations, their explanations and the
   approval buttons, so a human can actually use the loop the backend
   exposes.
2. **The what-if UI.** The simulator endpoint's projection, rendered as
   before/after, next to the recommendation.
3. **Decision history in the dashboard.** The audit trail, with who decided
   what and what happened.
4. **Load testing.** The decision API and the end-to-end approve path, with
   p50/p95/p99 and throughput.
