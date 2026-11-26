# Intelligence — model card

What the platform's intelligence layer does, how it does it, what it
measures, and where it fails.

**Scope.** Demand forecasting, shortage risk, anomaly and bottleneck
detection, and the fallback that takes over when the model is not usable.
The allocation *policy* is described in
[`docs/policy-comparison.md`](policy-comparison.md); the decisions it
drives are in [`docs/adr/0004-human-approval-before-allocation.md`](adr/0004-human-approval-before-allocation.md).

**Code.** The layer lives in [`backend/app/intelligence/`](../backend/app/intelligence/)
— `profiles.py`, `forecasting.py`, `risk.py`, `detection.py`, `engine.py` —
orchestrated by [`backend/app/intelligence_service.py`](../backend/app/intelligence_service.py).

---

## 1. Inputs and data sources

| Input | Where it comes from | Used for |
|---|---|---|
| Station, depot, route, region state | The simulator's REST endpoints, read by the sync loop into the platform's cache | Inventory, capacity, routes, dispatch limits |
| Demand history | `GET /v1/demand-history`, pulled with a `limit` and de-duplicated on insert | Training rows, the multiplier history |
| Demand profile, hour factors | The guide's published tables, transcribed into [`profiles.py`](../backend/app/intelligence/profiles.py) | The baseline prediction |
| Region demand factor, station demand multiplier | The simulator's own state | The baseline prediction, the anomaly reference |
| Simulator clock, tick | `GET /v1/instance` | The forecast horizon, the hour feature |

**The key structural fact** is that the simulator's demand is not a black box.
Its engine applies, per the guide's §8.5 and §8.6:

```
base   = daily_liters / 96          # 96 ticks per simulated day
factor = hour_factor(hour, profile)
         * region.demand_factor
         * station.demand_multiplier
demand = base * factor * jitter     # jitter = 1 +/- noise
```

The first two factors are **deterministic** — known exactly. Only the jitter
is random, and its magnitude is published as `noise`. That split is the whole
basis of the intelligence layer: the deterministic part is a strong baseline
that can always be computed, and the learned model only has to explain what
the baseline misses.

### The four published profiles

| Profile | DIESEL | PETROL | OCTANE | Noise |
|---|---:|---:|---:|---:|
| `urban_high` | 8,500 L | 10,500 L | 5,600 L | ±10 % |
| `industrial` | 14,000 L | 4,500 L | 2,200 L | ±8 % |
| `highway` | 10,500 L | 11,000 L | 6,200 L | ±12 % |
| `regional` | 7,200 L | 7,600 L | 3,600 L | ±10 % |

Per-tick daily litres: `urban_high` 8,500 L → 88.54 L/tick; `industrial`
14,000 L → 145.83 L/tick; `highway` 10,500 L → 109.38 L/tick; `regional`
7,200 L → 75.0 L/tick.

**Hour-of-day factors**, inclusive-start, exclusive-end, exactly as the
simulator applies them:

| Profile | Busy hours | Factor | Off-peak | Factor |
|---|---|---:|---|---:|
| `industrial` | 06:00–18:00 | 1.55 | else | 0.45 |
| `highway` | 06:00–10:00, 16:00–21:00 | 1.35 | else | 0.75 |
| `urban_high` | 07:00–10:00, 16:00–21:00 | 1.45 | else | 0.70 |
| `regional` | 07:00–21:00 | 1.25 | else | 0.65 |

An unknown profile name falls through to `regional`, because the simulator's
own engine does the same.

---

## 2. The profile baseline

[`profiles.py`](../backend/app/intelligence/profiles.py) computes the
deterministic expectation for one tick:

```
baseline_demand_per_tick = (daily_liters[fuel] / 96)
                          × hour_factor(hour, profile)
                          × region_demand_factor
                          × demand_multiplier
```

This is the simulator's own formula **without the jitter** — the part of the
demand that can be predicted exactly.

### The shape, and why the model predicts a ratio

`baseline_shape` is the same formula **without the station's multiplier**:

```
shape(t) = (daily_liters[fuel] / 96) × hour_factor(hour) × region_demand_factor
```

Dividing an observation by its shape gives the station's **multiplier** — the
ratio between what the station drew and what the published profile says it
should draw.

That ratio is dimensionless and comparable across every station and every
fuel, so **one model serves all 12 station/fuel pairs** — the four profiles
across three fuels. The learned model predicts the ratio, not litres.

### The baseline's reference multiplier

`BaselinePredictor` holds the multiplier **flat** across the horizon, using
the **median of the most recent** observed multipliers
(`REFERENCE_WINDOW = 12`).

> **A bug that biased every forecast.** `reference_multiplier` sorted the
> multipliers **by value** and took the top of that list, so the "recent"
> window was actually the **largest 12 multipliers in the entire history**. A
> single early spike pulled the anchor up and biased every forecast that used
> it, permanently. Fixed to sort by tick. Regression tests:
> [`test_predictor.py::test_the_baseline_reference_uses_the_most_recent_observations`](../backend/tests/test_predictor.py)
> and
> [`::test_the_baseline_reference_tracks_a_recent_level_change`](../backend/tests/test_predictor.py).

---

## 3. The learned model

A **`GradientBoostingRegressor`** from scikit-learn, predicting the demand
**multiplier**.

| Parameter | Value |
|---|---|
| `n_estimators` | 200 |
| `learning_rate` | 0.05 |
| `max_depth` | 4 |
| `subsample` | 0.9 |
| `random_state` | 42 |
| Split | First 80 % by time / last 20 % by time |
| Minimum training rows | 30 |

### The twelve features

From [`FEATURE_NAMES`](../backend/app/intelligence/forecasting.py):

| # | Feature | What it is |
|---:|---|---|
| 1 | `tick_within_day` | The tick's **position inside the simulated day** (0–95) |
| 2 | `hour` | The simulated hour of day |
| 3 | `hour_sin` | `sin(2π · hour / 24)` |
| 4 | `hour_cos` | `cos(2π · hour / 24)` |
| 5 | `multiplier_lag_1` | The most recent observed multiplier |
| 6 | `multiplier_lag_2` | The one before it |
| 7 | `multiplier_lag_3` | The one before that |
| 8 | `multiplier_mean_recent_4` | Mean of the last four observed multipliers |
| 9 | `multiplier_trend` | `lag_1 − lag_2` |
| 10 | `demand_multiplier` | The station's current published multiplier |
| 11 | `region_demand_factor` | The region's demand factor |
| 12 | `profile_index` | The station's profile, encoded numerically |

The lags are **newest-first**; the order matters, because the feature builder
reads `lag_1` as the most recent and `lag_3` as the oldest. Returning them
oldest-first would silently invert the lags and the trend feature
([`test_predictor.py::test_the_lags_are_newest_first`](../backend/tests/test_predictor.py)).

### Within-day, not absolute tick

The first feature is the tick's **position in the day**, not its absolute
number, and the difference is not cosmetic.

A gradient-boosted tree **cannot extrapolate**: a split on a feature whose
training values stop at tick 4,000 contributes nothing to a forecast about
tick 4,001, because every future tick falls into the *same* leaf as the
largest training value. An absolute-tick feature is not a prediction of the
future at all — it is a lookup of the past.

Worse, the simulator's demand is deterministic given
`(seed, tick, station, fuel)` and the seed is fixed, so an absolute-tick
feature is close to a **label leak**: the tree can memorise the answer for
the exact ticks it was trained on.

Measured against the stored history
([`scripts/horizon_probe.py`](../scripts/horizon_probe.py)):

| Feature | Model | Baseline | Difference | Pairs won |
|---|---:|---:|---:|---:|
| Absolute tick (as first shipped) | 189.2 L | 179.8 L | **+5.2 %** | 5 / 12 |
| **Tick within the day** | **132.2 L** | 179.8 L | **−26.5 %** | **7 / 12** |

The absolute-tick version was **worse than the rule**. The within-day
version is 26.5 % better.

> **A probe that had stopped being evidence.** The comparison probe borrowed
> the *shipped* builder for its "absolute tick" arm — and the shipped builder
> *is* the within-day builder, because the fix had already landed. Both arms
> were identical, and the comparison reported a confident number anyway. The
> probe now writes the absolute-tick builder out by hand so the two arms are
> genuinely different. A probe that cannot fail is not evidence.

---

## 4. Confidence intervals

Two constructions, one per predictor.

**Baseline.** The interval is the profile's own published noise bound:

```
bound = noise_bound(profile) × interval_scale
lower = expected × (1 − bound)
upper = expected × (1 + bound)
```

**Learned.** The interval comes from the model's own validation error, scaled
from multiplier units into litres by the known shape:

```
spread = max(validation_mae × shape, shape × 0.02)
lower  = max(0.0, predicted − spread)
upper  = predicted + spread
```

**Confidence** is then derived from the interval's width, on a 0–1 scale where
no error is perfect confidence and an error equal to the mean predicted demand
is worth nothing:

```
mae_litres = mean(point.width) / 2
confidence = 1.0 − min(1.0, mae_litres / mean_demand)
```

Live values from the evaluation run: `model_confidence{model="learned"}`
**0.949**, `model_confidence{model="baseline"}` **0.90**.

---

## 5. Shortage risk

[`risk.py`](../backend/app/intelligence/risk.py) answers three questions per
station and fuel.

**Hours to stockout** — the simulated hours until cumulative forecast demand
exhausts the available stock, interpolated within the tick. `None` when the
stock survives the whole horizon.

**Stockout probability** — derived from the forecast's confidence interval,
which is the honest source of uncertainty:

| Condition | Probability |
|---|---:|
| Lower cumulative bound ≥ available | 1.0 (certain) |
| Upper cumulative bound ≤ available | 0.0 (impossible) |
| In between | `overshoot / headroom`, where overshoot = upper − available and headroom = upper − lower |

**Available litres** = on hand **+ in-transit allocations** arriving inside
the horizon. A station that is empty but has a truck on the way is not the
same as one that is empty with nothing coming.

### Risk levels

The thresholds are in the code, and they are deliberately conservative:

| Constant | Value |
|---|---:|
| `CRITICAL_HOURS` | 8.0 |
| `WATCH_HOURS` | 24.0 |
| `CRITICAL_PROBABILITY` | 0.6 |
| `WATCH_PROBABILITY` | 0.25 |

**Time to stockout decides CRITICAL, not probability.** A certain stockout
sixteen hours away is not the same emergency as one two hours away, and a rule
that cannot tell them apart produces alerts nobody can act on. So:

```python
if hours_to_stockout is None:              # the stock survives the horizon
    return WATCH if probability >= WATCH_PROBABILITY else OK
if hours_to_stockout <= CRITICAL_HOURS:     # genuinely about to run dry
    return CRITICAL
return WATCH                                # will run out, not imminently
```

> **Discrepancy.** `docs/model-evaluation.md` §4.1 describes the bands as
> `OK` (> 24 h of cover), `WATCH` (8–24 h), `CRITICAL` (< 8 h or already
> out). The **8-hour boundary matches** the code, but the 24-hour boundary
> does not: `WATCH_HOURS = 24.0` is **declared and never used**. In the
> implemented rule, any station that *will* run out inside the horizon is
> `WATCH` regardless of how much cover it has left, and the 24-hour figure
> only appears as `WATCH_PROBABILITY` — a probability threshold, not an hours
> threshold. The 24-hour band as documented is **not verified** against the
> code.

The risk level counts over the evaluation run:

```
risk_assessments_total{level="OK"}        564
risk_assessments_total{level="WATCH"}    5688
risk_assessments_total{level="CRITICAL"} 1680
```

### Every score carries its reasoning

`GET /api/risk` returns the signals that produced the score, not just the
score — seven of them: `inventory_liters`, `capacity_liters`,
`incoming_liters`, `available_liters`, `forecast_demand_liters`,
`forecast_upper_liters`, `stockout_probability`, and `hours_to_stockout`.
They are what a dispatcher needs in order to **disagree** with the score.

---

## 6. Anomaly and bottleneck detection

[`detection.py`](../backend/app/intelligence/detection.py).

### Demand anomalies — two independent signals

**The z-score** compares the observation against the **published profile**,
not against the current multiplier. This matters: the simulator's demand
multiplier rises during a `demand_spike`, so comparing against the *current*
multiplier would let the platform explain the spike away with the very thing
that caused it. Comparing against the profile keeps the spike visible.

```
residual = actual − expected
scale    = max(uncertainty, |expected| × 0.05, 1e-6)
z_score  = residual / scale
```

**The EWMA drift** catches a *sustained* shift that a single tick would hide
inside the profile's published noise bound:

```
ewma = 0.3 × actual + 0.7 × previous
drift = (ewma − expected) / expected
```

| Constant | Value |
|---|---:|
| `Z_THRESHOLD` | 3.0 |
| `EWMA_ALPHA` | 0.3 |
| `EWMA_DRIFT_THRESHOLD` | 0.25 |

Severity is `critical` at `|z| ≥ 5.0` or `|drift| ≥ 0.5`, else `warning`.

The anomaly message is internally consistent: the EWMA drift and the raw
observation are two different numbers, so they are never quoted side by side.
A sustained shift quotes the *smoothed* level; a single observation quotes
the observation.

### Abnormal inventory change

Inventory that moves without a matching delivery is suspicious:

```
unexplained = (current − previous) − (deliveries − consumption)
```

Fires when `|unexplained| ≥ 50 L` **and** `|unexplained| ≥ 2.0 × scale`,
where `scale = max(|expected_change|, |consumption| × 0.25, 1.0)` — so a
busy station is not flagged for a small absolute wobble. `critical` at
`3 × scale`.

### Bottlenecks — five checks

| Check | Fires when |
|---|---|
| `dispatch_capacity` | A depot's committed allocations reach **80 %** of its per-tick limit |
| `route_disrupted` | A route's status is `DISRUPTED` |
| `supply_delayed` | A scheduled arrival is past its expected tick |
| `depot_low_stock` | A depot's stock falls below **20 %** of its operating floor |
| `station_outage` | A station is closed and cannot receive fuel |

Each fires only when it is actually binding — a healthy network produces no
bottlenecks.

### The spike test

A ×2.0 `demand_spike` is injected on `station-mirpur` at tick 603, and the
platform is sampled every tick:

| Response | Ticks after the spike |
|---|---:|
| Demand anomaly detected | **1** |
| Risk level changed (`OK` → `WATCH`) | **2** |
| Fuel dispatched | **4** |

Three anomalies fire together: the demand z-score, the EWMA drift and the
inventory check.

---

## 7. The fallback

**The platform never depends on the model being available.**

### Two distinct fallbacks

They are easy to confuse and they do different jobs.

| | Forecast-level fallback | Decision-level fallback |
|---|---|---|
| **What** | The rule-based baseline predictor answers instead of the learned model | The rule-based *policy* proposes instead of the optimizer |
| **When** | The learned model is not usable | The intelligence layer produced **no forecast or no risk assessment** |
| **Effect** | A degraded forecast, still worth acting on | A different allocation strategy — refill the lowest days-of-cover first |

The forecast-level fallback is **degraded, not broken**: it is still a
forecast and it is still worth acting on. The decision-level fallback is for
when there is no forecast at all.

The decision-level fallback runs on three paths, all through the same entry
point (`IntelligenceService.propose_now`):

| Path | Trigger |
|---|---|
| The engine raised | The evaluation failed outright |
| No forecast | The evaluation produced no forecast |
| No risk assessment | The evaluation produced no risk assessment |

### Why the learned model is not used

The selector's reasons, and the metric label each maps to:

| Reason | Condition | Label |
|---|---|---|
| The kill-switch is on | Operator disabled the model | `kill_switch` |
| The model is not trained | No model yet | `not_trained` |
| Not enough training rows | Below `min_training_rows` (30) | `insufficient_data` |
| Not enough history to featurise | Lags missing | `insufficient_history` |
| Confidence too low | Below the threshold | `low_confidence` |
| The model does not beat the rule | Gate failed | `worse_than_baseline` |

The kill-switch is checked **first**, because an operator who turns the model
off needs to see *that* on the dashboard, not a generic `model_error` — one
is deliberate, the other is a fault.

The fallback count over the evaluation run was **272**, all of them
`not_trained` — the model is trained at the end of the six-day run, and every
forecast before that fell back correctly.

### The `needs_human_review` rule

The rule is one line, in
[`risk.py`](../backend/app/intelligence/risk.py):

```python
needs_review = forecast.needs_human_review or forecast.confidence < 0.5
```

So a risk assessment needs a human when the forecast behind it asked for one
(the fallback sets it) **or** when the forecast's confidence is below 0.5.

Risk **level** does not enter this rule. The `CRITICAL` case is handled
separately, by the auto-mode's bar, which refuses to approve a `CRITICAL`
recommendation at all.

Separately, the decision layer's fallback policy **never** auto-approves, and
says so in its rationale: *"the forecast model is unavailable"*. It caps its
confidence at **0.4** — the rule has no model behind it, so it does not claim
more confidence than it deserves — and it sets `needs_human_review=True`.

---

## 8. Retraining

| Setting | Value |
|---|---|
| `MODEL_TRAIN_INTERVAL` | 300 s (5 minutes) |
| Floor | `max(60, model_train_interval)` |
| Manual trigger | `POST /api/model/train` |

The retrain runs in a background loop in
[`sync_service.py`](../backend/app/sync_service.py) — `_trainer_loop`. The
reasoning: training is not cheap and the history changes slowly, so the model
is retrained every five minutes rather than on every tick.

Until the first training completes, **the rule-based baseline answers every
forecast**. That is the 272 `not_trained` fallbacks.

> **The demo overstated it.** The demo script's last step used to say *"the
> model retrained, its confidence came back, and the platform is using it
> again — on its own, without a restart."* The platform does recover on its
> own — but **not within the demo's wait**. The model has to *earn* its way
> back, because it is only used when it beats the rule on a held-out split,
> and that score comes from a training run. The trainer's own schedule is
> five minutes, and the demo did not wait that long.

A retrain logs `training_rows` and `validation_mae`.

---

## 9. Evaluation method

### Two measures, because they answer different questions

| Measure | Learned model | Baseline | Winner |
|---|---:|---:|---|
| One-step validation MAE (multiplier) | **0.0512** | 0.0589 | learned |
| Horizon MAE (total litres over 96 ticks) | **78.1 L** | 118.1 L | learned |
| Training rows | 3,980 | — | — |
| Validation rows | 996 | 996 | — |

A one-step error measures the model. A horizon error measures whether it is
**useful** — the forecast is recursive, so a small bias compounds over 96
ticks.

Both come from the same held-out split — **the last 20 % of the history by
time, never a random shuffle**, because a shuffled split leaks the future
into the past.

### The gate

The model is used only if it beats the rule-based baseline on that split,
with a **5 % tolerance** (`BEAT_TOLERANCE`). The rule is scored on exactly
the same rows with exactly the rule it uses live.

Without the tolerance the gate was a coin toss: the model was rejected
whenever it was even one percent worse, and the platform spent its life
flipping between the learned model and the rule.

A score of zero is not a score — a model whose scores have not been computed
yet carries `0.0`, and comparing `0.0 >= 0.0` would reject a perfectly good
model. A zero on either side means "not measured yet", and the model is
given the benefit of the doubt.

### The leakage fix, and refit-per-window scoring

The horizon validator replays the recursive forecast over the held-out tail
of the history and compares the **total** demand it predicts against the
total that actually happened. Totalling is what matters operationally: the
risk engine needs the sum of the demand over the horizon.

**The leak.** The obvious implementation scores the *already trained* model on
a replay window. That is a look-ahead leak — the model was fitted on the
whole history, including the window it is then asked to predict, so the score
measures how well the model remembers rows it has already seen.

| Model | Error over the horizon | Against the honest baseline |
|---|---:|---|
| Scored with the window in its training data | 4,341 L | 13.7 % better |
| **Retrained on the prefix, then scored** | **5,035 L** | **0.1 % better** |

The leaky model wins on 11 of 12 station/fuel pairs; the honest one on 4.
That 13.7 % was the model being scored against itself, and it was the number
the platform published as its reason for trusting the model.

> These two figures are the `HorizonValidator` docstring's. The Day 7 log
> records the same two arms as 64.6 L / −60.7 % and 170.5 L / +3.7 %. See
> the discrepancy note in §10.

**The fix.** Each window is scored with a model fitted only on the history
that precedes it. That costs one extra training run per window.

**The other leak, in the baseline.** The first version of the validator took
the baseline's multiplier from `points[-12:]` — the **end** of the window
being predicted. The baseline was being handed the answer it was asked to
predict. The model was scored on information it did not have, against a rule
that had it, and it lost: 159.2 L against 111.6 L. The selector correctly
discarded the model and served the baseline for the entire run.

> **A benchmark that flatters the incumbent is worse than no benchmark.** The
> model was being tested against a rule that had been given the future.

Fixed, the same comparison against the stored history shows how large the
bias was:

```
learned model        horizon MAE:      77.3 L
baseline (look-ahead) horizon MAE:     118.3 L
baseline (honest)      horizon MAE:     686.4 L
```

The honest baseline is *worse* than the look-ahead one, as it must be — it is
predicting a level change it has not seen yet.

### Why the horizon score is a diagnostic, not the gate

The horizon score is averaged over `SCORE_WINDOWS = 4` replay windows,
because **one window is not a verdict**. Measured over the stored history,
the model beat the baseline on 11 of 12 pairs in one window, 9 of 12 in the
next, and the mean difference swung from **−53 % to +81 % and back to
−59 %**.

The platform acted on a single window's verdict, and the visible symptom was
a model trusted on one retrain and discarded on the next — for reasons that
were not true. The health panel would say *"the model does not beat the
baseline"* about a model that beat it by 79 % over four windows.

So the horizon score is **reported with its honest verdict** — it wins 10 of
24 windows and is 326 L against the rule's 250 L — and the model is gated on
the measurement that can actually be trusted.

---

## 10. The real results

Three improvement numbers appear in this project. **They measure different
things, and they do not agree.** Here is why.

### 34 % — the headline over the evaluation horizon

| Measure | Learned model | Rule-based baseline |
|---|---:|---:|
| Error over the forecast horizon | **78.1 L** | 118.1 L |
| **Improvement** | **34 %** | — |

This is the paced six-day evaluation run: the same held-out split, both
predictors scored on the same rows, with the honest baseline. It is the
number [`docs/model-evaluation.md`](model-evaluation.md) leads with, and it
is the most defensible of the three.

### 26.5 % — the within-day feature against the rule

| Measure | Learned model | Rule-based baseline |
|---|---:|---:|
| Error over the forecast horizon | **132.2 L** | 179.8 L |
| **Improvement** | **26.5 %** | — |

This compares the **within-day** feature against the same rule, over the
stored history rather than the evaluation run. The absolute error is roughly
1.7× higher than the 34 % run because the stored history covers a different,
longer window — but the *ratio* to the rule is what matters, and the
baseline's error moves with it.

The comparison's own control is the absolute-tick arm: the same model, the
same history, the same baseline — and it came out at **189.2 L against
179.8 L, i.e. 5.2 % *worse* than the rule**. The feature choice moves the
model from worse-than-the-rule to 26.5 % better. That is the number the
within-day feature earns.

### 3.7 % — the honest horizon score, and why it is *worse* than the rule

Measured against the stored history, after the leak was fixed. The sign
convention is `(model_error − baseline_error) / baseline_error`, so a
**negative** number means the model is **better**:

| Version | Model error | Difference vs the baseline |
|---|---:|---:|
| Scored with the window in its training data (**leaky**) | 64.6 L | **−60.7 %** (model better) |
| **Retrained on the prefix, then scored (honest)** | **170.5 L** | **+3.7 %** (model **worse**) |

The leaky model wins 37 of 48 window/pair comparisons; the honest one wins
25 of 48. **That 60.7 % was the model being scored against itself**, and it
was the number the platform published as its reason for trusting the model.

So the honest horizon score does not show the model beating the rule at all
— it shows it losing by 3.7 %.

> **Discrepancy.** `docs/progress/day7.md` records this table as 64.6 L /
> **−60.7 %** (leaky) and 170.5 L / **+3.7 %** (honest). The
> `HorizonValidator` docstring in
> [`forecasting.py`](../backend/app/intelligence/forecasting.py) records
> **4,341 L / 13.7 % better** and **5,035 L / 0.1 % better** for the same
> two arms. Both came from commit `426b574`. The qualitative conclusion is
> identical — the leaky score flattered the model, and the honest one does
> not beat the rule — but the magnitudes differ by two orders of magnitude
> and **no probe output for this measurement is committed**, so the specific
> figures are **not verified**. The `−60.7 %` / `+3.7 %` pair is used here
> because it is the most recent, and it is quoted with the conflict noted.

**The sign convention is worth stating**, because the two documents use it
differently: the probe prints `(model − baseline) / baseline`, so negative
means better; the docstring phrases its numbers as "better", so its
"13.7 % better" and "0.1 % better" are the *negatives* of the probe's
sign. Read that way the docstring's honest arm (0.1 % better) and day7's
honest arm (3.7 % worse) still disagree in magnitude, but not in direction.

### Why they differ

| | 34 % | 26.5 % | 60.7 % vs 3.7 % |
|---|---|---|---|
| What is measured | Total demand error over the horizon | The same, on the stored history | The same, on the stored history |
| Which history | The paced six-day evaluation | All stored history | All stored history |
| Which baseline | Honest — reads the history before the window | Honest | Honest |
| Leakage | None | None | **Leaky arm vs honest arm, side by side** |
| Model error | 78.1 L | 132.2 L | 64.6 L leaky / 170.5 L honest |

The three differ because of **history**, **leakage**, and **which arm you
are looking at** — in that order of size.

- The **34 %** and the **26.5 %** are both honest measurements, on different
  histories. The absolute error is roughly 1.7× higher in the 26.5 % case
  because the stored history covers a different, longer window — but the
  baseline's error moves with it, so the *ratio* is what carries across.
- The **60.7 % / 3.7 %** pair is not a third measurement of the same thing;
  it is the **before and after of the leak fix**. The leaky arm flattered the
  model by 60.7 %; the honest arm says it loses by 3.7 %. Taking the leaky
  number as the model's skill was the error the whole exercise existed to
  find.

**The honest summary** is in the selector's own docstring: measured
honestly, the learned model does **not** beat the rule over a whole forecast
window — it wins 10 of 24 windows and is 326 L against the rule's 250 L of
total demand error. That is a real finding, and it is published rather than
hidden. The rule's median-multiplier forecast is already very close to
exact, because the simulator's demand *is* close to "profile shape plus a
little noise".

The one-step gate is where the model wins consistently: its held-out error
sat between **0.82 and 0.90** of the rule's at every tick from 16 onwards.

> **Discrepancy.** The source gives the range two ways.
> `docs/progress/day7.md` and the `BEAT_TOLERANCE` docstring in
> [`forecasting.py`](../backend/app/intelligence/forecasting.py) say
> **0.82–0.90**; the `_model_is_beaten_by_the_baseline` and
> `_fallback_reason` docstrings in the same file say **0.83–0.90**. Both
> come from commit `426b574`. The **0.82–0.90** figure is used here because
> it is the one the progress log states; the difference is one hundredth of
> the baseline's error and does not affect the conclusion.

---

## 11. The supply ceiling

**The simulator's total supply is about 25 % below its total demand**, and no
policy can serve what is not there.

| Quantity | Value |
|---|---:|
| Total supply (depot stock + every scheduled arrival) | **468,000 L** |
| Total demand over the same window | **627,759 L** |
| Served | 553,900 L |
| **Unmet** | **73,859 L** |
| **Service level** | **0.882** |
| Allocation failures | **0** |
| Shipments | 687 across 327 distinct ticks |

The ceiling on the service level for this scenario is around **0.75** if the
platform shipped perfectly. That the run reaches **0.882** is possible
because the unmet demand is concentrated at `station-coxsbazar`, which is
also the station furthest from the depots. The platform serves the reachable
stations well and the distant one badly — the correct priority under
scarcity, but worth saying plainly rather than hiding behind an average.

The worst-served pairs:

| Station | Fuel | Demand | Served | Service |
|---|---|---:|---:|---:|
| station-coxsbazar | PETROL | 54,620 L | 34,194 L | **62.6 %** |
| station-mirpur | PETROL | 69,520 L | 58,385 L | 84.0 % |
| station-coxsbazar | OCTANE | 25,812 L | 22,396 L | 86.8 % |

The best-served is `station-karnaphuli` OCTANE at **100.0 %**.

**Any comparison of policies on absolute service level is meaningless**
under these conditions. The comparable numbers are per-station rates and
unmet litres.

---

## 12. Limitations

1. **The supply is genuinely short.** The 0.882 service level is measured
   against a scenario where supply is 25 % below demand. A run with adequate
   supply would be the fair test of the policy, and the simulator does not
   offer one on demand.
2. **The model is a gradient booster on a ratio.** It has no notion of
   price, weather, holidays or competition. It learns the station's own
   recent behaviour and the calendar — which is most of what the simulator
   generates — but it would not survive a real market.
3. **One scenario, one seed.** The model is trained and evaluated on a
   single scenario and a single seed. The results show the platform learns
   *this* world's demand model. They do not show generalisation to another
   seed.
4. **The horizon error is measured on a small number of replay windows.**
   96 ticks is a single simulated day. A rolling multi-window evaluation
   would give a confidence interval on the error itself — the number a
   dispatcher would want quoted.
5. **The anomaly detectors are unlabelled.** There is no ground truth for
   "is this an anomaly", so the thresholds are set from judgement and the
   false-positive rate is **not verified**.
6. **The `WATCH_HOURS` band is documented but not implemented.** The 24-hour
   WATCH boundary in `docs/model-evaluation.md` §4.1 is not what the code
   does. See the discrepancy note in §5.
7. **The horizon score is a diagnostic with an unresolved numeric conflict.**
   The three figures for it differ by two orders of magnitude and the
   underlying probe output is not committed.
8. **The horizon score barely beats the rule.** Measured honestly, it wins
   10 of 24 windows. The model earns its place on the one-step comparison
   and on the operational result, not on the horizon.
9. **The rule-based baseline is close to exact, by construction.** The
   simulator's demand is largely deterministic given the profile, the hour and
   the multiplier, and the rule uses the current multiplier. There is not much
   headroom above it, which bounds how large any improvement can be — and
   bounds the value of the learned model in this particular scenario.
10. **A single tick's forecast is a point estimate with a symmetric
    interval.** The interval comes from a mean absolute error scaled by the
    known shape; it is not a calibrated predictive distribution, and the
    stockout probability derived from it inherits that.

---

## 13. Reproducing this

```bash
docker compose up -d --build backend

python scripts/day3_evaluation.py --ticks 600 --watch-ticks 40 --step-pause 1.1
python scripts/spike_response.py --ticks 120 --multiplier 3.0
python scripts/horizon_probe.py
python scripts/horizon_honesty_probe.py
```

| Script | What it answers |
|---|---|
| [`day3_evaluation.py`](../scripts/day3_evaluation.py) | The full run: train, evaluate, spike, watch |
| [`spike_response.py`](../scripts/spike_response.py) | How fast the risk *level* moves on a stocked station |
| [`horizon_probe.py`](../scripts/horizon_probe.py) | Whether the model beats an honest baseline |
| [`horizon_honesty_probe.py`](../scripts/horizon_honesty_probe.py) | Whether the horizon score survives the leak |

**The pacing matters.** The evaluation must be paced to the platform's own
sync cycle. Stepping the simulator flat out lets the world run away from the
platform, and the resulting service level measures the step rate rather than
the policy: **0.763 unpaced against 0.882 paced** for the same code.

> **A benchmark that runs faster than the system under test measures the
> harness.** Every number in this document comes from the paced run.

### The tests that hold each claim

| Claim | Test |
|---|---|
| The profiles match the guide table | [`test_predictor.py::test_the_profiles_match_the_guide_table`](../backend/tests/test_predictor.py) |
| The hour factors match the guide | [`::test_hour_factors_match_the_guide`](../backend/tests/test_predictor.py) |
| The daily demand splits into 96 ticks | [`::test_the_daily_demand_splits_into_96_ticks`](../backend/tests/test_predictor.py) |
| The baseline uses the *most recent* observations | [`::test_the_baseline_reference_uses_the_most_recent_observations`](../backend/tests/test_predictor.py) |
| The baseline tracks a recent level change | [`::test_the_baseline_reference_tracks_a_recent_level_change`](../backend/tests/test_predictor.py) |
| The shape excludes the multiplier | [`::test_the_shape_excludes_the_multiplier`](../backend/tests/test_predictor.py) |
| The baseline holds the multiplier flat | [`::test_the_baseline_holds_the_multiplier_flat_across_the_horizon`](../backend/tests/test_predictor.py) |
| The lags are newest-first | [`::test_the_lags_are_newest_first`](../backend/tests/test_predictor.py) |
| The model trains and predicts | [`::test_the_learned_model_trains_and_predicts`](../backend/tests/test_predictor.py) |
| The model sees a multiplier step | [`::test_the_learned_model_sees_a_multiplier_step`](../backend/tests/test_predictor.py) |
| The baseline is scored on the same validation rows | [`::test_the_baseline_is_scored_on_the_same_validation_rows`](../backend/tests/test_predictor.py) |
| The horizon validator scores the way it is used | [`::test_the_horizon_validator_scores_the_model_the_way_it_is_used`](../backend/tests/test_predictor.py) |
| The horizon validator gives the baseline no look-ahead | [`::test_the_horizon_validator_gives_the_baseline_no_look_ahead`](../backend/tests/test_predictor.py) |
| The horizon result does not veto a good model | [`::test_the_horizon_result_does_not_veto_a_model_that_wins_the_validation_split`](../backend/tests/test_predictor.py) |
| A model within the tolerance is kept | [`::test_a_model_within_the_tolerance_is_kept`](../backend/tests/test_predictor.py) |
| The selector falls back when untrained | [`::test_the_selector_falls_back_when_the_model_is_untrained`](../backend/tests/test_predictor.py) |
| The kill-switch has its own reason | [`::test_the_kill_switch_is_counted_as_its_own_reason`](../backend/tests/test_predictor.py) |
| The fallback is counted once per cycle | [`::test_the_fallback_is_counted_once_per_evaluation_cycle`](../backend/tests/test_predictor.py) |
| A well-stocked station is OK | [`test_risk.py::test_a_well_stocked_station_is_ok`](../backend/tests/test_risk.py) |
| A station that will run dry is CRITICAL | [`::test_a_station_that_will_run_dry_is_critical`](../backend/tests/test_risk.py) |
| Moderate cover is WATCH | [`::test_a_station_with_moderate_cover_is_on_watch`](../backend/tests/test_risk.py) |
| A normal observation is not an anomaly | [`test_detection.py::test_a_normal_observation_is_not_an_anomaly`](../backend/tests/test_detection.py) |
| A sustained shift within noise is not an anomaly | [`::test_a_sustained_shift_within_the_noise_bound_is_not_an_anomaly`](../backend/tests/test_detection.py) |
| A zero-width interval gives no infinite z-score | [`::test_a_zero_width_interval_does_not_produce_an_infinite_z_score`](../backend/tests/test_detection.py) |
| A healthy network has no bottlenecks | [`::test_a_healthy_network_has_no_bottlenecks`](../backend/tests/test_detection.py) |
| The fallback needs human review | [`test_decision_fallback.py`](../backend/tests/test_decision_fallback.py), [`test_decision_fallback_rules.py`](../backend/tests/test_decision_fallback_rules.py) |

---

## Related documents

- [`docs/model-evaluation.md`](model-evaluation.md) — the full evaluation,
  with the per-station table and the reproducing scripts
- [`docs/policy-comparison.md`](policy-comparison.md) — the policy
  comparison and the supply ceiling
- [`docs/adr/0005-hybrid-forecast-with-rule-based-fallback.md`](adr/0005-hybrid-forecast-with-rule-based-fallback.md)
  — why the hybrid design
- [`docs/adr/0006-gate-model-on-held-out-one-step-comparison.md`](adr/0006-gate-model-on-held-out-one-step-comparison.md)
  — why the one-step gate
- [`docs/postmortems/07-benchmark-leaked-the-future.md`](postmortems/07-benchmark-leaked-the-future.md)
  — the three bugs in the benchmark
- [`docs/postmortems/08-fallback-never-engaged-stale-forecast.md`](postmortems/08-fallback-never-engaged-stale-forecast.md)
  — the fallback that did not engage
- [`docs/simulator-notes.md`](simulator-notes.md) — the simulator's
  documented behaviour and the discrepancies with the guide
