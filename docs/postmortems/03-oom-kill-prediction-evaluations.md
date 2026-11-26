# Postmortem: the OOM kill and the 30-second p99

**Incident:** the backend was OOM-killed nine times in a row while the
dashboard's read path saturated
**Day:** 6 of 7 (Part 3 — load testing)
**Severity:** high
**Status:** resolved and measured before and after

---

## Summary

The load test found a Python-side aggregate over a table that held **928,005
rows**. After every sync cycle the platform read *every row* of
`prediction_evaluations` into Python to compute a mean absolute error.

The container reached its **670 MB** memory limit — exactly the limit — and was
**OOM-killed outright**, nine times in a row, with `ExitCode: 137`. The
dashboard's p99 was **30,029.78 ms**, which was the harness's own thirty-second
timeout: requests were not merely slow, they were not being answered at all.

A second, independent leak sat in the same area: the "have I already scored
this forecast?" lookup pulled **760,809 rows** into memory on every sync
cycle.

---

## Impact

| Metric | Before | After | Change |
|---|---|---|---|
| Requests completed | 5,801 | **31,700** | 5.5× |
| Throughput | 41.75 req/s | **335.39 req/s** | **8.0×** |
| Average | 454.54 ms | **66.05 ms** | 6.9× faster |
| **p99** | **30,029.78 ms** | **286.13 ms** | **105× faster** |
| Slowest request | 30,887.70 ms | 527.53 ms | 59× faster |
| Error rate | 0.52% | **0.00%** | — |
| **Container memory (peak)** | **670 MB** (at the limit) | **102 MB** | 6.6× less |
| Container CPU (peak) | 0.031 cores | 0.060 cores | 2× — and doing 5.5× the work |

The symptom the judges would have seen is the **container restarting under
load**. Nine restarts during a single run is an outage that looks like a crash
loop, not like a slow query.

---

## Timeline

The documented order of events, from
[`docs/load-test-report.md`](../load-test-report.md).

1. The load test ramped the dashboard's read path — ten endpoints, the way the
   operator's dashboard polls them.
2. The average looked acceptable. The p99 was terrible: a small p50 and a p99
   in the seconds.
3. That pattern — a small p50 and a p99 in the seconds — is not a slow
   endpoint, it is a *global stall*: every endpoint showed it at once, which
   ruled out any single slow query.
4. The background sync cycle was split into its four steps and each was timed:

   ```
   duration_seconds: 10.2513
     persist_seconds:      0.0260
     history_seconds:      0.1236
     alerts_seconds:       0.0000
     intelligence_seconds: 9.8901   ← the whole problem
   ```

5. The intelligence evaluation took **9.9 seconds of the 10.25-second cycle**.
6. The model was ruled out: the engine evaluates the whole network in 36 ms.
   The cause was the **error metric**.
7. The table was measured: **928,005 rows, 257 MB** — one row per station, per
   fuel, per tick.
8. The container was observed at **670 MB**, exactly its limit, and
   `docker inspect` showed `OOMKilled: true`, `ExitCode: 137`,
   `RestartCount: 9`.
9. The aggregate was moved into SQL and the evaluation was moved off the event
   loop into a worker thread.
10. The run was repeated with the same workload, the same ramp and the same
    data.

---

## Root cause

### The aggregate read the table

After every cycle the platform read *every row* of `prediction_evaluations`
into Python to compute a mean absolute error. The table holds one row per
station, per fuel, per tick, so its size grows with the length of the run.

The fix computes the aggregate where the data already is:

```sql
SELECT model, AVG(absolute_error), COUNT(id),
       SUM(CAST(within_interval AS INTEGER))
FROM prediction_evaluations GROUP BY model
```

The numbers are identical; the latency is not. The evaluation was also moved
off the event loop into a worker thread, so the API can answer while the
forecast runs.

### The scoring window was unbounded

A second, independent leak in the same area. The code that checks *"have I
already scored this forecast?"* asked for *every* evaluation from the window
onwards:

```python
# before — reads the whole table into Python
select(PredictionEvaluation).where(PredictionEvaluation.target_tick >= window_start)
```

On the 928,005-row table that pulled **760,809 rows** into memory on every
sync cycle. It is now narrowed to the ticks actually being scored:

```python
# after — only the ticks this cycle could score
select(...).where(PredictionEvaluation.target_tick.in_(candidate_ticks))
```

The scoring window was supposed to be 200 ticks. The query it backed was not.

### Why the evaluation was on the event loop

The evaluation is CPU-bound: it forecasts every station and fuel, scores every
risk and detects every anomaly. Run on the event loop it blocks it for the
whole evaluation, and because the sync cycle calls it, the *API* stalls too —
a read that should take a millisecond waits for the forecast to finish. This
is why the stall appeared on **every** endpoint at once.

---

## How it was detected

By the **load test** — the host-side `asyncio`/`httpx` runner in
[`scripts/loadtest/async_loadtest.py`](../../scripts/loadtest/async_loadtest.py),
which samples the backend's CPU from its **own cgroup** (the same counter
Docker enforces when it decides to kill a container) and checks `OOMKilled`.

Two things made the finding sharp:

- **The per-step timing of the sync cycle.** Without it, "the intelligence
  evaluation is slow" would have been a guess.
- **`docker inspect`.** `OOMKilled: true`, `ExitCode: 137`, `RestartCount: 9`
  are the difference between "this container was slow" and "this container was
  killed and restarted".

---

## Resolution

| # | Fix | Where | Reproducible by |
|---|---|---|---|
| 1 | Aggregate the prediction error in SQL | `backend/app/intelligence_service.py` — `_update_error_metrics` | `PREDICTION_ERROR_AGGREGATE_IN_DATABASE=false` |
| 2 | Narrow the "already scored" lookup to the ticks being scored | `backend/app/intelligence_service.py` — `_score_previous_predictions` | — |
| 3 | Move the evaluation off the event loop into a worker thread | `backend/app/intelligence_service.py` — `evaluate_and_store()` | — |

The "before" side is **reproducible, not remembered**: the old code path is
still in the repository behind one setting.

```bash
PREDICTION_ERROR_AGGREGATE_IN_DATABASE=false docker compose up -d backend
```

---

## What went well

- **The load test was a real load test.** It ramped to 50 users against a
  running stack on a shared machine, and it read the container's vitals from
  the cgroup rather than from `docker stats`.
- **The first run's shape was informative.** "A small p50 and a p99 in the
  seconds" is the signature of a global stall, and the log says so explicitly:
  it *rules out any single slow query*.
- **Splitting the sync cycle into four timed steps found the culprit in one
  pass.** `intelligence_seconds: 9.8901` out of `duration_seconds: 10.2513`.
- **The fix is measured on both sides.** The same workload, the same ramp, the
  same data — and the "before" path is still in the repository.
- **The memory ceiling was taken seriously.** The report says it plainly: *"The
  fix is not 'use more memory' — it is 'never read the table'."*

## What went badly

- **The aggregate was written as if the table were small.** A fresh install
  has a handful of rows; a long run has 928,005. The code was correct and
  unbounded.
- **Two unbounded reads sat in the same component.** The aggregate and the
  scoring lookup were different code paths with the same shape, and neither was
  bounded.
- **The container's limit was the same as the natural working set.** 670 MB
  against a 670 MB limit is not a coincidence on this machine; it means the
  process wanted more memory than it was allowed, and Docker chose the
  predictable option.
- **The average hid it.** A 454.54 ms average over a 90-second run looks like
  a slow-but-working system. Only the p99 and the restart count told the
  truth.

---

## Action items

| # | Action | Status |
|---|---|---|
| 1 | Aggregate the prediction error in SQL | **Done** |
| 2 | Narrow the "already scored" lookup to the ticks being scored | **Done** |
| 3 | Move the evaluation off the event loop into a worker thread | **Done** |
| 4 | Keep the "before" path in the repository behind a setting, so the comparison can be re-measured | **Done** — `PREDICTION_ERROR_AGGREGATE_IN_DATABASE=false` |
| 5 | Sample the container's CPU from the cgroup, not from `docker stats`, in the harness | **Done** |
| 6 | Check `OOMKilled` as part of the harness's verdict | **Done** |
| 7 | A **retention job** for `prediction_evaluations` | **Not done** — the aggregate is now O(1) in memory so growth no longer costs latency, but *"The table still grows — a retention job would be the next step"* ([`day6.md`](../progress/day6.md)) |
| 8 | Alert on container restart count, so an OOM kill is visible without reading `docker inspect` | **Not done** — the backend publishes its own cgroup fraction (`container_memory_fraction`), but no alert rule fires on a restart count |
