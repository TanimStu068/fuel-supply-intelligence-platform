# Postmortem: the fallback never engaged because the forecast was stale

**Incident:** a failed evaluation left the previous result in place, so the
platform carried on acting on a forecast describing a world that had already
moved on
**Day:** 4 of 7 (the decision engine)
**Severity:** high
**Status:** resolved and proven live

---

## Summary

The platform has a rule-based fallback policy for when the intelligence layer
cannot produce forecasts: refill the lowest days-of-cover first, cap the
confidence at 0.4, and say so in the rationale.

It never engaged.

A failed evaluation left the **previous** result in place. The next proposal
then found a cached result, judged it usable, and proposed from a forecast that
described a world that had already moved on. The fallback path was correct,
tested, and unreachable in practice.

> The third path matters more than it looks: a failed evaluation used to leave
> the **previous** result in place, so the platform carried on acting on a
> forecast that described a world which had already moved on. A failed
> evaluation now clears the stale result, which forces the next proposal
> through the fallback policy.

---

## Impact

The platform kept working — it kept proposing allocations, kept serving the
dashboard, and never crashed. That is what makes this defect dangerous: there
was no visible failure at all.

What was wrong was the *quality and honesty* of what it did:

| | Before | After |
|---|---|---|
| The fallback policy during a failed evaluation | **Never engaged** | Engages, and says why |
| The platform's behaviour during a failed evaluation | Acted on a stale forecast | Acts on the rule-based policy, with the confidence capped at 0.4 |
| The operator's visibility | Nothing indicated degradation | `GET /api/decision-settings` reports `fallback_reason`; the fallback is sticky |

The stale forecast was not a *wrong number* in the sense the platform's
contract cares about — it was a forecast that had simply not been refreshed,
applied to a world that had moved.

---

## Timeline

The documented order of events, from [`day4.md`](../progress/day4.md).

1. Day 4 built the decision engine with three entry points into the fallback
   policy, all going through the same shared entry point
   (`IntelligenceService.propose_now`):

   | Path | Trigger |
   |---|---|
   | The engine raised | The evaluation failed outright. |
   | No forecast | The evaluation produced no forecast. |
   | No risk assessment | The evaluation produced no risk. |

2. The fallback policy was implemented in its own module,
   [`backend/app/decision/fallback.py`](../../backend/app/decision/fallback.py):
   refill the lowest days-of-cover first, confidence capped at 0.4, never
   auto-approved, and the rationale says *"the forecast model is unavailable"*.
3. The fallback drill — [`scripts/day4_fallback_drill.py`](../../scripts/day4_fallback_drill.py) —
   runs inside the backend container, builds the same object graph the
   lifespan builds, and then **breaks the intelligence layer inside it**.
4. The drill reported that the fallback **still proposed recommendations**,
   that every degraded recommendation used the rule-based policy, that the
   confidence was capped, that the rationale said the model was unavailable,
   that the idempotency key was the fallback's own, and that nothing reached
   the simulator without a human.
5. The reason the fallback never engaged in practice was found by reading the
   code path: a failed evaluation left `self._last_result` pointing at the
   previous result.
6. The fix was made in the evaluation's exception handler: it now clears the
   stale result before routing to the fallback.

---

## Root cause

### The cached result outlived the evaluation that produced it

[`evaluate_and_store()`](../../backend/app/intelligence_service.py) holds its
result in `self._last_result`. The proposal path then reads it:

```python
result = self._last_result
usable, reason = (
    self._forecasts_are_usable(result)
    if result
    else (False, "the intelligence layer has not evaluated the network yet")
)
```

The bug was in the *exception handler*. Before the fix, it logged the failure
and returned, leaving `_last_result` pointing at the previous run's result.
The next proposal found a non-`None` result, called `_forecasts_are_usable()`
on it, got `True` — because the stale result *did* contain forecasts and risks
— and proposed from it.

The fix clears the stale result, so the next proposal finds `None` and is
forced through the fallback policy:

```python
except Exception as exc:
    logger.warning("intelligence evaluation failed: %s", exc)
    # The previous result is stale now: it describes a world that
    # has moved on, and acting on it would be acting on a forecast
    # the platform could not refresh. Drop it, so the next proposal
    # has to use the fallback policy rather than an old forecast.
    self._last_result = None
    await self._recommend_with_fallback(state, reason=str(exc))
    return None
```

### The fallback reason was cleared as soon as it was counted

A second defect in the same area, found by the same drill: the reason was
cleared the moment the activation was counted, so
`GET /api/decision-settings` **could never report that the platform was in
fallback** — the flag was always `null`.

The reason is now **sticky**: it survives until the normal policy comes back.
The activation counter has its own latch, so an outage counts once however
many poll cycles it spans.

---

## How it was detected

By the **fallback drill** — [`scripts/day4_fallback_drill.py`](../../scripts/day4_fallback_drill.py).
The drill asserts on the specific facts the implementation is supposed to
hold, including that the fallback still proposes when the intelligence layer
is broken.

The drill's own output is the evidence:

```
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

A companion script,
[`scripts/day4_verify_state.py`](../../scripts/day4_verify_state.py), asserts
the specific facts the implementation is supposed to hold **straight from the
source files**, so a silently reverted edit is caught before it reaches a
commit.

---

## Resolution

| # | Fix | Where |
|---|---|---|
| 1 | A failed evaluation clears `_last_result`, forcing the next proposal through the fallback policy | `backend/app/intelligence_service.py` — `evaluate_and_store()` |
| 2 | The fallback reason is **sticky** and survives until the normal policy returns | `backend/app/decision/service.py` — `set_fallback_reason()` |
| 3 | The activation counter has its own latch, so an outage counts once rather than once per poll cycle | `backend/app/decision/service.py` — `consume_fallback_activation()` |
| 4 | All three fallback paths go through the same shared entry point | `backend/app/intelligence_service.py` — `propose_now()` |
| 5 | The operator's manual "propose" button uses the same policy as the background sync | `backend/app/intelligence_service.py` |

---

## What went well

- **The drill breaks the real object graph.** It runs inside the backend
  container and builds the same object graph the lifespan builds, so it
  exercises the real path rather than a reconstruction of it.
- **The fallback was implemented as a first-class path**, in its own module
  with its own rationale, not as an error handler bolted on the end.
- **The fallback was made visible.** Sticky reason, a capped confidence, an
  explanation in the rationale, and a metric that counts activations rather
  than poll cycles.
- **Nothing reached the simulator without a human**, in the degraded state as
  in the healthy one.
- **The companion verify script was written because of the silent reverts.**
  This day's log records that *"Twice that produced a green test suite with
  broken code behind it."*

## What went badly

- **A cached value outlived its validity, and nothing marked it invalid.** The
  result was stored without a timestamp, an age or a validity check. Any later
  reader would use it as if it were fresh.
- **The fallback was correct, tested, and unreachable.** The drill passed
  because the drill broke the layer in a way that left `_last_result` as
  `None`. The defect lived in the *other* failure mode.
- **The failure was invisible.** The platform kept proposing, kept serving
  and never crashed. An operator had no way to tell that the forecasts behind
  the proposals had stopped being refreshed.
- **Two defects in the same area were found separately.** The stale result and
  the non-sticky reason were found in different passes, by the same drill.

---

## Action items

| # | Action | Status |
|---|---|---|
| 1 | A failed evaluation clears the stale result | **Done** — `backend/tests/test_decision_fallback_lifecycle.py::test_a_failed_evaluation_drops_the_stale_result` |
| 2 | The fallback reason is sticky | **Done** — `backend/tests/test_decision_fallback_lifecycle.py::test_the_fallback_reason_survives_until_the_normal_policy_returns` |
| 3 | The activation counter counts engagements, not poll cycles | **Done** — `backend/tests/test_decision_fallback_lifecycle.py::test_the_fallback_recommendations_are_counted` |
| 4 | All three fallback paths share one entry point | **Done** — `backend/tests/test_decision_service.py::test_the_optimizer_and_the_fallback_are_reachable` |
| 5 | The operator's manual propose uses the same policy | **Done** — `backend/tests/test_decision_fallback_lifecycle.py::test_the_operator_proposal_uses_the_fallback_when_there_is_no_forecast` |
| 6 | The fallback is proven live in the running platform | **Done** — `scripts/day4_fallback_drill.py`, 22 checks, 0 failed |
| 7 | A source-level verify script, so a silently reverted edit is caught before it reaches a commit | **Done** — `scripts/day4_verify_state.py` |
| 8 | Put an **age** on the cached intelligence result, so a result that is too old is rejected on its own terms rather than only when the evaluation happens to fail | **Not done** — staleness is currently implied by "the last evaluation succeeded", not measured |
| 9 | A metric for "proposals made from a result older than N cycles" | **Not done** — there is no way to see from the metrics that the platform is proposing from stale forecasts |
| 10 | Add a fallback drill case that breaks the layer *after* a successful evaluation, so the exact defect above is covered by the drill and not only by the unit test | **Not done** — the unit test covers it; the drill's break happens before the first evaluation |
