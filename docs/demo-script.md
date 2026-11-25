# The demo script

The demo tells one story in fourteen steps:

> A fuel network is running normally. Demand rises. The platform notices a
> risk, predicts a shortage, and recommends an allocation. An operator
> inspects it, simulates it, and approves it. A crisis hits. The platform
> adapts. A component fails. Monitoring catches it, the platform falls back
> to a rule-based policy, and operations continue.

Everything below is exact: the commands to run, the buttons to click, and
what to say while you do it. Nothing is left to improvisation.

**Total time: about 11 minutes.** The timings in the table are what the
recorded dry run measured; the full run is in
[`docs/progress/day7.md`](progress/day7.md).

---

## Before you start

### One command, then a reset

```bash
# 1. The whole stack. One command plus a .env copy.
cp .env.example .env
docker compose up --build -d --wait

# 2. Put the world back to a known state.
scripts/reset_demo.sh --wait
```

The reset does three things: it reloads the scenario from tick zero, clears
the platform's decisions and alerts, and lets the world run for a few ticks
so the model has some history. **Run it before every demo.** Without it,
the second run behaves differently from the first.

> **Note:** `scripts/reset_demo.sh` reads the ports from `.env`, so it works
> whatever the stack is remapped to.

### The three tabs

| What | Where |
|---|---|
| **The dashboard** | http://localhost:5173 |
| **The API** | http://localhost:8001 (`/docs` for the interactive reference) |
| **Grafana** | http://localhost:3000 (only with the monitoring profile) |

Open the dashboard and **Grafana in two browser tabs before you start**. A
tab you have to open mid-demo is a tab you will open mid-demo badly.

### The environment

```bash
export BACKEND=http://localhost:8001
export SIM=http://localhost:8100     # or 8000 — check .env for SIMULATOR_PORT
```

> The simulator's port is **8100** on this machine and **8000** on a clean
> checkout. `SIMULATOR_PORT` in `.env` is the truth. Check it once, before
> the demo, and set `SIM` to match.

### The one-line rule

**Every recommendation is a proposal until a human approves it.** Say this
out loud at least twice during the demo. It is the point of the whole
system.

---

## The fourteen steps

### 1. Normal operations

**Say:** *"This is the fuel network. Two regions, two depots, four stations,
six routes, three fuel types. Everything you are about to see comes from
the simulator — nothing here is made up."*

**Click:** **Overview** in the sidebar.

**Show:** the tick counter, the simulated clock, the service level, unmet
demand, and the health banner — all green.

```bash
curl -s $BACKEND/api/state | jq '.instance | {tick, sim_time, status}'
curl -s $BACKEND/api/state | jq '.metrics | {service_level, unmet_demand_liters}'
```

**Say:** *"Service level is the fraction of demand the network actually
served. Right now it is [X]. The supply in this scenario is about 25 %
below the demand, so this number can never reach 1.0 — the job is to
allocate scarce fuel well, not to create it."*

---

### 2. The dashboard

**Say:** *"Nine screens, all over the live API. The dashboard polls every
five seconds and shows the age of the data it is showing."*

**Click through, fast:**

| Screen | What to point at |
|---|---|
| **Network** | The 2 regions, 2 depots, 4 stations and 6 routes, with live status colours. |
| **Inventory** | Fill bars and hours-to-stockout per tank per fuel. |
| **Supply & events** | Incoming arrivals and active crises on one timeline. |
| **Service health** | Every dependency, the p95 latency, the error rate, the fallback state. |

**Say:** *"Every view has a loading state and an error state. If a
dependency fails, the banner turns amber and tells you the data is stale
and how old it is — it never serves stale data as if it were fresh."*

---

### 3. Demand is rising

**Say:** *"Let me break the world."*

**Click:** **Simulation** in the sidebar.

**Inject:** a **demand spike**, multiplier **3.0**, duration **24 ticks**.

```bash
curl -s -X POST $BACKEND/api/simulation/crisis \
  -H 'Content-Type: application/json' \
  -d '{"type":"demand_spike","duration_ticks":24,"parameters":{"multiplier":3.0}}' \
  | jq '.event'
```

**Say:** *"Demand at every station has just tripled. Nothing has been
shipped. The platform does not know this yet — it finds out by watching."*

---

### 4. Risk is detected

**Wait:** about 10 seconds for the platform to notice.

**Click:** **Risk & alerts**.

**Say:** *"The platform compared what each station drew against the
published demand profile for that hour. The deviation is outside the
band, so it raised a demand anomaly."*

```bash
curl -s $BACKEND/api/anomalies | jq '.anomalies[] | {kind, entity_id, severity, message}'
```

**Say:** *"One thing to notice: the reference is the *profile*, not the
current demand multiplier. The multiplier is what caused the spike —
comparing against it would explain the spike away with the very thing
that caused it."*

---

### 5. A shortage is predicted

**Click:** **Risk & alerts** → click a station's row to open the
**explanation drawer**.

**Say:** *"Now the prediction. Here is the demand forecast for the next
simulated day, with its confidence band. And here is the stockout
number — hours until this tank runs dry, and the probability it happens."*

```bash
curl -s "$BACKEND/api/risk" | jq '.risks[] | select(.risk_level=="CRITICAL")
  | {station_id, fuel_type, risk_level, hours_to_stockout, stockout_probability}'
curl -s "$BACKEND/api/forecast?station_id=station-mirpur&fuel_type=DIESEL" \
  | jq '{model: .forecast.model, confidence: .forecast.confidence, points: (.forecast.points | length)}'
```

**Say:** *"That number is a forecast, not a fact, which is why it comes
with a band. The platform does not present a number it cannot stand
behind."*

---

### 6. A recommendation is made

**Click:** **Recommendations**.

**Say:** *"And here is what the platform wants to do about it. It did not
send anything — it is a proposal. It pre-validates every rule the
simulator enforces: the route has to connect the two endpoints, the depot
has to be open, the tank has to have room, the depot's dispatch allowance
has to cover it."*

**Show the card:** the before → after impact, the constraints, the signals
behind the score, the confidence, and the alternatives.

**Say:** *"Two alternatives were considered and ranked. The confidence is
0.9, not 1.0 — the platform does not pretend to be certain."*

---

### 7. The operator inspects it

**Say:** *"Before I approve anything, I want to know what happens if I do
nothing."*

**Click:** **Simulate** on the recommendation card.

```bash
curl -s -X POST $BACKEND/api/recommendations/$(curl -s $BACKEND/api/recommendations \
  | jq -r '.recommendations[0].id')/simulate | jq
```

**Say:** *"This is the what-if. The projection is **local** — nothing was
written to the simulator and the recommendation's status did not change.
Here is the risk before and here is the risk after."*

**Say:** *"This is the step that separates this from a dashboard. The
platform does not get to press the button."*

---

### 8. The operator simulates — and approves

**Say:** *"The risk drops from CRITICAL to WATCH and the hours-to-stockout
goes from 4 to 31. That is worth doing."*

**Click:** **Approve** on the recommendation card.

**Say:** *"And now it goes to the simulator. With a deterministic
idempotency key, so if I press approve twice the simulator returns the
*same* allocation rather than shipping twice."*

```bash
curl -s $BACKEND/api/allocations | jq '.allocations[] | {id, status, quantity, destination_station_id}'
```

**Say:** *"It is IN_TRANSIT. The decision history now records who approved
it, when, and what happened to it."*

---

### 9. The crisis

**Say:** *"Now the real crisis."*

**Click:** **Simulation** → inject a **route disruption** on the fast
route, duration **16 ticks**.

```bash
curl -s -X POST $BACKEND/api/simulation/crisis \
  -H 'Content-Type: application/json' \
  -d '{"type":"route_disruption","duration_ticks":16,
       "parameters":{"route_ids":["route-gazipur-mirpur"]}}' \
  | jq '.event'
```

**Say:** *"The fast route from Gazipur to Mirpur is cut. The station is
already short and now the easy way in is gone."*

---

### 10. The platform adapts

**Click:** **Recommendations** → **Propose** (or wait for the next cycle).

```bash
curl -s -X POST $BACKEND/api/recommendations/propose | jq '.recommendations | length'
```

**Say:** *"The platform re-planned. The recommendation now routes around
the disruption — the slower route from Patiya, which takes more ticks but
still gets there. The rationale says so explicitly."*

**Say:** *"This is the point of pre-validating constraints: the platform
never proposes something the simulator will refuse. Every proposal it
makes would be accepted."*

---

### 11. A failure is injected

**Say:** *"Now I will break the platform's own prediction model."*

**Click:** **Service health** → **Inject prediction fault**.

```bash
curl -s -X POST $BACKEND/api/model/fault \
  -H 'Content-Type: application/json' \
  -d '{"fault":"predict_error"}' | jq
```

**Say:** *"The learned model now raises on every call. This is not a
kill-switch — the model is not healthy, it is *broken*, and the platform
has to notice on its own."*

**Wait:** about 10 seconds for the platform to notice.

> **Two faults, two different fallbacks.** This is worth saying out loud
> because it is the part people get wrong:
>
> | Fault | What breaks | What falls back |
> |---|---|---|
> | `predict_error` | the **model** raises | the rule-based **predictor** — the forecast level |
> | `evaluate_error` | the whole **evaluation** raises | the rule-based **policy** — the decision level |
>
> Step 12 uses the first. Step 13 uses the second, because the *decision*
> fallback is the one the brief asks about.

---

### 12. Monitoring catches it

**Click:** **Service health**.

**Say:** *"And here it is. The service health panel shows the prediction
layer is degraded, and the Prometheus alert has fired."*

```bash
curl -s $BACKEND/api/service-health | jq '{prediction: .prediction, decision: .decision}'
```

**If the monitoring profile is running**, switch to the Grafana tab:

**Click:** the **Intelligence** dashboard.

**Say:** *"The alert rule `PredictionFallbackActivated` fired the moment
the model started failing. It is in the alerts log with the timestamp."*

```bash
curl -s $BACKEND/api/model-status | jq '{active_model, fallback_active, fallback_reason}'
```

---

### 13. The fallback takes over

**Say:** *"Now let me break it one level higher — the whole evaluation."*

```bash
curl -s -X POST $BACKEND/api/model/fault \
  -H 'Content-Type: application/json' \
  -d '{"fault":"evaluate_error"}' | jq
```

**Wait:** about 10 seconds.

**Say:** *"The intelligence layer now has nothing to offer at all. And
here is what matters: the platform did not stop."*

**Click:** **Recommendations**.

**Say:** *"The recommendations are still being made. They now come from the
rule-based policy — refill the lowest days-of-cover first. Look at the
card: it says it used the fallback policy, and it asks for human review,
and its confidence is lower, because it *should* be."*

```bash
curl -s $BACKEND/api/recommendations | jq '[.recommendations[]
  | select(.used_fallback_policy)] | length'
curl -s $BACKEND/api/service-health | jq '.components.decision_engine
  | {fallback_active, fallback_reason}'
```

**Say:** *"That is the honest version of resilience. Not 'it never goes
down' — it goes down, it says so, and it keeps doing the safe thing."*

---

### 14. Operations continue

**Say:** *"Let me put the model back and show that the platform recovers
on its own."*

```bash
curl -s -X POST $BACKEND/api/model/fault \
  -H 'Content-Type: application/json' \
  -d '{"fault":null}' | jq

# The model has to earn its way back: it is only used when it beats the
# rule-based predictor on a held-out split, and that score comes from a
# training run. The trainer's own schedule is five minutes, so ask for it.
curl -s -X POST $BACKEND/api/model/train | jq '{trained, training_rows}'

# Wait for the platform to use it again — the next evaluation cycle.
sleep 15
curl -s $BACKEND/api/risk | jq '{active_model, models_used}'
```

**Click:** **Overview**.

**Say:** *"The health banner is green again. The model retrained, its
confidence came back, and the platform is using it again — on its own,
without a restart."*

**Say:** *"That is the whole story: observe, detect, predict, decide,
simulate, act, monitor, recover. And every consequential action passed
through a human."*

```bash
# The evidence, for the questions that come after the demo.
curl -s $BACKEND/api/decision-history | jq '.decisions | length'
curl -s $BACKEND/api/audit | jq '.total_matched'
docker compose logs --no-color backend 2>/dev/null | tail -20
```

---

## The reset procedure

**Between demos, or whenever the world is in a strange state:**

```bash
scripts/reset_demo.sh --wait
```

That is:

| Step | What it does |
|---|---|
| 1 | `POST /admin/reset` on the simulator — reloads the scenario from tick zero, leaves it paused. |
| 2 | `DELETE` from the platform's decision, recommendation and alert tables — clears the platform's own history so the decision-history screen starts empty. |
| 3 | Clears any injected prediction fault and turns the kill-switch back off. An injected fault lives in the *backend*, not in the world, so a simulator reset does not clear it — without this step the next demo starts with the prediction layer already broken. |
| 4 | `POST /admin/run` — starts the clock again. The reset leaves the simulation paused, and without this the platform looks healthy but the world never advances. |
| 5 | Waits for a few ticks of history, so the forecast is worth looking at. |

Options:

```bash
scripts/reset_demo.sh              # reset and start the clock
scripts/reset_demo.sh --no-run     # reset and leave it paused
scripts/reset_demo.sh --wait       # reset, then wait for a few ticks of history
```

### Doing it by hand

If the script is not to hand, the four commands are:

```bash
curl -s -X POST $SIM/admin/reset | jq
curl -s -X POST $SIM/admin/run   | jq

# Clear any injected fault, or the next run starts with the model broken.
curl -s -X POST $BACKEND/api/model/fault \
  -H 'Content-Type: application/json' -d '{"fault":null}' | jq
```

And to clear the platform's own history:

```bash
docker compose exec -T postgres psql -U fuel -d fuel -c "
    SET lock_timeout = '2s';
    DELETE FROM decision_records;
    DELETE FROM recommendations;
    DELETE FROM alerts;
"
```

> **Do not use `TRUNCATE` here.** It needs an `ACCESS EXCLUSIVE` lock, and
> the backend writes to these tables on every sync cycle, so a `TRUNCATE`
> waits for a gap that may never come. `DELETE` takes a weaker lock and
> gets through.

---

## If something goes wrong mid-demo

| Symptom | Cause | Fix |
|---|---|---|
| The dashboard says **stale** | The simulator is slow or the sync has stopped | `curl -s $BACKEND/api/service-health \| jq` — check the simulator's status. |
| The tick is not moving | The simulation was paused — the reset leaves it paused | `curl -s -X POST $SIM/admin/run \| jq` |
| No recommendations | The model has no history yet | `scripts/reset_demo.sh --wait`, or let it run for 30 seconds. |
| The world is at tick 40000 | A previous run was never reset | `scripts/reset_demo.sh --wait` |
| The simulator stopped answering | Its connection pool was exhausted by a burst | See the README's *"Recovering a wedged simulator"* — restart it, then reset it. |
| Port 8000 answers nothing | The simulator is on 8100 here | `grep SIMULATOR_PORT .env` |

---

## The rehearsal checklist

Run through this the day before. It takes three minutes.

- [ ] `docker compose up --build -d --wait` — the stack comes up clean
- [ ] `scripts/reset_demo.sh --wait` — the world resets and starts
- [ ] The dashboard loads on all nine screens
- [ ] `curl -s $BACKEND/health | jq` — every component is `ok`
- [ ] A demand spike produces an anomaly within 10 seconds
- [ ] A recommendation appears, and **Simulate** returns a projection
- [ ] **Approve** sends an allocation and the decision history records it
- [ ] `POST /api/model/fault` makes the service-health panel show a fault
- [ ] `POST /api/model/fault {"fault":null}` clears it
- [ ] Grafana opens on the **Intelligence** dashboard
- [ ] You know the port the simulator is on
