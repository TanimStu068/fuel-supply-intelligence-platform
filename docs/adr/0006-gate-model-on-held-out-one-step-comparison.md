# ADR 0006 — Gate the learned model on a held-out one-step comparison

**Status:** Accepted
**Date:** Day 7 of 7 (superseding the Day 3 horizon gate)
**Deciders:** the team

---

## Context

The platform must decide, on every evaluation, whether the learned model is
good enough to use. Two measurements were available, and the choice between
them was made twice — wrongly the first time.

**The one-step score** compares the model's prediction against the observed
demand on held-out rows.

**The horizon score** replays the recursive forecast over the tail of the
history and compares the *total* demand it predicts against the total that
actually happened. It matters more operationally, because the platform
forecasts a whole day ahead and the risk engine needs the sum of the demand
over the horizon.

On Day 3 the horizon score won the argument and became the gate. That
decision was **overturned on Day 7**, and the path there is the reason this
ADR exists.

## Decision

**The gate is the held-out one-step comparison.**

The model is fitted on the **first 80 % of the history by time** and scored on
the **last 20 %**, against the rule-based predictor scored on exactly the same
rows with exactly the rule it uses live. Neither side can see the rows it is
judged on, so neither can flatter itself, and the result does not change from
one retrain to the next.

**The horizon score is still computed and still published** — as a diagnostic
that does not veto. `GET /api/model-status` reports
`beats_baseline_at_horizon: false` when that is the truth.

The gate carries a **5 % tolerance** (`BEAT_TOLERANCE`): a model more than
5 % worse than the rule on the held-out split is rejected. A zero on either
side means *"not measured yet"*, and the model is given the benefit of the
doubt — comparing `0.0 >= 0.0` would reject a perfectly good model as
"worse than the baseline".

Three things make the horizon score a diagnostic rather than a gate:

1. **It is noisy.** A window's verdict swings from window to window.
2. **It is leaky, and was fixed.** The validator scored the model on a replay
   window, but the model had been fitted on the *whole* history, including the
   window it was then asked to predict. Each window is now scored with a
   model **refitted on the prefix before it**.
3. **It was the cause of a flip.** The platform's visible symptom was a model
   that was trusted on one retrain and discarded on the next, for reasons that
   were not true — the health panel would say *"the model does not beat the
   baseline"* about a model that beat it by 79 % over four windows.

## Alternatives considered

| Alternative | Why not |
|---|---|
| **Gate on the horizon score (Day 3's choice)** | A single window is a coin flip. Measured over the stored history, the model beat the baseline on 11 of 12 pairs in one window, 9 of 12 in the next, and the mean difference swung from −53 % to +81 % and back to −59 %. The platform acted on it and flipped between the model and the rule. |
| **Gate on the horizon score, averaged over four windows** | Tried, and better — but the horizon score remained the noisier of the two measurements, and gating on it kept producing a model that looked worse than it was. |
| **Gate on the leaky horizon score** | It was measuring the model against itself. The leaky model won on 37 of 48 window/pair comparisons at 64.6 L, −60.7 % against the baseline — and that was the number the platform published as its reason for trusting the model. |
| **Gate on the one-step score with a random split** | A random split leaks: rows near the split are temporally adjacent to rows the model was fitted on, so the score is optimistic. The split is **by time**. |
| **No gate — always use the model** | Then a model that had learned nothing would be trusted. The gate is what lets the platform use the rule-based predictor on the 272 evaluations before the model was trained. |
| **Gate on the in-sample training error** | Meaningless — the model fits its training data. |

## Consequences

**Good.** The gate is stable. It does not change from one retrain to the
next, which is what stopped the platform flip-flopping between the model and
the rule.

**Good.** Both sides of the comparison are held out the same way, so the
comparison is fair by construction. The learned model's held-out error sits
at **0.82–0.90** of the rule's from tick 16 onwards.

**Good.** The horizon score is still published, so a judge can see the
long-horizon comparison — but it cannot veto, so a noisy window cannot
discard a good model.

**Bad.** The gate is a **one-step** measurement, and the platform's job is a
96-tick forecast. The gate is not the same measure as the use. That is a
deliberate trade: the gate measures what can be trusted, the horizon score
measures what matters operationally, and the two are reported side by side.

**Bad.** The horizon score requires a **refit per window** — one extra
training run per window, because the refit is what keeps the score honest.
A window-sized training run takes about 0.08 s against 1.5 s for the full
history; that figure appears in
[`docs/postmortems/07-benchmark-leaked-the-future.md`](../postmortems/07-benchmark-leaked-the-future.md)
but its probe output is **not committed**, so the timings are **not
verified** in the repo. The score is computed once per training cycle (every
5 minutes), not per forecast.

**Bad.** The two published figures for the horizon leak **disagree**, and the
repository cannot adjudicate between them. `docs/progress/day7.md` records
64.6 L / −60.7 % leaky and 170.5 L / +3.7 % honest; the `HorizonValidator`
docstring records 4,341 L / 13.7 % better and 5,035 L / 0.1 % better. Both
came from commit `426b574`. The qualitative conclusion is identical — the
leaky score flattered the model — but the magnitudes differ by two orders of
magnitude, and no probe output for this measurement is committed.

**Bad.** The model is trained on **one scenario and one seed**, so the gate
proves the model beats the rule *on this world*, not in general.

## Evidence

- [`docs/progress/day3.md`](../progress/day3.md) — the original horizon gate,
  and why the one-step check was dead code
- [`docs/progress/day7.md`](../progress/day7.md) — the leak, the fix, and the
  gate move
- [`docs/postmortems/07-benchmark-leaked-the-future.md`](../postmortems/07-benchmark-leaked-the-future.md)
  — the full account, including the Discrepancies section
- [`docs/model-evaluation.md`](../model-evaluation.md) §3.2, §3.5
- [`backend/app/intelligence/forecasting.py`](../../backend/app/intelligence/forecasting.py)
  — `HorizonValidator`, `SCORE_WINDOWS = 4`, `BEAT_TOLERANCE`
