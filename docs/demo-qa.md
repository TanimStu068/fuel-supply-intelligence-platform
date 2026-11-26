# Demo Q&A — 25 questions judges are likely to ask

Short, factual answers. Every one links to the file that holds the evidence.
Where a number exists it is quoted from that file; where something was not
verified, it says so.

---

## 1. The architecture

### Q1. Walk me through the architecture in one minute.

The simulator is the world — read over REST, notified over SSE, written only
through `POST /v1/allocations`. A sync service polls REST on a floor of one
read every 3 s, treats SSE as a hint and re-GETs the affected resource, and
owns the state cache. A data layer stores history in Postgres. The
intelligence layer forecasts, scores risk and detects anomalies. The decision
engine orders allocations and pre-validates them. A React dashboard shows it
all; Prometheus and Grafana watch it.

→ [`docs/architecture.md`](architecture.md) §1

### Q2. Why is it a modular monolith rather than microservices?

The brief warns against complexity that does not improve the solution, and
the platform's hardest problems were correctness ones — a stale snapshot, a
leaky benchmark — not distribution ones. Splitting the backend would have
made those harder to reason about without making them better.

→ [`docs/architecture.md`](architecture.md) §2

### Q3. Why does the platform talk to the simulator over REST *and* SSE?

Because the integration guide says to: REST is the source of truth, SSE is a
notification stream. The platform reads state from REST and uses SSE only to
learn *that something changed*, so it re-GETs the affected resource rather
than trusting the event payload.

→ [`docs/simulator-notes.md`](simulator-notes.md)

### Q4. Why poll on a timer instead of letting SSE drive everything?

Two reasons, both measured. The first is the simulator's own limits: its
SQLAlchemy pool is 5 connections plus 10 overflow, and reacting to every
tick meant roughly 70 requests a second against it — enough to exhaust the
pool and make the simulator answer 500 until it was restarted. The second is
that the event stream is not a reliable trigger on its own: a queue that
falls more than 200 events behind is dropped **silently**, and the guide
warns about exactly this.

→ [`docs/postmortems/04-simulator-connection-pool-exhaustion.md`](postmortems/04-simulator-connection-pool-exhaustion.md)

### Q5. What happens when the SSE feed dies without an error?

This was a real bug found on Day 6. The platform was dropped from the
simulator's event bus because it fell more than 200 events behind, but the
stream stayed open and kept sending keepalives — so `sse_connections_total`
stayed at 1 and `sse_reconnects_total` never moved, while the REST poller
correctly went degraded. Three separate causes: the reader awaited each sync
cycle, the read timeout was `read=None` (wait forever), and a live socket was
treated as proof of life. Fixed by scheduling refreshes instead of awaiting
them, bounding the read timeout to 30 s, and comparing the stream's tick
against the platform's own REST view.

→ [`docs/postmortems/01-silent-dead-sse-feed.md`](postmortems/01-silent-dead-sse-feed.md)

---

## 2. The intelligence

### Q6. What does the model actually do, and does it beat your baseline?

It predicts the station's **demand multiplier** — the ratio between what a
station drew and what the published profile says it should — not litres, so
one model serves all 12 station/fuel pairs. Held out by time, its error sits
at 0.82–0.90 of the rule-based predictor's from tick 16 onwards, and over
the forecast horizon it scores 132.2 L against the rule's 179.8 L — 26.5 %
better.

→ [`docs/progress/day7.md`](progress/day7.md)

### Q7. Isn't that a small margin? How do you know it is real?

That is exactly the right question, and it is why the margin is small. The
score is averaged over four contiguous replay windows, and the model is
**refitted on the prefix before each window** so it never sees the rows it is
judged on. It wins 35 of 48 window/pair comparisons once the leak is
removed.

→ [`docs/postmortems/07-benchmark-leaked-the-future.md`](postmortems/07-benchmark-leaked-the-future.md)

### Q8. How do you know your benchmark is fair?

That was a bug twice. First the **baseline** was given the future: its
multiplier was read from the *end* of the replay window, so the model lost
159.2 L against 111.6 L and the platform correctly threw the model away.
Then the **model** was fitted on its own test window. Both are fixed, and
the honest verdict is published even when it is unflattering —
`/api/model-status` reports `beats_baseline_at_horizon: false` when that is
the truth.

→ [`docs/model-evaluation.md`](model-evaluation.md) §3.2

### Q9. Why not reinforcement learning?

The brief marks RL optional, and it asks that RL justify itself against a
reasonable heuristic. With a 15-minute tick, four stations and a hard
per-tick dispatch cap, the credit assignment is slow and the action space is
small enough to enumerate. We shipped a constraint-aware greedy policy
measured against a naive baseline instead, which is a claim we can actually
evidence.

→ [`docs/requirements-traceability.md`](requirements-traceability.md) §8

### Q10. How good is the decision policy, really?

Against a deliberately reasonable naive policy over the same world and seed,
over 240 ticks: `service_level` **1.000000** against **0.656634**. The
platform met 79,242 L of demand the baseline left unmet and left **0**
station/fuel pairs at `CRITICAL` against the baseline's 8. The comparison is
between two complete policies, not two shipping schedules — the platform
shipped 339 shipments to the baseline's 21.

→ [`docs/policy-comparison.md`](policy-comparison.md)

### Q11. Is the service level of 1.0 believable?

It is the simulator's own metric — `served / (served + unmet)` — so it cannot
be gamed by the platform. But two caveats belong with it. The platform
**front-ran** demand, so it never hit a moment where fuel was missing; and
the simulator's total supply is about **25 % below** its total demand over a
longer window, so a service level near 1.0 is not generally reachable. The
comparison is between policies, not against a perfect score.

→ [`docs/policy-comparison.md`](policy-comparison.md) and
[`docs/model-evaluation.md`](model-evaluation.md) §7.2

### Q12. What is the supply ceiling?

The simulator's total supply is **468,000 L** — depot stock plus every
scheduled arrival — against **627,759 L** of demand over the same window, so
supply is **25 % short** and no policy can serve what is not there. The
ceiling on service level for that scenario is around 0.75 for a perfect
shipper. Over a shorter window the platform reached 1.000 by shipping early
and often.

→ [`docs/model-evaluation.md`](model-evaluation.md) §7.2

### Q13. How fast does it react to a demand spike?

A ×2.0 demand spike is detected in **1 tick** after injection; a ×3.0 spike
moves the risk level `OK` → `WATCH` in **2 ticks**, and fuel is dispatched
in 4.

→ [`docs/progress/day3.md`](progress/day3.md)

---

## 3. Resilience

### Q14. What happens if the simulator dies?

The circuit breaker opens after **5** consecutive failures, the API degrades
to the cached state and reports its **age** and the reason, and the dashboard
keeps answering. In the drill the breaker opened, degraded mode engaged, the
cached state was served with `age_seconds: 93.193` and
`degraded_reason: "the circuit breaker for the simulator is open"`, and the
breaker closed on its own when the simulator returned — **no backend
restart**.

→ [`docs/progress/day6.md`](progress/day6.md) and
[`docs/resilience-report.md`](resilience-report.md)

### Q15. What happens if the database dies?

The same way, from the other direction. The health endpoint reported the
database down in **0.1 s**, the platform kept serving from cache, and it
recovered in **2.0 s** when the database came back. Writes fail soft and
are logged; reads never touch the database.

→ [`docs/resilience-report.md`](resilience-report.md)

### Q16. How does the fallback work?

The intelligence layer forecasts; if it cannot, a rule-based policy proposes
instead — refill the lowest days-of-cover first, cap the confidence at 0.4,
and say *"the forecast model is unavailable"* in the rationale. The fallback
reason is **sticky**, so `GET /api/decision-settings` shows the operator
*why* the platform is degraded, and the activation counter counts engagements
rather than poll cycles.

→ [`docs/postmortems/08-fallback-never-engaged-stale-forecast.md`](postmortems/08-fallback-never-engaged-stale-forecast.md)

### Q17. Was the fallback ever actually reachable?

Not at first — and that was a bug worth having found. A failed evaluation
left the previous result in place, so the next proposal found a cached
forecast, judged it usable, and proposed from a world that had already moved
on. The correct path was implemented, tested, and unreachable. The fix drops
the stale result so the next proposal is forced through the fallback. The
fallback drill now passes 22 checks with 0 failures.

→ [`docs/postmortems/08-fallback-never-engaged-stale-forecast.md`](postmortems/08-fallback-never-engaged-stale-forecast.md)

### Q18. Can the platform serve a world that has already ended?

Not any more — and that was the Day 7 bug. `POST /admin/reset` restarts the
world at tick 0, and the cache kept serving the previous run's snapshot: tick
892, `degraded: false`, `age_seconds: 0.2`, while the world was at tick 0.
The age was technically true and the number was wrong. The cache now records
a rewind and `/api/state` answers **503** until the next cycle fetches the
new world. Verified on a fresh clone against the tagged commit.

→ [`docs/postmortems/02-stale-snapshot-after-reset.md`](postmortems/02-stale-snapshot-after-reset.md)

### Q19. How many failure scenarios have you actually drilled?

**17 of 17** recovered automatically, none needing a backend restart: a
demand spike, a shipment delay, a depot constraint, a route disruption, a
station outage, a supply shortfall, a combined crisis, all five simulator
fault types, the simulator's connection-pool exhaustion, a stopped database,
and the prediction layer in three shapes. Each records its detection time and
recovery time.

→ [`docs/resilience-report.md`](resilience-report.md)

### Q20. What about deadlocks between two writers?

The sync service persisting decision history and an operator approving a
recommendation deadlocked on each other, and Postgres killed one side with
`40001`. Three write paths were bypassing the retrying helper, and **35
writes were lost** in one load-test run. All three now go through
`Database.write()`, which retries the whole unit of work with a bounded
backoff — because the SQL standard says `40001` is retryable. The follow-up
run lost **0** writes.

→ [`docs/postmortems/06-postgres-deadlock-lost-writes.md`](postmortems/06-postgres-deadlock-lost-writes.md)

---

## 4. Human in the loop

### Q21. Does anything reach the simulator without a human?

Only if the operator turns on auto-mode, and it is **off by default**. When
it is on, the bar is high: `WATCH`-level only, confidence ≥ 0.85, no
human-review flag, not from the fallback policy, and every constraint must
have passed. Anything `CRITICAL`, anything uncertain, and anything from the
fallback is left for a human. A recommendation whose confidence is below 0.6
is flagged `needs_human_review` automatically.

→ [`docs/architecture.md`](architecture.md) §6

### Q22. Can an operator see *why* a recommendation was made?

Yes. Each recommendation carries the signals that drove it, the constraint
checks and whether each passed, the alternatives considered and ranked, the
projected risk before and after, its confidence, and a one-line rationale.
The explanation drawer is on the dashboard.

→ [`docs/screenshots/risk-explanation-drawer.png`](screenshots/risk-explanation-drawer.png) and
[`docs/model-evaluation.md`](model-evaluation.md) §4.2

### Q23. Can you see the alternatives and what-if?

`POST /api/recommendations/{id}/simulate` replays the forecast against the
fuel a shipment would deliver, at the tick it would arrive — a local
projection. It never asks the simulator what would happen, and it writes
nothing. The alternatives the engine rejected are on each recommendation.

→ [`docs/requirements-traceability.md`](requirements-traceability.md) §22

---

## 5. Testing and limits

### Q24. How do you know it works, and what are the limits?

**583** backend tests, **22** Vitest and **12** Playwright, **85 %** coverage,
and a CI pipeline that starts the whole stack the way a judge would and
checks every component's health. The limits are stated rather than buried:
the model is trained on one scenario and one seed; a malformed simulator
response is unit-tested but never drilled live; the platform's own API has no
authentication; there is no retention job on the table that OOM-killed the
container once; and the supply ceiling caps service level.

→ [`docs/progress/day7.md`](progress/day7.md),
[`docs/requirements-traceability.md`](requirements-traceability.md) §Gaps

### Q25. What was the most interesting bug you found, and why did it survive?

Eight are written up in full. The most instructive is the leaked benchmark:
the baseline was handed the future, the platform lost, and the platform
**correctly threw the model away**. The benchmark was rigged, not the model.
It survived because a benchmark that flatters the incumbent is
indistinguishable from one that is merely strict, and because the platform's
response to it was correct.

→ [`docs/postmortems/`](postmortems/)

---

## Also worth having open

| If they ask about | Open this |
|---|---|
| What it does | [`docs/demo-script.md`](demo-script.md) — the fourteen steps, with the exact command or click and what to say |
| Every requirement, mapped | [`docs/requirements-traceability.md`](requirements-traceability.md) |
| The numbers | [`docs/load-test-report.md`](load-test-report.md), [`docs/resilience-report.md`](resilience-report.md), [`docs/policy-comparison.md`](policy-comparison.md) |
| The model's honesty | [`docs/model-evaluation.md`](model-evaluation.md) |
| Everything that went wrong | [`docs/bug-catalog.md`](bug-catalog.md) — 70 defects, each with the test that pins it |
