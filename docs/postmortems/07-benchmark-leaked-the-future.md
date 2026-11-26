# Postmortem: the benchmark that leaked the future to the baseline

**Incident:** the horizon validator gave the rule-based baseline an answer it
was being asked to predict, and the platform discarded a good model because of
it
**Day:** 3 of 7 (the intelligence layer), and revisited on Day 7
**Severity:** high
**Status:** resolved; the leak was found twice, and the second leak is the
deeper one

---

## Summary

The horizon validator replays the last 96 ticks of history and compares the
model's predicted total against what actually happened. The baseline is scored
on the same window.

The **first** version of that validator took the baseline's multiplier from
`points[-12:]` — the **end** of the window being predicted. The baseline was
being handed the answer it was asked to predict. The model was scored on
information it did not have, against a rule that had it, and it lost:
**159.2 L against 111.6 L**.

That result was not wrong, it was *dishonest*. And the platform acted on it
correctly — it threw the model away.

> A benchmark that flatters the incumbent is worse than no benchmark. The
> model was being tested against a rule that had been given the future, and
> the platform acted correctly on a dishonest result — it threw away a good
> model because the comparison was rigged.

---

## Impact

| Horizon error over 96 ticks | Learned model | Baseline | Verdict |
|---|---|---|---|
| Baseline read from the end of the window | 159.2 L | 111.6 L | model loses → **discarded** |
| **Baseline read from before the window (fixed)** | **78.1 L** | 118.1 L | model wins → **kept** |

The learned model served **0** of the run's forecasts while the baseline
served all of them. The platform was correct and the outcome was wrong.

The bias measured directly, with [`scripts/horizon_probe.py`](../../scripts/horizon_probe.py):

```
learned model        horizon MAE:      77.3 L
baseline (look-ahead) horizon MAE:     118.3 L
baseline (honest)      horizon MAE:     686.4 L
```

The honest baseline is *worse* than the look-ahead one, as it must be — it is
predicting a level change it has not seen yet.

---

## Timeline

The documented order of events, from
[`docs/model-evaluation.md`](../model-evaluation.md) §3.2 and
[`day3.md`](../progress/day3.md).

1. The horizon validator was written to replay the last 96 ticks of history
   and compare the model's predicted total against what actually happened.
2. Its first version took the baseline's multiplier from `points[-12:]`.
3. The first evaluation run reported the model *worse* than the baseline.
4. Rather than accept that, the cause was investigated. Three separate causes
   were separated before any of them was fixed:
   - **The forecast design.** The forecast was recursive: the prediction for
     tick T+1 became the lag for tick T+2. Over 96 steps every error fed back
     into the next, and the model drifted back toward the mean multiplier.
   - **The data.** `POST /admin/reset` wipes the simulator's world and
     restarts the clock at tick 0. The stored demand history was kept, so the
     old run's tick 1 sat next to the new run's tick 1 — one series with a
     discontinuity in the middle.
   - **The benchmark itself.** The baseline was being handed the future.
5. The forecast was made **anchored** — every step predicted from the observed
   history, with the station's current level carried forward.
6. The stored history was dropped when the clock goes backwards.
7. The baseline was changed to use the history **before** the window, exactly
   like the model.

| Design | Horizon error | Baseline | Verdict |
|---|---|---|---|
| Recursive | 866 L | 110 L | baseline wins — the model was rejected |
| **Anchored** | 800 L | 1,262 L | learned wins by 37 % |
| **Anchored + honest benchmark** | **78.1 L** | 118.1 L | learned wins by **34 %** |

### The second leak, found on Day 7

The baseline was not the only thing being given the future. On Day 7 the
**model** was found to have the same problem from the other side:

> The horizon score was leaky. The validator scored the model on a replay
> window, but the model had been fitted on the *whole* history — including the
> window it was then asked to predict.

| Model | Error over the horizon | Against the baseline |
|---|---|---|
| scored with the window in its training data | 64.6 L | **−60.7 %** |
| retrained on the prefix, then scored | 170.5 L | **+3.7 %** |

The leaky model wins 37 of 48 window/pair comparisons. The honest one wins 25
of 48. **That 60.7 % was the model being scored against itself**, and it was
the number the platform published as its reason for trusting the model.

---

## Root cause

### The baseline was read from the wrong side of the window

The validator replays a window of history and asks: *given what was known at
the start of the window, what would each predictor have said?* The model was
given the history before the window. The baseline was given
`points[-12:]` — the **end** of the window — which is the answer.

The fix gives the baseline exactly what the model was given. The multiplier now
comes from `points[p.tick <= origin.tick]`, where `origin` is the tick
immediately before the window starts.

### The recursive forecast compounded its own error

A station's demand level is a **level**, not a random walk. Carrying the last
*prediction* forward treats it as one, so over 96 steps every error fed back
into the next. The forecast is now **anchored**: every step is predicted from
the observed history, with the station's current level carried forward.

### The selector checked the wrong measure first

Two checks decide whether the model is trusted: the one-step score and the
horizon score. The code checked the one-step score *first* and returned as
soon as it failed — which made the horizon check, described in the source as
*"the decisive check"*, unreachable dead code.

One injected spike inside the validation split was enough to make the model
look worse one step ahead, and the selector threw away a model that was winning
the horizon by more than 2×:

```text
one-step:  model 0.0940 vs baseline 0.0601   <- the model loses here
horizon:   model 1,227 L vs baseline 2,729 L   <- the model wins by 2.2x
active:    baseline                           <- wrong
```

**This was fixed on Day 3 by making the horizon check run first.** On Day 7 the
gate moved again, for a different reason: the horizon score turned out to be
*noisier* than the one-step score — a single window's verdict swings from
window to window, and the platform had been flipping between the model and the
rule. The gate now rests on the **held-out one-step comparison**, and the
horizon score is published as a diagnostic that does not veto. See
[`day7.md`](../progress/day7.md).

**This was fixed on Day 3 by making the horizon check run first.** On Day 7 the
gate moved again, for a different reason: the horizon score turned out to be
*noisier* than the one-step score — a single window's verdict swings from
window to window, and the platform had been flipping between the model and the
rule. The gate now rests on the **held-out one-step comparison**, and the
horizon score is published as a diagnostic that does not veto. See
[`day7.md`](../progress/day7.md).

### The model was fitted on the window it was scored against

Found on Day 7, and it is the deeper version of the same mistake. The
validator scored the **already trained** model on a replay window — a
look-ahead leak, because the model had been fitted on the whole history,
including the window.

The fix retrains the model on the prefix before each window it is scored on.
It costs one extra training run per window — a window-sized run takes about
**0.08 s** against **1.5 s** for the full history — and it is the only way for
the number to mean what it says. The probe is
[`scripts/horizon_honesty_probe.py`](../../scripts/horizon_honesty_probe.py).

### One window is not a verdict

The window slides with the daily demand cycle, and the difference between two
estimators on a single window is mostly noise. Measured over the stored
history:

```text
window 0: mean diff -58.6%   model wins 10/12
window 1: mean diff -36.5%   model wins 10/12
window 2: mean diff -57.7%   model wins 10/12
window 3: mean diff -66.1%   model wins 12/12
```

The score is now averaged over **four** contiguous windows, and the model is
refitted for each one.

---

## How it was detected

By the **evaluation script** — [`scripts/day3_evaluation.py`](../../scripts/day3_evaluation.py)
and its companion probes. The first run reported the model *worse* than the
baseline, and the cause was investigated rather than accepted.

Nothing in the unit suite could have found it: the benchmark was self-
consistent and the platform's response to it was correct.

---

## Resolution

| # | Fix | Where |
|---|---|---|
| 1 | The baseline uses the history **before** the window | `backend/app/intelligence/forecasting.py` |
| 2 | The forecast is **anchored** to the observed level, not recursive | `backend/app/intelligence/forecasting.py` |
| 3 | The stored history is dropped when the clock goes backwards | `backend/app/sync_service.py` |
| 4 | The horizon check ran **first** in the selector on Day 3; the one-step score only decided when there was no horizon score | `backend/app/intelligence/forecasting.py` |
| 5 | The model is retrained on the prefix before each window it is scored on | `backend/app/intelligence/forecasting.py` — `HorizonValidator` |
| 6 | The score is averaged over four contiguous windows | `backend/app/intelligence/forecasting.py` — `SCORE_WINDOWS = 4` |
| 7 | The horizon verdict is published whether it flatters the model or not | `backend/app/intelligence_service.py` — `/api/model-status` |

The gate now rests on the **held-out one-step comparison** — the model fitted
on the first 80 % of the history *by time* and scored on the last 20 %, against
the rule-based predictor scored on exactly the same rows with exactly the rule
it uses live. Neither side can see the rows it is judged on.

**The honest headline.** Measured against the stored history, the learned
model's error sat between **0.82 and 0.90** of the rule's at every tick from 16
onwards. It wins **7 of 12** station/fuel pairs over the horizon (132.2 L
against 179.8 L), and **35 of 48** window/pair comparisons once the leak is
removed.

---

## What went well

- **A negative result was investigated, not accepted.** *"Rather than accept
  that, the cause was found and fixed."* The platform's refusal to use the
  model was treated as a question, not as a verdict.
- **The three causes were separated.** Design, data and benchmark were fixed
  one at a time, each measured, so it was clear which one did what.
- **The platform's behaviour was never wrong.** The selector discarded a model
  that lost the comparison, and it kept the model once the comparison was
  honest. Only the benchmark was defective.
- **The lesson was written down and generalised.** *"A benchmark that flatters
  the incumbent is worse than no benchmark"* appears in both the day log and
  the evaluation report.
- **The verdict is published even when it is unflattering.** `/api/model-status`
  reports `beats_baseline_at_horizon: false` when that is the truth.

## What went badly

- **A benchmark that flatters the incumbent is indistinguishable from a
  benchmark that is merely strict.** Nothing about "159.2 L against 111.6 L"
  says "this comparison is rigged".
- **The same class of mistake was made twice, in opposite directions.** The
  baseline was given the future on Day 3; the model was fitted on its own test
  window on Day 7. Both are leaks in the same direction — the estimator sees
  the answer — and only the second one was found by re-auditing the first.
- **The platform discarded a good model because of it.** The selector was
  doing exactly what it was designed to do, and the damage was total: the
  baseline served the entire run.
- **The one-step check was the *decisive* check in practice and the horizon
  check was dead code.** The source comment described the intent; the code did
  something else.
- **The published headline number was the leaky one.** The 60.7 % was the
  model's number to beat itself, and it was what the platform published as its
  reason for trusting the model.

---

## Discrepancies

The repository publishes **two different sets** of numbers for the same
leaky-versus-honest comparison, and they disagree by two orders of magnitude.
Both were added in the same commit (`426b574`, *"fix(day7): stop the platform
serving a world that has ended"*, 2026-11-25), and neither has been changed
since.

| Source | Leaky | Honest | Wins |
|---|---|---|---|
| [`docs/progress/day7.md`](../progress/day7.md) | 64.6 L, **−60.7 %** | 170.5 L, **+3.7 %** | leaky 37/48, honest 25/48 |
| [`backend/app/intelligence/forecasting.py`](../../backend/app/intelligence/forecasting.py) — `HorizonValidator` docstring | 4 341 L, 13.7 % better | 5 035 L, 0.1 % better | leaky 11/12, honest 4 |

The two are **not** interchangeable, and the reason is visible in the
arithmetic. Working backwards from each published percentage:

| Source | Implied baseline error |
|---|---:|
| `day7.md` | ~40 L (leaky) and ~164 L (honest) — the two differ, so the baseline moved |
| `forecasting.py` | ~5 030 L for both — a single, stable baseline |

So the two measurements differ in **what the baseline is**:

* The code docstring's numbers are consistent with one baseline scored
  identically in both arms (~5 030 L), which is what
  [`HorizonValidator.score_windows()`](../../backend/app/intelligence/forecasting.py)
  does — it computes the baseline from the history before each window, once,
  and scores both arms against it.
* The `day7.md` numbers are consistent with a baseline whose error changes
  between the two arms (~40 L against ~164 L). That is the shape of
  **[`scripts/horizon_honesty_probe.py`](../../scripts/horizon_honesty_probe.py)**,
  which computes `honest_base` and `leaky_base` as separate lists but fills
  them from the same `baseline_total` — so the baseline is identical in both
  arms there too.

**The difference is most likely the history each measurement reads, and the
per-window baseline.** The probe reads `read_demand_history(limit=5000)`; the
validator reads the history the training cycle holds. The published
percentages also disagree on the *win counts* — 37/48 against 11/12 — which
are counts over different populations (48 window/pair comparisons against 12
station/fuel pairs).

**Resolution used in this document:** the `day7.md` figures are quoted, because
they are the ones in the published progress log and the commit message. The
code docstring's figures are **not verified** against a stored probe output —
no probe result files for this measurement are committed. The qualitative
conclusion is the same in both: the leaky score flattered the model, and
removing the leak made the model look worse.

**This is itself an instance of the incident.** Two numbers for the same
measurement, in the same commit, with no stored raw output to adjudicate
between them, is exactly the failure mode the postmortem is about. It is
listed here rather than silently resolved.

---

## Action items

| # | Action | Status |
|---|---|---|
| 1 | The baseline reads the history before the window | **Done** — `backend/tests/test_predictor.py::test_the_horizon_validator_gives_the_baseline_no_look_ahead` |
| 2 | The forecast is anchored, not recursive | **Done** |
| 3 | The stored history is dropped when the clock goes backwards | **Done** — `backend/tests/test_sync_service.py::test_a_reset_drops_the_stored_history` |
| 4 | The horizon check ran first in the selector | **Done** on Day 3 and **superseded** on Day 7, when the gate moved to the held-out one-step comparison — `backend/tests/test_predictor.py::test_the_horizon_result_does_not_veto_a_model_that_wins_the_validation_split`, `backend/tests/test_model_acceptance.py::test_the_horizon_score_does_not_gate_the_verdict` |
| 5 | The model is refitted on the prefix before each window it is scored on | **Done** — `backend/tests/test_model_stability.py::test_each_window_is_scored_with_a_model_that_never_saw_it`, `::test_the_forecast_for_a_window_cannot_see_the_window` |
| 6 | The score is averaged over four contiguous windows | **Done** — `backend/tests/test_model_stability.py::test_the_horizon_score_is_averaged_over_several_windows` |
| 7 | The verdict is published whether it flatters the model or not | **Done** — `backend/tests/test_model_acceptance.py::test_the_horizon_score_does_not_gate_the_verdict` |
| 8 | A probe that measures the leak directly | **Done** — `scripts/horizon_honesty_probe.py` |
| 9 | The gate moved to the held-out one-step comparison | **Done** |
| 10 | Re-audit every other estimator comparison in the platform for the same class of leak | **Not done** — the two leaks were found by different means (a negative result, and a deliberate re-reading of the model verdict), not by a systematic audit |
| 11 | Train/evaluate the model on more than one scenario and one seed | **Not done** — recorded as a documented limitation in [`day3.md`](../progress/day3.md): *"The results show the platform learns this world's demand model. They do not show generalisation to another seed."* |
| 12 | Commit the raw output of every probe that produces a published number, so two numbers for the same measurement can be adjudicated | **Not done** — see [Discrepancies](#discrepancies); the two sets of horizon numbers in this postmortem cannot be reconciled from the repository alone |
| 13 | Reconcile the two published horizon figures, or withdraw one of them | **Not done** — the qualitative conclusion agrees; the magnitudes do not |
