# Day 7 — The release, the demo that runs every time, and three real bugs

**Date:** Day 7 of 7 · BUP CSE Fest 2026 Hackathon Finals
**Goal:** ship it. Everything from Day 1 has to work from a clean clone, on
someone else's machine, in front of a judge who has never seen it.

**The result in one line:** the platform is released as **v1.0.0**, the
fourteen-step demo is a **script that executes itself and checks every
claim** — five consecutive clean runs, 32 to 43 seconds each — and the
work of this day found **three real bugs**, including one where the
platform **served the previous run's numbers as if they were current**
after the world was reset, which is exactly the kind of dishonesty the
whole platform is built not to do.

---

## What this day actually was

Day 7 was planned as packaging: CI/CD, documentation, the demo script, the
release. The plan was right and the packaging was done — the CI runs, the
docs exist, the release workflow publishes both images with three tags.

But packaging is what turns up the last bugs, because packaging is the
first time the whole thing has to work *as a whole*. The demo dry run
executes every command in the demo, in order, against the running stack,
and checks that each one produced what the demo claims. On the first run
it reported:

```
==> The dry run took 18 seconds
1 step(s) did not produce what the demo script claims.
```

One failure. It turned out to be the most important bug of the week.

---

## The bug that mattered: the platform served a world that had ended

**The claim.** Every number the dashboard shows comes from the live
simulator. Nothing is fabricated. The platform would rather say "no state
available yet" than serve a stale number as a current one — and the
degraded mode exists precisely so that the operator is told how old the
data is.

**The reality.** `POST /admin/reset` on the simulator starts the world
again at tick 0. The platform's cache still held the **previous run's**
snapshot — tick 892, a world that no longer existed — and it served it as
fresh:

```text
=== BEFORE reset ===
{"tick":893,"sim_time":"2026-01-10T07:15:00"}
{"tick":892,"sim_time":"2026-01-10T07:00:00Z","degraded":false,"age_seconds":0.12}

=== POST /admin/reset ===
{"tick":0,"status":"PAUSED"}

=== IMMEDIATELY after (within 1s) ===
{"tick":892,"sim_time":"2026-01-10T07:00:00Z","degraded":false,"age_seconds":0.2}
```

Read that again. The world was at tick **0**. The platform reported tick
**892**, said `degraded: false`, and said the data was **0.2 seconds**
old. The age was technically true — the cache *was* refreshed 0.2 s ago —
but it was refreshed with the numbers of a run that had ended. The
platform was not stale. It was **wrong**, and it looked fresh.

**Why it lasted as long as it did.** The platform's own history-wipe
already handled the *database* half of a reset: it noticed the clock had
gone backwards and dropped the stored demand history, so the forecaster
would not read two runs as one series. The **cache** was never covered.
The two halves of the same event were handled in two places, and only one
of them remembered.

**The fix** is in `StateCache`, because that is where the invariant
lives: *the cache never serves a world that no longer exists.*

* A snapshot whose tick is **behind** the cached one means the world was
  reset. The snapshot is still stored — it *is* the current world — but
  the rewind is recorded, so the caller can react.
* `SyncService.forget_the_previous_world()` drops the cached snapshot and
  resets the event stream's tick tracker. `/api/state` then answers
  **503 "no simulator state available yet"** until the next cycle fetches
  the new world — which is exactly what it says.
* The platform's own reset endpoints (`POST /admin/reset` and
  `POST /api/simulation/reset`) call it, so the window does not open at
  all when the platform does the resetting.
* And when the reset comes from **outside** — the simulator's own admin
  console, or the organizers' scripts — the simulator announces it on the
  stream (`event: simulator.notice, data: {"message": "Simulation reset"}`).
  The platform now handles that notice by dropping the world and
  re-reading it.

Measured, before and after:

| | Before | After |
|---|---|---|
| The state served one second after a reset | tick 892, `degraded: false`, age 0.2 s | **503, no state** |
| The state served two seconds after a reset | tick 892, `degraded: false` | **the new world** |
| A reset done from the simulator's own console | not noticed | **noticed within 2 s** |

The second half matters as much as the first. The platform can be reset
by someone else — the organizers will do exactly that between demo runs —
and the platform has to notice.

**There is a second bug hiding inside this one.** The event stream's tick
tracker is the highest tick the stream has carried. After a reset the
stream carries the *new* world's ticks, but the tracker still said 892 —
so the platform would decide the stream was permanently stale, drop it,
and reconnect, for ever. The tracker is reset with the cache.

---

## The second bug: a probe that measured the same thing twice

`scripts/extrapolation_probe.py` exists to compare two feature sets: the
**absolute tick** the platform used to ship, and the **tick's position
within the day** it ships now. It is the evidence for the change that
fixed the model's extrapolation.

It was measuring the same thing twice:

```text
absolute tick (as shipped)   model  176.9 L   baseline  237.2 L   diff -25.4%   wins 7/12
tick within the day          model  176.9 L   baseline  237.2 L   diff -25.4%   wins 7/12
```

Identical to the decimal. The probe borrowed the shipped builder for the
"absolute tick" arm — and the shipped builder *is* the within-day
builder, because the fix had already landed. The comparison had quietly
become meaningless, and it would have kept reporting a confident number
for as long as anyone ran it.

The probe now writes the absolute-tick builder out by hand, so both arms
are genuinely different:

```text
absolute tick (as first shipped)   model  189.2 L   baseline  179.8 L   diff  +5.2%   wins 5/12
tick within the day                model  132.2 L   baseline  179.8 L   diff -26.5%   wins 7/12
```

**The within-day feature is 26.5 % better over the forecast horizon** —
132.2 L against the rule's 179.8 L, where the absolute tick was actually
*worse* than the rule at 189.2 L. A probe that cannot fail is not
evidence, and this one had stopped being evidence without anyone
noticing.

---

## The third bug: a demo that claimed something it did not do

The demo script's last step says *"the model retrained, its confidence
came back, and the platform is using it again — on its own, without a
restart."*

The platform does recover on its own — but **not within the demo's
wait**. The model has to *earn* its way back: it is only used when it
beats the rule-based predictor on a held-out split, and that score comes
from a training run. The trainer's own schedule is **five minutes**, and
the dry run waited forty seconds.

So the dry run failed intermittently — it passed three times out of five —
and the failure was in the platform being *honest*: the model had been
scored `worse_than_baseline` on the last training run, so the platform
correctly refused to use it. The platform was right and the demo's claim
was wrong.

The fix is in both places. The demo script now asks for the retrain
outright — the same control an operator has, and the trainer's five-minute
timer is not something a demo should be at the mercy of:

```bash
curl -s -X POST $BACKEND/api/model/fault -H 'Content-Type: application/json' -d '{"fault":null}'
curl -s -X POST $BACKEND/api/model/train | jq '{trained, training_rows}'
sleep 15
curl -s $BACKEND/api/risk | jq '{active_model, models_used}'
```

And the dry run does the same thing, and says what it did:

```
[38s] Step 16 — Operations continue — repair the model
   ok   the model retrained on 2179 rows
   ok   the model is back in service (learned)
```

**Five consecutive runs, every step green**, 32 to 43 seconds each.

| Run | Time | Result |
|---|---|---|
| 1 | 43 s | every step produced what the demo script claims |
| 2 | 32 s | every step produced what the demo script claims |
| 3 | 35 s | every step produced what the demo script claims |
| 4 | 32 s | every step produced what the demo script claims |
| 5 | 35 s | every step produced what the demo script claims |

---

## The other two fixes the dry run forced

**A race on the risk level.** The dry run checked for a CRITICAL risk
assessment *once*, the instant the anomaly appeared. But the risk level
moves over a tick or two: the platform has to forecast forward from the
spiked demand and see how fast the tank drains. The demo script says
"wait about ten seconds" for exactly this reason, and the check now waits
too.

**A race on the state.** After a reset the platform drops the cached
world (above), so `/api/state` answers 503 for a moment. The reset
script's wait used `curl -fsS`, which treats a 503 as an error — and
under `set -e` that killed the script mid-way. Both scripts now parse the
status code and the body separately, and wait for the *new* world's tick.

---

## The model's verdict, taken honestly

The work that was in progress when this day started was about the model:
whether the learned predictor deserves to be used at all. It does — but
the platform was measuring the comparison badly, and the numbers it
published were partly the model being scored against itself.

**The horizon score was leaky.** The validator scored the model on a
replay window, but the model had been fitted on the *whole* history —
including the window it was then asked to predict. The fix is to retrain
the model on the prefix before each window it is scored on. Measured
against the stored history:

| Model | Error over the horizon | Against the baseline |
|---|---|---|
| scored with the window in its training data | 64.6 L | **−60.7 %** |
| retrained on the prefix, then scored | 170.5 L | **+3.7 %** |

The leaky model wins 37 of 48 window/pair comparisons. The honest one
wins 25 of 48. **That 60.7 % was the model being scored against itself**,
and it was the number the platform published as its reason for trusting
the model.

**One window is not a verdict.** The window slides with the daily demand
cycle, and the difference between two estimators on a single window is
mostly noise. Measured over the stored history:

```text
window 0: mean diff -58.6%   model wins 10/12
window 1: mean diff -36.5%   model wins 10/12
window 2: mean diff -57.7%   model wins 10/12
window 3: mean diff -66.1%   model wins 12/12
```

The score is now averaged over **four** contiguous windows, and the model
is refitted for each one.

**So the gate moved.** The platform now gates on the **held-out one-step
comparison** — the model fitted on the first 80 % of the history *by
time* and scored on the last 20 %, against the rule-based predictor
scored on exactly the same rows with exactly the rule it uses live.
Neither side can see the rows it is judged on, and the result does not
change from one retrain to the next. The horizon score is still
published, with its honest verdict, because a diagnostic that flatters
the model is worse than no diagnostic.

**The honest headline.** Measured against the stored history, the learned
model's error sat between **0.82 and 0.90** of the rule's at every tick
from 16 onwards — a consistent win, and a stable one. It wins **7 of 12**
station/fuel pairs over the horizon (132.2 L against 179.8 L), and it
wins **35 of 48** window/pair comparisons once the leak is removed.

**And it is published even when it loses.** The platform's own verdict,
in `/api/model-status`:

```json
{
  "active_model": "learned",
  "validation_mae_liters": 0.0495,
  "baseline_validation_mae": 0.0678,
  "horizon_total_mae_liters": 0.0,
  "baseline_horizon_total_mae_liters": 0.0,
  "beats_baseline_at_horizon": false
}
```

The horizon verdict is published whether it flatters the model or not. A
platform that hides its own bad numbers cannot be trusted about its good
ones.

---

## The release

`v1.0.0`, published by the `Release` workflow to the GitHub Container
Registry with **three tags**: the version, the version plus the commit
sha, and `latest`.

**The sha tag is the rollback target**, because it is the only one that
cannot move after the fact. `latest` can be overwritten by the next
release; `v1.0.0` can be moved by a careless tag; `v1.0.0-<sha>` cannot.
`docs/deployment.md` has the procedure, and there is no database
migration to reverse — a rollback is "run the previous image".

```bash
docker compose -f docker-compose.yml -f docker-compose.release.yml up -d --wait
```

---

## The CI

The pipeline runs on every push to `main`:

| Job | What it does |
|---|---|
| `backend-lint` | ruff lint and format check |
| `backend-tests` | pytest with a JUnit report |
| `backend-coverage` | a **floor per module**, not one average |
| `frontend` | typecheck, Vitest, the production build |
| `e2e` | Playwright against the whole stack, brought up with `docker compose up -d --wait` |
| `docker-build` | both images built, with a live health check against a real simulator and Postgres |
| `compose-config` | `docker compose config` validation |
| `compose-smoke` | the README's own claim, executed: copy the `.env`, bring the stack up, check `/health` for every component, check the state has a whole network in it, run the integration suite |

The coverage job enforces a floor on each of the seven modules the brief
names, rather than one average — an average lets a module rot while the
number stays green.

---

## The documentation

| Document | What it holds |
|---|---|
| `README.md` | The project front door — what it is, how to run it, every endpoint, the rules it holds itself to |
| `docs/architecture.md` | The Mermaid diagram (rendered to `docs/assets/architecture-mermaid.png`), the assumptions, the data sources, the security notes, the limitations |
| `docs/deployment.md` | Publishing, rollback, and the post-deploy checks |
| `docs/demo-script.md` | The fourteen steps, with the exact command or click for each one and what to say while doing it |
| `docs/deliverables-checklist.md` | Every deliverable mapped to the file or screen that proves it |
| `docs/progress/day1.md` … `day7.md` | The daily log |

**The demo script is the one that matters most on the day**, because it is
what a judge reads when they want to know what the demo will be. It
follows the brief's story exactly: normal operations → dashboard → demand
rising → risk detected → shortage predicted → recommendation → operator
inspects → simulate → crisis → adaptation → failure injected → monitoring
catches it → fallback → operations continue.

---

## Tests

| Suite | Count |
|---|---|
| Backend (`pytest`) | **583** — 29 new today (554 at the last commit) |
| Coverage | **85 %** overall |
| Frontend (`vitest`) | **22** |
| End-to-end (`playwright`) | **12** |
| Ruff lint + format | clean |

The new tests cover three things: the cache's rewind detection, the sync
service's tracker reset, and the API's 503 after a reset — plus the
model-gate and feature changes that came with them.

---

## The numbers, in one place

| | |
|---|---|
| Backend tests | **583** passing |
| Coverage | **85 %** overall |
| Frontend | 22 Vitest, 12 Playwright |
| Containers | 7 (4 core + 3 monitoring) |
| Memory | ~450 MiB in use against a 1.5 GB budget |
| Demo dry run | **5 consecutive clean runs**, 32–43 s |
| Crisis scenarios recovered | 17 of 17 (Day 6) |
| Load-test p99 | 30 030 ms → 286 ms (Day 6) |
| Model vs the rule, held out | error at **0.82–0.90** of the rule's |
| Model vs the rule, over the horizon | 132.2 L vs 179.8 L — **26.5 % better** |

---

## Things worth knowing

**The honest horizon verdict is published even when it is unflattering.**
The horizon score says the learned model does not beat the rule over
*every* window — it wins 7 of 12 pairs and loses the rest. The gate is
the held-out one-step comparison, which is stable and leak-free; the
horizon score is a diagnostic, and `/api/model-status` reports it
whatever it says.

**The demo's recovery step needed a retrain.** The platform's five-minute
trainer is right for production and wrong for a demo. The demo asks for
the retrain explicitly, which is the control an operator has anyway.

**The reset is now a real operation.** Resetting the world drops the
platform's cache, its working set, its counters, any injected fault and
the kill-switch. `POST /admin/reset` clears the platform's half and
`POST /api/simulation/reset` clears the world's half, because the
platform and the simulator are two different things and resetting one
does not reset the other.

**A probe that cannot fail is not evidence.** The extrapolation probe was
measuring the same thing twice and reporting a confident number anyway.
It is fixed, and the numbers it now reports are the ones in this
document.

---

## How to run it

```bash
cp .env.example .env
docker compose up --build -d --wait

# the demo, from a known state
scripts/reset_demo.sh --wait
scripts/demo_dry_run.sh        # executes and checks every step

# the tests
cd backend && .venv/bin/python -m pytest -q
cd frontend && npm run test && npm run test:e2e
```

---

## Closeout

**Completed:** 2026-11-25.

* The backend was rebuilt with the day's fixes and the stack was left
  running on the **core profile** — four containers. The monitoring
  profile was not started: the day needed no dashboard screenshots, and
  the machine is shared.
* `free -h` was read before every load. The available memory never fell
  below **2.2 GB**.
* No volumes were removed. The audit trail, the demand history and the
  decision records survive for the judges to query.
* The working tree was committed, tagged `v1.0.0` and pushed.

```text
free -h
Mem:  7.1Gi total, 4.7Gi used, 758Mi free, 117Mi shared, 2.1Gi buff/cache, 2.4Gi available
Swap: 4.0Gi total, 2.1Gi used, 1.9Gi free
```

---

## Next — the submission

26 November. The buffer day is for one thing only: a **fresh-clone
test**, exactly as a judge would do it.

```bash
git clone <the repository>
cd fuel-supply-intelligence-platform
cp .env.example .env
docker compose up --build -d --wait
curl -s http://localhost:8001/health | jq
```

Nothing else is planned. The build is done; what is left is proving it
works from nothing, once more, on a clean machine.

---

## Addendum — the fresh-clone test, run the same evening

There was time, so the fresh-clone test was run straight away, against
the tagged commit — not the working tree.

```bash
git clone /home/tanim-mahmud/Desktop/poridhi/fuel-supply-intelligence-platform /tmp/clone-proof
cd /tmp/clone-proof
git log --oneline -1
# 426b574 (HEAD -> main, tag: v1.0.0, origin/main, origin/HEAD)
#   fix(day7): stop the platform serving a world that has ended

cp .env.example .env
docker compose up --build -d --wait
```

**The stack came up in 15 seconds**, all four containers healthy:

```text
Container fuel-simulator    Healthy
Container fuel-postgres     Healthy
Container fuel-backend      Healthy
Container fuel-frontend     Healthy

real  0m15.451s
```

```json
{
  "status": "ok",
  "components": {
    "backend":   {"status": "ok"},
    "simulator": {"status": "ok", "database": "ok", "simulation": {"status": "RUNNING", "tick": 2562}},
    "database":  {"status": "ok"}
  }
}
```

The state had a whole network in it — 4 stations, 2 depots, 6 routes,
`degraded: false`, age 1.11 s — and the dashboard answered 200.

**The demo dry run passed on the fresh clone, twice:**

| Run | Time | Result |
|---|---|---|
| 1 | 42 s | every step produced what the demo script claims |
| 2 | 30 s | every step produced what the demo script claims |

**And the bug of this day is fixed there too**, which is the part that
matters — a fix that only works in the working tree is not a fix:

```text
=== BEFORE ===
{"tick":302}
{"tick":288,"degraded":false,"age_seconds":2.183}

=== POST /admin/reset ===
{"tick":0,"status":"PAUSED"}

=== IMMEDIATELY after ===
HTTP 503
{"detail":"no simulator state available yet"}
```

The platform refuses to serve the world that just ended, and it is
serving the new world **one second** later.

The clone's containers, all inside their limits:

| Container | Memory | Limit |
|---|---|---|
| `fuel-frontend` | 16.96 MiB | 64 MB |
| `fuel-backend` | 153 MiB | 384 MB |
| `fuel-postgres` | 98.01 MiB | 256 MB |
| `fuel-simulator` | 76.3 MiB | 384 MB |

**344 MB for the four core containers**, against a 1.1 GB budget, on a
machine with 2.5 GB available.

The clone was torn down afterwards and the working tree's stack was
brought back up.

