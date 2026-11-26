# ADR 0008 — Aggregate in the database, never in Python

**Status:** Accepted
**Date:** Day 6 of 7
**Deciders:** the team

---

## Context

After every sync cycle the platform updates its prediction-error metric. The
first implementation read **every row** of `prediction_evaluations` into
Python to compute a mean absolute error.

That table holds **one row per station, per fuel, per tick**. Over a long run
it is not a handful of rows — the load test's dataset was **928,005 rows,
257 MB**.

Two failures followed:

1. **The container was OOM-killed nine times in a row**, at exactly its
   670 MB limit (`OOMKilled: true`, `ExitCode: 137`, `RestartCount: 9`).
2. **The dashboard's p99 was 30,029.78 ms** — which was the harness's own
   thirty-second timeout. Requests were not merely slow; they were not being
   answered at all.

The evaluation was also running **on the event loop**, so while it read the
table, the API could not answer at all. The split-cycle timing showed it
exactly:

```
duration_seconds: 10.2513
  persist_seconds:      0.0260
  history_seconds:      0.1236
  alerts_seconds:       0.0000
  intelligence_seconds: 9.8901   ← the whole problem
```

The model was not the problem — the engine evaluates the whole network in
36 ms. The **error metric** was.

## Decision

**Compute the aggregate where the data already is.**

```sql
SELECT model, AVG(absolute_error), COUNT(id),
       SUM(CAST(within_interval AS INTEGER))
FROM prediction_evaluations GROUP BY model
```

Two supporting changes:

- The **"already scored" lookup** was also unbounded — it asked for every
  evaluation from the window onwards, pulling **760,809 rows** into memory on
  every cycle. It is now narrowed to the ticks actually being scored.
- The **evaluation moved off the event loop** into a worker thread with
  `asyncio.to_thread`, so the API keeps answering while the forecast runs.

The old behaviour is **not deleted**. It stays in the repository behind one
setting, so the before/after can be re-measured:

```bash
PREDICTION_ERROR_AGGREGATE_IN_DATABASE=false docker compose up -d backend
```

## Alternatives considered

| Alternative | Why not |
|---|---|
| **Keep it in Python but bound the window** | A bounded window would fix the p99 but leave the metric describing only the recent window, and the metric would still be wrong when the table grew. It treats the symptom. |
| **Aggregate in SQL but keep the full-table scan** | `AVG` over the whole table is still a full scan on every cycle. It happens in the database rather than in Python, but the work is the same. The windowed form is what the aggregate actually needs. |
| **Maintain the metric incrementally** | Attractive, and it is the natural answer to a table that grows without bound. Rejected for now because it introduces a second source of truth that can disagree with the table — and a disagreement between a metric and the data it measures is exactly the class of bug this project kept hitting. |
| **A retention job to keep the table small** | It would bound the size, but the Day 6 report is explicit that *"the fix is not 'use more memory' — it is 'never read the table'"*. A retention job is still an open item; it is not the fix. |
| **Sample the table** | A sampled metric is a metric with a confidence interval attached, and the platform would have to publish that interval. It also makes the metric noisier for no benefit when the aggregate can be computed exactly in the database. |

## Consequences

**Good.** Measured on the same workload, the same ramp, the same data:

| Metric | Before | After | Change |
|---|---|---|---|
| Requests completed | 5,801 | **31,700** | 5.5× |
| Throughput | 41.75 req/s | **335.39 req/s** | **8.0×** |
| Average | 454.54 ms | **66.05 ms** | 6.9× faster |
| **p99** | **30,029.78 ms** | **286.13 ms** | **105× faster** |
| Error rate | 0.52% | **0.00%** | — |
| **Container memory (peak)** | **670 MB** (at the limit) | **102 MB** | 6.6× less |

The container stopped being OOM-killed, and the container CPU only doubled
while doing 5.5× the work.

**Good.** The numbers are **identical** — the aggregate is the same
computation, done in a different place. The metric did not change meaning;
only its cost changed.

**Good.** The "before" path is still in the repository, so the comparison is
reproducible rather than remembered.

**Bad.** `prediction_evaluations` **still grows without bound**. The
aggregate is now O(1) in memory, so growth costs no latency and no memory,
but the table keeps growing. The Day 6 report names the next step: *"A
retention job is the next step."* It has not been written.

**Bad.** The table is still the largest in the database at 257 MB, and it is
one `DELETE` away from being the next bottleneck.

**Bad.** Moving the evaluation to a worker thread does not remove the GIL.
The engine's own work releases it often enough for the event loop to get a
look in, and the measured effect is a large drop in p99 — but the evaluation
is still CPU-bound work sharing a process with the API.

## Evidence

- [`docs/load-test-report.md`](../load-test-report.md) — the split-cycle
  timing, the before/after table, the OOM inspection, and the limits table
- [`docs/postmortems/03-oom-kill-prediction-evaluations.md`](../postmortems/03-oom-kill-prediction-evaluations.md)
  — the full account
- [`docs/bug-catalog.md`](../bug-catalog.md) B53
- [`docs/progress/day6.md`](../progress/day6.md) — *`prediction_evaluations`
  grows without bound* in the risk table
- [`backend/app/intelligence_service.py`](../../backend/app/intelligence_service.py)
  — `_update_error_metrics`, `_score_previous_predictions`
