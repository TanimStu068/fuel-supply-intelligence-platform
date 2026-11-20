# Day 2 — Data Layer, Live State Sync, Logging and Metrics

**Date:** Day 2 of 7 · BUP CSE Fest 2026 Hackathon Finals
**Goal:** reliable data ingestion, stored state, and the start of observability.

---

## Definition of done — status

| Requirement | Status | Evidence |
|---|---|---|
| Postgres schema + Alembic migrations | ✅ | 8 tables + `alembic_version`; `alembic upgrade head` applied |
| Background sync: REST poll as source of truth | ✅ | `/api/state` reads the cache, never the simulator |
| SSE listener for `/v1/stream` | ✅ | 3 `simulation.tick` events observed driving syncs |
| SSE treated as a hint, re-GET on every event | ✅ | `_handle_event` re-reads only the affected resource |
| Auto-reconnect with backoff | ✅ | `sse_reconnects_total{reason="unreachable"} 3` during the drill |
| Full refetch after every reconnect | ✅ | `test_stream_reconnect_triggers_a_full_rest_refetch` |
| 15 s silence is not a disconnect | ✅ | No idle timeout; only a real error reconnects |
| `stream_disconnect` fault (503) handled | ✅ | `test_stream_fault_503_is_a_reconnect_not_a_crash` |
| Stale header invalidates the cache and marks the API | ✅ | Backend reported `stale: True` during the fault run |
| Incremental demand-history ingest with a limit | ✅ | 108 rows in Postgres |
| De-duplication by (station, fuel, tick) | ✅ | 108 rows / 108 distinct keys across repeated cycles |
| Cached-state fallback with a degraded flag and age | ✅ | `degraded: true`, `age_seconds: 46.301` while the simulator was down |
| Structured JSON logging with correlation IDs | ✅ | Every log line is one JSON object with a `correlation_id` |
| Prometheus metrics for all seven series | ✅ | See the metric list below |
| Read APIs over the stored state | ✅ | 8 new endpoints |
| Tests for SSE reconnect, stale, degraded | ✅ | 107 unit + 14 integration + 10 fault checks |
| Real fault test via `POST /admin/faults` | ✅ | [`docs/fault-tests.md`](docs/fault-tests.md) |
| **Backend survives the simulator being stopped** | ✅ | Degraded in 5 s, recovered in 5 s |

---

## What was done

### 1. Postgres schema — Alembic owns it now

Eight tables replace the two placeholders from Day 1.

| Table | Purpose | Key property |
|---|---|---|
| `state_snapshots` | One row per sync cycle | Append-only history |
| `inventory_snapshots` | Station and depot inventory per tick | Unique on `(entity_type, entity_id, sim_tick, fuel_type)` |
| `demand_observations` | The demand time series | Unique on `(station_id, fuel_type, tick)` |
| `allocations` | Mirror of the simulator's ledger | Unique on `simulator_id` |
| `domain_events` | Mirror of the simulator's events | Unique on `simulator_id` |
| `alerts` | Platform-generated alerts | Unique on `(kind, entity_id, sim_tick)` |
| `decision_records` | Decision audit trail | Empty until the decision engine lands (Day 4) |
| `system_health` | Latest health per component | Unique on `component` |

Every write path is an **upsert**, so a replayed sync cycle changes nothing.
The unique constraints do the de-duplication; the code never has to.

Two decisions worth flagging:

- **Alembic reads the URL from the environment**, not from `alembic.ini`. The
  committed file has no `sqlalchemy.url` at all, so no credential can leak
  into version control.
- **The container runs `alembic upgrade head` before serving**, and the step is
  allowed to fail — the service also creates any missing table at startup, so a
  migration hiccup never blocks the boot.

### 2. The background sync service

Two loops run side by side inside `backend/app/sync_service.py`.

**The poller** is the source of truth. Every `SYNC_POLL_INTERVAL` seconds
(default 5) it re-reads the entire world over REST in parallel, writes it to
Postgres, and replaces the cached snapshot.

**The SSE listener** is a hint channel. Every event triggers a re-read of the
affected resource:

| Event | Response |
|---|---|
| `simulation.tick` | Full refetch |
| `allocation.status_changed` | Re-read the allocation ledger only |
| `inventory.updated` | Re-read that one depot or station |
| `simulator.notice` | Full refetch |

The event payload is **never written to the database**. The simulator
documents that its per-subscriber queue is dropped silently after 200 events,
so the payload only decides *what to re-read*; the values always come from
REST.

The failure handling is where the guide's warnings mattered:

- **No `Last-Event-ID` replay.** After every reconnect the service does a
  full REST refetch, because anything that happened while disconnected exists
  only in the REST view.
- **Silence is not a disconnect.** The simulator sends a keepalive every 15
  seconds and nothing else when idle. There is no idle timeout on the reader;
  only a real transport error or a closed connection triggers a reconnect.
- **`stream_disconnect` answers 503 at connect time**, not mid-stream. The
  loop classifies it as `reason="fault"` and backs off.

### 3. The state cache and the degraded contract

`backend/app/state_cache.py` holds one consistent snapshot plus the metadata
that says how much to trust it. One slot, replaced wholesale — a partial
update would let a consumer see a mixture of two ticks, which is worse than a
slightly older consistent one.

Every API response carries:

```json
{
  "stale": false,
  "degraded": false,
  "age_seconds": 4.167,
  "degraded_reason": null,
  "last_sync_at": "2026-11-20T15:44:12.881000+00:00"
}
```

When the simulator is unreachable, `degraded` flips to `true`, `age_seconds`
keeps counting, and the last-known data is still served.

### 4. Structured logging

`backend/app/logging_config.py` renders every log line as a single JSON
object. Correlation IDs ride on a `contextvars.ContextVar`, so concurrent
tasks never overwrite each other's ID — each sync cycle and each SSE
reconnection gets its own.

```json
{"timestamp": "2026-11-20T15:30:34+0000", "level": "INFO", "logger": "app.sync_service",
 "message": "sse event received", "correlation_id": "sse-108961fc80f9",
 "event": "simulation.tick", "entity_id": null}
```

uvicorn's own handlers are routed through ours, so the container emits one
format end to end.

### 5. Prometheus metrics

| Metric | Type | What it answers |
|---|---|---|
| `http_requests_total` | counter | Request rate, by method/path/status |
| `http_request_duration_seconds` | histogram | Latency |
| `simulator_requests_total` | counter | Simulator call outcomes |
| `simulator_request_duration_seconds` | histogram | Simulator call latency |
| `simulator_stale_responses_total` | counter | How often the simulator said "stale" |
| `sync_cycles_total` | counter | Sync outcomes |
| `sync_cycle_duration_seconds` | histogram | Sync cycle cost |
| `sse_connections_total` | counter | Stream connections opened |
| `sse_reconnects_total` | counter | Reconnects, by reason |
| `sse_events_total` | counter | Events received, by name |
| `degraded_mode_activations_total` | counter | Times the platform *entered* degraded mode |
| `degraded_mode` | gauge | Currently degraded? |
| `stale_data` | gauge | Is the cached state stale? |
| `last_sync_age_seconds` | gauge | How old is the data? |
| `demand_observations_ingested_total` | counter | Demand rows ingested |

`degraded_mode_activations_total` increments **once per entry**, not once per
failed poll — the counter is a usable alert signal rather than a noise
generator.

### 6. New API endpoints

All of them read from the stored state, not from the simulator.

| Endpoint | Returns |
|---|---|
| `GET /api/stations` | Stations |
| `GET /api/depots` | Depots |
| `GET /api/routes` | Routes |
| `GET /api/events` | Domain events |
| `GET /api/supply-arrivals` | Scheduled arrivals |
| `GET /api/allocations` | The allocation ledger |
| `GET /api/history/{station_id}` | Demand history from Postgres |
| `GET /api/alerts` | Platform alerts |

`/api/state` now also reads from the cache, and carries the same freshness
metadata as everything else.

---

## The definition of done, proven

`scripts/degraded_mode_drill.sh` stops the simulator **container** mid-run:

```
=== 2. Stopping the simulator container ===
    degraded after 5s
  ok    sync degraded flag                                   True
  ok    api still answers                                    200
  ok    served payload flagged degraded                      True
  info  last-known state age: 20.916s
  info  stations still served: 4

=== 3. Restarting the simulator container ===
    recovered after 5s
  ok    recovered automatically                              yes (5s)
  ok    sync degraded flag                                   False
  ok    health status                                        ok
```

Eight checks, all passing. The full transcript, the fault matrix and eight
findings about the simulator's fault behaviour are in
[`docs/fault-tests.md`](../fault-tests.md).

---

## Bugs and surprises the verification caught

| # | What happened | Why it mattered | Fix |
|---|---|---|---|
| 1 | The **autogenerated Alembic migration was missing two tables** (`state_snapshots`, `decision_records`) | They already existed in the live database, so Alembic's diff against it saw nothing to create. A fresh database would have been missing them entirely. | Dropped the Day 1 tables, regenerated against an empty schema, and verified all 8 tables exist after `upgrade head`. |
| 2 | The **fault script hung forever** on `GET /v1/stream` | `curl` waits for a stream that never ends — the first version of the script had no timeout and blocked the whole run. | Added `--max-time 5` to the check helper. |
| 3 | The **integration suite failed with a 409** on the allocation round trip | Not a code bug: my own fault-test had left `station-mirpur` in `OUTAGE`, and the simulator correctly refused to allocate to a closed station. | `POST /admin/reset` and re-ran: 14/14 pass. The 409 was the simulator being right. |
| 4 | The **demand-history table was empty** after the first deploy | The ingest existed as a method but was never called from the sync loop — it was only reachable from a test. | Wired `_ingest_demand_history` into every sync cycle. 108 rows now land and stay at 108. |
| 5 | The **`alerts` table stayed empty** | Same shape of bug: the alert logic was written but never invoked. | Wired `_raise_alerts` into the sync cycle; verified with a real `station_outage` injection. |
| 6 | The **old `test_app.py` contradicted the new architecture** | It asserted `/api/state` calls the simulator directly — exactly what Day 2 replaced. | Replaced with `test_api.py`, which asserts the cached-state contract. |

The last three share one root cause worth naming: **code that exists but is
never called looks identical to code that works.** Each of them was found by
looking at the database, not by running the unit tests. The lesson for the
remaining days: assert on the *stored* state, not just on the return value.

---

## How to run

### The whole stack

```bash
cp .env.example .env          # change the passwords
docker compose up --build
```

| Service | URL |
|---|---|
| Backend API | http://localhost:8001 |
| Simulator | http://localhost:8000 (`/admin` console, `/docs`) |
| Frontend | http://localhost:5173 |
| Prometheus | http://localhost:9090 |
| Grafana | http://localhost:3000 → `admin` / `.env` |

### Verify Day 2

```bash
curl -s http://localhost:8001/health | jq '.sync'
curl -s http://localhost:8001/api/stations | jq '{degraded, stale, age_seconds}'

# the fault matrix and the definition-of-done drill
./scripts/fault_test.sh
./scripts/degraded_mode_drill.sh
```

### Migrations

```bash
cd backend
alembic upgrade head          # apply
alembic revision --autogenerate -m "describe the change"
```

The URL comes from `DATABASE_URL` in the environment.

---

## Blockers and risks

**No hard blockers.** The simulator behaves as documented apart from the ten
discrepancies already recorded, and the eight new findings in
`docs/fault-tests.md`.

| Risk | Impact | Mitigation |
|---|---|---|
| The SSE queue drops slow subscribers after 200 events, **silently** | A missed event looks like a state change that never happened | Every reconnect triggers a full REST refetch; the payload is never trusted |
| The poll interval is 5 s but the simulator ticks at ~6.7/s | The cache is up to a tick behind | The SSE listener triggers an immediate refetch on every tick event |
| `demand-history` grows without bound in the simulator | An unlimited pull would eventually time out | Always pulled with `limit`; de-duplicated on insert |
| Postgres is a single point of failure | A DB outage means no new writes | Writes already fail soft — the platform keeps serving from the cache. **The DB-down path is unit-tested but not yet drilled live** |

### Environment notes

- Ports `8000`, `5432` and `3000` were already taken on the development
  machine by unrelated projects, so the local `.env` moves the simulator to
  `8100`, Postgres to `55432` and Grafana to `33000`. The committed defaults
  are the standard ports.
- The cross-service suite resolves its target URLs the same way — environment
  variable, then `.env`, then the committed default — so it never talks to an
  unrelated app that happens to own the default port.

---

## Next — Day 3

1. **The intelligence layer.** Forecast demand per station per fuel from the
   stored history we now have, and score shortage risk.
2. **Shortage detection.** Compare the forecast against current inventory and
   raise a risk alert — the `alerts` table already exists and the machinery
   that writes to it is proven.
3. **Backfill the history.** Run the simulator long enough to build a
   multi-hundred-tick series so the forecast has something to learn from.
4. **Grafana dashboards.** The metrics series exist; the panels that read
   them do not.
5. **A real load test.** `k6` against the read endpoints, with the simulator
   faulted, to prove the cache actually absorbs the load.
