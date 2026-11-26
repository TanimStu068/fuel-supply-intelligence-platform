# ADR 0005 — Hybrid forecast: a profile baseline with a learned model on top

**Status:** Accepted
**Date:** Day 3 of 7
**Deciders:** the team

---

## Context

The brief requires *"at least one meaningful AI / ML / optimization / detection
capability"* and lists demand forecasting first among the prediction
capabilities.

A pure rule-based predictor would not satisfy the requirement, and a pure
learned model would be a bad engineering choice here, because the simulator's
demand is not a black box: it is a published profile shape plus a multiplier
plus bounded noise. The `urban_high` profile draws 8,500 L of diesel per
simulated day, the `highway` profile 10,500 L, and so on — the platform can
read those shapes from the simulator's own published demand profiles.

A pure learned model would also have nowhere to fall back to, and the model
would need a large amount of history before it could be trusted at all.

## Decision

**Two predictors, and a selector that chooses between them.**

| Predictor | What it does |
|---|---|
| `BaselinePredictor` | Holds the station's observed multiplier flat across the horizon, using the median of the **most recent** observed multipliers |
| `LearnedPredictor` | A scikit-learn model over the tick's **position within the simulated day**, the hour-of-day factor, the region demand factor and the observed multipliers |

The learned model predicts the **multiplier** — the ratio between what a
station drew and what the published profile says it should draw — not litres.
That ratio is dimensionless and comparable across every station and every
fuel, so **one model serves all 12 station/fuel pairs**.

The selector falls back to the rule-based predictor for six recorded reasons:

| Reason | Meaning |
|---|---|
| `not_trained` | No model yet |
| `insufficient_data` | Not enough training rows |
| `insufficient_history` | Not enough history to featurise |
| `low_confidence` | Model confidence below the threshold |
| `worse_than_baseline` | The model lost the held-out comparison |
| `kill_switch` | An operator turned it off deliberately |

The `kill-switch` is checked **first**, because an operator who turns the
model off needs to see *that* on the dashboard rather than a generic
"model_error" — one is deliberate, the other is a fault.

The fallback policy for the **decision** layer is a separate thing again: when
the intelligence layer cannot produce forecasts at all, the decision layer
proposes with a rule-based policy — refill the lowest days-of-cover first,
confidence capped at 0.4, never auto-approved, and the rationale says
*"the forecast model is unavailable"*.

## Alternatives considered

| Alternative | Why not |
|---|---|
| **Rule-based only** | Would not satisfy §7 — at least one meaningful learned capability is required. It also cannot track a drifting multiplier, which is the thing the simulator's `demand_spike` events change. |
| **Learned model only** | Nowhere to fall back to. The model needs history before it is usable, and the platform has to serve from tick 0. It would also be *worse* than the baseline in the early run — the fallback count over the Day 3 run was **272**, all of them `not_trained`. |
| **Predict litres directly** | A litres model would have to learn each station's profile separately, and would have nothing to compare against. Predicting the multiplier makes the published profile the anchor and the model the correction. |
| **Absolute tick as the feature** | Measured on the stored history, the absolute tick scored **189.2 L** against the rule's 179.8 L — **worse** than the rule, and it won only 5 of 12 pairs. The tick's *position within the day* scores **132.2 L** against 179.8 L, and wins 7 of 12. |
| **Two implementations of the fallback** | The optimizer's own fallback branch was removed once the standalone policy existed, *"because two copies of the same rule would drift apart and only one of them would be the one that actually runs."* |

## Consequences

**Good.** The platform is useful from the first tick and improves as the
model trains. The rule-based predictor is not a placeholder — it is the
honest benchmark the learned model has to beat, and the comparison between
them is published on every evaluation.

**Good.** The fallback reasons are a specific, countable vocabulary. An
operator can tell *why* the platform degraded, and the metric distinguishes
`kill_switch` from `model_error`.

**Good.** The rule-based predictor earned its place. It is what the learned
model is measured against, and the measured gap is small: over the forecast
horizon **132.2 L against 179.8 L**, a 26.5 % improvement.

**Bad.** Two predictors means two things that can disagree, and the selector
is where they meet. The **dead horizon check** was a bug here: the selector
checked the one-step score first and returned as soon as it failed, which
made the horizon check — described in the source as *"the decisive check"* —
unreachable dead code. One injected spike in the validation split was enough
to throw away a model winning the horizon by 2.2×.

**Bad.** The learned model's advantage is real but **small**, and a single
window's verdict is mostly noise. Measured over the stored history, the model
beat the baseline on 11 of 12 pairs in one window, 9 of 12 in the next, and
the mean difference swung from −53 % to +81 % and back to −59 %. The score is
averaged over four contiguous windows for exactly this reason.

**Bad.** The model is trained on **one scenario and one seed**. The Day 3
log states the limitation plainly: *"The results show the platform learns this
world's demand model. They do not show generalisation to another seed."*

**Bad.** The learned model has to be re-evaluated on every retrain, and the
evaluation is not free. A window-sized training run takes about 0.08 s
against 1.5 s for the full history — a figure recorded in
[`docs/postmortems/07-benchmark-leaked-the-future.md`](../postmortems/07-benchmark-leaked-the-future.md)
but **not verifiable** in the repo's own probe output, which prints its
elapsed time without the run being committed. The evaluation runs once per
training cycle (every 5 minutes), not per forecast.

## Evidence

- [`docs/progress/day3.md`](../progress/day3.md) — the model's design, the
  fallback reasons, and the risk table
- [`docs/progress/day7.md`](../progress/day7.md) — the absolute-tick versus
  within-day measurement, and the honest headline
- [`docs/model-evaluation.md`](../model-evaluation.md) §2, §3.3, §6
- [`docs/postmortems/07-benchmark-leaked-the-future.md`](../postmortems/07-benchmark-leaked-the-future.md)
  — the dead horizon check and the noisy single-window verdict
- [`backend/app/intelligence/forecasting.py`](../../backend/app/intelligence/forecasting.py)
  — `BaselinePredictor`, `LearnedPredictor`, `_normalise_reason`
