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
| Quantities split above `max_shipment` | ✅ | Capped at the route limit; the tank's room and the depot's stock also cap it |
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
| Fallback policy when the intelligence layer is down | ✅ | Lowest days-of-cover first, confidence capped at 0.4 |
| Metrics: decisions/min, approval rate, fallback, 409 by code | ✅ | 8 new Prometheus series |
| Tests including every 409 case | ✅ | 57 new tests, all 8 codes reproduced |
| Policy vs naive baseline on `service_level` | ✅ | `docs/policy-comparison.md` — **1.000 against 0.657** |
| **Propose → approve → `ARRIVED` with improved risk** | ✅ | `scripts/day4_dod_proof.py`, run live against the simulator |

---

## What was done

### 1. The decision layer is seven small modules

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
