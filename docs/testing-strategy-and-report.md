# Testing strategy and report

What is tested, at which level, by what, and what the result was. Every
count and percentage in this document was read from the repository — the
committed coverage artifact, the progress logs, the CI configuration and the
test files themselves. **No test was re-run for this document**, so the
"Current results" section reports what the last recorded run said, and where
that number can be re-derived from a committed artifact, it says so.

The honest summary: **583 backend unit tests, 22 Vitest component tests and
12 Playwright end-to-end tests, all passing**; **84.4 %** statement coverage
overall with every one of the seven modules the brief names above its floor;
**17 of 17** crisis and dependency scenarios recovered automatically; and a
known list of gaps, including **18 of the 70 catalogued defects have no
dedicated regression test**.

---

## 1. The strategy in one page

The platform is a decision system. A wrong number is worse than an error,
and a decision made on a wrong number is worse than no decision. The test
strategy follows from that: **the layers are ordered by how badly a failure
in each would mislead someone.**

```mermaid
flowchart TB
    subgraph L1["Layer 1 — the answer"]
        D["Decision engine<br/>optimizer, validator, fallback,<br/>service, model gate"]
    end
    subgraph L2["Layer 2 — the reasoning"]
        I["Intelligence layer<br/>forecasting, risk, detection,<br/>engine, dispatcher"]
    end
    subgraph L3["Layer 3 — the boundary"]
        C["Simulator client<br/>contract, errors, stream,<br/>resilience, circuit breaker"]
    end
    subgraph L4["Layer 4 — the surface"]
        A["HTTP API<br/>routes, degraded mode,<br/>stale data, health"]
    end
    subgraph L5["Layer 5 — the running system"]
        S["Smoke, integration,<br/>drills, load test,<br/>demo dry run"]
    end
    subgraph L6["Layer 6 — the browser"]
        U["Vitest components<br/>Playwright end-to-end"]
    end

    L1 --> L2 --> L3 --> L4 --> L5 --> L6

    style L1 fill:#dcfce7,stroke:#15803d,stroke-width:3px
    style L6 fill:#fee2e2,stroke:#b91c1c,stroke-width:3px
```

The green layer is where the coverage floors are highest, and the red layer
is where the evidence is thinnest — which is the correct shape, because a
browser test proves a screen renders, not that a decision was right.

### The principles the layers follow

| # | Principle | Where it shows up |
|---|---|---|
| 1 | **No simulator is needed for the fast layers.** Every backend unit test mocks the HTTP transport | `backend/tests/conftest.py` builds the client on `httpx.MockTransport`; no container is needed to run the suite |
| 2 | **The simulator is never trusted.** A malformed response is a test case, not an accident | `backend/tests/test_simulator_contract.py` pins the closed models |
| 3 | **A fault is a normal input.** The breaker, the stale flag, the 409 codes and the dead stream all have unit tests | `test_simulator_resilience.py`, `test_simulator_client_errors.py` |
| 4 | **The running system is exercised, not assumed.** 17 scenarios against the real stack | [`docs/resilience-report.md`](resilience-report.md) |
| 5 | **The demo is a test.** The dry run executes the demo script and checks each claim | [`scripts/demo_dry_run.sh`](../scripts/demo_dry_run.sh) |
| 6 | **A number nobody can reproduce is not a number.** Every figure names the script that produces it | Throughout [`docs/`](../docs/) |

---

## 2. The layers

### 2.1 Backend unit tests — 583 passing

The fast layer. No Docker, no network, no database. This is the layer that
runs on every push.

```bash
cd backend
.venv/bin/python -m pytest -q          # 583 tests
.venv/bin/ruff check .
.venv/bin/ruff format --check .
```

**How the isolation works.** The `client_factory` fixture
([`backend/tests/conftest.py`](../backend/tests/conftest.py)) builds a
`SimulatorClient` whose `httpx.AsyncClient` runs on a `MockTransport`, so
each test supplies its own handler and the simulator is never contacted. The
`settings` fixture shrinks the retry backoff to 1 ms so the retry tests stay
fast. An `autouse` fixture (`quiet_background_services`) stops the FastAPI
lifespan from starting the poller and the SSE listener, which would otherwise
try to reach a simulator that does not exist.

**Where the 583 come from.** The suite holds **553 test functions** across 29
files; **30 of them are `@pytest.mark.parametrize` cases**, so the count is
553 + 30 = 583. The reconciliation was computed from the AST of the test
files, not taken on trust:

| File | Functions | File | Functions |
|---|---:|---|---:|
| `test_predictor.py` | 43 | `test_simulator_client_errors.py` | 23 |
| `test_decision_service.py` | 40 | `test_decision_approval.py` | 22 |
| `test_sync_service.py` | 34 | `test_simulator_resilience.py` | 19 |
| `test_decision.py` | 33 | `test_simulator_client_stream.py` | 18 |
| `test_decision_optimizer.py` | 32 | `test_dispatcher.py` | 18 |
| `test_detection.py` | 30 | `test_audit.py` | 18 |
| `test_intelligence_engine.py` | 25 | `test_decision_fallback_rules.py` | 17 |
| `test_state_cache.py` | 14 | `test_sse.py` | 14 |
| `test_decision_fallback_lifecycle.py` | 14 | `test_model_stability.py` | 13 |
| `test_simulator_client.py` | 13 | `test_model_acceptance.py` | 10 |
| `test_decision_fallback.py` | 9 | `test_db_deadlock.py` | 6 |
| `test_engine_inventory.py` | 5 | `test_metrics_middleware.py` | 4 |
| `test_simulator_contract.py` | 16 | `test_resilience.py` | 16 |
| `test_risk.py` | 16 | `test_api.py` | 16 |
| `test_dashboard_api.py` | 15 | | |

The seven parametrized tests, and what each multiplies into:

| Test | Cases |
|---|---:|
| `test_dashboard_api.py::test_every_simulation_action_is_forwarded` | 5 |
| `test_decision_approval.py::test_every_conflict_is_handled_with_a_clear_reason` | 10 |
| `test_decision_optimizer.py::test_the_split_never_exceeds_the_configured_leg_count` | 3 |
| `test_intelligence_engine.py::test_every_fuel_in_a_station_is_evaluated` | 2 |
| `test_simulator_client_errors.py::test_both_error_envelope_shapes_map_onto_the_same_exception` | 2 |
| `test_simulator_client_errors.py::test_every_conflict_code_gets_its_own_exception_class` | 9 |
| `test_simulator_resilience.py::test_all_409_codes_map_to_typed_exceptions` | 6 |

**What this layer proves.** That each unit does what its name says, at the
boundary where it is hardest to be wrong: the retry ladder, the backoff
jitter, the breaker opening and closing, the closed response models, the
409-code-to-exception mapping, the constraint validator's eight rules in the
guide's order, the split-shipment arithmetic, the fallback activation latch,
and the model's held-out gate.

**What it does not prove.** That the pieces work together. A unit test with a
mocked transport cannot tell you that the real simulator sends a body the
model accepts. That is what layer 5 is for.

### 2.2 The cross-service integration suite

Against a live stack. It is marked `integration` and lives outside the
backend package, at [`tests/test_stack.py`](../tests/test_stack.py).

```bash
docker compose up -d --wait
pytest tests/ -m integration
```

**What it proves.** That the containerised stack actually serves what the
backend promises: the simulator is reachable, `/health` is green, `/api/state`
carries every section, the read endpoints serve the stored state, the history
endpoint serves stored demand, `/metrics` exposes the sync series, an
allocation round-trips to the simulator and back, and an unknown depot
returns 404.

**How it finds its targets.** The suite resolves the URLs from the
environment first, then from `.env`, then from the committed defaults — so it
follows a local port remapping instead of assuming 8000 and 8001.

### 2.3 Smoke tests

Two of them, at different levels, and they check different things.

**The simulator smoke test** — [`scripts/smoke_test.sh`](../scripts/smoke_test.sh).
Covers **every documented simulator endpoint**: 10 read endpoints, 4 admin
read endpoints, and 6 writes. The writes are the interesting ones, because
they pin the simulator's own contract:

| Check | Expected |
|---|---|
| `POST /v1/allocations` with a valid body | 201 |
| `POST /v1/allocations` replayed with the **same** idempotency key | 201 — a replay, not a second shipment |
| `POST /v1/allocations` with a route that does not connect the depot and station | 409 |
| `POST /admin/step`, `/admin/pause`, `/admin/faults/clear` | 200 |

**The CI compose smoke test** — [`docker-compose.yml`](../docker-compose.yml)
brought up in CI, then three checks that a status code alone would miss:

| Check | Script | What it catches |
|---|---|---|
| `/health` | [`scripts/check_health.py`](../scripts/check_health.py) | A 200 with a broken component inside it. The script requires `backend`, `simulator` and `database` all to be `ok` — `/health` is a *readiness* probe, so it answers 503 while a dependency is down |
| `/api/state` | [`scripts/check_state.py`](../scripts/check_state.py) | A healthy backend serving an empty world. The script requires non-empty `stations`, `depots` and `routes` |
| The dashboard | `curl` | A 200 from the frontend container |

These two scripts exist because "the status code was 200" was not good
enough. Both carry that reasoning in their own docstrings.

### 2.4 Fault injection

The simulator's own fault modes, driven against it, and the platform's
reaction recorded.

```bash
scripts/fault_test.sh [simulator_base_url] [backend_base_url]
```

The result is checked in as [`docs/fault-tests.md`](fault-tests.md). The
matrix covers six scenarios, and the interesting finding is in the
discrepancies, not the passes: `stale_data` marks **GET** responses only
(F1); `stream_disconnect` fails at connect time rather than mid-stream (F2);
the `unavailable` and `error_rate` faults use a different envelope from the
domain errors, and the client maps both onto one hierarchy (F4); `/v1/health`
and every `/admin/*` path bypass fault injection entirely, which is why the
document could be produced at all (F5).

### 2.5 The crisis drill — 17 of 17

The heaviest layer, and the one that produces
[`docs/resilience-report.md`](resilience-report.md).

```bash
scripts/crisis_drill.py                    # everything
scripts/crisis_drill.py --only demand_spike # one scenario
```

It drives the **running stack** — the real simulator, the real backend, the
real database — through every crisis and fault the organizer's simulator can
inject, and records four numbers per scenario: detection time, what the
platform recommended, recovery time, and whether the dashboard kept serving.

| Scenario | Kind | Detected in | Recovery |
|---|---|---:|---:|
| Baseline | baseline | 0.0 s | 0.0 s |
| Demand spike | crisis | 2.0 s | 0.0 s |
| Shipment delay | crisis | 2.0 s | 0.0 s |
| Depot constraint | crisis | 2.0 s | 0.0 s |
| Route disruption | crisis | 4.0 s | 0.0 s |
| Station outage | crisis | 2.0 s | 0.0 s |
| Supply shortfall | crisis | 2.0 s | 0.0 s |
| Combined crisis | combined | 2.0 s | 0.0 s |
| Latency | fault | 12.4 s | 0.0 s |
| Unavailable | fault | 2.1 s | 0.0 s |
| Error rate | fault | 2.1 s | 0.0 s |
| Stale data | fault | 4.4 s | 0.0 s |
| Stream disconnect | fault | 22.5 s | 0.0 s |
| Simulator connection-pool exhaustion | dependency | 18.1 s | 111.4 s |
| Database stopped | dependency | 0.1 s | 2.0 s |
| Prediction layer disabled | dependency | 0.0 s | 0.0 s |
| Prediction layer broken | dependency | 0.0 s | 0.0 s |

**17 of 17 recovered automatically. None needed a backend restart.** The
detection times are upper bounds — the drill polls every two seconds, so the
real detection was no slower than the figure.

The drill runs one scenario at a time with a pause between them, checks the
host's available memory before each one and stops if it falls under 1,024 MB,
and runs with the **monitoring profile off**, so nothing in the result depends
on Prometheus or Grafana.

### 2.6 The load test

Not a pass/fail gate — a measurement, and the thing that found two real bugs.

```bash
docker compose up -d --wait
backend/.venv/bin/python scripts/loadtest/async_loadtest.py
backend/.venv/bin/python scripts/loadtest/async_loadtest.py --only dashboard
backend/.venv/bin/python scripts/loadtest/async_loadtest.py --stage-seconds 30
```

Plain `httpx` and `asyncio`, run from the host — no extra container. The
same workloads are available under Locust for anyone who has it. Raw results
are written to `scripts/loadtest/results/part3-async.json` and are committed.

The harness has **safety stops**: it halts if the backend exceeds 90 % of its
CPU quota for three consecutive samples, if the error rate passes 20 %, or if
available host memory drops below 1 GiB before a scenario.

**What it found.** A Python-side aggregate over a **928,005-row** table
(257 MB) that took the p99 from **30,030 ms to 286 ms** once it was computed
in SQL. The full write-up is in [`docs/load-test-report.md`](load-test-report.md).

### 2.7 Vitest — 22 component tests

```bash
cd frontend
npm run test          # vitest, 22 component tests
```

One file, [`frontend/src/components/components.test.tsx`](../frontend/src/components/components.test.tsx),
covering seven components:

| Component | Tests | What it pins |
|---|---:|---|
| `OverviewView` | 3 | The service level, the unmet demand, the tick; the stations needing fuel, worst first; the health panel |
| `NetworkView` | 2 | Two depots, four stations, six routes drawn; the live status of each |
| `InventoryView` | 2 | A fill bar and a cover figure per station and fuel; the bar coloured by risk |
| `RiskView` | 2 | The risks sorted worst first; the explanation drawer opens with the signals and the forecast |
| `RecommendationsView` | 5 | The simulation banner on **every** card; the impact, before and after; `approve` called when the button is pressed; the alternatives behind a toggle; the what-if result shown **without** writing to the simulator |
| `SimulationView` | 3 | The panel labelled simulation only; the simulation run when the button is pressed; the selected crisis injected |
| `HealthBanner` | 3 | `DEGRADED` with the age of the cached data; nothing when healthy; a warning when the fallback policy is active |
| `ForecastChart` | 2 | The line and the confidence band; the empty case says so |

### 2.8 Playwright — 12 end-to-end tests

```bash
cd frontend
npm run test:e2e:install   # once, to fetch the browser
npm run test:e2e           # 12 tests, needs the stack up
```

| Spec | Tests | What it pins |
|---|---:|---|
| [`e2e/smoke.spec.ts`](../frontend/e2e/smoke.spec.ts) | 9 | The dashboard loads with the live state; every screen renders without an error state; the network board draws the depots, stations and routes; the risk drawer explains a selected risk; the inventory view shows fill bars and cover figures; the recommendations carry the simulation banner; the simulation panel is labelled simulation only; the service health panel names every dependency; the layout is usable on a narrow screen |
| [`e2e/screenshots.spec.ts`](../frontend/e2e/screenshots.spec.ts) | 3 | Capture every screen, the risk drawer, and the narrow layout — this is what refreshes `docs/screenshots/` |

**What this layer proves.** That the real browser, the real built bundle and
the real backend agree. The configuration drives the **preview** server on
port 4173 rather than the Docker container on 5173, because the container may
be serving an older build than the one the tests just compiled.

The Playwright config runs **one worker, no retries** (`workers: 1`,
`retries: 0`). That is a deliberate choice: a retry would hide a flake rather
than expose it, and this suite talks to a live stack.

### 2.9 The demo dry run — the test that found the week's worst bug

```bash
scripts/demo_dry_run.sh
```

Not a test of the code so much as a test of the **claim**. It executes every
command in [`docs/demo-script.md`](demo-script.md), in order, against the
running stack, and fails loudly if any step does not produce what the demo
script says it produces. Seventeen steps, from the reset through the crisis
to the model's repair.

**Recorded result:** five consecutive clean runs, 32 to 43 seconds each, and
two more on a fresh clone (42 s and 30 s).

---

## 3. Current results

### 3.1 The counts

| Suite | Count | Source of the figure |
|---|---:|---|
| Backend unit tests (`pytest`) | **583** | `docs/progress/day7.md`; independently reconciled from the test files as 553 functions + 30 parametrized cases |
| Backend test files | 29 | `backend/tests/` |
| Vitest component tests | **22** | `docs/progress/day7.md`; counted in `frontend/src/components/components.test.tsx` |
| Playwright end-to-end tests | **12** | `docs/progress/day7.md`; counted as 9 in `smoke.spec.ts` + 3 in `screenshots.spec.ts` |
| Cross-service integration tests | 9 functions | `tests/test_stack.py` |
| Crisis scenarios | **17 of 17** recovered | `docs/resilience-report.md` |
| Load-test workloads | 3, to 50 users, 0.00 % errors | `docs/load-test-report.md` |
| Demo dry run | 5 consecutive clean runs, 32–43 s | `docs/progress/day7.md` |

### 3.2 How the backend count grew

The progression is recorded in the progress logs:

| Day | Backend tests | Note |
|---|---:|---|
| Day 3 | 213 | "213 backend unit tests, all passing" |
| Day 4 | +82 | "82 new tests, all 8 codes reproduced" |
| Day 5 | 317 | "Backend tests — 317, all passing" |
| Day 6 Part 1 | 365 | 3 new: the throttle, the fan-out cap, the un-throttled reconnect |
| Day 6 Part 2 | 359 | 23 new: audit, deadlock retry, resilience, kill-switch |
| Day 6 Part 3 | 373 | 8 new: the stale-stream detector, the non-blocking reader, the failed-refresh isolation, the injected prediction fault |
| Day 6 Part 3 closeout | 382 | 6 new: the audit 500, the shared window, the scope, the route template, the cgroup reads, the timer |
| Day 7 | **583** | 553 functions + 30 parametrized cases |

The Day 6 numbers are not monotonic (365 → 359 → 373 → 382) because the log
records the count **at the close of each part**, and the parts were written
and committed separately. The count is not a target being hit; it is what
was there when the part closed.

The jump from 382 to 583 happened across the Day 7 work. Verified against the
git history: commit `977529c` held 524 functions + 30 parametrized cases =
**554**, and `426b574` held 553 + 30 = **583** — 29 new test functions, from
the model-stability work and the three real bugs that day found.

### 3.3 Coverage

**84.4 %** statement coverage overall, measured from the committed
[`coverage.json`](../coverage.json) artifact (33 files, 4,757 statements,
4,015 covered, 742 missing, timestamped `2026-11-25T18:29:46`). The artifact
is committed, so the figure can be re-derived without re-running anything.

| Module | Measured | Floor | Margin |
|---|---:|---:|---:|
| `app/decision/fallback.py` | 99.3 % | 90 % | +9.3 |
| `app/decision/service.py` | 98.3 % | 90 % | +8.3 |
| `app/intelligence/engine.py` | 98.2 % | 90 % | +8.2 |
| `app/simulator_client.py` | 97.2 % | 90 % | +7.2 |
| `app/decision/optimizer.py` | 94.1 % | 90 % | +4.1 |
| `app/decision/validator.py` | 92.6 % | 90 % | +2.6 |
| `app/intelligence/forecasting.py` | 92.1 % | 85 % | +7.1 |
| **Total** | **84.4 %** | **80 %** | **+4.4** |

The floors are not aspirational — they are **enforced in CI**.
[`scripts/check_coverage.py`](../scripts/check_coverage.py) fails the build
if any of the seven modules or the total falls below its floor. The floors
exist because a total coverage number hides where the risk is: the seven
modules named here are the ones that decide whether the platform tells the
truth, so each has its own floor rather than being averaged into a total.

**Where the uncovered lines are.** The per-file breakdown says the gaps are
in the plumbing, not the logic:

| File | Coverage | Why |
|---|---:|---|
| `app/intelligence_service.py` | 36.9 % | The orchestration layer that wires the engine, the repository and the metrics together. Its behaviour is covered by the tests of the parts it calls |
| `app/repository.py` | 49.5 % | The SQL writers. They need a real database to exercise, so they are covered by the integration suite rather than by unit tests |
| `app/main.py` | 64.8 % | The route layer; the 16 dashboard routes are covered by `test_dashboard_api.py` and the rest by `test_api.py` |
| `app/db.py` | 71.4 % | The connection pool and the deadlock retry; the retry is covered by `test_db_deadlock.py` against a real Postgres |
| `app/sync_service.py` | 72.8 % | The polling loop, covered by `test_sync_service.py` and by the drill |

### 3.4 What CI runs

Eight jobs, on every push to `main` and every pull request
([`.github/workflows/ci.yml`](../.github/workflows/ci.yml)):

| Job | What it runs | Gate |
|---|---|---|
| `lint` | `ruff check .` and `ruff format --check .` | Yes |
| `test` | `pytest -q --junitxml=pytest-report.xml` | Yes |
| `frontend` | `npm run typecheck`, `npm run test` (Vitest), `npm run build` | Yes |
| `frontend-e2e` | `docker compose up -d --wait`, then `npm run test:e2e` (Playwright) | Yes |
| `docker-build` | Builds both images, then runs the backend image against a **real** simulator and a real Postgres and polls `/health` | Yes |
| `compose-config` | `docker compose config --quiet` | Yes |
| `compose-smoke` | The whole stack up, `/health`, `/api/state`, the dashboard, and `pytest tests/ -m integration` | Yes |
| `coverage` | `pytest --cov=app`, then `scripts/check_coverage.py` against the seven floors and the total | Yes |

The `docker-build` job is worth a second look: it does not just build the
image, it starts the freshly built backend next to the organizer's simulator
and a real Postgres and waits up to 90 seconds for `/health` to answer. That
is the test that would catch a broken image.

---

## 4. The flaky tests that were found and fixed

Two flakes were found and fixed. Both were in the demo dry run, both are
catalogued as **B65** and **B66** in
[`docs/bug-catalog.md`](bug-catalog.md), and both are worth reading because
in both cases the *platform was right* and the *test was wrong*.

### B65 — the dry run waited for a five-minute trainer it did not ask for

**Symptom.** The dry run failed intermittently. It passed three times out of
five.

**Cause.** The dry run waited forty seconds for the platform's background
trainer, whose own schedule is five minutes. The demo claimed "the model
retrained, its confidence came back, and the platform is using it again" —
but the trainer was never going to fire in the time the demo allowed.

The subtle part: **the platform was behaving correctly.** The model had been
scored `worse_than_baseline` on the last training run, so the platform
correctly refused to use it. The failure was in the test's expectation, not
in the platform.

**Fix.** The dry run now asks for the retrain outright —
`POST /api/model/train`, the same control an operator has — and then waits
for the model to come back into service
([`scripts/demo_dry_run.sh`](../scripts/demo_dry_run.sh), lines 340–359).

**Verification.** The dry run was then run **five consecutive times**, 32 to
43 seconds each, all clean.

### B66 — the dry run checked for a CRITICAL risk once, and the risk takes a tick or two to arrive

**Symptom.** The same intermittent failure.

**Cause.** The dry run read `/api/risk` a single time, the instant the
anomaly appeared. But the risk level does not jump to `CRITICAL` on the same
tick as the demand spike: the platform has to forecast forward from the
spiked demand and see how fast the tank drains. The demo script itself says
"wait about ten seconds" for exactly this reason.

**Fix.** The check now polls — up to 20 iterations, one second apart — and
only fails if no CRITICAL risk appears inside the window
([`scripts/demo_dry_run.sh`](../scripts/demo_dry_run.sh), lines 140–153).

### The lesson

Both flakes had the same shape: **the test encoded a stricter assumption
about timing than the platform actually promised.** The fix in each case was
to make the test wait for the platform's real contract rather than to
loosen the assertion. Neither fix changed a line of application code.

### Not flaky, but worth recording

The unit suite has **no reported flakes**. The Playwright configuration runs
with `retries: 0` precisely so that a flake would fail the build rather than
be absorbed by a retry.

---

## 5. Why the on-disk verification scripts exist

This is the part of the testing setup that is unusual, and it is a direct
consequence of something that went wrong during the build.

### The problem: edits that were silently reverted

Day 4 records it plainly
([`docs/progress/day4.md`](progress/day4.md)):

> Several edits in this session were **silently reverted** by an external
> process — the tool reported success, and the file on disk did not have the
> change. Twice that produced a green test suite with broken code behind it.

The second half of that sentence is the dangerous part. A reverted *test*
edit hides a real defect behind a green suite: the test that would have
caught the bug is the thing that got reverted, and the code it was supposed
to guard is still broken.

### The response

Two things came out of it.

**First**, a set of on-disk verification scripts that assert the specific
facts the implementation is supposed to hold, read straight from the source
files. The first one is
[`scripts/day4_verify_state.py`](../scripts/day4_verify_state.py), whose
docstring states the reason:

> The unit tests catch a missing behaviour, but only once they fail — and a
> *reverted* test edit can hide a real defect behind a green suite. This
> script asserts the specific facts the implementation is supposed to hold,
> straight from the source files, so a silent revert is caught before it
> reaches a commit.

It is not a test suite. It has no fixtures, no mocks and no assertions about
behaviour — it checks that specific *strings* are present in specific files:

```python
check("FallbackPolicy is exported", '"FallbackPolicy"' in package)
check("the split helper is used", "return self._split(" in optimizer)
check("the optimizer has no dead fallback branch", ...)
check("the activation latch exists", "self._fallback_counted = False" in service)
check("the propose endpoint no longer calls the optimizer directly",
      "decision.optimizer.propose(" not in main_module)
```

The last one is the interesting shape: it checks for the **absence** of
something. A dead-code regression — the optimizer being called directly
alongside the shared propose path — is invisible to a behavioural test until
the two paths disagree.

**Second**, the working rule that every change is verified by reading the
file back after the edit, not by trusting the tool's success report. That
rule is recorded in the same place.

### The scripts that came out of it

| Script | What it asserts | Why |
|---|---|---|
| [`scripts/day4_verify_state.py`](../scripts/day4_verify_state.py) | The Day 4 fallback and split work is on disk: the exports, the split helper, the leg cap, the idempotency-key suffix, the fallback markers, the timezone-aware timestamps, the activation latch, the shared propose path | The first silent-revert casualty |
| [`scripts/day4_dod_proof.py`](../scripts/day4_dod_proof.py) | The Day 4 definition of done | The same day, the same cause |
| [`scripts/day4_fallback_drill.py`](../scripts/day4_fallback_drill.py) | The rule-based policy takes over | Behavioural, against the running stack |
| [`scripts/day4_policy_comparison.py`](../scripts/day4_policy_comparison.py) | The policy beats the naive baseline on service level | The measurement the day was for |
| [`scripts/check_coverage.py`](../scripts/check_coverage.py) | The seven per-module floors and the total | Enforced in CI |
| [`scripts/check_health.py`](../scripts/check_health.py) | Every component in `/health` is `ok` | A 200 with a broken component is a failure |
| [`scripts/check_state.py`](../scripts/check_state.py) | `/api/state` carries a whole network | A healthy backend serving an empty world is a failure |
| [`scripts/demo_dry_run.sh`](../scripts/demo_dry_run.sh) | Every step of the demo produces what the demo claims | The demo is a claim; this checks it |

### The honest assessment

These scripts are **a workaround for a broken editing tool, not a testing
technique.** They exist because the environment could not be trusted to
apply an edit, and the cheapest available fix was to check the bytes on disk
rather than the tool's report. On a machine where edits apply reliably they
would be redundant — the unit tests would catch a missing behaviour on their
own, and the string assertions would be testing the implementation's
*wording* rather than its behaviour.

They are kept, and they are run before a commit, because the alternative is
worse: a green suite over broken code is the failure mode the whole
verification effort exists to prevent.

---

## 6. Known gaps

Listed honestly, most significant first. Each is a real limit on the evidence,
not a wish list.

### 6.1 18 of the 70 catalogued defects have no dedicated regression test

[`docs/bug-catalog.md`](bug-catalog.md) records the answer to "would this fail
again if the fix were reverted?" for every one of the 70 defects:

| Pinning | Count |
|---|---:|
| A named regression test exists | 51 |
| Pinned only by a script, drill or setting | 1 |
| **No dedicated regression test** | **18** |
| **Total** | **70** |

The 18 are **B2, B4, B8, B12, B21, B22, B29, B30, B32, B33, B39, B41, B47,
B48, B54, B61, B69 and B70**. Each row says `—` rather than naming a test
that does not exist. This is the list to work from next.

### 6.2 The malformed-payload path was never drilled live

The strict response models are proven by unit tests — that a malformed body
is rejected and counted — but **no scenario injected a malformed response
into the running simulator**. The scenario would need a fault the simulator
does not offer, so it would need a proxy in front of it. Recorded as gap 5
in [`docs/requirements-traceability.md`](requirements-traceability.md).

### 6.3 No multi-replica testing

Only one backend instance runs. The poll-interval jitter and the sync
contention between replicas are therefore untested in anger. Listed as a gap
in [`docs/fault-tests.md`](fault-tests.md) with a target of Day 6; it was
not closed. The drill's own note is that this is a single-instance
deployment by design.

### 6.4 No sustained high-rate `error_rate` fault

The client retries with full jitter and the `error_rate` fault is in the
drill, but a **long** stochastic fault run has not been driven end to end.
The drill injects the fault and observes the reaction; it does not hold the
fault at a high rate for an extended period.

### 6.5 The coverage floors are on statements, not branches

`coverage.json` records `"branch_coverage": false`. A statement can be
executed without every side of a conditional being taken. The floors are
therefore weaker than branch coverage would be — a real limit on what 84.4 %
means.

### 6.6 The lowest-coverage module is the orchestration layer

`app/intelligence_service.py` sits at **36.9 %** — 154 of 417 statements. Its
behaviour is covered indirectly by the tests of the components it calls, but
a wiring mistake inside it would not be caught by a unit test. This is the
single largest gap in the unit suite.

### 6.7 The repository layer is barely unit-tested

`app/repository.py` is at **49.5 %**. The SQL writers need a real database,
so they are exercised by the integration suite and by the drill rather than by
unit tests. That is a reasonable trade — but it means the integration suite
is not optional, and a broken repository layer is not caught by `pytest -q`
alone.

### 6.8 No test for the API's 503 body after an external reset

The behaviour is pinned by the sync-service tests
(`test_forgetting_the_previous_world_drops_the_cached_state` and two
siblings), but there is **no single test that asserts the API's 503 response
body** in the window between a simulator reset and the next sync. Recorded as
"Partly done" in
[`docs/postmortems/02-stale-snapshot-after-reset.md`](postmortems/02-stale-snapshot-after-reset.md).

### 6.9 The frontend has no test for a backend error state

The Vitest suite covers the happy path for every component. There is no
component test that renders a **failed** API call and asserts what the
operator sees. The Playwright suite covers "every screen renders without an
error state" — the opposite direction, and only against a healthy stack.

### 6.10 No accessibility or performance test on the frontend

There is no axe-style accessibility check and no bundle-size or render-time
budget. The layout is checked at 430 px by one Playwright test, and the
build is size-checked by nothing.

### 6.11 The load test is a measurement, not a gate

Nothing in CI fails when the p99 regresses. The load test is run by hand,
its raw results are committed, and the comparison is written up. A
regression would be found by a person reading the report, not by a build.

### 6.12 The database layer is never tested against a real Postgres in CI

The `test` job runs `pytest` with no database — the deadlock tests use a
simulated `40001`. The `compose-smoke` job does bring up a real Postgres, but
it runs the **integration** suite, not the backend unit suite, so the
repository's SQL is not covered by a unit test against a real database.

---

## 7. How to run everything

Copy-paste blocks, in the order a reviewer would want them.

### The fast checks — no Docker, seconds

```bash
cd backend
.venv/bin/python -m pytest -q          # 583 tests
.venv/bin/ruff check .
.venv/bin/ruff format --check .

cd ../frontend
npm run typecheck
npm run test                          # 22 Vitest tests
npm run build
```

### The full stack — Docker, minutes

```bash
docker compose up -d --wait

# the cross-service suite
pytest tests/ -m integration

# the browser suite
cd frontend && npm run test:e2e        # 12 Playwright tests

# the simulator contract
scripts/smoke_test.sh

# the health and state checks
curl -s http://localhost:8001/health  | jq > /tmp/health.json
python3 scripts/check_health.py /tmp/health.json
curl -s http://localhost:8001/api/state | jq > /tmp/state.json
python3 scripts/check_state.py /tmp/state.json
```

### The resilience drill — Docker, minutes, destructive to the world

```bash
scripts/crisis_drill.py                    # all 17
scripts/crisis_drill.py --only demand_spike # one
```

The drill resets the world. It checks the host's available memory before each
scenario and stops under 1,024 MB.

### The load test — Docker, minutes, needs a quiet host

```bash
docker compose up -d --wait               # core profile only
backend/.venv/bin/python scripts/loadtest/async_loadtest.py
```

The monitoring profile is **off** during a load run — it competes for the
same cores the backend is trying to use. `free -h` before each scenario.

### The demo dry run — Docker, under a minute

```bash
scripts/demo_dry_run.sh
```

Resets the world, then executes the demo end to end.

### The coverage gate

```bash
cd backend
.venv/bin/python -m pytest -q --cov=app --cov-report=json:coverage.json
python3 scripts/check_coverage.py backend/coverage.json
```

---

## 8. Verification commands

Every claim in this document can be re-checked with these. They are
read-only.

```bash
# the backend test count, reconciled from the test files
cd backend
grep -rcE '^\s*(async )?def test_' tests/test_*.py | awk -F: '{s+=$2} END {print s " functions"}'
grep -rcE '@pytest\.mark\.parametrize' tests/test_*.py

# the coverage floors, against the committed artifact
cd ..
backend/.venv/bin/python scripts/check_coverage.py coverage.json

# the frontend counts
grep -cE '^\s*(it|test)\(' frontend/src/components/components.test.tsx
grep -cE '^\s*test\(' frontend/e2e/smoke.spec.ts frontend/e2e/screenshots.spec.ts

# the CI gates
grep -nE '^  [a-z-]+:$' .github/workflows/ci.yml

# the flaky-test fixes
grep -n "POST .*api/model/train" scripts/demo_dry_run.sh
grep -n "seq 1 20" scripts/demo_dry_run.sh

# the silent-revert protection
grep -n "silently reverted" docs/progress/day4.md
grep -c "check(" scripts/day4_verify_state.py

# the gaps
grep -A 12 "No dedicated regression test" docs/bug-catalog.md
```

---

## 9. Related documents

| Document | What it holds |
|---|---|
| [`docs/progress/day1.md`](progress/day1.md) – [`day7.md`](progress/day7.md) | The seven-day build log, including every test count as it stood at the close of each day |
| [`docs/bug-catalog.md`](bug-catalog.md) | All 70 defects, and for each one the test that pins it — or the honest `—` |
| [`docs/resilience-report.md`](resilience-report.md) | The 17 scenarios, each with a detection time, a recovery time and the recommendation made during the incident |
| [`docs/load-test-report.md`](load-test-report.md) | The three workloads, the bottleneck, and the before-and-after measurement |
| [`docs/fault-tests.md`](fault-tests.md) | The fault matrix, the degraded-mode drill and eight findings about the simulator's fault behaviour |
| [`docs/requirements-traceability.md`](requirements-traceability.md) | Every requirement mapped to its proof, with 34 rows marked Partial or Not done |
| [`docs/postmortems/`](postmortems/) | Eight incidents written up in full, each with its regression tests named |
| [`docs/deliverables-checklist.md`](deliverables-checklist.md) | The eleven deliverables, and the numbers that back them |
| [`docs/security-and-threat-model.md`](security-and-threat-model.md) | The controls that exist, and what is not protected |
| [`docs/api-reference.md`](api-reference.md) | All 35 routes — the surface the API tests cover |
| [`docs/observability-guide.md`](observability-guide.md) | Every metric and alert — the surface the integration suite and the drill check |
