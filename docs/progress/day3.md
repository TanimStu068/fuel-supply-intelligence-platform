# Day 3 — Intelligence Layer

**Date:** Day 3 of 7 · BUP CSE Fest 2026 Hackathon Finals
**Goal:** a meaningful, explainable intelligence layer in `backend/app/intelligence/`.

**The result in one line:** the platform forecasts demand 96 ticks ahead,
scores every station and fuel for shortage risk with the signals that drove
the score, detects a ×2.0 demand spike **1 tick** after it is injected, and
served **0.882** of the demand over a six-day run with **zero allocation
failures** — while beating its own rule-based baseline by **34 %** over the
forecast horizon.

---

## Definition of done — status

| Requirement | Status | Evidence |
|---|---|---|
| Demand forecasting per (station, fuel) | ✅ | `forecasting.py` — baseline + learned, 12 forecasts per cycle |
| Profile baseline × learned model with confidence intervals | ✅ | Both predictors, both scored on the same split |
| Hours-to-stockout and stockout probability | ✅ | `risk.py`, live on `/api/risk` |
| Risk levels `OK / WATCH / CRITICAL` | ✅ | Thresholds at 8 h and 24 h of cover |
| Demand anomalies | ✅ | z-score against the profile + EWMA drift |
| Abnormal inventory change | ✅ | Tick-span aware, deliveries accounted for |
| Bottlenecks (dispatch, route, supply, depot stock, outage) | ✅ | Five checks, each only fires when binding |
| Model-unavailable fallback to a rule-based predictor | ✅ | `reason="not_trained"`, `reason="worse_than_baseline"` |
| Low confidence sets `needs_human_review=true` | ✅ | Every fallback forecast is flagged |
| `GET /api/alerts` and `GET /api/risk` with the signals | ✅ | 10 new endpoints, every score carries its signals |
| Metrics: prediction error, confidence, alert rate, fallback count | ✅ | 9 new Prometheus series |
| Multi-day stepped run for training and evaluation | ✅ | 600 ticks = 6.25 simulated days |
| Documented in `docs/model-evaluation.md` | ✅ | Real numbers, two defects found and fixed |
| `demand_spike` detection-time test | ✅ | Detected 1 tick after the spike |
| Unit tests for predictor, anomaly detector and fallback | ✅ | 213 backend unit tests, all passing |
| **`/api/risk` returns sensible, explained risk** | ✅ | Every risk carries 7 signals and a recommendation |
| **A `demand_spike` changes the risk within a few ticks** | ✅ | Anomaly in 1 tick, risk level `OK`→`WATCH` in 2 ticks, fuel dispatched in 4 |

---

## What was done

### 1. The layer is four small modules, not one big one

```
backend/app/intelligence/
├── profiles.py     the simulator's demand model, as data
├── forecasting.py  two predictors and the one that picks between them
├── risk.py         hours-to-stockout, stockout probability, risk levels
├── detection.py    demand anomalies, inventory anomalies, bottlenecks
├── dispatcher.py   risk → shipment
└── engine.py       drives all of it over one world state
```

`profiles.py` is the foundation. The guide publishes the demand profiles, the
hour-of-day factors and the noise bounds, and the simulator's own engine
applies them like this:

```
base   = daily_liters / 96          # 96 ticks per simulated day
factor = hour_factor × region_factor × station_multiplier
demand = base × factor × jitter      # jitter = 1 ± noise
```

The first two factors are **deterministic — we know them exactly**. Only the
jitter is random. That split is the whole basis of the layer: the
deterministic part is a baseline we can always compute, and the learned model
only has to explain what the baseline misses.

### 2. Two predictors, and the one that picks between them

**The baseline** holds the station's multiplier flat across the horizon and
shapes it with the known profile. It needs no training, it never fails, and it
still follows a spike that has already started. Its interval comes from the
profile's published noise bound.

**The learned model** is a gradient-boosting regressor on the *multiplier*,
not on litres. The target is the ratio between what a station actually drew
and what the profile says it should draw. That ratio is the same quantity for
every station and every fuel, so **one model serves the whole network**.

Features: the multiplier's own lags, its recent mean, its trend, the
calendar, the demand profile, the region factor and the station's own
multiplier.

**The selector** chooses per forecast and records why. It falls back to the
baseline when the model is untrained, has too little data, is not confident
enough, raises, **or does not beat the baseline on a held-out split**.

That last condition is the important one, and §4 explains what it caught.

### 3. The score is explainable by construction

Every risk assessment carries the signals that produced it:

```json
{
  "station_id": "station-mirpur",
  "fuel_type": "DIESEL",
  "risk_level": "CRITICAL",
  "hours_to_stockout": 5.3,
  "stockout_probability": 1.0,
  "risk_score": 0.87,
  "model": "learned",
  "confidence": 0.949,
  "needs_human_review": false,
  "recommendation": "Ship diesel now; the station has 5.3 h of cover.",
  "signals": [
    {"name": "inventory_liters", "value": 6510.5, "detail": "6,511 L on hand"},
    {"name": "capacity_liters",  "value": 15000.0, "detail": "15,000 L capacity"},
    {"name": "incoming_liters", "value": 0.0, "detail": "nothing on the way"},
    {"name": "forecast_demand", "value": 4947.3, "detail": "4,947 L expected over 24 h"},
    {"name": "hours_to_stockout", "value": 5.3, "detail": "5.3 h of cover left"},
    {"name": "stockout_probability", "value": 1.0, "detail": "the forecast exceeds the stock"}
  ]
}
```

The same signals, the same forecasts and the same risk rows are written to
Postgres, so a decision can be explained after the fact without re-running
anything.

### 4. What the evaluation found

The full numbers are in [`docs/model-evaluation.md`](../model-evaluation.md).
Four defects came out of it, and none of them was visible from reading the
code.

#### The learned model was losing to the rule

The first evaluation run reported the model *worse* than the baseline. Rather
than accept that, the cause was found and fixed.

**The first cause was the forecast design.** The forecast was recursive: the
prediction for tick T+1 became the lag for tick T+2. Over 96 steps, every
error fed back into the next, and the model drifted back toward the mean
multiplier. A station's demand level is a **level**, not a random walk, so
the right thing to carry forward is the level, not the last prediction.

The forecast is now **anchored**: every step is predicted from the observed
history, with the station's current level carried forward.

**The second cause was data.** `POST /admin/reset` wipes the simulator's
world and restarts the clock at tick 0. The stored demand history was kept,
so the old run's tick 1 sat next to the new run's tick 1 — one series with a
discontinuity in the middle. The forecaster was learning from a jump that
never happened. The stored history is now dropped when the clock goes
backwards.

**The third cause was the benchmark itself.** The horizon validator scored
the learned model against the rule-based baseline over the same replay
window — but it took the baseline's multiplier from the **end** of that
window. The baseline was being handed the answer it was asked to predict,
while the model only ever saw the history before it. The model lost to a
rule that had been given the future.

Fixing it means giving the baseline exactly what the model was given. The
model then won on both error measures, and the selector kept it.

| Horizon error over 96 ticks | Learned model | Baseline | Verdict |
|---|---|---|---|
| Baseline read from the end of the window | 159.2 L | 111.6 L | model loses → discarded |
| Baseline read from before the window (**fixed**) | **78.1 L** | 118.1 L | model wins → **kept** |

| Design | Horizon error | Baseline | Verdict |
|---|---|---|---|
| Recursive | 866 L | 110 L | baseline wins — the model was rejected |
| **Anchored** | 800 L | 1,262 L | learned wins by 37 % |
| **Anchored + honest benchmark** | **78.1 L** | 118.1 L | learned wins by **34 %** |

> A benchmark that flatters the incumbent is worse than no benchmark. The
> model was being tested against a rule that had been given the future, and
> the platform acted correctly on a dishonest result — it threw away a good
> model because the comparison was rigged.

**The fourth cause was the selector itself.** Two checks decide whether the
model is trusted: the one-step score and the horizon score. The code checked
the one-step score *first* and returned as soon as it failed — which made
the horizon check, described in the source as "the decisive check",
unreachable dead code.

The effect was not subtle. One injected spike inside the validation split is
enough to make the model look worse one step ahead, and the selector threw
away a model that was winning the horizon by more than 2×:

```
one-step:  model 0.0940 vs baseline 0.0601   <- the model loses here
horizon:   model 1,227 L vs baseline 2,729 L   <- the model wins by 2.2x
active:    baseline                           <- wrong
```

The horizon check now runs first, because it is the measure that matches how
the model is *used*. The one-step score only decides when there is no
horizon score to judge by.

#### The selector did its job

The platform refused to use a model that was losing, and fell back for 272
cycles of the run. That is the intended behaviour, not a failure: **the
fallback is a first-class path**, and the counters say which reason fired
each time.

```
fallback_activations_total{reason="not_trained"} 272
```

Once the benchmark was fixed the model was trusted for the whole run:
`models used: {'learned': 12}`.

### 5. Detection speed

A ×2.0 demand spike on `station-mirpur` was detected **1 tick** after it was
injected.

The detector compares the observation against the **published profile**, not
against the current forecast. The simulator's `demand_multiplier` rises during
a spike, so a detector that compared against it would explain the spike away
with the very thing that caused it.

Two signals are used because they catch different things: a **z-score** for a
single large deviation, and an **EWMA drift** for a sustained shift that no
single tick would reveal.

### 6. The dispatcher moves fuel

The simulator never refills a station on its own — fuel only moves when
someone calls `POST /v1/allocations`. So the risk assessment is not
informational: it is what decides whether fuel moves.

| Behaviour | Detail |
|---|---|
| Only WATCH and CRITICAL stations | An OK station is never touched |
| The quantity is the deficit | Over a 24 h target cover, capped by the tank's capacity |
| A CONSTRAINED depot is still used | The simulator accepts it; refusing would stop shipping exactly when the network is under pressure |
| Alternative routes are tried | A station is not short because one route's depot happens to be empty |
| One shipment per station, fuel and tick | The simulator's idempotency key is permanent |
| Every decision is recorded | In `decision_records`, with its rationale |

The result: **467,131 L dispatched, 1,506 L lost in transit (0.3 %)**, and
every station between 83 % and 95 % served except the one furthest from its
supply.

### 7. Observability

Nine new Prometheus series:

| Series | Type | What it answers |
|---|---|---|
| `prediction_error_mae_liters{model}` | gauge | How wrong is the forecast, in litres |
| `prediction_interval_coverage_ratio{model}` | gauge | Did the actual demand land inside the interval |
| `model_confidence{model}` | gauge | How much the platform trusts the forecast |
| `forecasts_produced_total{model}` | counter | Which model is answering |
| `risk_assessments_total{level}` | counter | The alert rate |
| `anomalies_detected_total{kind}` | counter | Detection volume |
| `bottlenecks_detected_total{kind}` | counter | Detection volume |
| `fallback_activations_total{reason}` | counter | How often the fallback engaged, and why |
| `needs_human_review_total{kind}` | counter | How often a human has to look |

The fallback counter counts **once per evaluation cycle**, not once per
forecast. Twelve forecasts a cycle would otherwise turn one degraded model
into twelve "fallback activations" and bury the signal — the same mistake
Day 2 fixed in `degraded_mode_activations_total`.

---

## Bugs the verification caught

| # | What happened | Why it mattered | Fix |
|---|---|---|---|
| 1 | **Fuel in transit to one station counted as cover for every station** | Every station's risk was computed against stock that was going somewhere else. It hid the shortages the risk engine exists to surface, for all four stations at once. | `InTransitShipment` carries its destination; the risk engine filters on it. Two regression tests. |
| 2 | **The learned model's lag features were inverted** | `lag_1` was the *oldest* observation and `lag_3` the newest, so the trend feature pointed backwards. | `_recent_multipliers` returns newest-first. |
| 3 | **Inventory anomalies compared across an unknown number of ticks** | Two evaluations are several ticks apart, and fuel is consumed every tick. Comparing a multi-tick drop against one tick of demand made every normal refill a false *critical* alert. | The tick span is tracked, and deliveries inside the window are counted. |
| 4 | **The dispatcher refused to ship from a CONSTRAINED depot** | The simulator explicitly allows it. The platform stopped shipping exactly when the network was under pressure. | `CONSTRAINED` is accepted, with the reasoning in the docstring. |
| 5 | **The dispatcher gave up if the first route's depot was empty** | A station is not short because one depot is. | Every available route is tried, fastest first. |
| 6 | **A second shipment in the same tick would be rejected as a key mismatch** | The simulator's idempotency key is permanently occupied, and the body differs once the depot's stock changes. | One shipment per station, fuel and tick, plus a duplicate guard. |
| 7 | **The shipment floor rejected a depot's last 100 L of allowance** | The floor was applied to the final quantity instead of the deficit, so a depot with 100 L left shipped nothing. | The floor judges the *deficit*; the quantity is then bounded by whatever is actually available. |
| 8 | **The dispatcher planned against the cached tick** | The simulator had moved on by the time it submitted, so the platform believed an allowance it had already spent. 157 rejections, all `DISPATCH_CAPACITY_EXCEEDED`. | The current tick is read fresh immediately before dispatching. |
| 9 | **A simulator reset mixed two timelines in the stored history** | The forecaster learned from a discontinuity that never happened, and the model looked worse than it was. | The stored history is dropped when the clock goes backwards. |
| 10 | **The learned forecast drifted back to the mean over the horizon** | 96 recursive steps compound every error. The model lost to the baseline and the selector correctly rejected it. | The forecast is anchored to the observed level. |
| 11 | **`fallback_activations_total` read 3,158,016** | It counted once per forecast, so it was noise rather than a signal. | Counted once per evaluation cycle. |
| 12 | **Training was quadratic in the history size** | The multiplier history was recomputed for every row. | Computed once per (station, fuel) and reused. |
| 13 | **The prediction scoring scanned the whole table every cycle** | It grew without bound as the run went on. | Bounded to a 200-tick window. |
| 14 | **Training was selected on a one-step score but deployed on a 96-tick recursive forecast** | The model was being judged by a yardstick that did not match how it was used. | The model is now scored over the horizon, and the horizon score is what the selector uses. |
| 15 | **The horizon benchmark handed the baseline the future** | The baseline's multiplier was taken from the *end* of the replay window while the model only saw the history before it. The model lost to a rule that had the answer, and the platform correctly threw the model away. | The baseline now uses the history before the window, exactly like the model. The model wins by 34 %. |
| 16 | **The baseline's "recent" window was the 12 *largest* multipliers in the history** | `sorted(multipliers.values())` sorts by value, not by tick. One early spike permanently biased every forecast that used the anchor. | Sorted by tick. Two regression tests. |
| 17 | **The evaluation stepped the simulator faster than the platform could act** | The world ran away from the platform: most ticks were never seen by anything that could ship against them. The service level measured the step rate, not the policy. | `--step-pause 1.1` paces the world to the platform's own sync cycle. 0.763 becomes 0.882 for the same code. |
| 18 | **A zero-quantity dispatch was submitted** | The simulator rejects it, and the rejection looked like a capacity problem. | Quantities below the floor are not submitted at all. |
| 19 | **The one-step check vetoed the model before the horizon check ran** | The horizon check was dead code. A model that lost one step ahead — one spike in the validation split was enough — was discarded even when it won the horizon by 2.2×. | The horizon check runs first, because it is the measure that matches how the model is used. The one-step score only decides when there is no horizon score. |

Twelve of the nineteen were only findable by running the thing. None of them
would have surfaced in a unit test written from the intended design — which
is the argument for the evaluation script existing at all.

---

## How to run

### The whole stack

```bash
cp .env.example .env          # change the passwords
docker compose up --build
```

| Service | URL |
|---|---|
| Backend API | http://localhost:8001 |
| Simulator | http://localhost:8000 (`/admin` console, `/docs`) |
| Frontend | http://localhost:5173 |
| Prometheus | http://localhost:9090 |
| Grafana | http://localhost:33000 → `admin` / `.env` |

### The intelligence endpoints

```bash
# The risk, with the signals behind each score
curl -s http://localhost:8001/api/risk | jq '.risks[] | {station_id, fuel_type, risk_level, hours_to_stockout}'

# One station's forecast, with its confidence band
curl -s "http://localhost:8001/api/forecast?station_id=station-mirpur" | jq '.forecast'

# The anomalies
curl -s http://localhost:8001/api/anomalies | jq '.anomalies[] | {kind, entity_id, severity, message}'

# The dispatch decisions and their outcomes
curl -s http://localhost:8001/api/decisions | jq '.decisions[] | {station_id, fuel_type, quantity, status, outcome}'

# Which model is answering, and why
curl -s http://localhost:8001/api/model-status | jq

# The intelligence metrics
curl -s http://localhost:8001/metrics | grep -E '^(prediction_error|model_confidence|fallback_activations)'
```

### The evaluation

```bash
# The full run: train, evaluate, spike, watch. The step pause matters —
# it paces the world to the platform's own sync cycle.
python scripts/day3_evaluation.py --ticks 600 --watch-ticks 40 --step-pause 1.1

# The risk *level* transition on a station that still holds stock
python scripts/spike_response.py --ticks 120 --multiplier 3.0

# Whether the model beats an honest baseline
python scripts/horizon_probe.py
```

### Tests

```bash
cd backend
pytest -q              # 216 unit tests
ruff check .
ruff format --check .
```

---

## Blockers and risks

**No hard blockers.** The simulator behaves as documented apart from the ten
discrepancies already recorded, and the two new findings in this log.

| Risk | Impact | Mitigation |
|---|---|---|
| **The simulator's total supply is 25 % below its total demand** | Service level cannot approach 1.0. Any comparison of policies on absolute service level is meaningless. | Measured and documented. Per-station rates and unmet litres are the comparable numbers. The platform reached 0.882 in the paced run. |
| **The evaluation must be paced to the platform's sync cycle** | Stepping the simulator flat out lets the world run away from the platform, and the resulting service level measures the step rate rather than the policy. | `--step-pause 1.1` paces the world to the platform's own cycle. The difference is 0.763 (unpaced) against 0.882 (paced) for the same code. |
| The learned model is trained and evaluated on one scenario and one seed | The results show the platform learns *this* world's demand model. They do not show generalisation to another seed. | Documented as a limitation. The baseline comparison is the honest measure, and it is re-checked on every retrain. |
| The horizon validator replays a 96-tick forecast for every station and fuel | Training does more work than before. | It runs once per training cycle (every 5 minutes), not per forecast. |
| The forecast horizon was 24 ticks (6 h) | The platform could only see a station running dry 6 hours ahead — too late to ship fuel to it. | Raised to 96 ticks (one simulated day), which matches the simulator's own resupply cadence. |

### Environment notes

- Ports `8000`, `5432` and `3000` were already taken on the development
  machine, so the local `.env` moves the simulator to `8100`, Postgres to
  `55432` and Grafana to `33000`. The committed defaults are the standard
  ports.
- The cross-service suite resolves its target URLs the same way — environment
  variable, then `.env`, then the committed default.

---

## Next — Day 4

1. **The decision engine.** A constraint-aware allocator that pre-validates
   every simulator rule — route mismatch, depot and station closed, route
   disrupted, route capacity, inventory, dispatch capacity and destination
   capacity — before anything is submitted.
2. **Human approval.** Recommendations start as `PROPOSED` and only reach the
   simulator after an operator approves them. The dispatcher's relief valve
   becomes an explicit, switchable mode rather than the default path.
3. **Route choice and shipment splitting.** Shipments above `max_shipment`
   are split, and transit time is weighed against the route cost.
4. **A policy comparison.** The allocator against a naive "fill the emptiest
   station first" baseline, on `service_level`, in `docs/policy-comparison.md`.
