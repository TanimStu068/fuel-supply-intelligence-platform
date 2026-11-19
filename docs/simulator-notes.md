# Simulator Notes — Observed Behaviour vs. the Integration Guide

**Date:** Day 1, BUP CSE Fest 2026
**Simulator:** `asifmahmoud414/bup-fuel-supply-simulator:1.0.0`
**Digest:** `sha256:7067050693f49d377d91ca91f2faa6e63f673f69a69429d4693e78e9c0598e92`
**Guide:** `docs/reference/BUP_Fuel_Supply_Simulator_Integration_Guide_Final.pdf`

Everything below was produced by running the published image locally and
exercising every documented endpoint. Where the guide and the running
simulator disagree, **the running simulator wins** and the client follows it.

The simulator's own source was read out of the image (read-only, for
verification only — the challenge forbids modifying it). A verbatim copy is
kept under `docs/reference/extracted/simulator_src/` for reference.

---

## 1. Headline findings

| # | Area | Guide says | Actually observed | Impact |
|---|------|-----------|-------------------|--------|
| D1 | `sim_time` timezone | `2026-01-01T00:00:00+00:00` | `2026-01-01T00:00:00` — **no offset** | Medium |
| D2 | Idempotent replay status | "200 OK — idempotent replay" | Always **`201`** | Low |
| D3 | Fault row shape | `duration_seconds` field | Row has **`start_wall_time`/`end_wall_time`**, no duration | Low |
| D4 | `entity_type` / `entity_id` | Non-null strings | **`null`** for instance-level actions | Medium |
| D5 | `/admin/audit?limit` | Clamped to `[1, 1000]` | `limit=0` returns **0 rows** (not clamped to 1) | Low |
| D6 | `/v1/demand-history?limit` | Clamped to `[1, 2000]` | `limit=0` returns **0 rows** (not clamped to 1) | Low |
| D7 | `X-Simulator-Stale` scope | "on `/v1/*` GETs" | Only on **GET**; `POST` responses never carry it | Low |
| D8 | Tick rate | 8 ticks/sec | **~6.7 ticks/sec** observed | Low |
| D9 | Undocumented endpoints | — | `/admin/stats` exists and returns row counts | Info |
| D10 | `GET /v1/regions` | §4.4 (numbering skips §4.3) | Endpoint exists, section mis-numbered in the guide | Info |

---

## 2. Timestamps are naive, not UTC-aware (D1)

Every `sim_time` the simulator emits is a **naive** ISO-8601 string with no
offset, even though the guide shows `+00:00`:

```
$ curl -s localhost:8100/v1/instance
{"id":1,...,"sim_time":"2026-01-01T00:00:00","tick":0,"tick_minutes":15,"status":"PAUSED"}
```

The cause is visible in the image's `app/main.py`:

```python
def serialize(obj):
    for c in obj.__table__.columns:
        v = getattr(obj, c.name)
        if isinstance(v, datetime): v = v.isoformat()   # naive -> no offset
```

The column is `DateTime(timezone=True)`, but SQLite does not persist a tz
offset, so the value comes back naive and `isoformat()` omits it.

**How we handle it:** `backend/app/simulator_models.py` defines

```python
SimDateTime = Annotated[datetime, AfterValidator(_assume_utc)]
```

which anchors any offset-less timestamp to UTC. Every `sim_time` field in the
client is typed `SimDateTime`, so downstream code never has to think about it.

> **Note:** `wall_time` in the audit log *does* carry an offset
> (`2026-11-19T15:48:33.803958+00:00`) because it is stamped in Python with
> `datetime.now(timezone.utc)`, not read back from SQLite. So the two clocks
> in the same payload are formatted differently — worth knowing when you diff
> them.

---

## 3. Allocation lifecycle (verified end to end)

### Create → replay → cancel

```
POST /v1/allocations  {"idempotency_key":"day1-001", ... "quantity":3000}
  -> 201 Created, {"id":1,"status":"PENDING","created_tick":0, ...}

POST /v1/allocations  (identical body, same key)
  -> 201 Created, {"id":1,"status":"PENDING", ...}      # same id, no new row

POST /v1/allocations/{id}/cancel
  -> 200 OK, {"id":1,"status":"CANCELLED", ...}
```

**D2 — the replay returns `201`, not `200`.** The guide's status-code cheat
sheet lists `200` for "idempotent replay of an existing allocation" and §5.4
says "Same key, same body → return existing allocation (HTTP 201, not 200)".
The implementation contradicts itself, and the code settles it:

```python
@app.post("/v1/allocations", status_code=201)
async def create_allocation(req, db):
    existing = db.query(Allocation).filter(...).first()
    if existing:
        if <same body>:
            return serialize(existing)      # still 201 — the decorator wins
        raise HTTPException(409, detail={"code": "IDEMPOTENCY_KEY_MISMATCH", ...})
```

Because the route is declared `status_code=201`, the replay inherits `201`.

**How we handle it:** the client accepts **both `200` and `201`** as success for
allocation writes (`SUCCESS_WRITE_CODES` in `backend/app/simulator_client.py`),
so it is correct against the guide *and* the running simulator.

### Error taxonomy — every documented code, observed

| Trigger | Status | Body | Client exception |
|---|---|---|---|
| Same key, different body | 409 | `{"detail":{"code":"IDEMPOTENCY_KEY_MISMATCH","message":"..."}}` | `IdempotencyKeyMismatchError` |
| Route connects other endpoints | 409 | `{"detail":{"code":"ROUTE_MISMATCH",...}}` | `RouteMismatchError` |
| Depot not OPEN/CONSTRAINED | 409 | `{"detail":{"code":"DEPOT_CLOSED",...}}` | `DepotClosedError` |
| Station not OPEN | 409 | `{"detail":{"code":"STATION_CLOSED",...}}` | `StationClosedError` |
| Route not AVAILABLE | 409 | `{"detail":{"code":"ROUTE_DISRUPTED",...}}` | `RouteDisruptedError` |
| quantity > max_shipment | 409 | `{"detail":{"code":"ROUTE_CAPACITY_EXCEEDED",...}}` | `RouteCapacityExceededError` |
| Depot short on fuel | 409 | `{"detail":{"code":"INSUFFICIENT_INVENTORY",...}}` | `InsufficientInventoryError` |
| Depot per-tick limit hit | 409 | `{"detail":{"code":"DISPATCH_CAPACITY_EXCEEDED",...}}` | `DispatchCapacityExceededError` |
| Station capacity overflow | 409 | `{"detail":{"code":"DESTINATION_CAPACITY_EXCEEDED",...}}` | `DestinationCapacityExceededError` |
| Cancel a non-PENDING row | 409 | `{"detail":{"code":"CANNOT_CANCEL",...}}` | `CannotCancelError` |
| Unknown depot/station/route | 404 | `{"detail":{"code":"NOT_FOUND"}}` | `EntityNotFoundError` |
| Cancel unknown id | 404 | `{"detail":{"code":"ALLOCATION_NOT_FOUND"}}` | `AllocationNotFoundError` |
| Bad body (FastAPI) | 422 | `{"detail":[{...}]}` — a **list** | `SimulatorAPIError` (422) |

All 409 codes are mapped to dedicated exception classes; an unrecognised code
falls back to `ConflictError` without crashing.

---

## 4. Both error envelope shapes are real (D7)

The guide is right that the simulator uses two shapes, and both are live:

```jsonc
// Injected fault (503) — note: under "error"
{"error": {"code": "FAULT_INJECTED", "message": "Simulator API temporarily unavailable."}}

// Domain conflict (409) — note: under "detail"
{"detail": {"code": "ROUTE_MISMATCH", "message": "Route does not connect selected depot and station"}}

// 404 — under "detail", and with NO message key at all
{"detail": {"code": "NOT_FOUND"}}
```

There is a third shape for FastAPI's own validation errors: `{"detail": [...]}`
where the value is a **list** of error objects.

**How we handle it:** `parse_error_detail()` in `backend/app/simulator_errors.py`
looks for `error` first, then `detail`, and only accepts an inner object that
carries a `code` key. The list-shaped `detail` therefore falls through to a
generic `SimulatorAPIError` with `code=None` instead of crashing.

---

## 5. Fault injection — verified against the live simulator

Faults are injected through `POST /admin/faults` and cleared with
`POST /admin/faults/clear` (which returns `{"status":"cleared"}`).

| Fault | Observed effect |
|---|---|
| `unavailable` | **Every** `/v1/*` request → `503 {"error":{"code":"FAULT_INJECTED",...}}` |
| `error_rate` | Random 503 with `{"error":{"code":"FAULT_INJECTED","message":"Injected transient API error."}}` |
| `latency` | Sleeps `delay_ms` before every non-admin, non-health request |
| `stale_data` | Adds `X-Simulator-Stale: true` to `/v1/*` **GET** responses |
| `stream_disconnect` | `GET /v1/stream` → `503 {"detail":{"code":"FAULT_INJECTED"}}` (nested under `detail`, not `error`) |

Confirmed behaviours:

* **`/v1/health` bypasses every fault.** With an `unavailable` fault active,
  `/v1/health` still returned `{"status":"ok",...}`. It is a safe liveness probe.
* **`/admin/*` bypasses every fault.** The fault middleware returns early for
  any path starting with `/admin`.
* **Faults auto-expire.** A `latency` fault with `duration_seconds: 1` was
  stored as `start_wall_time` / `end_wall_time` one second apart and stopped
  applying on its own.

### D3 — the fault row has no `duration_seconds`

The guide's `POST /admin/faults` response implies the row echoes the request.
It does not:

```json
{
  "id": 3,
  "type": "latency",
  "start_wall_time": "2026-11-19T15:48:33.803958+00:00",
  "end_wall_time": "2026-11-19T15:48:34.803958+00:00",
  "active": true,
  "parameters": {"delay_ms": 300}
}
```

`duration_seconds` is absent; the duration is recoverable only by subtracting
the two wall-clock stamps. **How we handle it:** `AdminFault.duration_seconds`
is a derived `@property` that computes the difference, so callers still get a
duration without the raw payload needing the field.

### D7 — the stale header is GET-only

The middleware only adds the header when `request.method == "GET"`. A `POST`
never carries it, even with the fault active. Our client therefore only
consults `stale` after reads.

**How we handle it:** `SimulatorClient.stale` is a **sticky** flag — it stays
`True` once any response has been marked stale until `reset_stale()` is called.
A per-response flag would be racy, because `/api/state` fans out nine
concurrent requests and any one of them may be the stale one. The sticky flag
means a snapshot reports stale if *any* of its components were stale, which is
the safe direction to fail.

---

## 6. Admin control surface

| Endpoint | Observed response |
|---|---|
| `POST /admin/run` | `200` + the **serialized instance** (`status`, `tick`, `sim_time`, `seed`, ...) |
| `POST /admin/pause` | `200` + the serialized instance |
| `POST /admin/toggle` | `200` + the serialized instance |
| `POST /admin/step` | `200` + `{"tick": 6, "sim_time": "2026-01-01T01:30:00"}` |
| `POST /admin/reset` | `200` + the serialized instance; wipes and reloads the scenario |
| `POST /admin/events` | `201` + the persisted event |
| `POST /admin/faults` | `201` + the fault row (see D3) |
| `POST /admin/faults/clear` | `200` + `{"status":"cleared"}` (idempotent) |
| `GET /admin/audit?limit=` | `200` + array of audit rows, id-desc |
| `GET /admin/faults` | `200` + array, last 50 rows, id-desc |
| `GET /admin/events` | `200` + array, last 50 rows, id-desc |
| `GET /admin/stats` | `200` + `{"row_counts": {...}, "db_bytes": N, ...}` — **not in the guide** (D9) |
| `GET /admin` | `200` + server-rendered HTML console (auto-refresh every 2s) |

`/admin/run` and `/admin/pause` return the whole instance, not the bare
status string the guide implies. **How we handle it:** `AdminActionResponse`
is permissive (`extra="allow"`) with optional fields, so it accepts both the
observed instance shape and a hypothetical `{"status": "..."}` shape.

### D4 — audit rows have nullable entity fields

```json
{
  "id": 12, "wall_time": "...", "sim_time": "...", "tick": 2,
  "action": "admin.pause", "entity_type": null, "entity_id": null,
  "result": "OK", "metadata_json": {}
}
```

Instance-level actions (`admin.run`, `admin.pause`, `fault.clear_all`) have no
entity, so `entity_type` and `entity_id` are `null`. `sim_time` and `tick` are
nullable too. Our strict model was **rejecting** these rows until we relaxed
those four fields to optional — a good example of strict validation catching a
real schema difference rather than a bug.

### D5 — `audit` limit is not clamped at the low end

```
GET /admin/audit?limit=0   -> []      (guide says clamp to [1, 1000])
GET /admin/audit?limit=1   -> 1 row
GET /admin/audit?limit=5000-> 1 row   (only 1 row existed)
```

The implementation is `limit(min(limit, 1000))` — only the **upper** bound is
applied. The lower bound is absent.

### D6 — `demand-history` limit behaves the same way

```
GET /v1/demand-history?limit=0 -> []
```

The implementation is `min(max(limit, 1), 2000)` per the guide, but `limit=0`
returned `[]` rather than 1 row. The `max(limit, 1)` is applied to the
**query limit**, and the `station_id` filter plus an empty table made the
result indistinguishable from a clamp in this run. Treat `limit=0` as
"no rows" and always send `limit >= 1`.

---

## 7. Tick clock and speed (D8)

`TICK_MINUTES` is env-only and is applied at startup, overwriting whatever the
scenario YAML says:

```yaml
# app/scenario.py — the comment is explicit about this
inst = SimulationInstance(..., tick_minutes=15, ...)   # hard-coded here
# and main.py startup does:
inst.tick_minutes = settings.tick_minutes               # env wins
```

Verified: running with `TICK_MINUTES=30` produced
`{"tick_minutes":30,"sim_time":"2026-01-01T00:00:00"}`, and one
`POST /admin/step` advanced `sim_time` to `2026-01-01T00:30:00` — exactly one
tick of 30 minutes.

**Measured speed.** With `SIMULATION_SPEED=8`, `TICK_MINUTES=15`:

```
wall 10.04s -> 67 ticks = 6.67 ticks/sec   (configured: 8 ticks/sec)
simulated minutes advanced: 1005 = 16.75 sim hours
1 simulated day (96 ticks) => ~14.4s wall clock
```

The shortfall is expected: the runner sleeps `1 / SIMULATION_SPEED` **between**
ticks and the tick itself takes real time (SQLite writes, demand generation,
audit rows), so the achievable rate is always below the nominal one. **Do not
use `SIMULATION_SPEED` as a wall-clock estimate** — measure it.

**A simulated day is 96 ticks** because the demand model divides daily litres
by 96: `base = daily_liters / 96.0`. So 96 ticks = 24 h and `TICK_MINUTES=15`
is exactly a quarter-hour step. Changing `TICK_MINUTES` to anything else
changes the *size* of each demand step, not the number of ticks per day.

---

## 8. The world (matches the guide)

| Entity | Count | Notes |
|---|---|---|
| Regions | 2 | `region-dhaka` (1.00), `region-chattogram` (1.08) |
| Depots | 2 | `depot-gazipur` (12,000/tick), `depot-patiya` (11,000/tick) |
| Stations | 4 | mirpur, tongi, karnaphuli, coxsbazar |
| Routes | 6 | transit 2–4 ticks, max_shipment 5,000–7,000 |
| Fuel types | 3 | DIESEL, PETROL, OCTANE |
| Supply arrivals | 22 | 4 "initial burst" at ticks 12–20, then 18 recurring |
| Demand rows | 12 per tick | 4 stations × 3 fuels |

Initial inventory matched the guide exactly (Gazipur 60,000/45,000/26,000;
Mirpur 9,000/9,000/5,000; etc.), so the guide's §8 tables are trustworthy.

---

## 9. SSE stream

`GET /v1/stream` returns `text/event-stream`. On connect it emits the SSE
comment `: connected`, then one `event: <name>` / `data: <json>` pair per
change, and `: keepalive` after 15 s of silence.

The simulator's queue is `asyncio.Queue(maxsize=200)`; a subscriber that falls
more than 200 events behind is **silently dropped** and must reconnect. There
is no `Last-Event-ID` replay, so a reconnect only yields events from that
moment forward — always re-`GET` REST state after reconnecting.

Observed event names: `simulation.tick`, `allocation.status_changed`,
`inventory.updated`, `simulator.notice`. Audit-log action values such as
`event.started` are **not** SSE event names; read them via `GET /admin/audit`.

---

## 10. Items the guide got right (worth keeping)

* The base URL, endpoint list, and the `/v1` vs `/admin` split.
* The `/v1/*` vs `/admin/*` fault-bypass split, and `/v1/health` being immune.
* The two error envelope shapes (`error` vs `detail`) — both are real.
* The `X-Simulator-Stale: true` header name and its meaning.
* The `idempotency_key` living in the **body**, not a header.
* The world tables: depots, stations, routes, demand profiles, supply schedule.
* The validation order in §5.2, which matches `create_allocation` exactly.

---

## 11. Consequences for our client

1. **Accept `200` and `201`** on allocation writes — the simulator always
   answers `201`, but the guide promises `200` on replay, and both are correct
   success semantics.
2. **Normalise timestamps to UTC** on the way in (D1).
3. **Treat `entity_type`/`entity_id`/`sim_time`/`tick` as optional** in audit
   rows (D4).
4. **Derive `duration_seconds`** on fault rows (D3).
5. **Keep the stale flag sticky** across a fan-out snapshot (D7).
6. **Do not clamp `limit` client-side to 1** — pass through and let the
   simulator decide (D5, D6).
7. **Never assume `SIMULATION_SPEED` is the observed tick rate** (D8).
8. **Treat `GET /v1/health` as the only liveness signal** — it is the one
   endpoint that survives every injected fault.

---

## 12. Open questions for the organizers

1. **D2** — is `200` or `201` correct for an idempotent replay? The guide
   contradicts itself between §5.4 and §9, and the code returns `201`.
2. **D5/D6** — are the `limit` lower bounds meant to clamp to 1, or is
   `limit=0` → `[]` intentional?
3. **D8** — should `SIMULATION_SPEED` be documented as a *target* rather than
   an exact rate? It is ~17% short of nominal on this machine.
4. Is there a documented scenario-switching procedure? The guide says the
   scenario is baked into the image and cannot be switched at runtime, but the
   image also ships `demand_spike.yaml`, `supply_disruption.yaml` and
   `final_combined.yaml` under `/app/scenarios/`. Only `baseline.yaml` is
   loaded.
5. Is `/admin/stats` intended to be part of the supported surface? It is
   useful for judging "data stored" and is not in the guide.
