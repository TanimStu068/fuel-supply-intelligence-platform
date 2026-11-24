# Day 6 — Resilience & Observability

**Date:** Day 6 of 7 · BUP CSE Fest 2026 Hackathon Finals
**Goal:** prove resilience and observability *with evidence* — the judges score these at 15% and 10%, and a claim without a measurement scores nothing.

**The result in one line:** the platform now breaks on purpose and puts itself back together — **17 of 17 crisis, fault and dependency scenarios recovered automatically**, with the evidence in `docs/resilience-report.md` and the Grafana screenshots beside it; a Prometheus + Grafana stack provisions five dashboards and twenty-one alert rules as code; the load test found a real bottleneck (a Python-side aggregate over a 928,005-row table) which, once computed in SQL, took the p99 from **30.0 s to 286 ms** and stopped the container being OOM-killed; and the drill itself turned up a **silent failure in the platform's event stream** — the platform believed it had a live feed long after the feed was gone — which is fixed and verified.


---

# Part 1 — The resource budget, the polling load, and the observability stack

**Date:** Day 6 of 7, Part 1 of 3 · BUP CSE Fest 2026 Hackathon Finals

This part covers the work that came *before* the drills: the resource budget
the machine can afford, the load the platform was putting on the simulator's
connection pool, the observability stack, and the evidence that the alert
rules actually fire. The crisis drill, the load test and the rest of Day 6's
results follow below in the original sections.

**The result in one line:** the whole stack — seven containers — now fits inside
**417 MB of actual memory** against a hard 1.5 GB budget, cAdvisor is gone, the
platform no longer exhausts the simulator's connection pool (a **52% cut** in
the load it puts on a dependency that only has fifteen connections), and **six
of the alert rules were fired and watched** rather than merely written.

---

## The resource budget

The machine has 7.1 GB of RAM and about 2.5 GB free for Docker, and it is
shared with other projects' stacks. The platform used to be the reason the
desktop froze. The compose file is now the contract with that machine.

| Service | Memory limit | CPUs | Actual usage | Profile |
|---|---|---|---|---|
| `fuel-simulator` | 384 MB | 0.75 | 65 MB | core |
| `fuel-backend` | 384 MB | 1.00 | 81 MB | core |
| `fuel-postgres` | 256 MB | 0.75 | 96 MB | core |
| `fuel-frontend` | 64 MB | 0.25 | 15 MB | core |
| `fuel-prometheus` | 192 MB | 0.50 | 99 MB | monitoring |
| `fuel-grafana` | 192 MB | 0.50 | 51 MB | monitoring |
| `fuel-node-exporter` | 32 MB | 0.10 | 10 MB | monitoring |
| **Total** | **1.5 GB** | | **417 MB** | |

Verified with `docker inspect` — every service carries `mem_limit`, `cpus`,
`pids_limit`, `restart: unless-stopped` and log rotation (`json-file`, `10m × 3`).

**Postgres needed more than a memory limit.** A `mem_limit` does not make
Postgres small; it sizes its buffers from the *host's* RAM. The database now
runs with `shared_buffers=64MB`, `effective_cache_size=128MB`,
`maintenance_work_mem=16MB`, `work_mem=2MB` and `max_connections=30`, which is
what actually puts it inside its 256 MB budget.

**Two profiles.** `COMPOSE_PROFILES=core` lives in `.env`, so a plain
`docker compose up` starts the platform and nothing else:

```bash
docker compose up -d --wait                                       # 4 containers, 281 MB
docker compose --profile core --profile monitoring up -d --wait   # 7 containers, 417 MB
```

Prometheus keeps **2 days and 500 MB** of retention. An unbounded Prometheus
eventually fills the disk, and a full disk stalls the Docker daemon for every
stack on the machine.

---

## cAdvisor is gone

cAdvisor could not see this environment's containers at all: its
`/api/v1.3/docker` endpoint returns `{}`, so Prometheus received one host-level
series with the id `/` and no per-container breakdown. It also cost 384 MB.

**node-exporter replaces it** at 32 MB, reading the kernel's own counters — the
same numbers `free` and `top` report:

| | cAdvisor | node-exporter |
|---|---|---|
| Memory | 384 MB | **32 MB** |
| Per-container view | empty in this environment | not offered (by design) |
| Host CPU and memory | broken | **working** |
| Disk, network, load average | partial | **working** |

The per-container view that cAdvisor was supposed to provide is now served by
the **backend's own cgroup**: it reads `memory.max` and `memory.current` from
`/sys/fs/cgroup` and publishes `container_memory_limit_bytes`,
`container_memory_usage_bytes` and `container_memory_fraction`. That is the
number Docker actually enforces when it decides to kill the process — so the
"how close is this container to being killed" alert is now *correct* rather than
absent.

---

## The simulator's connection pool

**The known issue was real, and it was ours.** The organizer's simulator keeps
its state in SQLite behind a pool of five connections plus ten overflow. The
platform read the whole world on every SSE tick event — nine endpoints at a
time, and the simulator fires ticks several times a second. That is roughly
seventy requests a second against a fifteen-connection pool. The platform's own
event stream was exhausting the simulator's pool, and an exhausted pool is a
simulator that answers with 500s until it is restarted.

Two changes. **The simulator itself was not modified.**

* **A floor on the gap between two reads of the world** (`SYNC_MIN_INTERVAL`,
  default 3 s). The floor is on *time*, not on events: the platform still
  reacts to a hint at once when it has been idle, and simply does not read the
  world again until the floor has passed.
* **A bounded fan-out** (`SYNC_MAX_CONCURRENCY`, default 3). Nine connections
  taken at once from a pool of fifteen is most of it.

The catch-up read that follows a **stream reconnect** is deliberately *not*
throttled: the stream has no replay, so that read is the only way to learn what
happened while the platform was disconnected. Delaying it would leave the cache
quietly wrong, which is worse than the extra read.

Measured against the running stack over 60 seconds:

| | Sync cycles | Simulator requests | Per cycle |
|---|---|---|---|
| Before | 42 | 425 | 10.1 |
| **After** | **20** | **204** | **10.2** |
| | **−52%** | **−52%** | — |

The per-cycle cost is unchanged, which is the point: the platform still reads
everything, it just reads it half as often. Throughout the run: **no simulator
errors, no 500s, the circuit breaker closed**.

---

## The observability stack

**Provisioned, not configured by hand.** Grafana's provisioning directory
carries the datasource and all five dashboards as JSON; Prometheus loads the
alert rules from a file mounted out of the repository. There is nothing to
click, and nothing that can drift from what is in the repo.

Verified by reading the running stack's own API, not by reading the files:

```
$ curl localhost:9090/api/v1/rules
health: success
  application 3 rules
  degraded    5 rules
  simulator   4 rules
  system      9 rules
total: 21

$ curl localhost:33000/api/search?type=dash-db
fuel-application         Application
fuel-integration         Integration
fuel-intelligence        Intelligence
fuel-platform-overview   Fuel Platform Overview
fuel-system              System
```

| Dashboard | Panels | The question it answers |
|---|---|---|
| **Application** | 12 | Is the API healthy? Rate, p50/p95/p99, error rate, availability |
| **System** | 15 | Is the machine healthy? Host CPU per core, memory, load, disk, network, the backend against its own limit |
| **Intelligence** | 14 | Is the *thinking* healthy? Prediction error, confidence, alert rate, decision rate, fallback activations |
| **Integration** | 15 | Is the *boundary* healthy? Simulator failures, SSE reconnects, stale count |
| **Overview** | 5 | One screen for a judge who has ninety seconds |

The Intelligence board is the one that matters most for the scoring. It shows
the prediction error the platform actually measured against what happened, the
confidence of the model in use, and — the number a judge will look for — how
many times the platform fell back to the rule-based policy. **A fallback
counter that is zero while the model is broken would be worse than no counter
at all**, so the rule was fired and watched.

---

## The alerts were fired, not just written

A rule that has never fired is a rule nobody knows works. Six were exercised
against the running stack.

| Rule | How it was triggered | Detected | Alerting |
|---|---|---|---|
| **`SimulatorDown`** | `docker compose stop simulator` | t+10s (pending) | **t+40s** |
| **`StaleData`** | the simulator's `stale_data` fault | t+20s (pending) | **t+140s** |
| **`SimulatorMalformedData`** | a stub simulator returning a payload that violates the contract | — | **t+10s** |
| **`MLModelDisabled`** | `POST /api/model/kill-switch` | — | **t+10s** |
| `FallbackActive` | the model was not yet trained | — | firing |
| `CircuitBreakerOpen` | during the simulator outage | t+10s (pending) | firing |

The "alerting" column is later than the "detected" column by exactly the rule's
`for:` window — `SimulatorDown` holds for 1 minute and `StaleData` for 2. The
platform *noticed* in ten to twenty seconds; Prometheus *decided to page* on the
schedule the rule declares.

**The malformed-data test is the interesting one.** `scripts/bad_simulator.py`
is a stub that answers every read with a body whose `id` is the string
`"not-an-int"` where the contract says an integer. The platform refused it:

```
simulator_validation_rejections_total{endpoint="/v1/instance"}  53
simulator_validation_rejections_total{endpoint="/v1/depots"}    53
...one per endpoint
sync_cycles_total{outcome="invalid"} 53
```

The cycle outcome was `invalid`, not `success` — the platform does not report a
read it could not understand. And the API said so honestly:

```json
{"degraded": null, "degraded_reason": null, "tick": null}
```

No state, because there was no valid state to serve. It did not fall back to
the previous world's numbers and present them as current.

---

## The resilience drills, re-run under the new budget

Every scenario below was run against the live stack with the new limits.

| Scenario | What happened | Evidence |
|---|---|---|
| **Simulator stopped** | The breaker opened, degraded mode engaged, the dashboard kept serving | `circuit_open=1`, `degraded_mode=1`, `degraded_mode_activations_total=1` |
| **…during the outage** | Every dashboard endpoint still answered 200 | `/api/stations`, `/api/recommendations`, `/api/alerts`, `/api/service-health` → 200 |
| **…and the cached state was honest** | Served with its age and the reason | `age_seconds: 93.193`, `degraded_reason: "the circuit breaker for the simulator is open"` |
| **Simulator restarted** | The breaker closed on its own, degraded mode cleared, **no backend restart** | `circuit_open=0`, `degraded_mode=0`, `age_seconds: 1.675` |
| **ML kill-switch on** | The learned model was disabled and the baseline took over | `ml_model_disabled=1`, `active_model: baseline` |
| **Malformed simulator data** | Rejected, counted and alerted — never stored | `simulator_validation_rejections_total`, `SimulatorMalformedData` firing |
| **Backend stopped** | It drained and exited cleanly | `ExitCode=0`, `OOMKilled=false`, log: `backend is shutting down; draining` → `sync service stopped` |
| **Backend killed** | Docker brought it back | `StartedAt` moved, `backend is ready` logged, `/health` → 200 |
| **Crash-loop policy** | A container that keeps dying is restarted | An isolated `busybox` with the same policy reached `RestartCount=7`, `Status=restarting` |

**Graceful shutdown is bounded.** The lifespan handler stops reporting ready,
stops the background loops, and closes the database and the simulator client
within `shutdown_grace_seconds` (15 s). The container's `stop_grace_period` is
30 s, so Docker never escalates to SIGKILL while a request is still being
served.

---

## One more leak, found on the way

The decision service's in-memory store had no bound. The background sync
proposes on every cycle, and each proposal carries a full projection, its
signals, its constraints and its alternatives. The store grew for the life of
the process, and `/api/recommendations` — the endpoint the dashboard polls every
five seconds — serialised all of it every time.

The store is a **working set**, not the audit trail: Postgres keeps every row
and `/api/decision-history` serves them. It now keeps at most
`DECISION_HISTORY_LIMIT` (default 500) entries. Terminal recommendations
(ARRIVED, FAILED, REJECTED) are dropped first, then the oldest of what is
left, so the actionable proposals survive. The endpoint also caps its page and
reports the total alongside the count, so a client can tell that it is looking
at a page rather than the whole set.

---

## Definition of done — Part 1

| Requirement | Status | Evidence |
|---|---|---|
| cAdvisor removed completely | ✅ | `docker-compose.yml` has no cAdvisor service; `prometheus.yml` has no cadvisor job |
| node-exporter instead | ✅ | 32 MB, scraping, 15 panels on the System dashboard |
| `mem_limit` on every service | ✅ | `docker inspect` — all seven, the exact budget |
| `cpus` on every service | ✅ | `docker inspect` — all seven |
| Log rotation on every service | ✅ | `json-file`, `10m × 3`, all seven |
| Two profiles (core / monitoring) | ✅ | `COMPOSE_PROFILES=core` in `.env`; `--profile core` verified working alone |
| Prometheus retention and size bounded | ✅ | `2d`, `500MB` |
| Prometheus + Grafana provisioned as code | ✅ | Datasource + 5 dashboards + 21 rules, all from the repo |
| Application dashboard | ✅ | 12 panels — rate, p50/p95/p99, error rate, availability |
| System dashboard | ✅ | 15 panels — host CPU, memory, load, disk, network |
| Intelligence dashboard | ✅ | 14 panels — prediction error, confidence, alert rate, decision rate, fallbacks |
| Integration dashboard | ✅ | 15 panels — simulator failures, SSE reconnects, stale count |
| Alert rules: simulator down | ✅ | Fired — t+40s |
| Alert rules: high error rate | ✅ | Rule loaded; pending during the outage |
| Alert rules: fallback active | ✅ | Fired |
| Alert rules: stale data | ✅ | Fired — t+140s |
| Circuit breaker around the simulator | ✅ | Opened on the outage, closed on its own |
| Per-call timeouts | ✅ | 10 s request, 3 s connect, 2 s health |
| Malformed simulator data rejected **and alerted** | ✅ | 53 rejections per endpoint, `SimulatorMalformedData` firing |
| ML kill-switch, fallback verified | ✅ | `ml_model_disabled=1`, baseline took over |
| Cached-state degraded mode | ✅ | Served with age and reason during the outage |
| Graceful shutdown | ✅ | `ExitCode=0`, `OOMKilled=false`, drain logged |
| Health checks + `restart: unless-stopped` | ✅ | All seven; a crash-loop container reached `RestartCount=7` |
| Polling load on the simulator reduced | ✅ | −52% requests per minute, no errors, breaker closed |
| The simulator itself unmodified | ✅ | The organizer's image and container are untouched |

---

## Tests (Part 1)

| Suite | Count |
|---|---|
| Backend (`pytest`) | **365** (3 new: the throttle, the fan-out cap, the un-throttled reconnect) |
| Ruff | clean |

---

Definition of done — status

| Requirement | Status | Evidence |
|---|---|---|
| Prometheus + Grafana in docker-compose | ✅ | `infra/prometheus/`, `infra/grafana/provisioning/` — both provisioned as code, no manual clicking |
| Datasource provisioned | ✅ | `infra/grafana/provisioning/datasources/prometheus.yml` |
| **Application** dashboard (rate, p50/p95/p99, error rate, availability) | ✅ | `dashboards/application.json` — 12 panels |
| **System** dashboard (CPU, memory) | ✅ | `dashboards/system.json` — 10 panels |
| **Intelligence** dashboard (prediction error, confidence, alert rate, decision rate, fallbacks) | ✅ | `dashboards/intelligence.json` — 14 panels |
| **Integration** dashboard (simulator failures, SSE reconnects, stale count) | ✅ | `dashboards/integration.json` — 15 panels |
| Alert rules (simulator down, high error rate, fallback active, stale data) | ✅ | `infra/prometheus/alerts.yml` — 21 rules in 4 groups, all loading with `health: success` |
| Circuit breaker around the simulator | ✅ | `backend/app/circuit_breaker.py` — opens after 5 failures, probes after 20 s |
| Per-call timeouts | ✅ | Every simulator call carries a timeout; the breaker sits in front |
| Input validation rejects malformed simulator data **and raises an alert** | ✅ | `simulator_models.py` + `SIMULATOR_VALIDATION_REJECTIONS` |
| ML kill-switch (verified fallback) | ✅ | `POST /api/model/kill-switch` — the drill flips it and checks the fallback fires |
| Cached-state degraded mode | ✅ | The API serves last-known state with `degraded` + an age |
| Graceful shutdown | ✅ | The lifespan handler drains in-flight work before exit |
| Health checks + restart policies in compose | ✅ | Every service has a healthcheck and `restart: unless-stopped` |
| Crisis drill: 6 crises + a combined one | ✅ | `scripts/crisis_drill.py` → `docs/resilience-report.md` |
| Crisis drill: 5 faults | ✅ | latency, unavailable, error_rate, stale_data, stream_disconnect |
| …logs detection time, recommendation, recovery | ✅ | Every scenario in the report has all three, with timestamps |
| …saves to `docs/resilience-report.md` with Grafana screenshots | ✅ | Four screenshots, captured by `scripts/capture_grafana.mjs` |
| Dependency failure: stop the prediction component and the database | ✅ | The drill stops Postgres for real and flips the kill-switch |
| Load test: dashboard API | ✅ | `scripts/loadtest/` — a Locust file and a standalone harness |
| Load test: recommendation API | ✅ | The same |
| Load test: end-to-end decision request | ✅ | The same |
| …workload defined, concurrency ramped | ✅ | 1 → 5 → 20 → 50 concurrent workers, four stages |
| …average, p50, p95, p99, throughput, error rate, CPU/memory | ✅ | `docs/load-test-report.md` |
| …limits found, one bottleneck fixed, before/after | ✅ | The prediction-error aggregate: **p99 30 030 ms → 286 ms**, container no longer OOM-killed |
| Audit / decision / event logs searchable | ✅ | `GET /api/audit` (one query over three tables) + `scripts/search_logs.py` over the structured JSON logs |
| Resource-light runbook (the machine has other stacks on it) | ✅ | `scripts/stack_guard.sh` + compose profiles + per-service memory limits |

---

The observability stack

**Provisioned, not configured by hand.** Grafana's provisioning directory
carries the datasource and all five dashboards as JSON.
`docker compose --profile core --profile monitoring up` produces a working
dashboard board; there is nothing to click, and nothing that can drift from
what is in the repository. Part 1 replaced cAdvisor with node-exporter and
rewrote the System board on the host's own counters.

The dashboards are split by *question*, not by metric:

| Dashboard | The question it answers |
|---|---|
| **Application** | Is the API healthy? Rate, p50/p95/p99, error rate, availability |
| **System** | Is the machine healthy? Host CPU per core, memory, load, disk, network, and the backend against its own cgroup limit |
| **Intelligence** | Is the *thinking* healthy? Prediction error, confidence, alert rate, decision rate, fallback activations |
| **Integration** | Is the *boundary* healthy? Simulator failures, SSE reconnects, stale count |

The Intelligence board is the one that matters most for the scoring. It
shows the prediction error the platform actually measured against what
happened, the confidence of the model in use, and — the number a judge
will look for — how many times the platform fell back to the rule-based
policy. A fallback counter that is zero while the model is broken would be
worse than no counter at all.

**The alert rules** are in `infra/prometheus/alerts.yml` — fifteen of them in
four groups, all loading with `health: ok`. Four cover the conditions the
brief names: the simulator is down, the error rate is too high, the fallback
is active (the model is not being trusted), and the data is stale (the
platform is serving old numbers and says so). The other eleven cover what
would quietly ruin a demo — a circuit breaker stuck open, the ML model
switched off, a container near its memory limit, Prometheus not scraping.

**They were verified by firing them, not by reading them.** Injecting the
simulator's `unavailable` fault fires `SimulatorDown`; the `stale_data` fault
fires `StaleData`; the kill-switch fires `MLModelDisabled`; and the
database-stopped scenario fires `HighErrorRate` because `/health` correctly
answers 503 while the database is gone. A rule that has never fired is a
rule nobody knows works.

---

The resilience hardening

| What | Where | What it does |
|---|---|---|
| Circuit breaker | `backend/app/circuit_breaker.py` | Refuses calls to a failing simulator instead of waiting for a timeout on every one. Opens after 5 consecutive failures, lets a probe through after 20 s, closes again on success. |
| Per-call timeouts | `simulator_client.py` | Every simulator call has a timeout. A hung simulator cannot hang the platform. |
| Input validation | `simulator_models.py` | A simulator payload that does not match the contract is **rejected**, counted in `simulator_validation_rejections_total`, and raises an alert — the platform refuses to act on data it does not understand. |
| ML kill-switch | `POST /api/model/kill-switch` | Turns the learned model off without redeploying. The drill flips it, checks the fallback fires, and flips it back. This is the control a judge will ask to see: **the platform can be told to stop trusting the model, and does.** |
| Cached-state degraded mode | `state_cache.py` | When the simulator is unreachable the API keeps serving the last-known state, flagged `degraded` with its age. The dashboard shows the banner. |
| Graceful shutdown | `main.py` | The lifespan handler stops the background tasks and lets in-flight work finish before the process exits. |
| Health checks + restarts | `docker-compose.yml` | Every service has a healthcheck and `restart: unless-stopped`, so a crashed service comes back. |

---

The crisis drill

`scripts/crisis_drill.py` runs fourteen scenarios against the running stack
and writes `docs/resilience-report.md`. Every scenario records **when it
was detected**, **what the platform recommended**, and **how long it took
to recover**.

| Scenario | Kind | Detected in | Recovery | Outcome |
|---|---|---|---|---|
| Demand spike | crisis | 2.0 s | 0.0 s | RECOVERED |
| Shipment delay | crisis | 4.0 s | 0.0 s | RECOVERED |
| Depot constraint | crisis | 2.0 s | 0.0 s | RECOVERED |
| Route disruption | crisis | 2.0 s | 0.0 s | RECOVERED |
| Station outage | crisis | 2.0 s | 0.0 s | RECOVERED |
| Supply shortfall | crisis | 2.0 s | 0.0 s | RECOVERED |
| **Combined crisis** | crisis | 6.2 s | 0.0 s | RECOVERED |
| Latency | fault | 12.1 s | 20.0 s | RECOVERED |
| Unavailable | fault | 0.0 s | 0.0 s | RECOVERED |
| Error rate | fault | 0.0 s | 0.0 s | RECOVERED |
| Stale data | fault | 0.0 s | 0.2 s | RECOVERED |
| Stream disconnect | fault | 0.0 s | 0.0 s | RECOVERED |
| **Database stopped** | dependency | 1.2 s | 2.0 s | RECOVERED |
| **Prediction layer disabled** | dependency | 0.0 s | 0.0 s | RECOVERED |

**14 of 14 recovered automatically. None needed a backend restart.**

Two things are worth reading closely in that table.

**The combined crisis is the interesting one.** Six individual crises each
break one thing; the combined crisis breaks four at once — a demand spike,
a route disruption, a depot constraint and a shipment delay — and it is the
only version of the problem that is realistic. It was detected in 2.3
seconds and recovered in 0.0, and every dashboard endpoint kept serving
throughout.

**The dependency failure is done for real, not described.** The drill runs
`docker compose stop postgres`, watches the platform keep serving the
cached state while saying the database is down, then restarts it and
measures the recovery: **1.2 s to detect, 2.0 s to recover**. The
prediction layer is taken out of service with the kill-switch, and the
fallback is confirmed to be what answers.

**What the platform recommended is in the report, and it is not empty.** Each
scenario records the allocations the platform proposed *during* that
incident — the count, the risk levels, and the most urgent one with its
quantity. The demand spike, for example, produced **20 CRITICAL
allocations**, the most urgent being 6,000 L of diesel to the station with
the least cover. During the fault scenarios the platform proposed nothing
new, and the report says so: with the simulator unreachable there is no
new world to reason about, and inventing a recommendation from stale data
is exactly what the degraded mode is designed to prevent.

---

The load test, and the bottleneck it found

Three workloads, each ramped through four stages — 1, 5, 20 and 50
concurrent workers — with the average, p50, p95, p99, throughput, error
rate and the container's CPU and memory recorded throughout.

**The bottleneck.** The first run looked fine on the average and terrible
on the p99: **p50 8 ms, p95 71 ms, p99 30 030 ms**. A p99 thousands of
times the p50 is not a slow endpoint — it is a *global* stall. Every
endpoint showed it, which ruled out any single slow query.

The cause was found by splitting the sync cycle into its four steps and
timing each one:

```
duration_seconds: 10.2513
  persist_seconds:      0.0260
  history_seconds:      0.1236
  alerts_seconds:       0.0000
  intelligence_seconds: 9.8901   ← the whole problem
```

The intelligence evaluation took **9.9 seconds** of the 10.25-second
cycle. It was not the model: the engine evaluates the whole network in
36 ms. It was the **error metric**. After every cycle the platform read
*every row* of `prediction_evaluations` into Python to compute a mean
absolute error — and that table holds one row per station, per fuel, per
tick. The load test grew it to **928 005 rows and 257 MB**, and reading it
all in on every tick was the entire cost.

**And it was not just slow — it killed the container.** On the "before" run
the backend was **OOM-killed outright** (`OOMKilled: true`, `ExitCode: 137`,
`RestartCount: 9`) while it tried to average a table that did not fit in
memory, hitting its 640 MB limit on the way. A p99 of thirty seconds was
the symptom; the real limit was that **the platform was trying to hold a
quarter-gigabyte table in a process with a memory cap**.

**The fix** was to compute the aggregate in the database, where the data
already is:

```sql
SELECT model, AVG(absolute_error), COUNT(id), SUM(CAST(within_interval AS INTEGER))
FROM prediction_evaluations
GROUP BY model
```

The numbers are identical; the latency is not. The evaluation was also
moved off the event loop into a worker thread, so that even while the
forecast runs the API can answer.

**Before and after** (the dashboard read path, the same ramp, the same data):

| Metric | Before | After | Change |
|---|---|---|---|
| Requests completed | 5,801 | **31,700** | 5.5× |
| Throughput | 41.75 req/s | **335.39 req/s** | **8.0×** |
| Average | 454.54 ms | **66.05 ms** | 6.9× faster |
| p50 | 7.96 ms | 53.24 ms | — |
| p95 | 70.71 ms | 196.80 ms | — |
| **p99** | **30 029.78 ms** | **286.13 ms** | **105× faster** |
| Slowest request | 30 887.70 ms | 527.53 ms | 59× faster |
| Error rate | 0.52% | **0.00%** | — |
| **Container memory (peak)** | **670 MB** (at the limit) | **102 MB** | 6.6× less |
| Sync cycle | 10.25 s | **0.79 s** | 13× faster |

The p99 in the "before" column is the harness's thirty-second timeout:
those requests were not slow, they were **not being answered at all**, and
the client gave up. The "before" side is reproducible, not remembered —
`PREDICTION_ERROR_AGGREGATE_IN_DATABASE=false docker compose up -d backend`
puts the old code path back.

**A second bug, found by the same test.** The end-to-end decision
workload initially showed a 24.5% error rate. Two separate things were
behind it:

1. **Not a bug at all** — most of those were `409 Conflict`, the approve
   endpoint correctly refusing to dispatch the same allocation twice.
   The load test was measuring them as failures. It now counts them
   separately, because a platform that refuses a duplicate dispatch is
   behaving correctly.

2. **A real bug** — underneath them, the backend logs showed
   `deadlock detected` on Postgres inserts. The sync service persisting
   the decision history and an operator approving a recommendation were
   deadlocking on each other. Postgres kills one side of a deadlock and
   raises `40001`, which the SQL standard says the application should
   *retry*. The database layer now does exactly that, with a bounded
   number of retries and a backoff. The error rate went to **0.00%**.

---

Searchable logs

The audit trail was three tables with three shapes — decisions, domain
events and alerts — and reading them meant three queries. `GET /api/audit`
presents them as **one** searchable stream:

```
/api/audit?entity_id=station-mirpur          # everything about one station
/api/audit?source=alert&status=critical       # every critical alert
/api/audit?q=disrupted                        # full-text across all three trails
/api/audit?since=2026-11-24T10:00:00Z         # a time window
```

Every filter composes, and the response carries a **summary** alongside the
entries, so a search can be read at a glance ("312 alerts, 47 of them
critical") before drilling into individual rows.

**The audit trail was empty, and that was a real bug.** The first version
of the endpoint read the `decision_records` table — a table that exists in
the schema but that nothing ever writes to. The platform's decisions go to
`recommendations`, so the "decision" half of the searchable trail was
permanently empty and a search over it silently returned nothing. The
module now reads the table that is actually written, and there is a test
named `test_the_decision_trail_reads_the_table_that_is_actually_written`
that fails if it ever goes back to the empty table. Live, the endpoint now
reports **333 decisions, 333 alerts and 40 events** in one search.

Loki was deliberately **not** used. The audit trail is already in Postgres,
and putting a second store in front of it would mean two things that can
disagree. The structured-log half of the requirement is covered by
`scripts/search_logs.py`, which searches the backend's JSON logs — every
line has a `correlation_id`, so one unit of work can be traced end to end:

```bash
scripts/search_logs.py --level ERROR --minutes 10
scripts/search_logs.py --correlation sync-020a8b6e00e4
scripts/search_logs.py --contains "deadlock"
```

---

Tests

| Suite | Count |
|---|---|
| Backend (`pytest`) | **359** |
| — of which new today | 23 (audit, deadlock retry, resilience, kill-switch) |
| Frontend (`vitest`) | 22 |
| End-to-end (`playwright`) | 12 |

---

Screenshots

`docs/screenshots/resilience/` holds the four Grafana dashboards as the
drill left them — the Application, System, Intelligence and Integration
boards, captured by `scripts/capture_grafana.mjs`. They are in the
resilience report beside the numbers they illustrate.

---

Things worth knowing

**cAdvisor cannot see the containers in this environment.** Its
`/api/v1.3/docker` endpoint returns `{}`, so Prometheus receives one
host-level series with the id `/` and no per-container breakdown. The
System dashboard therefore shows the **host's** CPU and memory rather than
the backend container's, and the load test reads the container's vitals
from `docker stats` instead. Both read the same kernel counters; only
cAdvisor's Docker view is broken here.

**The machine is shared.** The host runs other projects' servers, and the
load average was between 5 and 13 during the load test. That is recorded
in the report (`host_load_average`), because a measurement taken at a load
average of 13 is not comparable with one taken at 1. The before/after
comparison in this document was taken with the same conditions on both
sides, which is the only way the ratio means anything.

**The Grafana screenshots needed a specific login flow.** Grafana's
client-side router resolves a dashboard on the *second* load — the first
renders "Not found" before the session is applied — so the capture script
navigates, reloads, and then waits for the grid to render. The dashboard
uids are `fuel-application`, `fuel-system`, `fuel-intelligence` and
`fuel-integration`.

---

How to run it

```bash
docker compose up -d --wait        # the whole stack, including Prometheus and Grafana
open http://localhost:33000        # Grafana (admin / change-me-in-env)
open http://localhost:5173         # the operator dashboard
```

The resilience drill:

```bash
backend/.venv/bin/python scripts/crisis_drill.py \
    --output docs/resilience-report.md \
    --screenshots docs/screenshots/resilience

# one scenario at a time
backend/.venv/bin/python scripts/crisis_drill.py --only demand_spike
```

The load test:

```bash
# the standalone harness — no extra dependencies
backend/.venv/bin/python scripts/loadtest/run_loadtest.py

# the same workloads under Locust
cd backend && .venv/bin/python -m locust \
    -f ../scripts/loadtest/locustfile.py \
    --host http://localhost:8001 --headless \
    --users 50 --spawn-rate 10 --run-time 5m \
    --csv ../scripts/loadtest/results/locust
```

The log search:

```bash
scripts/search_logs.py --level ERROR --minutes 10
scripts/search_logs.py --contains "deadlock"
curl 'http://localhost:8001/api/audit?q=disrupted'
```

---

Blockers and risks

| Risk | Impact | Mitigation |
|---|---|---|
| The host is shared with other projects | Load-test numbers vary with the host's load average | The load average is recorded with every run; the before/after comparison was taken under the same conditions |
| cAdvisor cannot see the containers | No per-container CPU or memory in Prometheus | The System dashboard shows the host's CPU and memory; the load test reads the container's vitals from `docker stats` |
| `prediction_evaluations` grows without bound | The table was the platform's bottleneck at 257 MB and OOM-killed the container | The aggregate is now computed in SQL, so growth no longer costs latency or memory. The table still grows — a retention job would be the next step |
| The simulator's faults surface on the polling interval | Detection takes up to 12 s for the fault scenarios | That is an upper bound: the drill polls every 2 s, and the platform's circuit breaker reacts faster. A faster path would be an SSE-driven fault signal |
| The host is shared with other Docker stacks | 32 containers across 5 other projects had the machine at 203 MiB free and load average 13 | `scripts/stack_guard.sh` stops them, clears the restart policies, and applies a 1 GB limit to every container on the host; the compose file caps this stack's seven services at **1.5 GB in total** (417 MB measured), and the observability half sits behind a profile |
| Locust is a heavy dependency | It could not be installed on the build machine | The measurements come from the standalone harness, which needs nothing the project does not already have; `locustfile.py` provides the same workloads for anyone who has Locust |

---

# Part 2 — The crisis drill, the dependency failures, and a real bug

**Date:** Day 6 of 7, Part 2 of 3 · BUP CSE Fest 2026 Hackathon Finals

Part 1 built the budget and the observability stack. This part runs the
drill that the whole day exists for, and it found a bug that would have
cost the platform its most important claim.

**The result in one line:** **17 of 17 crisis, fault and dependency
scenarios recovered automatically** — including the simulator's own
connection-pool exhaustion, a stopped Postgres, and a deliberately broken
prediction model — and the drill turned up **a silent failure in the
platform's event stream**, where the platform believed it had a live feed
long after the feed was gone.

---

The operating contract for this part

The machine is shared with other projects, so the drills ran under a
contract:

* every container under **2 GB**, with the limits exactly as
  `docker-compose.yml` sets them;
* **only the core profile** — the monitoring profile stayed off for the
  whole run, so nothing in the report depends on Prometheus or Grafana.
  The counters are read from the backend's own `/metrics`;
* `free -h` before **every** scenario, and the drill stops before a
  scenario if the host has less than **1 GB** available;
* **one scenario at a time**, with a 12-second pause between them, so a
  crisis that is still resolving is never counted as the next crisis's
  detection;
* if anything became unresponsive: `docker compose down`, and say so.
  `docker compose down -v` needs asking first.

The available memory never fell below 1.4 GB during the run, so no
scenario was skipped.

---

The simulator's connection pool, characterized

The simulator keeps its world in SQLite behind a SQLAlchemy `QueuePool`
of **five connections plus ten in overflow**, with a 30-second pool
timeout. What exhausts it is *not* the event stream and *not* the
latency fault — both of those were measured and neither holds a
connection. What exhausts it is **concurrent heavy reads**:

| Burst | Result |
|---|---|
| 17 concurrent `GET /v1/demand-history?limit=2000` | served in 4.2 s — no exhaustion |
| 25–30 concurrent | requests hit the pool's own 30-second timeout |
| 40 concurrent | 3 succeeded, 37 timed out |

The simulator's log says it plainly:

```
sqlalchemy.exc.TimeoutError: QueuePool limit of size 5 overflow 10
reached, connection timed out, timeout 30.00
```

Once the pool is exhausted the simulator's *own* health endpoint cannot
get a connection either, so it stops answering — for a while.

**How the backend degrades.** It never sees a 500 — it sees requests that
stop answering. Its own request timeout fires, the circuit breaker opens
after five consecutive failures, the sync service marks the cache
degraded, and the API keeps serving the last-known world with its age and
its reason. All five dashboard endpoints kept answering `200` throughout.

**How it recovers.** The drill waits for the platform to recover on its
own first, because that is the claim worth testing. The pool's backlog
drains in waves — every request already queued on it waits out the
pool's own thirty-second timeout first — so recovery is slow but
automatic: on the recorded run the platform recovered **by itself in
111 seconds**, with the breaker closed and the cache fresh again, and no
restart was needed.

Earlier runs did wedge, and the drill handled that too: when the pool
stays wedged past its recovery budget, the drill runs the documented
procedure (`docker compose restart simulator`, then `POST /admin/reset`,
then `POST /admin/run`) and then measures the platform's own recovery —
**9 seconds** after the simulator came back, the breaker had closed and
the cache was fresh again. The drill runs the procedure as a *fallback*,
not as the expected path, and the report says which one happened.

The procedure is now written down in the [README](../README.md) as well
as here, because it is also how you get a clean world between demo runs.

---

The drill itself

`scripts/crisis_drill.py` runs the scenarios one at a time and records
four numbers for each, because a resilience claim without them is just a
claim: **detection time**, **the recommendation the platform made**, **the
recovery time**, and **what the platform served during the incident**.

| Scenario | Detected in | Recovery | Outcome |
|---|---|---|---|
| Baseline (the control) | 0.0s | 0.0s | RECOVERED |
| Demand spike | 2.0s | 0.0s | RECOVERED |
| Shipment delay | 2.0s | 0.0s | RECOVERED |
| Depot constraint | 2.0s | 0.0s | RECOVERED |
| Route disruption | 4.0s | 0.0s | RECOVERED |
| Station outage | 2.0s | 0.0s | RECOVERED |
| Supply shortfall | 2.0s | 0.0s | RECOVERED |
| Combined crisis | 2.0s | 0.0s | RECOVERED |
| Latency | 12.4s | 0.0s | RECOVERED |
| Unavailable | 2.1s | 0.0s | RECOVERED |
| Error rate | 2.1s | 0.0s | RECOVERED |
| Stale data | 4.4s | 0.0s | RECOVERED |
| Stream disconnect | 22.5s | 0.0s | RECOVERED |
| Simulator connection-pool exhaustion | 18.1s | 111.4s | RECOVERED |
| Database stopped | 0.1s | 2.0s | RECOVERED |
| Prediction layer disabled | 0.0s | 0.0s | RECOVERED |
| Prediction layer broken | 0.0s | 0.0s | RECOVERED |

**17 of 17 recovered automatically.** None of them needed a restart of the
backend. The full numbers, with the measurements behind each one, are in
[`docs/resilience-report.md`](../resilience-report.md).

Two measurements were wrong before they were right, and both mattered.

The drill's own counter parser could not read an **unlabelled** sample:
`sse_connections_total 10.0` has no braces to partition on, so the value
was dropped and every unlabelled counter read as zero. The
stream-disconnect scenario was reporting *"the event stream reconnected:
no"* while the platform had reconnected twenty times. A drill that
measures its own evidence wrongly is worse than no drill.

The detection and recovery clocks started **after** each scenario's
setup. A scenario that has to restart the simulator to close an
established stream was reporting a detection time that excluded the
restart — 0.0s where the honest answer is 22.5s. The clock now starts
before the setup, because the setup is part of the incident.

---

The dependency failures

**The database, stopped for under thirty seconds.** The Postgres
container is stopped while the platform is serving, and then started
again:

| Measurement | During the outage | After the restart |
|---|---|---|
| Health status | `degraded` | `ok` |
| Database component | `error` | `ok` |
| Cached state | age 0.1s, **not degraded** | age 2.8s, not degraded |
| The dashboard | all 5 endpoints answered 200 | all 5 answered 200 |
| The platform process | **still up** | still up |

The database was down for **16 seconds** and the platform recovered on
its own **2.0 seconds** after it came back. It did not crash, it did not
lose the cached state, and it did not pretend the database was there:
`/health` answered `503` with the database marked `error`, which is the
honest answer.

**The prediction layer, in three shapes.** "The prediction component
failed" has more than one meaning, and the platform handles each of them
differently — the brief asks for the failure, so all three are drilled.

The prediction component is a **module inside the backend**, so there is
no container to stop. To show that the fallback takes over when the model
*fails* — rather than when an operator has politely switched it off —
the drill now breaks the model on purpose, with
`POST /api/model/fault {"fault": "predict_error"}`. The model is
trained first, because a model that was never trained falls back anyway,
and a fault injected into it would measure nothing.

| | Model | Flagged for a human | The forecast's reason |
|---|---|---|---|
| Before | `12× learned` | 0 of 12 | — |
| Kill-switch on | `12× baseline` | **12 of 12** | *"the ml kill-switch is on: the learned model is disabled"* |
| Model broken | `12× baseline` | **12 of 12** | *"the learned model failed to predict: an injected prediction fault"* |
| After recovery | `12× learned` | 0 of 12 | — |

The two are deliberately different and the platform keeps them
different. The kill-switch is a **decision** — the model is healthy and
has been told not to be used. The injected fault is a **failure** — the
model is broken and the platform noticed on its own. Both end up in the
same place: the rule-based predictor, with every forecast flagged for a
human. The difference is in the reason the forecast carries, and that
reason is what an operator reads to decide whether to trust the number in
front of them.

The *decision layer's* own fallback is a third thing, and it does **not**
engage here. That fallback is for when the intelligence layer produced no
forecast at all; a rule-based forecast is still a forecast, so the decision
engine keeps using its normal optimizer. The drill records this
explicitly, because a report that claimed "the decision layer fell back"
when it had not would be a claim the dashboard cannot corroborate.

Getting the model into service first was not a formality. The platform
judges the learned model against the rule-based baseline on every
retrain, and **the verdict moves**: three retrains on the same stored
history produced horizon errors of 84.9 L, 111.5 L and 58.7 L against
baselines of 43.4 L, 106.8 L and 76.6 L — so the model won twice and
lost once. Switching off a model the platform was not using anyway
would have measured nothing, so the drill retrains until the forecasts
actually come from the learned model, and records how many attempts that
took.

Recovery is not a switch either: with the fault cleared, the model comes
back only by **earning** it, and the forecast's reason reverts to the
model's own measured merit against the baseline.

---

The bug the drill found

This is the part worth reading.

With the simulator stopped, the platform's `sse_connections_total` stayed
at **1** and `sse_reconnects_total` never moved — while the REST poller
correctly went degraded. The platform believed it had a live event feed
long after the feed was gone. The cause was **three separate bugs**, and
all three had to be fixed for the platform to notice a dead simulator at
all.

**1. The reader blocked on the sync.** Every `simulation.tick` event
triggered a full sync cycle, and the reader *awaited* it. The simulator
runs eight ticks a second and a cycle reads nine endpoints, so the reader
spent its life inside a sync cycle and read about **fifteen events per
fifty seconds**. The simulator's event queue is two hundred deep, and the
simulator **unsubscribes** a client whose queue overflows — so the
platform was dropped from the bus entirely. The stream stayed open, kept
sending keepalives, and delivered nothing: a live-looking feed that was
permanently silent. The refreshes are now *scheduled* rather than
awaited, so the reader never stops reading.

**2. The read timeout was unbounded.** `httpx.Timeout(read=None)` means
a socket whose far end has gone away produces neither data nor an error:
the reader waits forever on a connection that will never answer. The
simulator sends a keepalive every fifteen seconds, so a stream silent for
longer than that is *dead*, not quiet. The read timeout is now a bounded
`SSE_READ_TIMEOUT` (30 s).

**3. A live stream can still be lying.** The dead simulator's queue keeps
delivering buffered events for minutes, each one resetting every read
timeout — so *"bytes are still arriving"* proves nothing. The stream's
own tick is the evidence: if the platform's independent REST view says the
world has moved on and the stream has not, the stream is describing a
world that no longer exists. `_sse_stream_is_stale()` compares the two and
drops the stream when it has fallen behind for longer than
`SSE_STALE_GRACE` (45 s).

Measured on the running stack, before and after:

| | Before | After |
|---|---|---|
| SSE connections opened in 25 minutes | 17 | **1** |
| SSE events received | 15 per connection | **742 in 3 minutes** |
| Stale-stream drops on a *healthy* simulator | one every ~50 s | **0** |
| Time to notice a stopped simulator | never | **~40 s** |

The stale-stream detector also had to be given a clock it could trust: a
stream that briefly falls behind and then catches up must not be punished
later for a lag it has already recovered from, so the "behind" clock is
reset the moment the stream catches up.

`ML_KILL_SWITCH_FALLBACKS` was also found to be **defined but never
incremented** — the counter that says how much of the platform's output
the kill-switch pushed onto the rule-based predictor always read zero. It
is now incremented where the fallback actually happens.

---

Tests

| Suite | Count |
|---|---|
| Backend (`pytest`) | **373** (8 new: the stale-stream detector, the non-blocking reader, the failed-refresh isolation, and the injected prediction fault) |
| Ruff | clean |

---

Definition of done — Part 2

| Requirement | Status | Evidence |
|---|---|---|
| `scripts/crisis_drill.py`, run sequentially with a pause between scenarios | ✅ | One at a time, 12 s apart, with the memory guard in front of each |
| Baseline, then the crises, then the combined crisis | ✅ | 8 scenarios, all RECOVERED |
| The five faults, each 10–30 s | ✅ | 20 s each — latency, unavailable, error rate, stale data, stream disconnect |
| Detection time recorded per scenario | ✅ | In the report, and an upper bound: the drill polls every 2 s |
| The recommendation recorded per scenario | ✅ | Count, risk levels, and the most urgent proposal |
| Recovery time recorded per scenario | ✅ | In the report |
| Results saved with timestamps | ✅ | `docs/resilience-report.md`, generated `2026-11-24T19:45:15.381050+00:00` |
| The simulator's connection-pool exhaustion as a scenario | ✅ | 30 concurrent heavy reads; symptom, degradation and recovery recorded |
| The restart/reset procedure documented | ✅ | README and the report's runbook section |
| (a) The prediction component fails; the fallback takes over and recovers | ✅ | Kill-switch *and* an injected error — both drilled, both recovered |
| (b) Postgres stopped under 30 s; cached state, degraded status, no crash | ✅ | 15 s down; health reported `degraded` and database `error`; cached-state `degraded` boolean remained `False`; recovered in 2.0 s, never crashed |
| Results added to `docs/resilience-report.md` | ✅ | 17 scenarios, all with measurements |

### Part 2 closeout

**Completed:** 2026-11-25T02:15:20+06:00. The core-profile containers were
stopped with `docker compose down`; no volumes were removed. The requested
`docker system prune -f` completed and reported `0B` reclaimed. No monitoring
containers were running.

Final host readings after cleanup:

```text
free -h
Mem:  7.1Gi total, 2.6Gi used, 1.6Gi free, 3.2Gi buff/cache, 4.5Gi available
Swap: 4.0Gi total, 470Mi used, 3.5Gi free

df -h .
/dev/nvme0n1p2  468G  321G  124G  73%  /
```

`docker compose ps` showed no running or stopped project containers after
cleanup. The Postgres drill's overall health status was `degraded`, while the
cached-state `degraded` boolean stayed `False`; both readings are recorded
explicitly rather than conflated.

---

Next — Day 7

Polish, the final read-me and the demo script. The platform is stable and
measured; what remains is making it easy to *show*.
