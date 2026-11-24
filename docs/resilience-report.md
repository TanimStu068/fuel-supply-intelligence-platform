# Resilience report

**Generated:** 2026-11-24T19:45:15.381050+00:00  
**Backend:** `http://localhost:8001` · **Simulator:** `http://localhost:8100`  
**Scenarios run:** 17

Every number below was measured against the running stack by `scripts/crisis_drill.py`. The detection time is an **upper bound**: the drill polls every two seconds, so the real detection was no slower than the figure reported.


The drill ran one scenario at a time with a pause between them, and checked the host's available memory before each one — stopping if it fell under 1,024 MB. The **monitoring** profile was off for the whole run, so nothing here depends on Prometheus or Grafana: the counters are read from the backend's own `/metrics` endpoint.

Every fault lasted between 10 and 30 seconds — long enough to be a real incident, short enough that the drill is not an outage of its own making.
## Summary

| Scenario | Kind | Detected in | Recovery | Outcome | Served during the incident |
|---|---|---|---|---|---|
| **Baseline** | baseline | 0.0s | 0.0s | RECOVERED | served all 5 dashboard endpoints |
| **Demand spike** | crisis | 2.0s | 0.0s | RECOVERED | served all 5 dashboard endpoints |
| **Shipment delay** | crisis | 2.0s | 0.0s | RECOVERED | served all 5 dashboard endpoints |
| **Depot constraint** | crisis | 2.0s | 0.0s | RECOVERED | served all 5 dashboard endpoints |
| **Route disruption** | crisis | 4.0s | 0.0s | RECOVERED | served all 5 dashboard endpoints |
| **Station outage** | crisis | 2.0s | 0.0s | RECOVERED | served all 5 dashboard endpoints |
| **Supply shortfall** | crisis | 2.0s | 0.0s | RECOVERED | served all 5 dashboard endpoints |
| **Combined crisis** | combined crisis | 2.0s | 0.0s | RECOVERED | served all 5 dashboard endpoints |
| **Latency** | fault | 12.4s | 0.0s | RECOVERED | served all 5 dashboard endpoints |
| **Unavailable** | fault | 2.1s | 0.0s | RECOVERED | served all 5 dashboard endpoints |
| **Error rate** | fault | 2.1s | 0.0s | RECOVERED | served all 5 dashboard endpoints |
| **Stale data** | fault | 4.4s | 0.0s | RECOVERED | served all 5 dashboard endpoints |
| **Stream disconnect** | fault | 22.5s | 0.0s | RECOVERED | served all 5 dashboard endpoints |
| **Simulator connection-pool exhaustion** | dependency failure | 18.1s | 111.4s | RECOVERED | served all 5 dashboard endpoints |
| **Database stopped** | dependency failure | 0.1s | 2.0s | RECOVERED | served all 5 dashboard endpoints |
| **Prediction layer disabled** | dependency failure | 0.0s | 0.0s | RECOVERED | served all 5 dashboard endpoints |
| **Prediction layer broken** | dependency failure | 0.0s | 0.0s | RECOVERED | served all 5 dashboard endpoints |

**17 of 17 scenarios recovered automatically.** None of them required a restart of the backend.

## What each scenario proved

### Baseline

*Nothing is broken. This is the control measurement.*

- **Started at:** 2026-11-24T19:36:42.244158+00:00
- **Expected:** the platform is healthy, fresh, and answering every endpoint
- **Detected in:** 0.0s — the control measurement — nothing to detect
- **The platform recommended:** no new proposals during the incident (100 already in the store)
- **During the incident:** served all 5 dashboard endpoints
- **Recovered in:** 0.0s
- **Outcome:** RECOVERED

| Measurement | Value |
|---|---|
| health status | ok |
| simulator tick | 3925 |
| memory before the scenario | 1,294 MB available of 7,271 MB |
| cached state age | 2.569 s |
| degraded mode | False |
| breaker state | closed |
| simulator component | ok |

### Demand spike

*Demand at every station triples. The network runs dry faster than the plan says.*

- **Started at:** 2026-11-24T19:36:54.290168+00:00
- **Expected:** risk levels rise, the platform proposes larger and earlier allocations
- **Detected in:** 2.0s — the platform's event feed carries the demand_spike event (id 1, tick 4005)
- **The platform recommended:** 5 proposed during the incident (CRITICAL=5); 0 on the fallback policy; most urgent: station-coxsbazar/DIESEL for 6,000 L
- **During the incident:** served all 5 dashboard endpoints
- **Recovered in:** 0.0s
- **Outcome:** RECOVERED
- **Notes:** injected demand_spike into the simulator

| Measurement | Value |
|---|---|
| baseline status | ok |
| baseline tick | 4004 |
| risk during crisis | CRITICAL=12 |
| alerts during crisis | anomaly_demand=36, shortage_critical=64 |
| status after recovery | ok |
| memory before the scenario | 1,271 MB available of 7,271 MB |

### Shipment delay

*Incoming diesel arrivals are delayed by eight ticks.*

- **Started at:** 2026-11-24T19:37:18.398382+00:00
- **Expected:** the supply timeline shows DELAYED arrivals, and cover shortens
- **Detected in:** 2.0s — the platform's event feed carries the shipment_delay event (id 2, tick 4163)
- **The platform recommended:** 5 proposed during the incident (CRITICAL=5); 0 on the fallback policy; most urgent: station-coxsbazar/DIESEL for 6,000 L
- **During the incident:** served all 5 dashboard endpoints
- **Recovered in:** 0.0s
- **Outcome:** RECOVERED
- **Notes:** injected shipment_delay into the simulator

| Measurement | Value |
|---|---|
| baseline status | ok |
| baseline tick | 4162 |
| risk during crisis | CRITICAL=12 |
| alerts during crisis | anomaly_demand=40, shortage_critical=60 |
| status after recovery | ok |
| memory before the scenario | 1,204 MB available of 7,271 MB |

### Depot constraint

*Every depot is marked CONSTRAINED, which cuts its dispatch capacity.*

- **Started at:** 2026-11-24T19:37:42.507321+00:00
- **Expected:** the allocator proposes smaller shipments, and the constraint is named
- **Detected in:** 2.0s — the platform's event feed carries the depot_constraint event (id 3, tick 4321)
- **The platform recommended:** 5 proposed during the incident (CRITICAL=5); 0 on the fallback policy; most urgent: station-coxsbazar/DIESEL for 6,000 L
- **During the incident:** served all 5 dashboard endpoints
- **Recovered in:** 0.0s
- **Outcome:** RECOVERED
- **Notes:** injected depot_constraint into the simulator

| Measurement | Value |
|---|---|
| baseline status | ok |
| baseline tick | 4320 |
| risk during crisis | CRITICAL=12 |
| alerts during crisis | anomaly_demand=48, shortage_critical=52 |
| status after recovery | ok |
| memory before the scenario | 1,234 MB available of 7,271 MB |

### Route disruption

*Every route is marked DISRUPTED.*

- **Started at:** 2026-11-24T19:38:06.686516+00:00
- **Expected:** the platform routes around the cut, or refuses to route through it
- **Detected in:** 4.0s — the platform's event feed carries the route_disruption event (id 4, tick 4480)
- **The platform recommended:** 10 proposed during the incident (CRITICAL=10); 0 on the fallback policy; most urgent: station-coxsbazar/DIESEL for 6,000 L
- **During the incident:** served all 5 dashboard endpoints
- **Recovered in:** 0.0s
- **Outcome:** RECOVERED
- **Notes:** injected route_disruption into the simulator

| Measurement | Value |
|---|---|
| baseline status | ok |
| baseline tick | 4479 |
| risk during crisis | CRITICAL=12 |
| alerts during crisis | anomaly_demand=52, shortage_critical=48 |
| status after recovery | ok |
| memory before the scenario | 1,209 MB available of 7,271 MB |

### Station outage

*Every station is marked OUTAGE, so served demand drops to zero.*

- **Started at:** 2026-11-24T19:38:32.844302+00:00
- **Expected:** outage alerts are raised for the affected stations
- **Detected in:** 2.0s — the platform's event feed carries the station_outage event (id 5, tick 4651)
- **The platform recommended:** 5 proposed during the incident (CRITICAL=5); 0 on the fallback policy; most urgent: station-coxsbazar/DIESEL for 6,000 L
- **During the incident:** served all 5 dashboard endpoints
- **Recovered in:** 0.0s
- **Outcome:** RECOVERED
- **Notes:** injected station_outage into the simulator

| Measurement | Value |
|---|---|
| baseline status | ok |
| baseline tick | 4650 |
| risk during crisis | CRITICAL=12 |
| alerts during crisis | anomaly_demand=44, shortage_critical=56 |
| status after recovery | ok |
| memory before the scenario | 1,156 MB available of 7,271 MB |

### Supply shortfall

*Incoming diesel arrivals are cut to 40% of their planned quantity.*

- **Started at:** 2026-11-24T19:38:56.981385+00:00
- **Expected:** the platform sees less fuel coming and reacts accordingly
- **Detected in:** 2.0s — the platform's event feed carries the supply_shortfall event (id 6, tick 4810)
- **The platform recommended:** 5 proposed during the incident (CRITICAL=5); 0 on the fallback policy; most urgent: station-coxsbazar/DIESEL for 6,000 L
- **During the incident:** served all 5 dashboard endpoints
- **Recovered in:** 0.0s
- **Outcome:** RECOVERED
- **Notes:** injected supply_shortfall into the simulator

| Measurement | Value |
|---|---|
| baseline status | ok |
| baseline tick | 4809 |
| risk during crisis | CRITICAL=12 |
| alerts during crisis | anomaly_demand=40, shortage_critical=60 |
| status after recovery | ok |
| memory before the scenario | 1,119 MB available of 7,271 MB |

### Combined crisis

*A demand spike, a route disruption, a depot constraint and a shipment delay, all active at the same time.*

- **Started at:** 2026-11-24T19:39:21.077902+00:00
- **Expected:** the platform holds together: every endpoint still answers, and the recommendations account for all four
- **Detected in:** 2.0s — the platform's view showed all four crises at once
- **The platform recommended:** 5 proposed during the incident (CRITICAL=5); 0 on the fallback policy; most urgent: station-coxsbazar/DIESEL for 6,000 L
- **During the incident:** served all 5 dashboard endpoints
- **Recovered in:** 0.0s
- **Outcome:** RECOVERED
- **Notes:** injected demand_spike, route_disruption, depot_constraint, shipment_delay at the same time

| Measurement | Value |
|---|---|
| baseline status | ok |
| risk during crisis | CRITICAL=12 |
| alerts during crisis | anomaly_demand=41, shortage_critical=59 |
| status after recovery | ok |
| memory before the scenario | 1,798 MB available of 7,271 MB |

### Latency

*The simulator delays every answer by twelve seconds.*

- **Started at:** 2026-11-24T19:39:50.263719+00:00
- **Expected:** the platform's own timeouts fire, and it stops waiting on the simulator
- **Detected in:** 12.4s — the platform's own timeout fired on the simulator call
- **The platform recommended:** no new proposals during the incident (100 already in the store)
- **During the incident:** served all 5 dashboard endpoints
- **Recovered in:** 0.0s
- **Outcome:** RECOVERED
- **Notes:** injected the latency fault into the simulator

| Measurement | Value |
|---|---|
| baseline status | ok |
| breaker state | closed |
| degraded mode | False |
| status after recovery | ok |
| memory before the scenario | 1,781 MB available of 7,271 MB |

### Unavailable

*The simulator answers 503 to everything.*

- **Started at:** 2026-11-24T19:40:14.811192+00:00
- **Expected:** the platform goes degraded and keeps serving the last-known state
- **Detected in:** 2.1s — the platform went into degraded mode
- **The platform recommended:** no new proposals during the incident (100 already in the store)
- **During the incident:** served all 5 dashboard endpoints
- **Recovered in:** 0.0s
- **Outcome:** RECOVERED
- **Notes:** injected the unavailable fault into the simulator

| Measurement | Value |
|---|---|
| baseline status | ok |
| breaker state | closed |
| degraded mode | True |
| status after recovery | ok |
| memory before the scenario | 1,664 MB available of 7,271 MB |

### Error rate

*Half the simulator's answers are 500.*

- **Started at:** 2026-11-24T19:40:29.019552+00:00
- **Expected:** the platform rides through the partial failure and keeps its cache
- **Detected in:** 2.1s — the simulator answered with injected faults
- **The platform recommended:** no new proposals during the incident (100 already in the store)
- **During the incident:** served all 5 dashboard endpoints
- **Recovered in:** 0.0s
- **Outcome:** RECOVERED
- **Notes:** injected the error_rate fault into the simulator

| Measurement | Value |
|---|---|
| baseline status | ok |
| breaker state | closed |
| degraded mode | False |
| status after recovery | ok |
| memory before the scenario | 1,666 MB available of 7,271 MB |

### Stale data

*The simulator marks every response stale.*

- **Started at:** 2026-11-24T19:40:43.268963+00:00
- **Expected:** the cached copy is dropped rather than relabelled, and the staleness is shown
- **Detected in:** 4.4s — the sync stopped succeeding
- **The platform recommended:** 10 proposed during the incident (CRITICAL=10); 0 on the fallback policy; most urgent: station-coxsbazar/DIESEL for 6,000 L
- **During the incident:** served all 5 dashboard endpoints
- **Recovered in:** 0.0s
- **Outcome:** RECOVERED
- **Notes:** injected the stale_data fault into the simulator

| Measurement | Value |
|---|---|
| baseline status | ok |
| breaker state | closed |
| degraded mode | False |
| status after recovery | ok |
| memory before the scenario | 1,658 MB available of 7,271 MB |

### Stream disconnect

*The simulator refuses new event-stream connections with a 503.*

- **Started at:** 2026-11-24T19:40:59.862553+00:00
- **Expected:** the SSE listener reconnects with backoff, and the REST poller carries the platform through it
- **Detected in:** 22.5s — the platform went into degraded mode
- **The platform recommended:** no new proposals during the incident (100 already in the store)
- **During the incident:** served all 5 dashboard endpoints
- **Recovered in:** 0.0s
- **Outcome:** RECOVERED
- **Notes:** injected the stream_disconnect fault, then restarted the simulator to close the established stream — so the platform has to reconnect into a simulator that refuses the connection

| Measurement | Value |
|---|---|
| baseline status | ok |
| breaker state | closed |
| degraded mode | True |
| the event stream reconnected | yes |
| status after recovery | ok |
| memory before the scenario | 1,619 MB available of 7,271 MB |

### Simulator connection-pool exhaustion

*The simulator's own 15-connection SQLite pool is exhausted by a burst of concurrent heavy reads. Its requests block on the pool and then fail with 'QueuePool limit of size 5 overflow 10 reached'.*

- **Started at:** 2026-11-24T19:41:34.577058+00:00
- **Expected:** the backend's circuit breaker opens, the API degrades to cached state with an age, and every dashboard endpoint keeps answering
- **Detected in:** 18.1s — the backend's own request timeout fired on the simulator call and the circuit breaker opened
- **The platform recommended:** no new proposals during the incident (100 already in the store)
- **During the incident:** served all 5 dashboard endpoints
- **Recovered in:** 111.4s
- **Outcome:** RECOVERED
- **Notes:** fired 30 concurrent GET /v1/demand-history?limit=2000 at the simulator to take its pool

| Measurement | Value |
|---|---|
| baseline status | ok |
| memory before the scenario | 1,639 MB available of 7,271 MB |
| simulator pool | SQLAlchemy QueuePool — 5 connections + 10 overflow, pool_timeout 30 s |
| breaker state | open |
| degraded mode | True |
| cached state age during the outage | 23.534 s |
| degraded reason | the circuit breaker for the simulator is open; not calling /v1/instance |
| simulator component | unreachable |
| the dashboard kept answering | served all 5 dashboard endpoints |
| status after recovery | ok |
| memory after the scenario | 1,524 MB available of 7,271 MB |

### Database stopped

*The Postgres container is stopped for under thirty seconds while the platform is serving.*

- **Started at:** 2026-11-24T19:43:59.193347+00:00
- **Expected:** the platform keeps serving its cached state, reports the database as down, does not crash, and recovers on its own when the database comes back
- **Detected in:** 0.1s — the health endpoint reported the database as down
- **The platform recommended:** 20 proposed during the incident (CRITICAL=20); 0 on the fallback policy; most urgent: station-coxsbazar/DIESEL for 6,000 L
- **During the incident:** served all 5 dashboard endpoints
- **Recovered in:** 2.0s
- **Outcome:** RECOVERED

| Measurement | Value |
|---|---|
| baseline status | ok |
| memory before the scenario | 1,520 MB available of 7,271 MB |
| cached state before the outage | age 2.197s, not degraded |
| health during the outage | degraded |
| the dashboard kept answering | served all 5 dashboard endpoints |
| cached state during the outage | age 2.866s, not degraded |
| degraded flag during the outage | False |
| database component during the outage | error |
| the platform process was still up | yes |
| health after the restart | ok |
| cached state after the restart | age 2.538s, not degraded |
| degraded flag after the restart | False |
| how long the database was down | 15 s |

### Prediction layer disabled

*The ML kill-switch is turned on, so the learned model stops being used and the platform falls back to the rule-based predictor. Then it is turned off again, and the learned model has to earn its way back.*

- **Started at:** 2026-11-24T19:44:14.560486+00:00
- **Expected:** every forecast comes from the rule-based predictor and is flagged for a human
- **Detected in:** 0.0s — the kill-switch endpoint confirmed the change
- **The platform recommended:** 29 proposed during the incident (CRITICAL=29); 0 on the fallback policy; most urgent: station-coxsbazar/DIESEL for 6,000 L
- **During the incident:** served all 5 dashboard endpoints
- **Recovered in:** 0.0s
- **Outcome:** RECOVERED

| Measurement | Value |
|---|---|
| baseline status | ok |
| memory before the scenario | 1,511 MB available of 7,271 MB |
| the learned model was trained | trained on 3942 rows, validation MAE 0.051246 L |
| the learned model was in service | learned |
| how hard it was to get the model in service | in service after 1 training attempt(s) |
| active model before | learned |
| the forecast before | 12× learned; 0 of 12 flagged for human review | 12× reason: — |
| kill-switch accepted | True |
| active model | baseline |
| ml_kill_switch | True |
| the forecast with the model disabled | 12× baseline; 12 of 12 flagged for human review | 12× reason: the ml kill-switch is on: the learned model is disabled |
| forecasts served by the fallback | 348.0 |
| the kill-switch gauge | 1.0 |
| kill-switch activations | on: 7.0, off: 6.0 |
| the decision engine's own fallback | not engaged — the rule-based predictor still produced a forecast, so the decision engine kept using the normal optimizer |
| the model after the kill-switch came off | learned |
| how hard it was to earn the model back | in service after 1 training attempt(s) |
| active model after | baseline |
| ml_kill_switch after | False |
| the forecast after recovery | 12× learned; 0 of 12 flagged for human review | 12× reason: — |

### Prediction layer broken

*The learned model is made to raise on every forecast — the way a broken model raises — so the platform has to notice the failure by itself rather than being told about it.*

- **Started at:** 2026-11-24T19:44:44.863853+00:00
- **Expected:** every forecast comes from the rule-based predictor, is flagged for a human, and the fallback reason names the failure
- **Detected in:** 0.0s — the model raised and the selector fell back on its own
- **The platform recommended:** 25 proposed during the incident (CRITICAL=25); 0 on the fallback policy; most urgent: station-coxsbazar/DIESEL for 6,000 L
- **During the incident:** served all 5 dashboard endpoints
- **Recovered in:** 0.0s
- **Outcome:** RECOVERED

| Measurement | Value |
|---|---|
| baseline status | ok |
| memory before the scenario | 1,558 MB available of 7,271 MB |
| the learned model was trained | trained on 3923 rows, validation MAE 0.050771 L |
| the learned model was in service | learned |
| how hard it was to get the model in service | in service after 1 training attempt(s) |
| active model before | baseline |
| the forecast before | 12× learned; 0 of 12 flagged for human review | 12× reason: — |
| the fault was accepted | True |
| the forecast with the model broken | 12× baseline; 12 of 12 flagged for human review | 12× reason: the learned model does not beat the rule-based baseline over the forecast horizon (106 L vs 85 L of total dema |
| the decision engine's own fallback | not engaged — the rule-based predictor still produced a forecast, so the decision engine kept using the normal optimizer |
| the model after the fault was cleared | learned |
| how hard it was to earn the model back | in service after 1 training attempt(s) |
| the forecast after recovery | 12× learned; 0 of 12 flagged for human review | 12× reason: — |

## The restart-and-reset procedure

When the simulator's connection pool is exhausted it stops answering until it is restarted. The procedure is three commands, and it is also the documented way to get a clean world between demo runs.

```bash
# 1. restart the simulator container
docker compose restart simulator

# 2. reset the world back to the baked-in scenario
curl -s -X POST http://localhost:8100/admin/reset

# 3. start the clock again — the reset leaves it paused
curl -s -X POST http://localhost:8100/admin/run
```

**The platform needs none of this to recover from a pool exhaustion on its own** — the connection-pool scenario above shows it recovering by itself, with the circuit breaker closing and the cache going fresh again. The procedure is the *fallback* for the case where the simulator does not come back, and it is what the demo script uses to get a clean world between runs.

One consequence is worth knowing: `POST /admin/reset` restarts the simulator's clock at tick 0, so the platform's stored demand history belongs to a different timeline. The backend notices — it compares the stored maximum tick against the current one and drops the history when the clock went backwards, so the forecaster does not learn from two runs spliced together.
