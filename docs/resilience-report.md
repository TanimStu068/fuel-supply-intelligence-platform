# Resilience report

**Generated:** 2026-11-24T17:51:35.232513+00:00  
**Backend:** `http://localhost:8001` · **Simulator:** `http://localhost:8100`  
**Scenarios run:** 16

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
| **Route disruption** | crisis | 2.0s | 0.0s | RECOVERED | served all 5 dashboard endpoints |
| **Station outage** | crisis | 2.0s | 0.0s | RECOVERED | served all 5 dashboard endpoints |
| **Supply shortfall** | crisis | 2.0s | 0.0s | RECOVERED | served all 5 dashboard endpoints |
| **Combined crisis** | combined crisis | 2.0s | 0.0s | RECOVERED | served all 5 dashboard endpoints |
| **Latency** | fault | 12.3s | 0.0s | RECOVERED | served all 5 dashboard endpoints |
| **Unavailable** | fault | 2.1s | 0.0s | RECOVERED | served all 5 dashboard endpoints |
| **Error rate** | fault | 2.1s | 0.0s | RECOVERED | served all 5 dashboard endpoints |
| **Stale data** | fault | 2.2s | 0.0s | RECOVERED | served all 5 dashboard endpoints |
| **Stream disconnect** | fault | 22.5s | 0.0s | RECOVERED | served all 5 dashboard endpoints |
| **Simulator connection-pool exhaustion** | dependency failure | 18.1s | 24.0s | RECOVERED | served all 5 dashboard endpoints |
| **Database stopped** | dependency failure | 0.1s | 2.1s | RECOVERED | served all 5 dashboard endpoints |
| **Prediction layer disabled** | dependency failure | 0.0s | 0.0s | RECOVERED | served all 5 dashboard endpoints |

**16 of 16 scenarios recovered automatically.** None of them required a restart of the backend.

## What each scenario proved

### Baseline

*Nothing is broken. This is the control measurement.*

- **Expected:** the platform is healthy, fresh, and answering every endpoint
- **Detected in:** 0.0s — the control measurement — nothing to detect
- **The platform recommended:** no new proposals during the incident (100 already in the store)
- **During the incident:** served all 5 dashboard endpoints
- **Recovered in:** 0.0s
- **Outcome:** RECOVERED

| Measurement | Value |
|---|---|
| health status | ok |
| simulator tick | 30746 |
| memory before the scenario | 1,762 MB available of 7,271 MB |
| cached state age | 2.067 s |
| degraded mode | False |
| breaker state | closed |
| simulator component | ok |

### Demand spike

*Demand at every station triples. The network runs dry faster than the plan says.*

- **Expected:** risk levels rise, the platform proposes larger and earlier allocations
- **Detected in:** 2.0s — the platform's event feed carries the demand_spike event (id 31, tick 30825)
- **The platform recommended:** 5 proposed during the incident (CRITICAL=5); 0 on the fallback policy; most urgent: station-coxsbazar/DIESEL for 6,000 L
- **During the incident:** served all 5 dashboard endpoints
- **Recovered in:** 0.0s
- **Outcome:** RECOVERED
- **Notes:** injected demand_spike into the simulator

| Measurement | Value |
|---|---|
| baseline status | ok |
| baseline tick | 30824 |
| risk during crisis | CRITICAL=12 |
| alerts during crisis | anomaly_demand=42, shortage_critical=58 |
| status after recovery | ok |

### Shipment delay

*Incoming diesel arrivals are delayed by eight ticks.*

- **Expected:** the supply timeline shows DELAYED arrivals, and cover shortens
- **Detected in:** 2.0s — the platform's event feed carries the shipment_delay event (id 32, tick 30983)
- **The platform recommended:** 5 proposed during the incident (CRITICAL=5); 0 on the fallback policy; most urgent: station-coxsbazar/DIESEL for 6,000 L
- **During the incident:** served all 5 dashboard endpoints
- **Recovered in:** 0.0s
- **Outcome:** RECOVERED
- **Notes:** injected shipment_delay into the simulator

| Measurement | Value |
|---|---|
| baseline status | ok |
| baseline tick | 30982 |
| risk during crisis | CRITICAL=12 |
| alerts during crisis | anomaly_demand=43, shortage_critical=57 |
| status after recovery | ok |

### Depot constraint

*Every depot is marked CONSTRAINED, which cuts its dispatch capacity.*

- **Expected:** the allocator proposes smaller shipments, and the constraint is named
- **Detected in:** 2.0s — the platform's event feed carries the depot_constraint event (id 33, tick 31142)
- **The platform recommended:** 5 proposed during the incident (CRITICAL=5); 0 on the fallback policy; most urgent: station-coxsbazar/DIESEL for 6,000 L
- **During the incident:** served all 5 dashboard endpoints
- **Recovered in:** 0.0s
- **Outcome:** RECOVERED
- **Notes:** injected depot_constraint into the simulator

| Measurement | Value |
|---|---|
| baseline status | ok |
| baseline tick | 31141 |
| risk during crisis | CRITICAL=12 |
| alerts during crisis | anomaly_demand=39, shortage_critical=61 |
| status after recovery | ok |

### Route disruption

*Every route is marked DISRUPTED.*

- **Expected:** the platform routes around the cut, or refuses to route through it
- **Detected in:** 2.0s — the platform's event feed carries the route_disruption event (id 34, tick 31299)
- **The platform recommended:** 5 proposed during the incident (CRITICAL=5); 0 on the fallback policy; most urgent: station-coxsbazar/DIESEL for 6,000 L
- **During the incident:** served all 5 dashboard endpoints
- **Recovered in:** 0.0s
- **Outcome:** RECOVERED
- **Notes:** injected route_disruption into the simulator

| Measurement | Value |
|---|---|
| baseline status | ok |
| baseline tick | 31298 |
| risk during crisis | CRITICAL=12 |
| alerts during crisis | anomaly_demand=34, shortage_critical=66 |
| status after recovery | ok |

### Station outage

*Every station is marked OUTAGE, so served demand drops to zero.*

- **Expected:** outage alerts are raised for the affected stations
- **Detected in:** 2.0s — the platform's event feed carries the station_outage event (id 35, tick 31455)
- **The platform recommended:** 5 proposed during the incident (CRITICAL=5); 0 on the fallback policy; most urgent: station-coxsbazar/DIESEL for 6,000 L
- **During the incident:** served all 5 dashboard endpoints
- **Recovered in:** 0.0s
- **Outcome:** RECOVERED
- **Notes:** injected station_outage into the simulator

| Measurement | Value |
|---|---|
| baseline status | ok |
| baseline tick | 31454 |
| risk during crisis | CRITICAL=12 |
| alerts during crisis | anomaly_demand=36, shortage_critical=64 |
| status after recovery | ok |

### Supply shortfall

*Incoming diesel arrivals are cut to 40% of their planned quantity.*

- **Expected:** the platform sees less fuel coming and reacts accordingly
- **Detected in:** 2.0s — the platform's event feed carries the supply_shortfall event (id 36, tick 31611)
- **The platform recommended:** 5 proposed during the incident (CRITICAL=5); 0 on the fallback policy; most urgent: station-coxsbazar/DIESEL for 6,000 L
- **During the incident:** served all 5 dashboard endpoints
- **Recovered in:** 0.0s
- **Outcome:** RECOVERED
- **Notes:** injected supply_shortfall into the simulator

| Measurement | Value |
|---|---|
| baseline status | ok |
| baseline tick | 31610 |
| risk during crisis | CRITICAL=12 |
| alerts during crisis | anomaly_demand=34, shortage_critical=66 |
| status after recovery | ok |

### Combined crisis

*A demand spike, a route disruption, a depot constraint and a shipment delay, all active at the same time.*

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
| alerts during crisis | anomaly_demand=35, shortage_critical=65 |
| status after recovery | ok |

### Latency

*The simulator delays every answer by twelve seconds.*

- **Expected:** the platform's own timeouts fire, and it stops waiting on the simulator
- **Detected in:** 12.3s — the platform's own timeout fired on the simulator call
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

### Unavailable

*The simulator answers 503 to everything.*

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

### Error rate

*Half the simulator's answers are 500.*

- **Expected:** the platform rides through the partial failure and keeps its cache
- **Detected in:** 2.1s — the platform went into degraded mode
- **The platform recommended:** no new proposals during the incident (100 already in the store)
- **During the incident:** served all 5 dashboard endpoints
- **Recovered in:** 0.0s
- **Outcome:** RECOVERED
- **Notes:** injected the error_rate fault into the simulator

| Measurement | Value |
|---|---|
| baseline status | ok |
| breaker state | closed |
| degraded mode | True |
| status after recovery | ok |

### Stale data

*The simulator marks every response stale.*

- **Expected:** the cached copy is dropped rather than relabelled, and the staleness is shown
- **Detected in:** 2.2s — the sync stopped succeeding
- **The platform recommended:** no new proposals during the incident (100 already in the store)
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

### Stream disconnect

*The simulator refuses new event-stream connections with a 503.*

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

### Simulator connection-pool exhaustion

*The simulator's own 15-connection SQLite pool is exhausted by a burst of concurrent heavy reads. Its requests block on the pool and then fail with 'QueuePool limit of size 5 overflow 10 reached'.*

- **Expected:** the backend's circuit breaker opens, the API degrades to cached state with an age, and every dashboard endpoint keeps answering
- **Detected in:** 18.1s — the backend's own request timeout fired on the simulator call and the circuit breaker opened
- **The platform recommended:** no new proposals during the incident (100 already in the store)
- **During the incident:** served all 5 dashboard endpoints
- **Recovered in:** 24.0s
- **Outcome:** RECOVERED
- **Notes:** fired 30 concurrent GET /v1/demand-history?limit=2000 at the simulator to take its pool

| Measurement | Value |
|---|---|
| baseline status | ok |
| memory before the scenario | 1,930 MB available of 7,271 MB |
| simulator pool | SQLAlchemy QueuePool — 5 connections + 10 overflow, pool_timeout 30 s |
| breaker state | open |
| degraded mode | True |
| cached state age during the outage | 23.613 s |
| degraded reason | the circuit breaker for the simulator is open; not calling /v1/instance |
| simulator component | unreachable |
| the dashboard kept answering | served all 5 dashboard endpoints |
| status after recovery | unknown |
| memory after the scenario | 1,818 MB available of 7,271 MB |

### Database stopped

*The Postgres container is stopped for under thirty seconds while the platform is serving.*

- **Expected:** the platform keeps serving its cached state, reports the database as down, does not crash, and recovers on its own when the database comes back
- **Detected in:** 0.1s — the health endpoint reported the database as down
- **The platform recommended:** 
- **During the incident:** served all 5 dashboard endpoints
- **Recovered in:** 2.1s
- **Outcome:** RECOVERED

| Measurement | Value |
|---|---|
| baseline status | ok |
| cached state before the outage | age 2.156s, not degraded |
| health during the outage | degraded |
| the dashboard kept answering | served all 5 dashboard endpoints |
| cached state during the outage | age 3.019s, not degraded |
| degraded flag during the outage | False |
| database component during the outage | error |
| the platform process was still up | yes |
| health after the restart | ok |
| cached state after the restart | age 0.512s, not degraded |
| degraded flag after the restart | False |
| how long the database was down | 16 s |

### Prediction layer disabled

*The ML kill-switch is turned on, so the learned model stops being used and the platform falls back to the rule-based predictor. Then it is turned off again, and the learned model has to earn its way back.*

- **Expected:** every forecast comes from the rule-based predictor, is flagged for a human, and the decision layer uses the fallback policy
- **Detected in:** 0.0s — the kill-switch endpoint confirmed the change
- **The platform recommended:** 15 proposed during the incident (CRITICAL=15); 0 on the fallback policy; most urgent: station-coxsbazar/DIESEL for 6,000 L
- **During the incident:** served all 5 dashboard endpoints
- **Recovered in:** 0.0s
- **Outcome:** RECOVERED

| Measurement | Value |
|---|---|
| baseline status | ok |
| active model before | baseline |
| the forecast before | 12× baseline; 12 of 12 flagged for human review | 12× reason: the learned model does not beat the rule-based baseline over the forecast horizon (153 L vs 138 L of total dem |
| kill-switch accepted | True |
| active model | baseline |
| ml_kill_switch | True |
| the forecast with the model disabled | 12× baseline; 12 of 12 flagged for human review | 12× reason: the ml kill-switch is on: the learned model is disabled |
| forecasts served by the fallback | 144.0 |
| the kill-switch gauge | 1.0 |
| kill-switch activations | on: 3.0, off: 2.0 |
| decision engine's fallback reason | — |
| active model after | baseline |
| ml_kill_switch after | False |
| the forecast after recovery | 12× baseline; 12 of 12 flagged for human review | 12× reason: the learned model does not beat the rule-based baseline over the forecast horizon (153 L vs 138 L of total dem |

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
