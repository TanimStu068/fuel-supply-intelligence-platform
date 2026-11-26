# Project summary

## The problem

A simulated Bangladeshi fuel supply chain — two depots, four stations, six
routes, three fuels, a 15-minute tick — where a delayed shipment, a demand
spike or a broken route propagates through the network. The brief asked for a
decision-support platform: observe the network, predict emerging shortages,
recommend allocations a human can inspect, and stay usable while things
break.

## What we built

A FastAPI backend over the organizer-provided simulator, a React operator
dashboard, a learned forecasting model with a rule-based fallback, and a
constraint-aware allocation engine. Every number the dashboard shows comes
from the simulator's REST endpoints. Nothing is fabricated.

## The architecture in five lines

1. **Simulator** — the world. Read over REST, notified over SSE, written only
   through `POST /v1/allocations`.
2. **Sync service** — polls REST on a floor of one read every 3 s, treats SSE
   as a hint and re-GETs the affected resource, and owns the state cache.
3. **Data + intelligence** — Postgres for history; a learned predictor, a risk
   engine and three detectors over it.
4. **Decision engine** — urgency-ordered allocation, pre-validating all eight
   simulator rules before it proposes anything.
5. **Dashboard + monitoring** — nine screens; Prometheus and Grafana.

Full diagram: [`architecture.md`](architecture.md).

## Six headline results

| Result | Number | Evidence |
|---|---|---|
| **Service level** | **1.000000** against a naive baseline's 0.656634 — 79,242 L of demand met that the baseline left unmet, 0 stations left at `CRITICAL` | [`policy-comparison.md`](policy-comparison.md) |
| **Model vs the rule** | Held-out error at **0.82–0.90** of the rule's from tick 16 on; over the horizon **132.2 L against 179.8 L**, 26.5 % better | [`day7.md`](progress/day7.md) |
| **Recovery drills** | **17 of 17** crisis, fault and dependency scenarios recovered automatically, no backend restart | [`resilience-report.md`](resilience-report.md) |
| **Load-test p99** | **30,030 ms → 286 ms**; throughput 41.75 → 335.39 req/s; container memory peak 670 MB → 102 MB | [`load-test-report.md`](load-test-report.md) |
| **Tests and coverage** | **583** backend, **22** Vitest, **12** Playwright; **85 %** coverage | [`day7.md`](progress/day7.md) |
| **Memory footprint** | **446 MiB** for the 4 core containers against a **1.5 GB** budget; **562 MiB** for all 7 | [`day6.md`](progress/day6.md) |

## What makes it resilient

The simulator's REST API is the source of truth and the platform keeps a
cached copy, so a dead simulator degrades the API rather than breaking it. A
circuit breaker opens after five consecutive failures. Writes retry a
Postgres deadlock, because `40001` is retryable. A failed model evaluation
drops the stale result and forces the next proposal through a rule-based
fallback. Every allocation is pre-validated against all eight simulator rules,
so the platform never proposes something the simulator will reject. Nothing
reaches the simulator without a human approving it.

## Honest limitations

- The **model is not better than the rule on every measure**. It gates on a
  held-out one-step comparison; the horizon score is published as a
  diagnostic because it was leaky.
- **One scenario was never drilled live** — a malformed simulator response.
  It is unit-tested only.
- **No authentication on the platform's own API**. The behaviour is
  restricted; the endpoints are not.
- The simulator's supply is **25 % below its demand**, so service level
  cannot approach 1.0 in general.
- The model was trained on **one scenario and one seed**; it has not been
  shown to generalise.
- No generative AI, no Kubernetes, no drift detection.

Full list: [`requirements-traceability.md`](requirements-traceability.md) §Gaps.
