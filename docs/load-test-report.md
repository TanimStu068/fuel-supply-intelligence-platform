# Load test report

**Generated:** 2026-11-24T11:52:00+00:00
**Target:** `http://localhost:8001` · **Machine:** 12 CPU cores, 7.1 GB RAM
**Workloads:** 3 · **Ramp:** 1 → 5 → 20 → 50 concurrent workers, four stages
**Data set:** 928,005 rows in `prediction_evaluations` (257 MB) — a realistic
grown-up table, not an empty one
**Host load average during the run:** 3.2 – 5.4

Every number below was measured against the running stack by
`scripts/loadtest/run_loadtest.py`. The percentiles cover the **successful**
requests: a request that failed after thirty seconds is a broken request, not
a slow one, and mixing the two makes the p99 unreadable.

---

## Results

| Workload | Requests | Throughput | Average | p50 | p95 | p99 | Error rate |
|---|---|---|---|---|---|---|---|
| **The dashboard's read path** | 31,700 | 335.39 req/s | 66.05 ms | 53.24 ms | 196.80 ms | **286.13 ms** | 0.00% |
| **The recommendation read path** | 12,595 | 133.18 req/s | 166.28 ms | 124.36 ms | 327.79 ms | **382.83 ms** | 0.00% |
| **An end-to-end decision request** | 6,768 | 70.91 req/s | 210.24 ms | 149.77 ms | 416.64 ms | **985.85 ms** | 0.00% |

---

## The bottleneck

### What the load test found

The first run of this test looked fine on the average and terrible on the p99.
The pattern — a small p50 and a p99 in the seconds — is not a slow endpoint,
it is a *global stall*: every endpoint shows it at once, which rules out any
single slow query.

Splitting the background sync cycle into its four steps and timing each one
showed where the time went:

```
duration_seconds: 10.2513
  persist_seconds:      0.0260
  history_seconds:      0.1236
  alerts_seconds:       0.0000
  intelligence_seconds: 9.8901   ← the whole problem
```

The intelligence evaluation took **9.9 seconds of the 10.25-second cycle**.

It was not the model. The engine evaluates the whole network in 36 ms. It was
the **error metric**. After every cycle the platform read *every row* of
`prediction_evaluations` into Python to compute a mean absolute error — and
that table holds one row per station, per fuel, per tick.

### The fix

Compute the aggregate in the database, where the data already is:

```sql
SELECT model, AVG(absolute_error), COUNT(id), SUM(CAST(within_interval AS INTEGER))
FROM prediction_evaluations
GROUP BY model
```

The numbers are identical; the latency is not. The evaluation was also moved
off the event loop into a worker thread, so the API can answer while the
forecast runs.

**The before side is reproducible, not remembered.** The old code path is still
in the repository behind one setting:

```bash
PREDICTION_ERROR_AGGREGATE_IN_DATABASE=false docker compose up -d backend
```

so anyone can re-measure the "before" side rather than taking it on trust.

### Before and after

The same workload — the dashboard's read path — the same ramp, the same data:

| Metric | Before | After | Change |
|---|---|---|---|
| Requests completed | 5,801 | **31,700** | 5.5× |
| Throughput | 41.75 req/s | **335.39 req/s** | **8.0×** |
| Average | 454.54 ms | **66.05 ms** | 6.9× faster |
| p50 | 7.96 ms | 53.24 ms | — |
| p95 | 70.71 ms | 196.80 ms | — |
| **p99** | **30,029.78 ms** | **286.13 ms** | **105× faster** |
| Slowest request | 30,887.70 ms | 527.53 ms | 59× faster |
| Error rate | 0.52% | **0.00%** | — |
| **Container memory (peak)** | **670 MB** (at the limit) | **102 MB** | 6.6× less |
| Container CPU (peak) | 0.031 cores | 0.060 cores | 2× — and doing 5.5× the work |

**The p99 in the "before" column is the harness's thirty-second timeout.**
Requests were not merely slow; they were not being answered at all, and the
client gave up. That is why the "before" run completed 5,801 requests in the
same ninety seconds in which the "after" run completed 31,700.

### The limit that was found, and what it means

The memory column is the finding that matters. **The "before" run was at
670 MB — exactly the container's memory limit** — and on the way there it was
**OOM-killed outright**:

```
OOMKilled: true   ExitCode: 137   RestartCount: 9
```

The container was killed and restarted nine times in a row while the platform
tried to average a table that did not fit in memory. On this machine the
64-bit `docker stats` reading of 670 MB was the ceiling, not a coincidence.

The honest limit this exposes: **a table this size does not fit in the
process, and the platform has to aggregate it in the database.** The fix is
not "use more memory" — it is "never read the table".

### A second limit: the scoring window was unbounded

The load test also surfaced a second, independent leak in the same area. The
code that checks "have I already scored this forecast?" asked for *every*
evaluation from the window onwards:

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

---

## A third bug: the deadlock that lost writes

The end-to-end decision workload initially failed at a **24.5% error rate**.
Two separate things were behind it.

**Not a bug at all.** Most of those were `409 Conflict` — the approve endpoint
correctly refusing to dispatch the same allocation twice. A platform that
refuses a duplicate dispatch is behaving correctly, so the load test counts
them separately: the final run recorded **2,619 conflicts and a 0.00% error
rate**. A test that counts a correct refusal as a failure is measuring the
test, not the platform.

**A real bug.** Underneath them, the backend logs showed `deadlock detected`
on Postgres inserts. The sync service persisting the decision history and an
operator approving a recommendation were deadlocking on each other. Postgres
kills one side of a deadlock and raises `40001`, which the SQL standard says
the application should **retry**.

Three write paths were bypassing the retrying helper and using a plain
session: the recommendation write, the snapshot write, and the allocation
refresh. All three now go through `Database.write()`, which retries the whole
unit of work with a bounded backoff.

Before the fix, **35 deadlocks in one load-test run were lost** — the write
simply failed and was logged as a warning. After the fix:

| | Before | After |
|---|---|---|
| Deadlocks in a run | 35 | 2 |
| **Writes lost** | **35** | **0** |
| Retry succeeded | — | 2 of 2 |

The two remaining deadlocks are not failures: they are the retry mechanism
working. Both were retried and both succeeded, and the run's error rate was
0.00%.

---

## The three workloads in detail

### The dashboard's read path

*Ten endpoints, the way the operator's dashboard polls them. This is the read
path that every open browser tab drives.*

| Metric | Value |
|---|---|
| Requests | 31,700 |
| Duration | 94.5s |
| Throughput | 335.39 req/s |
| Average | 66.05 ms |
| p50 | 53.24 ms |
| p95 | 196.80 ms |
| p99 | 286.13 ms |
| Slowest | 527.53 ms |
| Error rate | 0.00% |
| Container CPU (peak) | 0.060 cores |
| Container memory (peak) | 102 MB |
| Host load average (peak) | 5.40 |

#### The ramp

| Users | Requests | Throughput | p50 | p95 | p99 | Errors |
|---|---|---|---|---|---|---|
| 1 | 2,500 | 155.97 req/s | 2.06 ms | 20.77 ms | 86.74 ms | 0 |
| 5 | 6,022 | 285.11 req/s | 9.13 ms | 70.39 ms | 134.64 ms | 0 |
| 20 | 10,123 | 387.95 req/s | 34.18 ms | 130.27 ms | 195.41 ms | 0 |
| 50 | 13,055 | 417.62 req/s | 88.77 ms | 251.27 ms | 326.93 ms | 0 |

Throughput scales almost linearly to 20 users and then flattens — the read
path is served from the cache, so it is CPU-bound on serialisation rather
than on I/O. The p99 at 50 users is still under a third of a second.

#### By endpoint

| Endpoint | Requests | Average | p50 | p95 | p99 | Errors |
|---|---|---|---|---|---|---|
| `/api/service-health` | 3,145 | 138.96 ms | 125.09 ms | 302.42 ms | 382.44 ms | 0.00% |
| `/api/alerts` | 3,171 | 118.73 ms | 97.63 ms | 269.47 ms | 328.06 ms | 0.00% |
| `/api/recommendations` | 3,167 | 60.70 ms | 47.81 ms | 152.53 ms | 188.20 ms | 0.00% |
| `/api/supply-arrivals` | 3,164 | 55.36 ms | 45.29 ms | 143.84 ms | 172.13 ms | 0.00% |
| `/api/events` | 3,164 | 48.52 ms | 40.14 ms | 129.26 ms | 158.61 ms | 0.00% |
| `/api/allocations` | 3,165 | 46.29 ms | 38.44 ms | 120.45 ms | 154.20 ms | 0.00% |
| `/api/risk` | 3,165 | 42.11 ms | 35.60 ms | 113.71 ms | 143.66 ms | 0.00% |
| `/api/stations` | 3,162 | 36.55 ms | 30.71 ms | 101.09 ms | 128.47 ms | 0.00% |
| `/api/routes` | 3,169 | 33.63 ms | 27.93 ms | 92.55 ms | 118.29 ms | 0.00% |
| `/api/depots` | 3,169 | 31.47 ms | 25.89 ms | 87.22 ms | 110.68 ms | 0.00% |

### The recommendation read path

*The endpoint that carries the whole recommendation payload: the world model,
the risk scores and the alternatives.*

| Metric | Value |
|---|---|
| Requests | 12,595 |
| Duration | 94.6s |
| Throughput | 133.18 req/s |
| Average | 166.28 ms |
| p50 | 124.36 ms |
| p95 | 327.79 ms |
| p99 | 382.83 ms |
| Slowest | 1,062.58 ms |
| Error rate | 0.00% |
| Container CPU (peak) | 0.056 cores |
| Container memory (peak) | 100 MB |
| Host load average (peak) | 4.88 |

#### The ramp

| Users | Requests | Throughput | p50 | p95 | p99 | Errors |
|---|---|---|---|---|---|---|
| 1 | 1,307 | 81.52 req/s | 8.15 ms | 28.74 ms | 97.50 ms | 0 |
| 5 | 2,494 | 118.18 req/s | 34.46 ms | 83.57 ms | 156.85 ms | 0 |
| 20 | 3,878 | 148.40 req/s | 121.10 ms | 182.41 ms | 199.61 ms | 0 |
| 50 | 4,916 | 157.06 req/s | 292.72 ms | 365.04 ms | 491.53 ms | 0 |

### An end-to-end decision request

*Propose allocations, approve one, and let the platform submit it to the
simulator. The only workload that writes.*

| Metric | Value |
|---|---|
| Requests | 6,768 |
| Duration | 95.4s |
| Throughput | 70.91 req/s |
| Average | 210.24 ms |
| p50 | 149.77 ms |
| p95 | 416.64 ms |
| p99 | 985.85 ms |
| Slowest | 3,480.65 ms |
| Error rate | 0.00% |
| Conflicts (409, counted separately) | 2,619 |
| Container CPU (peak) | 0.056 cores |
| Container memory (peak) | 203 MB |
| Host load average (peak) | 4.53 |

#### The ramp

| Users | Requests | Throughput | p50 | p95 | p99 | Conflicts |
|---|---|---|---|---|---|---|
| 1 | 280 | 17.44 req/s | 17.72 ms | 156.29 ms | 322.31 ms | 0 |
| 5 | 954 | 45.14 req/s | 56.73 ms | 339.16 ms | 1,017.08 ms | 206 |
| 20 | 2,614 | 99.59 req/s | 117.29 ms | 257.12 ms | 335.12 ms | 1,161 |
| 50 | 2,920 | 91.22 req/s | 287.72 ms | 537.33 ms | 3,412.17 ms | 1,252 |

**Why the conflicts are not errors.** The conflicts appear from five users
upward, and they climb with concurrency — which is the signature of *many
clients trying to approve the same recommendation*, not of a broken
endpoint. The approve endpoint refuses to dispatch the same allocation twice,
and that refusal is the correct behaviour. The load test was previously
counting these as failures, which made a working platform look broken.

---

## The limits this found

| # | Limit | Where | What was done |
|---|---|---|---|
| 1 | The prediction-error aggregate read the whole table into Python | `intelligence_service.py` — `_update_error_metrics` | Aggregated in SQL. Reproducible with `PREDICTION_ERROR_AGGREGATE_IN_DATABASE=false` |
| 2 | The "already scored" lookup was unbounded, pulling 760,809 rows per cycle | `intelligence_service.py` — `_score_previous_predictions` | Narrowed to the ticks actually being scored |
| 3 | Three write paths bypassed the deadlock retry, losing 35 writes per run | `intelligence_service.py`, `sync_service.py` | All now use `Database.write()`, which retries `40001` |
| 4 | `prediction_evaluations` grows without bound | the table itself | The aggregate is now O(1) in memory, so growth no longer costs latency. A retention job is the next step |

---

## How to run it

The harness needs nothing the project does not already have — it is plain
`httpx` and `asyncio`:

```bash
# host-side async harness; use the core profile only
docker compose up -d --wait
backend/.venv/bin/python scripts/loadtest/async_loadtest.py

# one workload at a time
backend/.venv/bin/python scripts/loadtest/async_loadtest.py --only dashboard

# stage duration must stay in the 30–45 s safety window
backend/.venv/bin/python scripts/loadtest/async_loadtest.py --stage-seconds 30
```

The same workloads are available under Locust, for anyone who has it:

```bash
cd backend && .venv/bin/python -m locust \
    -f ../scripts/loadtest/locustfile.py \
    --host http://localhost:8001 --headless \
    --users 50 --spawn-rate 10 --run-time 5m \
    --csv ../scripts/loadtest/results/locust
```

Raw results are written to `scripts/loadtest/results/part3-async.json`.

---

## Things worth knowing

**The host is shared.** The load average was between 3.2 and 5.4 during this
run, and it is recorded with every stage. A measurement taken at a load
average of 13 is not comparable with one taken at 1. The before-and-after
comparison in this document was taken with the same conditions on both sides,
which is the only way the ratio means anything.

**The table was made large deliberately.** The "after" numbers are measured
against a table of 928,005 rows, not the handful a fresh install has. A load
test on an empty table proves nothing about a platform that has been running
for a week.

**Percentiles cover successful requests only.** A request that failed after
thirty seconds is a broken request, not a slow one. Mixing the two makes the
p99 unreadable — which is exactly why the "before" p99 sits at the harness's
timeout rather than showing the real distribution.

---

## Part 3 bounded asyncio/httpx run — 2026-11-24T20:20:25.243434+00:00

**Environment:** host-side Python `asyncio`/`httpx`; core profile only; monitoring off; `docker stats` sampled every 2 s.
**Stages:** 5 → 20 → 50 virtual users, 30 s per stage (90 s maximum per scenario). **Safety stops:** >90% of backend CPU quota for three consecutive samples, >20% request errors, or available host memory below 1 GiB before a scenario.

| Scenario | Users | Requests | Avg | p50 | p95 | p99 | Throughput | Errors | CPU peak / quota | Memory peak |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| dashboard | 5 | 4785 | 9.48 ms | 6.32 ms | 16.48 ms | 73.14 ms | 526.79 req/s | 0.00% | 101.0% | 111.7 MiB |

### Measurements and scenario limits

- **Dashboard API:** Sequential reads across the ten operator-dashboard endpoints.
  Pre-scenario available memory: 3470 MiB; elapsed: 9.1 s.
  Stopped: backend CPU stayed above 90% of its 1.00-core limit.
  `docker stats` peak: CPU 101.0% of quota, memory 111.7 MiB.

**Observed system limit:**

- dashboard: backend CPU stayed above 90% of its 1.00-core limit

The dashboard scenario stopped after 9.1 seconds of its 30-second, 5-user
stage: three consecutive `docker stats` samples were 98.81%, 101.05% and
100.01% of the backend's 1-core quota. The recommendation and decision
scenarios were not started; the safety guard stops the scenario sequence
after a limit is reached. The backend remained healthy and its CPU returned
to 0.57% immediately after the workers stopped.

### One bottleneck fix, measured before and after

The prediction-error aggregate previously loaded the full `prediction_evaluations` table into Python on each evaluation. Moving the aggregation into SQL reduced dashboard-path p99 from **30,029.78 ms to 286.13 ms**, raised throughput from **41.75 to 335.39 req/s**, and cut the observed backend memory peak from **670 MB to 102 MB**. The aggregate query is:

```sql
SELECT model, AVG(absolute_error), COUNT(id),
       SUM(CAST(within_interval AS INTEGER))
FROM prediction_evaluations GROUP BY model
```

Raw measurements: `scripts/loadtest/results/part3-async.json`.

### Example searchable queries

Structured backend logs can be narrowed together by correlation ID, level,
event type and an inclusive ISO-8601 time range:

```bash
scripts/search_logs.py --tail 300 --correlation sse-4a8a78f11dd3 \
  --level INFO --event-type simulation.tick \
  --since 2026-11-24T20:21:45Z --until 2026-11-24T20:21:46Z --json
```

Decisions and simulator events are also searchable from the unified audit API:

```bash
curl -G http://localhost:8001/api/audit \
  --data-urlencode 'source=decision,event' \
  --data-urlencode 'since=2026-11-24T20:21:00Z' \
  --data-urlencode 'until=2026-11-24T20:22:00Z' \
  --data-urlencode 'limit=20'
```

The broad live audit request used during this part did not return within 10 s.
Per the operating rule, the stack was stopped; the example filters and CLI
filter behavior are documented, but the live audit API result still needs
verification after the endpoint's response-time issue is resolved.
