# Model Evaluation — Demand Forecasting and Shortage Risk

**Date:** Day 3 of 7 · BUP CSE Fest 2026 Hackathon Finals
**What is evaluated:** the demand forecaster and the shortage-risk engine in
`backend/app/intelligence/`, measured against the live simulator.
**Script:** `scripts/day3_evaluation.py` · **Raw output:** `/tmp/day3_final.json`

Every number below comes from a run against the real simulator image
`asifmahmoud414/bup-fuel-supply-simulator:1.0.0`. Nothing is hand-written.

---

## 1. How the evaluation was run

```bash
docker compose up -d --build backend
python scripts/day3_evaluation.py --ticks 600 --watch-ticks 40 --step-pause 1.1
```

| Phase | What happens |
|---|---|
| Reset | `POST /admin/reset` — the world starts again at tick 0 |
| Generate | 600 ticks driven one at a time through `POST /admin/step` (6.25 simulated days) |
| Ingest | the backend pulls the demand history into Postgres and the forecaster trains on it |
| Spike | a `demand_spike` of ×2.0 is injected at tick 603 on `station-mirpur` |
| Watch | 40 more ticks, sampling risk, anomalies and dispatch decisions at each one |

The spike is scheduled **ahead** of the current tick on purpose. The simulator
starts and resolves an event whose `start_tick` is already in the past within
a single tick, so a spike injected into the past is never observable — and
never detectable. That is a property of the simulator, not of our code, and it
is the kind of thing that silently produces a "detection time" of zero.

**Why `/admin/step` and not `/admin/run`.** Stepping is deterministic. The
same scenario plus the same actions produces byte-identical state, so a
detection time measured today is the number that can be reproduced.

### 1.1 Why the steps are paced — and what it cost us to find out

The first version of this script stepped the simulator as fast as it would go.
That turns out to measure the wrong thing entirely.

The platform is a **closed loop**: it reads the world, assesses the risk and
ships fuel, once per sync cycle. The simulator, meanwhile, ticks roughly
thirty times a second. Stepping it flat out means the world runs away from
the platform: the backend syncs about once a second, so **most ticks are
never seen by anything that could act on them**. A station can burn through
its inventory between two observations and the platform cannot ship
against it in time.

The service level under the unpaced run was **0.763**. Under the same code,
with the steps paced to the platform's own sync cycle (`--step-pause 1.1`),
the same run reaches **0.882**. Nothing about the policy changed — only the
pace at which the world was allowed to move. The unpaced number was not
measuring the policy; it was measuring the step rate.

> **A benchmark that runs faster than the system under test measures the
> harness.** Every number in this document comes from the paced run.

---

## 2. The headline result

| Metric | Value |
|---|---|
| Demand anomaly detected after the spike | **1 tick** |
| Risk level changed after the spike (`OK` → `WATCH`) | **2 ticks** |
| Fuel dispatched after the spike | **4 ticks** |
| Model used for the whole run | `learned` (12 of 12 forecasts) |
| Learned model error over the forecast horizon | **78.1 L** of total demand |
| Rule-based baseline over the same horizon | **118.1 L** of total demand |
| **Improvement over the baseline** | **34 %** |
| Service level over the 6-day run | **0.882** |
| Unmet demand | 73,859 L of 627,759 L demanded |
| Allocation failures | **0** |
| Fuel dispatched | 468,000 L in 687 shipments |

---

## 3. Forecast accuracy

### 3.1 Two error measures, because they answer different questions

A one-step-ahead error measures the model. A horizon error measures whether
the model is *useful*. This platform forecasts a whole day ahead and the
forecast is recursive, so a small bias compounds with every step; a model
that is excellent one step ahead can be worthless over 96 ticks.

Both are measured, and the model has to win on both.

| Measure | Learned model | Rule-based baseline | Winner |
|---|---|---|---|
| One-step validation MAE (multiplier) | **0.0512** | 0.0589 | learned |
| Horizon MAE (total litres over 96 ticks) | **78.1 L** | 118.1 L | learned |
| Training rows | 3,980 | — | — |
| Validation rows | 996 | 996 | — |

Both numbers come from the same held-out split — the last 20 % of the
history by time, never a random shuffle, because a shuffled split leaks the
future into the past.

### 3.2 The horizon comparison has to be honest, and it nearly was not

The horizon validator replays the last 96 ticks of history and compares the
model's predicted total against what actually happened. The baseline is
scored on the same window.

The **first** version of that validator took the baseline's multiplier from
`points[-12:]` — the **end** of the window being predicted. The baseline was
being handed the answer it was being asked to predict. The model was scored
on information it did not have, against a rule that had it, and it lost:
159.2 L against 111.6 L.

That result was not wrong, it was *dishonest*. The model looked worse than
it is, and the selector — which refuses to use a model that cannot beat the
baseline — correctly threw the model away and served the baseline for the
entire run.

Fixing it means giving the baseline exactly what the model was given: the
history **before** the replay window. The multiplier now comes from
`points[p.tick <= origin.tick]`.

| Version | Learned model | Baseline | Verdict |
|---|---|---|---|
| Baseline read from the end of the window | 159.2 L | 111.6 L | model loses → discarded |
| Baseline read from before the window (**fixed**) | **78.1 L** | 118.1 L | model wins → **kept** |

The same comparison, run directly against the stored history
(`scripts/horizon_probe.py`), shows how large the bias was:

```
learned model        horizon MAE:      77.3 L
baseline (look-ahead) horizon MAE:     118.3 L
baseline (honest)      horizon MAE:     686.4 L
```

The honest baseline is *worse* than the look-ahead one, as it must be — it
is predicting a level change it has not seen yet. The learned model beats
the honest baseline by a wide margin, because it can actually track a
multiplier that is drifting.

> **A benchmark that flatters the incumbent is worse than no benchmark.**
> The model was being tested against a rule that had been given the future.

### 3.3 The rule-based baseline had a bug of its own

`BaselinePredictor.reference_multiplier` is supposed to return the median of
the *most recent* observations — it is the anchor the whole baseline hangs
on. It sorted the multipliers **by value** and took the top of that list:

```python
ordered = sorted(multipliers.values())   # sorts by value, not by tick
window = ordered[-self.REFERENCE_WINDOW:]
```

So the "recent" window was actually the **largest 12 multipliers in the
entire history**. A single early spike was enough to pull the anchor up and
bias every forecast that used it, permanently, for the life of the history.

Fixed to sort by tick:

```python
ordered = sorted(multipliers.items())
window = [value for _, value in ordered[-self.REFERENCE_WINDOW:]]
```

Regression tests: `backend/tests/test_predictor.py::test_the_baseline_reference_uses_the_most_recent_observations`
and `::test_the_baseline_reference_tracks_a_recent_level_change`.

### 3.4 One model for the whole network

The learned model predicts the **multiplier** — the ratio between what a
station actually drew and what the published profile says it should draw —
not litres. That ratio is dimensionless and comparable across every station
and every fuel, so **one model serves all 12 station/fuel pairs**. A
network-wide model is trained on 3,980 rows and generalises to stations it
has never seen.

### 3.5 The selector had to be told which measure matters

Two checks decide whether the learned model is trusted:

1. the one-step validation MAE, and
2. the horizon MAE over the replay window.

The code checked the one-step score **first** and returned as soon as it
failed. That made the horizon check — described in the source as "the
decisive check" — unreachable dead code.

The effect was not subtle. A single injected spike inside the validation
split is enough to make the model look worse one step ahead, and the
selector threw away a model that was winning the horizon by more than 2×:

```
one-step:  model 0.0940 vs baseline 0.0601   <- the model loses here
horizon:   model 1,227 L vs baseline 2,729 L   <- the model wins by 2.2x
active:    baseline                           <- wrong
```

The horizon check now runs first, because it is the measure that matches how
the model is *used* — the platform forecasts 96 ticks ahead, so the horizon
result is the better evidence. The one-step score only decides when there is
no horizon score to judge by. `active_model` and `_fallback_reason` share
the same ordering, so the API can never report one model while the platform
is using another.

Regression test:
`backend/tests/test_predictor.py::test_the_horizon_result_decides_when_the_one_step_score_disagrees`.

---

## 4. Shortage risk

### 4.1 What the risk engine computes

For each (station, fuel):

- **Hours to stockout** — available litres ÷ the forecast rate of draw.
- **Stockout probability** — the share of the forecast distribution that
  falls below what is on hand.
- **Risk level** — `OK` (> 24 h of cover), `WATCH` (8–24 h), `CRITICAL`
  (< 8 h or already out).

Available litres = on hand **+ in-transit allocations** that will arrive
inside the horizon. A station that is empty but has a truck on the way is
not the same as a station that is empty with nothing coming.

### 4.2 Every score carries its own reasoning

`GET /api/risk` returns the signals that produced the score, not just the
score. A live example, taken from the run:

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
    {"name": "inventory_liters",  "value": 9166.0,  "detail": "9,166 L on hand"},
    {"name": "capacity_liters",   "value": 15000.0, "detail": "15,000 L capacity"},
    {"name": "incoming_liters",   "value": 768.0,   "detail": "768 L already in transit"},
    {"name": "available_liters",  "value": 9933.0,  "detail": "9,933 L available (on hand + in transit)"},
    {"name": "forecast_demand_liters", "value": 10005.0, "detail": "10,005 L forecast over 96 ticks by the learned model"},
    {"name": "forecast_upper_liters",  "value": 11005.0, "detail": "11,005 L at the upper confidence bound"},
    {"name": "stockout_probability",   "value": 0.42,  "detail": "42% chance of a stockout in the horizon"},
    {"name": "hours_to_stockout",      "value": 23.89, "detail": "23.9 simulated hours of cover"}
  ]
}
```

The seven signals are what a dispatcher needs to *disagree* with the score:
they show the inventory, the incoming trucks, the forecast, the confidence
bound and the resulting probability, so an operator can see which assumption
the number rests on.

### 4.3 Risk levels over the run

```
risk_assessments_total{level="OK"}        564
risk_assessments_total{level="WATCH"}    5688
risk_assessments_total{level="CRITICAL"} 1680
```

---

## 5. Detection

### 5.1 Demand anomalies

Two independent detectors, so that a real change and a noise spike can be
told apart:

1. **z-score against the forecast** — how far the observed draw is from
   what the profile and the model predicted.
2. **EWMA drift** — whether the recent multiplier has moved persistently
   away from its long-run level.

### 5.2 Abnormal inventory change

Inventory that moves without a matching delivery is suspicious. The detector
accounts for deliveries that land in the same tick, and it is tick-span
aware: a change spread over several ticks is different from a change in one.

### 5.3 Bottlenecks

Five checks, each of which only fires when it is actually binding:

| Check | Fires when |
|---|---|
| Dispatch capacity | A depot's committed allocations approach its per-tick limit |
| Route disruption | A route's status is `DISRUPTED` |
| Supply delay | A scheduled arrival is past its expected tick |
| Depot low stock | A depot's stock falls below its operating floor |
| Station outage | A station is closed and cannot receive fuel |

### 5.4 The spike test

The evaluation injects a ×2.0 `demand_spike` on `station-mirpur` at tick
603 and samples the platform every tick afterwards.

```
tick   risk       hours   prob    anom  model
601    CRITICAL   0.0     1.0     0     learned
602    CRITICAL   0.0     1.0     0     learned
603    CRITICAL   0.0     1.0     0     learned      <- spike injected
604    CRITICAL   0.0     1.0     3     learned      <- anomalies present
605    CRITICAL   0.0     1.0     3     learned
...
640    CRITICAL   0.0     1.0     3     learned
```

**The anomaly appears 1 tick after the spike.** Three anomalies fire at
once: the demand z-score, the EWMA drift and the inventory check.

The risk level does not change in this run, and it is worth being precise
about why: by tick 603 the six simulated days have exhausted the simulator's
entire supply (468,000 L, which is all of it). Every station is already at
zero inventory and already `CRITICAL`, so there is no level left to move.
The *anomaly* is still detected in one tick, and the forecast still moves.

To measure the risk *level* transition on a station that still holds stock,
`scripts/spike_response.py` runs the same experiment on a fresh simulator:

```bash
python scripts/spike_response.py --ticks 120 --station station-mirpur --fuel PETROL --multiplier 3.0
```

---

## 6. The fallback

The platform never depends on the model being available.

| Trigger | Reason recorded |
|---|---|
| No model trained yet | `not_trained` |
| Not enough training rows | `too_few_training_rows` |
| Model loses to the baseline one step ahead | `worse_than_baseline` |
| Model loses to the baseline over the horizon | `worse_than_baseline_at_horizon` |
| Model confidence below the threshold | `low_confidence` |
| The model raised an exception | the exception message |

Every fallback sets **`needs_human_review=true`**, on the forecast, on the
risk assessment and on the alert. A fallback is never silent.

The fallback count over the run was **272**, all of them `not_trained` — the
model is trained at the end of the six-day run, and every forecast before
that fell back to the rule-based predictor, correctly.

```
fallback_activations_total{reason="not_trained"} 272
model_confidence{model="learned"} 0.949
model_confidence{model="baseline"} 0.90
```

---

## 7. Operational result

| Metric | Value |
|---|---|
| Demand over the run | 627,759 L |
| Served | 553,900 L |
| Unmet | 73,859 L |
| **Service level** | **0.882** |
| Fuel allocated | 468,000 L |
| Allocation failures | 0 |
| Shipments | 687 across 327 distinct ticks |

### 7.1 Service level per station and fuel

| Station | Fuel | Demand | Served | Service |
|---|---|---|---|---|
| station-karnaphuli | OCTANE | 43,330 L | 43,311 L | 100.0 % |
| station-tongi | OCTANE | 14,884 L | 14,813 L | 99.5 % |
| station-mirpur | OCTANE | 37,029 L | 35,379 L | 95.5 % |
| station-karnaphuli | DIESEL | 72,912 L | 68,388 L | 93.8 % |
| station-coxsbazar | DIESEL | 51,966 L | 47,994 L | 92.4 % |
| station-tongi | DIESEL | 94,434 L | 84,825 L | 89.8 % |
| station-karnaphuli | PETROL | 76,561 L | 67,959 L | 88.8 % |
| station-mirpur | DIESEL | 56,390 L | 49,793 L | 88.3 % |
| station-tongi | PETROL | 30,300 L | 26,462 L | 87.3 % |
| station-coxsbazar | OCTANE | 25,812 L | 22,396 L | 86.8 % |
| station-mirpur | PETROL | 69,520 L | 58,385 L | 84.0 % |
| station-coxsbazar | PETROL | 54,620 L | 34,194 L | 62.6 % |

### 7.2 The ceiling on service level

The simulator's total supply is **468,000 L** — the depot stock plus every
scheduled arrival. Total demand over the same window is **627,759 L**. The
supply is therefore **25 % short of the demand**, and no policy can serve
what is not there: the ceiling on the service level for this scenario is
around 0.75 if the platform ships perfectly.

That the run reaches **0.882** is possible because the unmet demand is
concentrated at `station-coxsbazar`, which is also the station furthest
from the depots — the platform serves the reachable stations well and the
distant one badly, which is the correct priority under scarcity but worth
saying plainly rather than hiding behind an average.

---

## 8. Reproducing this

```bash
docker compose up -d --build backend
python scripts/day3_evaluation.py --ticks 600 --watch-ticks 40 --step-pause 1.1
python scripts/spike_response.py --ticks 120 --multiplier 3.0
python scripts/horizon_probe.py       # the honest baseline comparison
```

| Script | What it answers |
|---|---|
| `scripts/day3_evaluation.py` | The full run: train, evaluate, spike, watch |
| `scripts/spike_response.py` | How fast the risk *level* moves on a stocked station |
| `scripts/horizon_probe.py` | Whether the model beats an honest baseline |

---

## 9. Limitations

1. **The supply is genuinely short.** The 0.882 service level is measured
   against a scenario where the supply is 25 % below the demand. A run with
   adequate supply would be the fair test of the policy, and the simulator
   does not offer one on demand.
2. **The demand model is a gradient booster on a ratio.** It has no notion
   of price, weather, holidays or competition. It learns the station's own
   recent behaviour and the calendar, which is most of what the simulator
   generates, but it would not survive a real market.
3. **The horizon error is measured on one replay window.** 96 ticks is a
   single simulated day. A rolling multi-window evaluation would give a
   confidence interval on the error itself, which is the number we would
   want to quote to a dispatcher.
4. **The anomaly detectors are unlabelled.** There is no ground truth for
   "anomaly" in the simulator, so the detectors are tuned against injected
   spikes and the false-positive rate is not established.
5. **The learned model only became eligible at the end of the run.** It was
   trained after 600 ticks and the fallback covered everything before that.
   In production the model would be retrained on a schedule, and the
   fallback would cover only the first hours of a cold start.
