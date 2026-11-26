# Backend API reference

Every route the backend exposes, derived from the route definitions in
[`backend/app/main.py`](../backend/app/main.py) and the Pydantic schemas it
uses.

**Base URL.** The backend listens on port `8000` inside the container and is
published on the host port in `.env` (`BACKEND_PORT`).

**Interactive documentation.** FastAPI's generated OpenAPI UI is at `/docs`,
and the raw schema at `/openapi.json`.

**No authentication.** Every endpoint below is unauthenticated and reachable
on the container's port. That is a deliberate trade, and it is recorded as a
gap — see [`docs/requirements-traceability.md`](requirements-traceability.md).

**Read-only vs mutating.** A **mutating** endpoint changes the platform's
state, the simulated world, or both. Everything else is read-only. The
distinction matters because the mutating ones are the ones a judge's demo
will drive.

---

## Contents

| Area | Endpoints |
|---|---|
| [1. Health and state](#1-health-and-state) | `/`, `/health`, `/ready`, `/api/state` |
| [2. Data reads](#2-data-reads) | `/api/stations`, `/api/depots`, `/api/routes`, `/api/events`, `/api/supply-arrivals`, `/api/allocations`, `/api/history/{station_id}` |
| [3. Intelligence](#3-intelligence) | `/api/risk`, `/api/forecast`, `/api/anomalies`, `/api/alerts`, `/api/decisions`, `/api/model-status` |
| [4. Recommendations and approvals](#4-recommendations-and-approvals) | `/api/recommendations`, `/api/recommendations/{id}`, `/api/recommendations/{id}/simulate`, `/api/recommendations/{id}/approve`, `/api/recommendations/{id}/reject`, `/api/recommendations/propose`, `/api/decision-settings`, `/api/decision-history` |
| [5. Simulation controls and crisis](#5-simulation-controls-and-crisis) | `/api/simulation`, `/api/simulation/{action}`, `/api/simulation/crisis`, `/admin/reset` |
| [6. Service health](#6-service-health) | `/api/service-health` |
| [7. Audit](#7-audit) | `/api/audit` |
| [8. Model control](#8-model-control) | `/api/model/train`, `/api/model/kill-switch`, `/api/model/fault` |
| [9. Metrics](#9-metrics) | `/metrics` |
| [10. Common response fields](#10-common-response-fields) | `stale`, `degraded`, `age_seconds` |
| [11. Error shapes](#11-error-shapes) | |

---

## 1. Health and state

### `GET /`

**Read-only.** The service banner.

| | |
|---|---|
| **Purpose** | Tell a caller what this service is and where to start |
| **Parameters** | None |
| **Status codes** | `200` |

```json
{
  "service": "fuel-supply-intelligence-platform",
  "version": "1.0.0",
  "docs": "/docs",
  "health": "/health",
  "state": "/api/state",
  "stations": "/api/stations",
  "history": "/api/history/{station_id}"
}
```

### `GET /health`

**Read-only.** Component health, and the sync status.

| | |
|---|---|
| **Purpose** | Report the backend, the database and the simulator |
| **Parameters** | None |
| **Status codes** | `200` all components ok · `503` a dependency is down |

The endpoint returns `503` when a dependency is down, so it doubles as a
container liveness probe.

```json
{
  "status": "ok",
  "components": {
    "backend": {"status": "ok"},
    "database": {"status": "ok"},
    "simulator": {"status": "ok", "simulation": {"tick": 3}}
  },
  "sync": { }
}
```

> **Note.** `/health` and `/ready` are different probes. `/health` asks
> "are the dependencies working"; `/ready` asks "should traffic be sent
> here?". The Day 1 log records the CI smoke test asserting the *wrong*
> thing: it booted the backend with no simulator and no database, then
> expected `/health` to return 200 — but `/health` is a *readiness* probe and
> correctly returns 503 while a dependency is down.

### `GET /ready`

**Read-only.** Readiness, deliberately **narrower** than liveness.

| | |
|---|---|
| **Purpose** | Report whether the backend should receive traffic |
| **Parameters** | None |
| **Status codes** | `200` ready · `503` not ready or draining |

The backend is alive as soon as the process is running, but it is only ready
when it has a database connection and a simulator client, and it stops being
ready the moment it starts draining at shutdown. During a rolling restart
Docker's healthcheck keeps failing while the container drains, so the
orchestrator stops sending traffic before the socket is closed.

```json
{"ready": true, "shutting_down": false, "database": "ok"}
```

### `GET /api/state`

**Read-only.** The consolidated snapshot of the whole network.

| | |
|---|---|
| **Purpose** | One snapshot of the simulated fuel network |
| **Parameters** | None |
| **Status codes** | `200` · `503` no state has been fetched yet |
| **Source** | The platform's cache — **never** the simulator |

| Response field | What it is |
|---|---|
| `instance` | The simulator's clock, status, tick, scenario |
| `regions` | The regions and their demand factors |
| `depots` | Depots, their stock and dispatch capacity |
| `stations` | Stations, their tanks, status and demand profile |
| `routes` | Routes, their transit time and `max_shipment` |
| `supply_arrivals` | Scheduled supply arrivals |
| `events` | The domain events |
| `allocations` | The allocation ledger |
| `metrics` | The simulator's own metrics, including `service_level` |
| `stale`, `degraded`, `age_seconds`, `degraded_reason`, `last_sync_at` | The freshness envelope — see §10 |

```json
{
  "instance": {"tick": 3},
  "stations": [{"id": "station-mirpur"}],
  "metrics": {"service_level": 0.99},
  "stale": false,
  "degraded": false,
  "age_seconds": 0.12
}
```

Each fetch is persisted to Postgres.

---

## 2. Data reads

All of these are **read-only**, served from the platform's cache, and all
carry the freshness envelope from §10.

| Endpoint | Returns | Wrapper key | 503 when |
|---|---|---|---|
| `GET /api/stations` | Every station | `stations` | No state yet |
| `GET /api/depots` | Every depot | `depots` | No state yet |
| `GET /api/routes` | Every route | `routes` | No state yet |
| `GET /api/events` | The domain events | `events` | No state yet |
| `GET /api/supply-arrivals` | Scheduled supply arrivals | `supply_arrivals` | No state yet |
| `GET /api/allocations` | The allocation ledger | `allocations` | No state yet |

Each returns the entity's full `model_dump`, so the fields are the
simulator's own schemas — see
[`backend/app/simulator_models.py`](../backend/app/simulator_models.py).

### `GET /api/history/{station_id}`

**Read-only.** The stored demand history for one station.

| | |
|---|---|
| **Purpose** | The time series the intelligence layer reads from |
| **Path parameter** | `station_id` — required, 1–64 characters |
| **Query parameter** | `limit` — integer, default `200`, range 1–2000 |
| **Status codes** | `200` · `404` the station id is empty · `503` the history is not available |
| **Source** | **Postgres**, not the simulator |

| Response field | What it is |
|---|---|
| `station_id` | Echo of the path parameter |
| `count` | How many observations were returned |
| `observations[].id` | The simulator's own id for the observation |
| `observations[].station_id` / `fuel_type` | What it is for |
| `observations[].tick` | The simulator tick |
| `observations[].sim_time` | The simulated wall-clock time |
| `observations[].demand_liters` | Demand that was generated |
| `observations[].served_liters` | Demand that was met |
| `observations[].unmet_liters` | Demand that was not met |

```json
{
  "station_id": "station-mirpur",
  "count": 2,
  "observations": [
    {"id": 91, "station_id": "station-mirpur", "fuel_type": "PETROL",
     "tick": 3, "sim_time": "2026-01-01T00:45:00Z",
     "demand_liters": 88.5, "served_liters": 88.5, "unmet_liters": 0.0}
  ]
}
```

> This endpoint does **not** carry the freshness envelope — it reads Postgres
> directly, so the data is as fresh as the database.

---

## 3. Intelligence

### `GET /api/risk`

**Read-only.** The risk assessment for every station and fuel.

| | |
|---|---|
| **Purpose** | How close each station is to running dry, and why |
| **Query parameters** | `station_id` (optional), `fuel_type` (optional, upper-cased before matching) |
| **Status codes** | `200` · `503` the intelligence layer has not evaluated yet |

| Response field | What it is |
|---|---|
| `evaluated_at` | When the evaluation ran |
| `models_used` | Which predictors were used |
| `active_model` | `learned` or `baseline` |
| `needs_human_review_count` | How many assessments need a human |
| `fallback_activations` | The fallback reasons and their counts |
| `summary` | Counts per risk level — `OK`, `WATCH`, `CRITICAL` |
| `count` | How many assessments were returned |
| `risks[]` | The assessments |

Each risk carries `risk_level`, `hours_to_stockout`,
`stockout_probability`, `model`, `confidence`, `needs_human_review`,
`recommendation`, and the `signals` that produced the score.

```json
{
  "station_id": "station-mirpur",
  "fuel_type": "PETROL",
  "risk_level": "WATCH",
  "hours_to_stockout": 23.89,
  "stockout_probability": 0.42,
  "model": "learned",
  "confidence": 0.948,
  "needs_human_review": false,
  "recommendation": "Dispatch petrol within the next 24 hours: about 23.9 simulated hours of cover left.",
  "signals": [
    {"name": "inventory_liters", "value": 9166.0, "detail": "9,166 L on hand"},
    {"name": "available_liters", "value": 9933.0, "detail": "9,933 L available (on hand + in transit)"}
  ]
}
```

### `GET /api/forecast`

**Read-only.** The demand forecasts.

| | |
|---|---|
| **Purpose** | The predicted demand, with its confidence interval |
| **Query parameters** | `station_id` (optional), `fuel_type` (optional) |
| **Status codes** | `200` · `503` the intelligence layer has not evaluated yet |

| Response field | What it is |
|---|---|
| `evaluated_at` | When the evaluation ran |
| `models_used` | Which predictors were used |
| `count` | How many forecasts were returned |
| `forecasts[]` | Each with `station_id`, `fuel_type`, `model`, `horizon_ticks`, `confidence`, `needs_human_review`, `explanation`, and `points[]` |

Each forecast point carries `tick`, `predicted_demand`, `lower_bound`,
`upper_bound`, `width` and `confidence`.

### `GET /api/anomalies`

**Read-only.** The detected anomalies and bottlenecks.

| | |
|---|---|
| **Purpose** | What the platform believes is wrong with the network |
| **Parameters** | None |
| **Status codes** | `200` · `503` the intelligence layer has not evaluated yet |

| Response field | What it is |
|---|---|
| `evaluated_at` | When the evaluation ran |
| `count` | How many anomalies |
| `anomalies[]` | Each with `kind` (`demand` or `inventory`), `entity_id`, `fuel_type`, `severity`, `score`, `message`, `expected`, `actual`, and a `detail` object |
| `bottlenecks[]` | Each with `kind`, `entity_id`, `severity`, `message` |

The five bottleneck kinds are `dispatch_capacity`, `route_disrupted`,
`supply_delayed`, `depot_low_stock` and `station_outage`.

### `GET /api/alerts`

**Read-only.** The platform-generated alerts, newest first.

| | |
|---|---|
| **Purpose** | The alert trail, from Postgres |
| **Query parameter** | `limit` — integer, default `100`, range 1–1000 |
| **Status codes** | `200` · `503` the alerts are not available |
| **Source** | **Postgres** |

| Response field | What it is |
|---|---|
| `count` | How many alerts were returned |
| `alerts[].id` | The row id |
| `alerts[].created_at` | When it was raised |
| `alerts[].kind` | What kind of alert |
| `alerts[].severity` | `warning` or `critical` |
| `alerts[].entity_type` / `entity_id` | What it concerns |
| `alerts[].sim_tick` | The tick it was raised against |
| `alerts[].message` | The human-readable explanation |
| `alerts[].resolved` | Whether it has been resolved |

The `payload` column — about a kilobyte a row, holding the whole risk
assessment as JSON — is **deliberately not selected**. The load test
measured that omission at 58 objects per request.

### `GET /api/decisions`

**Read-only.** The most recent dispatch decisions.

| | |
|---|---|
| **Purpose** | What actually moved fuel, and why |
| **Parameters** | None |
| **Status codes** | `200` |

The simulator never refills a station on its own, so these decisions are
what move fuel. Each carries the reason it was made.

```json
{"count": 1, "decisions": [{"station_id": "station-mirpur", "reason": "..."}]}
```

### `GET /api/model-status`

**Read-only.** Which predictor is active, and how well it is doing.

| | |
|---|---|
| **Purpose** | The model's own honest report on itself |
| **Parameters** | None |
| **Status codes** | `200` |

| Response field | What it is |
|---|---|
| `active_model` | `learned` or `baseline` |
| `ml_kill_switch` | Whether the kill-switch is on |
| `prediction_fault` | The injected fault, if one is active |
| `fallback_reasons` | The fallback reason labels and their counts |
| `learned_model.trained` | Whether a model exists at all |
| `learned_model.training_rows` | How many rows it was fitted on |
| `learned_model.validation_mae_liters` | The one-step held-out error — **the gate** |
| `learned_model.baseline_validation_mae` | The rule's error on the same rows |
| `learned_model.validation_error_ratio` | The model's error as a fraction of the rule's |
| `learned_model.horizon_total_mae_liters` | The horizon diagnostic |
| `learned_model.baseline_horizon_total_mae_liters` | The rule's horizon diagnostic |
| `learned_model.beats_baseline` | Whether the gate passed |
| `learned_model.beats_baseline_at_horizon` | Whether the **diagnostic** agrees — published even when false |

The endpoint publishes the horizon verdict **whether it flatters the model
or not**. Measured honestly, it says the learned model does *not* beat the
rule over a whole forecast window.

```json
{
  "active_model": "learned",
  "ml_kill_switch": false,
  "prediction_fault": null,
  "fallback_reasons": {"not_trained": 272},
  "learned_model": {
    "trained": true,
    "training_rows": 3980,
    "validation_mae_liters": 0.0512,
    "baseline_validation_mae": 0.0589,
    "validation_error_ratio": 0.8693,
    "beats_baseline": true,
    "beats_baseline_at_horizon": false
  }
}
```

---

## 4. Recommendations and approvals

### `GET /api/recommendations`

**Read-only.** The live working set of recommendations, newest first.

| | |
|---|---|
| **Purpose** | What the operator can act on right now |
| **Query parameters** | `status` (optional, filter by status) · `station_id` (optional) · `limit` (optional, integer, 1–1000) |
| **Status codes** | `200` |
| **Source** | The in-memory working set — **not** Postgres |

| Response field | What it is |
|---|---|
| `count` | How many were returned |
| `total` | How many the working set holds in total |
| `auto_mode` | Whether the auto-approve mode is on |
| `fallback_reason` | Why the fallback policy is active, if it is |
| `recommendations[]` | The recommendations themselves |

**The list is capped.** The dashboard polls this endpoint every few seconds,
and a response that grows with the life of the process eventually stops being
served at all. When `limit` is omitted the service's own page limit applies
(`min(100, DECISION_HISTORY_LIMIT)`). The full history is in
`/api/decision-history`.

### `GET /api/recommendations/{recommendation_id}`

**Read-only.** One recommendation in full.

| | |
|---|---|
| **Purpose** | Inspect one recommendation before deciding on it |
| **Path parameter** | `recommendation_id` |
| **Status codes** | `200` · `404` no recommendation with that id |

The payload is the recommendation's full field set — see
[`docs/decision-engine.md`](decision-engine.md) §6 for every field, and for
a real example.

### `POST /api/recommendations/{recommendation_id}/simulate`

**Read-only in effect** — it writes nothing, to the simulator or the database.

| | |
|---|---|
| **Purpose** | Project what would happen if this were approved |
| **Path parameter** | `recommendation_id` |
| **Body** | None |
| **Status codes** | `200` · `404` no recommendation with that id |

| Response field | What it is |
|---|---|
| `simulated` | Whether a projection was available |
| `writes_to_simulator` | Always `false` |
| `before` / `after` | `risk_level`, `hours_to_stockout`, `stockout_probability`, `available_liters` |
| `improvement` | `risk_level_changed`, `hours_to_stockout_gained`, `stockout_probability_reduction` |
| `constraints` | The eight checks that were made |
| `alternatives` | The routes that lost, and why |
| `reason` | Present when `simulated` is `false` |

A recommendation with no projection returns
`{"simulated": false, "reason": "this recommendation has no projection to simulate"}`
rather than failing.

### `POST /api/recommendations/{recommendation_id}/approve`

**Mutating.** Approves the recommendation and submits it to the simulator.

| | |
|---|---|
| **Purpose** | The one action that changes the simulated world |
| **Path parameter** | `recommendation_id` |
| **Body** | Optional — `ApprovalRequest` |
| **Status codes** | `200` · `404` no recommendation with that id · `409` the recommendation is not `PROPOSED` |

**Body** (`ApprovalRequest`, both fields optional):

| Field | Type | Default | Meaning |
|---|---|---|---|
| `decided_by` | string, max 64 | `"operator"` | Who approved it — the audit trail needs a name |
| `submit` | boolean | `true` | Whether to send it to the simulator straight away |

```bash
curl -X POST http://localhost:8000/api/recommendations/rec-station-coxsbazar-OCTANE-235-949/approve \
  -H 'Content-Type: application/json' \
  -d '{"decided_by": "day4-dod-proof", "submit": true}'
```

The submission uses the recommendation's **deterministic idempotency key**,
so approving twice is a **replay** rather than a second shipment. Every 409
the simulator can return is handled and explained — see
[`docs/decision-engine.md`](decision-engine.md) §10.

### `POST /api/recommendations/{recommendation_id}/reject`

**Mutating.** Rejects the recommendation. **Nothing** is sent to the simulator.

| | |
|---|---|
| **Purpose** | Decline a proposal, with a recorded reason |
| **Path parameter** | `recommendation_id` |
| **Body** | Optional — `RejectionRequest` |
| **Status codes** | `200` · `404` no recommendation with that id · `409` the recommendation is not `PROPOSED` |

**Body** (`RejectionRequest`, both fields optional):

| Field | Type | Default | Meaning |
|---|---|---|---|
| `decided_by` | string, max 64 | `"operator"` | Who rejected it |
| `reason` | string, max 500 | `""` | Why |

### `POST /api/recommendations/propose`

**Mutating.** Forces a fresh pass of the decision engine.

| | |
|---|---|
| **Purpose** | Ask for recommendations right now, rather than waiting for the next sync cycle |
| **Body** | None |
| **Status codes** | `200` · `503` the platform has no state yet |

It goes through the same shared path as the background sync, so it picks the
same policy: the model-driven optimizer when the intelligence layer has
usable forecasts, and the rule-based fallback when it does not.

```json
{
  "count": 12,
  "used_fallback_policy": false,
  "recommendations": [ ]
}
```

### `GET /api/decision-settings`

**Read-only.** The decision engine's settings and counters.

| | |
|---|---|
| **Purpose** | Show the operator the policy in force and the counters |
| **Parameters** | None |
| **Status codes** | `200` |

| Response field | What it is |
|---|---|
| `auto_mode` | Whether the auto-approve mode is on |
| `fallback_reason` | Why the fallback policy is active, if it is |
| `counters` | `proposed`, `approved`, `rejected`, `submitted`, `failed`, `conflicts` |
| `approval_rate` | `approved / proposed` |
| `conflicts_by_code` | The 409 codes and how many of each |
| `policy` | `approach`, `target_cover_hours`, `min_shipment_liters`, `max_alternatives` |
| `fallback_policy` | `approach`, `used` |

```json
{
  "auto_mode": false,
  "fallback_reason": null,
  "counters": {"proposed": 362, "approved": 361, "rejected": 0, "submitted": 361, "failed": 1, "conflicts": 1},
  "approval_rate": 0.9917,
  "conflicts_by_code": {"ROUTE_DISRUPTED": 1},
  "policy": {"approach": "priority-greedy", "target_cover_hours": 24.0, "min_shipment_liters": 500.0, "max_alternatives": 3},
  "fallback_policy": {"approach": "lowest days-of-cover first", "used": false}
}
```

### `POST /api/decision-settings`

**Mutating.** Turns the auto-approve mode on or off.

| | |
|---|---|
| **Purpose** | Enable or disable automatic approval |
| **Body** | `DecisionSettingsRequest` — `auto_mode`: boolean, default `false` |
| **Status codes** | `200` |

The auto-mode is **off by default**. When it is on it still only approves a
narrow class of action: a `WATCH`-level station, confidence ≥ 0.85, no
human-review flag, not from the fallback policy, and every constraint
satisfied.

### `GET /api/decision-history`

**Read-only.** The audit trail from Postgres.

| | |
|---|---|
| **Purpose** | Every decision ever made, durably |
| **Query parameters** | `limit` (default `50`, range 1–500) · `status` (optional) · `station_id` (optional) |
| **Status codes** | `200` · `503` the history could not be read |
| **Source** | **Postgres** |

This is the durable counterpart of `/api/recommendations`. The working set
is bounded and per-process; this is not.

---

## 5. Simulation controls and crisis

> These drive the **simulator**, not the platform. Every response is
> labelled `simulation_only` so the dashboard can say so on screen.

### `GET /api/simulation`

**Read-only.** What the simulation can currently be told to do.

| | |
|---|---|
| **Purpose** | Let the dashboard decide whether to offer a "Run" button |
| **Parameters** | None |
| **Status codes** | `200` |

| Response field | What it is |
|---|---|
| `simulation_only` | Always `true` |
| `status` | The simulator's status, or `UNKNOWN` |
| `tick` | The current tick, or `null` |
| `sim_time` | The simulated clock |
| `tick_minutes` | The tick length |
| `scenario_id` | The scenario running |
| `actions` | The five supported actions |

```json
{
  "simulation_only": true,
  "status": "RUNNING",
  "tick": 2562,
  "sim_time": "2026-01-10T07:15:00Z",
  "tick_minutes": 15,
  "scenario_id": "default",
  "actions": ["run", "pause", "toggle", "step", "reset"]
}
```

### `POST /api/simulation/{action}`

**Mutating.** Runs, pauses, toggles, steps or resets the simulation.

| | |
|---|---|
| **Purpose** | Drive the simulated world from the dashboard |
| **Path parameter** | `action` — one of `run`, `pause`, `toggle`, `step`, `reset` |
| **Body** | None |
| **Status codes** | `200` · `400` unknown action · `502` the simulator refused |

```json
{"simulation_only": true, "action": "run", "result": {"status": "RUNNING", "tick": 2563}}
```

A **reset** is the exception: it rewinds the world's clock, so the cached
snapshot is dropped at once. Left in place it would keep serving the
previous world's numbers as fresh until the next sync cycle — up to fifteen
seconds of a world that has ended.

### `POST /api/simulation/crisis`

**Mutating.** Injects a crisis into the simulator.

| | |
|---|---|
| **Purpose** | Run the crisis drills from the dashboard |
| **Body** | `CrisisRequest` — all three fields required or defaulted |
| **Status codes** | `200` · `400` unknown crisis type · `502` the simulator refused |

**Body** (`CrisisRequest`):

| Field | Type | Default | Constraints | Meaning |
|---|---|---|---|---|
| `type` | string | — | 1–40 characters | One of the simulator's six event types |
| `duration_ticks` | integer | `8` | 1–200 | How long the crisis lasts |
| `parameters` | object | `{}` | — | Passed through to the simulator |

The six crisis types are `demand_spike`, `route_disruption`,
`station_outage`, `depot_constraint`, `shipment_delay` and
`supply_shortfall`.

```bash
curl -X POST http://localhost:8000/api/simulation/crisis \
  -H 'Content-Type: application/json' \
  -d '{"type": "demand_spike", "duration_ticks": 8, "parameters": {"station_id": "station-mirpur", "fuel_type": "PETROL", "multiplier": 3.0}}'
```

The crisis starts at the **current** tick, not at tick 0.

### `POST /admin/reset`

**Mutating.** Puts the **platform** back to a known state.

| | |
|---|---|
| **Purpose** | The reset half of the demo procedure |
| **Body** | None |
| **Status codes** | `200` |

This clears the things a simulator reset does not:

- the decision engine's in-memory working set — the proposals from the last
  run are still there, and every one of them fails to approve, because they
  were planned for a world that no longer exists;
- the counters and the fallback reason;
- the kill-switch and any injected prediction fault;
- the cached snapshot of the world.

The simulator's own world is reset separately, through
`POST /api/simulation/reset`, because the platform and the simulator are two
different things and resetting one does not reset the other.

**The audit trail in Postgres is not cleared.** A reset is a demo
convenience, and destroying the record of what was decided would be a
strange thing for an operator's button to do.

```json
{
  "reset": true,
  "recommendations_cleared": 362,
  "kill_switch": false,
  "prediction_fault": null,
  "note": "the platform's working set and fault state are clear; reset the simulator's world with POST /api/simulation/reset"
}
```

---

## 6. Service health

### `GET /api/service-health`

**Read-only.** The health panel the dashboard renders.

| | |
|---|---|
| **Purpose** | One place to see every dependency, the model and the decision engine |
| **Parameters** | None |
| **Status codes** | `200` |

| Response field | What it is |
|---|---|
| `components.backend.status` | Always `"ok"` |
| `components.database` | The database's status |
| `components.simulator` | The simulator's status |
| `components.prediction_service.status` | `ok`, or `degraded` when the model has fallen back |
| `components.prediction_service.active_model` | `learned` or `baseline` |
| `components.prediction_service.fallback_active` | Whether the fallback is answering |
| `components.prediction_service.fallback_reasons` | The reason labels and counts |
| `components.prediction_service.ml_kill_switch` | Whether the kill-switch is on |
| `components.prediction_service.evaluated_at` | When the last evaluation ran |
| `components.prediction_service.forecast_count` | How many forecasts exist |
| `components.prediction_service.needs_human_review_count` | How many need a human |
| `components.decision_engine.status` | `ok`, or `degraded` when the engine is absent |
| `components.decision_engine.auto_mode` | Whether auto-approve is on |
| `components.decision_engine.fallback_active` | Whether the fallback policy is active |
| `http` | The HTTP latency summary, agreeing with the Prometheus registry |

The prediction service is only `ok` when the learned model is the one
answering. When it has fallen back to the rule-based predictor the platform
is still working, but it is working **degraded**, and an operator has to be
able to tell that apart. Reporting `ok` while the model is broken is the one
thing this panel must never do.

**This endpoint does not block on the simulator.** The Day 5 log records that
the health panel no longer probes the simulator, because the dashboard polls
it and a blocking probe would make the panel slow in exactly the moments
the operator needs it.

---

## 7. Audit

### `GET /api/audit`

**Read-only.** One search over three trails.

| | |
|---|---|
| **Purpose** | Ask "what happened to this station?" once, instead of joining three tables by hand |
| **Query parameters** | `q` · `source` · `kind` · `status` · `entity_id` · `since` · `until` · `limit` |
| **Status codes** | `200` · `503` the trail could not be read |
| **Source** | **Postgres** |

The platform writes decisions, domain events and alerts to three separate
tables. This endpoint presents them as one stream.

| Query parameter | Type | Meaning |
|---|---|---|
| `q` | string | Full-text search over the trail |
| `source` | comma-separated | `decision`, `event`, `alert` |
| `kind` | comma-separated | Kinds within those trails |
| `status` | comma-separated | Statuses within those trails |
| `entity_id` | string | The station, route or depot it concerns |
| `since` / `until` | datetime | The time window |
| `limit` | integer | Default `200`, range 1–1000 |

Every filter is optional and they compose:

```bash
# Everything about one station
/api/audit?entity_id=station-karnaphuli

# Every critical alert in the last hour
/api/audit?source=alert&status=critical&since=2026-11-24T10:00:00Z

# Every mention of "route" in any trail
/api/audit?q=route
```

| Response field | What it is |
|---|---|
| `count` | How many entries the page holds |
| `total_matched` | How many matched in total |
| `summary.count` | The total, counted in the database |
| `summary.by_source` | Counts per trail |
| `summary.by_kind` | Counts per kind, highest first |
| `summary.by_status` | Counts per status |
| `summary.newest` / `summary.oldest` | The time range covered |
| `summary.scope` | `table` or `window` — see below |
| `summary.window_rows` | The window size, when `scope` is `window` |
| `entries[]` | The entries themselves |

Each entry carries `source`, `id`, `at`, `kind`, `status`, `entity_id`,
`message` and a `payload` object.

**`scope` says what the count covers.** A facet query — *"how many critical
alerts"* — counts the **whole table**, and its scope is `table`. A full-text
search (`?q=...`) counts the **newest slice** the search looked at, and its
scope is `window` with the size in `window_rows` — the same slice the page
returned, so the total and the entries always describe the same rows.

---

## 8. Model control

### `POST /api/model/train`

**Mutating.** Retrains the learned demand model on the stored history.

| | |
|---|---|
| **Purpose** | Force a retrain, or train for the first time |
| **Body** | None |
| **Status codes** | `200` |

Returns the training metadata, including the validation error, so the
operator can see how good the model is before trusting it.

```json
{
  "trained": true,
  "training_rows": 3980,
  "validation_mae": 0.0512,
  "baseline_validation_mae": 0.0589,
  "beats_baseline": true
}
```

The background trainer already runs on a schedule
(`MODEL_TRAIN_INTERVAL`, default 300 s). This endpoint is for when an
operator wants it **now**.

### `POST /api/model/kill-switch`

**Mutating.** Turns the learned demand model on or off.

| | |
|---|---|
| **Purpose** | Stop the model immediately, without a redeploy |
| **Body** | `KillSwitchRequest` — `enabled`: boolean, **required** |
| **Status codes** | `200` |

With it on, the platform stops using the learned model entirely: every
forecast comes from the rule-based predictor, every forecast is flagged for
human review, and the decision layer uses the days-of-cover fallback policy.

It exists because a model can look healthy on every metric the platform
tracks and still be producing bad numbers for *this* network.

```bash
curl -X POST http://localhost:8000/api/model/kill-switch \
  -H 'Content-Type: application/json' -d '{"enabled": true}'
```

```json
{
  "ml_kill_switch": true,
  "active_model": "baseline",
  "effect": "the learned model is disabled; every forecast uses the rule-based predictor and is flagged for human review"
}
```

### `POST /api/model/fault`

**Mutating.** Breaks the learned model on purpose, or repairs it.

| | |
|---|---|
| **Purpose** | Show that the fallback takes over when the model *fails* |
| **Body** | `PredictionFaultRequest` — `fault`: string or `null` |
| **Status codes** | `200` |

| `fault` value | What it does |
|---|---|
| `"predict_error"` | The learned model raises. The selector answers with the rule-based **predictor**. |
| `"evaluate_error"` | The whole intelligence evaluation raises. The decision layer falls back to the rule-based **policy**. |
| `null` | Clears the fault |

The prediction component is a **module inside the backend**, so there is no
container to stop and no service to take down. The only honest way to show
that the fallback engages when the model *fails* — rather than when an
operator has politely switched it off — is to break it on demand.

This is the counterpart to the kill-switch, and the two are **not** the same.
The kill-switch is a *decision*: the model is healthy and has been told not
to be used. This is a *fault*: the model is broken and the platform has to
notice on its own. Both end up in the same place — the rule-based predictor,
with every forecast flagged for a human — and the difference is in the
reason the forecast carries.

```json
{
  "prediction_fault": "predict_error",
  "active_model": "baseline",
  "effect": "the learned model will raise on every forecast; every forecast falls back to the rule-based predictor and is flagged for human review"
}
```

---

## 9. Metrics

### `GET /metrics`

**Read-only.** The Prometheus exposition format.

| | |
|---|---|
| **Purpose** | Scraped by Prometheus every 15 seconds |
| **Parameters** | None |
| **Status codes** | `200` |

```text
# HELP http_requests_total Total HTTP requests handled.
# TYPE http_requests_total counter
http_requests_total{method="GET",path="/api/state",status="200"} 42.0
```

The path is the route **template**, not the concrete path, so
`/api/recommendations/{id}` does not explode the label cardinality.

The registry includes the platform's own series — sync cycles, simulator
requests, decision counters, fallback activations by reason, and the
container's cgroup memory (`container_memory_limit_bytes`,
`container_memory_usage_bytes`, `container_memory_fraction`).

---

## 10. Common response fields

Every endpoint that reads the platform's cached state carries the same
**freshness envelope**. It is added by `_state_envelope`, which merges
`cache.metadata()` into the payload.

| Field | Type | What it means |
|---|---|---|
| `stale` | boolean | The simulator answered with `X-Simulator-Stale: true`, meaning the data it returned may be out of date |
| `degraded` | boolean | The platform is serving **last-known** state because the simulator could not be reached |
| `age_seconds` | number \| null | How long ago the cache was refreshed |
| `degraded_reason` | string \| null | Why it is degraded |
| `last_sync_at` | ISO 8601 string \| null | When the last successful sync happened |

**Which endpoints carry it:** `/api/state`, `/api/stations`, `/api/depots`,
`/api/routes`, `/api/events`, `/api/supply-arrivals`,
`/api/allocations`.

**Which do not:** `/api/history/{station_id}` reads Postgres directly; the
intelligence, decision, audit and service-health endpoints read their own
sources.

### `stale` and `degraded` are different things

| | `stale` | `degraded` |
|---|---|---|
| **Means** | The simulator said the data may be old | The simulator could not be reached |
| **Data shown** | The simulator's own answer | The platform's last-known copy |
| **Set by** | The `X-Simulator-Stale` response header | The sync service failing to refresh |

**`stale` is sticky.** A snapshot built from several concurrent reads
reports stale if *any* component was stale, because a per-response flag
would be racy.

### The failure modes are distinguished, on purpose

| Situation | `degraded` | `age_seconds` | What the operator sees |
|---|---|---|---|
| The simulator is up and fresh | `false` | Small | Normal |
| The simulator says its data is stale | `false` | Small | A stale banner, but live data |
| The simulator is down | `true` | **Growing** | A DEGRADED banner, and the age |
| The platform has never synced | — | `null` | `503` — *"no simulator state available yet"* |

The `503` case is the important one: the platform would rather say *"no
state available yet"* than serve a number as if it were current.

### The Day 7 bug this shape exists to prevent

> *"The world was at tick 0. The platform reported tick 892, said
> `degraded: false`, and said the data was 0.2 seconds old. The age was
> technically true — the cache was refreshed 0.2 s ago — but it was
> refreshed with the numbers of a run that had ended. The platform was not
> stale. It was **wrong**, and it looked fresh."*

The fix was to treat a **rewound clock** as a new world rather than a
fresher view of the old one: a snapshot whose tick is *behind* the one
already cached means the world was reset behind the platform's back, and the
cache is dropped.

---

## 11. Error shapes

### The backend's own errors

The backend registers **no custom exception handlers**, so its errors use
FastAPI's default shape: a JSON object with a `detail` key.

| Status | When | Body |
|---|---|---|
| `400` | Unknown simulation action | `{"detail": "unknown simulation action: fly"}` |
| `404` | No recommendation with that id | `{"detail": "no recommendation with id rec-..."}` |
| `409` | The recommendation is not `PROPOSED` | `{"detail": "recommendation rec-... is REJECTED, not PROPOSED"}` |
| `503` | No state has been fetched yet | `{"detail": "no simulator state available yet"}` |
| `503` | The intelligence layer has not evaluated | `{"detail": "the intelligence layer has not evaluated the network yet"}` |
| `503` | The database is unavailable | `{"detail": "could not read the audit trail: ..."}` |
| `502` | The simulator refused an action | `{"detail": "the simulator refused to run: ..."}` |

### Validation errors

A body that does not match the Pydantic schema returns FastAPI's default
**list-shaped** `detail`:

```json
{"detail": [{"loc": ["body", "duration_ticks"], "msg": "value is not a valid integer", "type": "type_error.integer"}]}
```

### The simulator's errors — two shapes

The simulator uses **two** envelope shapes, and the platform normalises both
into one exception hierarchy.

| Shape | When | Example |
|---|---|---|
| `{"detail": {"code": "...", "message": "..."}}` | Allocation and validation errors | `{"detail": {"code": "ROUTE_DISRUPTED", "message": "route is disrupted"}}` |
| `{"error": {"code": "...", "message": "..."}}` | Injected faults | `{"error": {"code": "FAULT_INJECTED", "message": "Simulator API is unavailable"}}` |

The simulator's allocation errors are **always** `{"detail": {"code":
"<UPPER_SNAKE>", "message": "..."}}`; injected faults return
`{"error": {...}}`. The difference is deliberate in the guide, and the
platform's client parses both.

FastAPI's list-shaped `{"detail": [...]}` degrades gracefully — a
validation error from the simulator is not an allocation conflict, and the
client treats it as a generic API error rather than a domain one.

### The status codes the simulator returns

| Status | Code | Meaning |
|---|---|---|
| `200` | — | An idempotent replay of an existing allocation |
| `201` | — | A new allocation accepted. Cache the returned id. |
| `404` | `NOT_FOUND` | The depot, station or route id is unknown |
| `404` | `ALLOCATION_NOT_FOUND` | The allocation id is unknown |
| `409` | `IDEMPOTENCY_KEY_MISMATCH` | Same key, different body. The first submission wins. |
| `409` | `ROUTE_MISMATCH` | The route connects different endpoints |
| `409` | `DEPOT_CLOSED` | The depot's status is not in `{OPEN, CONSTRAINED}` |
| `409` | `STATION_CLOSED` | The station's status is not `OPEN` |
| `409` | `ROUTE_DISRUPTED` | The route's status is not `AVAILABLE` |
| `409` | `ROUTE_CAPACITY_EXCEEDED` | The quantity exceeds `route.max_shipment` |
| `409` | `INSUFFICIENT_INVENTORY` | The depot does not have the fuel |
| `409` | `DISPATCH_CAPACITY_EXCEEDED` | The depot's in-flight plus pending quantities exceed the per-tick allowance |
| `409` | `DESTINATION_CAPACITY_EXCEEDED` | The station's tank would overflow |
| `409` | `CANNOT_CANCEL` | The allocation is no longer `PENDING` |
| `422` | — | A validation error from the simulator's Pydantic models |

The platform maps each conflict code to a dedicated exception class. An
unknown code falls back to a generic `ConflictError`.

---

## Related documents

- [`docs/architecture.md`](architecture.md) — the system these routes sit in
- [`docs/decision-engine.md`](decision-engine.md) — the recommendation
  lifecycle, the idempotency key and the 409 handling in full
- [`docs/intelligence-methodology.md`](intelligence-methodology.md) — the
  forecasts, risks and anomalies these endpoints serve
- [`docs/simulator-notes.md`](simulator-notes.md) — the simulator's
  documented behaviour and its discrepancies with the guide
- [`docs/requirements-traceability.md`](requirements-traceability.md) — which
  brief requirement each endpoint satisfies
