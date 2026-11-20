# Fault Tests — Day 2

Real transcripts from the running stack, not mocks. Every scenario below was
executed against the published simulator image
(`asifmahmoud414/bup-fuel-supply-simulator:1.0.0`) with the backend running
against it.

**Reproduce it:**

```bash
docker compose up -d
./scripts/fault_test.sh          # the fault matrix
./scripts/degraded_mode_drill.sh # the container-stop drill
```

---

## 1. The fault matrix

`scripts/fault_test.sh` injects each fault type through the simulator's own
`POST /admin/faults` endpoint and records what the platform did.

```
=== 1. Baseline (no faults) ===
  ok    GET /v1/stations                                     HTTP 200
  ok    GET /v1/stream                                       HTTP 200
  ok    GET /api/stations (backend)                          HTTP 200

=== 2. stale_data fault -> X-Simulator-Stale header ===
  response header: x-simulator-stale: true
  ok    stale_data fault sets X-Simulator-Stale: true
  backend stale flag: True

=== 3. stream_disconnect fault -> GET /v1/stream answers 503 ===
  ok    GET /v1/stream with stream_disconnect                HTTP 503

=== 4. unavailable fault -> /v1/* answers 503 ===
  ok    GET /v1/stations with unavailable                    HTTP 503
  ok    GET /v1/health (bypasses faults)                     HTTP 200
  ok    GET /admin/faults (bypasses faults)                  HTTP 200

=== 5. latency fault -> responses are slow but succeed ===
  ok    GET /v1/stations with latency                        HTTP 200
  elapsed: 624ms

=== 6. Backend still serves the API while the simulator is faulted ===
  ok    GET /api/stations (backend, simulator faulted)       HTTP 200
  backend degraded flag: False

=== Result ===
passed: 10   failed: 0
```

### What each scenario proves

| # | Fault | Simulator behaviour | Platform behaviour |
|---|---|---|---|
| 2 | `stale_data` | Adds `X-Simulator-Stale: true` to `/v1/*` GETs | The flag propagates to the API payload as `stale: true` within one poll cycle |
| 3 | `stream_disconnect` | `GET /v1/stream` answers `503 {"detail":{"code":"FAULT_INJECTED"}}` | The SSE listener treats it as a reconnect, not a crash; the REST poller keeps serving |
| 4 | `unavailable` | `/v1/*` answers `503 {"error":{"code":"FAULT_INJECTED"}}` | The backend keeps serving the cached state; `/v1/health` and `/admin/*` still bypass faults as documented |
| 5 | `latency` | Delays the response by `delay_ms` | The request still succeeds — 624 ms for a 600 ms injected delay, so the client overhead is ~24 ms |
| 6 | `unavailable` | `/v1/*` answers 503 | The API answers `200` from cache. The `degraded` flag reads `False` because a *fault* is not an *outage* — the simulator is reachable, its data is merely failing. |

---

## 2. The degraded-mode drill (the definition of done)

`scripts/degraded_mode_drill.sh` stops the simulator **container** mid-run and
watches the platform degrade and then recover on its own.

```
=== 1. Baseline ===
  ok    simulator component status                           ok
  ok    sync degraded flag                                   False

=== 2. Stopping the simulator container ===
 Container fuel-simulator Stopping
 Container fuel-simulator Stopped
    stopped; waiting for the poller to notice...
    degraded after 5s
  ok    sync degraded flag                                   True
  ok    api still answers                                    200
  ok    served payload flagged degraded                      True
  info  last-known state age: 20.916s
  info  stations still served: 4

=== 3. Restarting the simulator container ===
 Container fuel-simulator Starting
 Container fuel-simulator Started
    started; waiting for automatic recovery...
    recovered after 5s
  ok    recovered automatically                              yes (5s)
  ok    sync degraded flag                                   False
  ok    health status                                        ok

=== Result ===
passed: 8   failed: 0
```

### The degraded response, verbatim

```json
{
  "stations": [ ... 4 stations ... ],
  "degraded": true,
  "degraded_reason": "timeout calling /v1/instance:",
  "age_seconds": 46.301
}
```

And `/health` during the outage:

```json
{
  "status": "degraded",
  "components": {
    "backend":   { "status": "ok" },
    "simulator": { "status": "unreachable", "error": "timeout calling /v1/health: " },
    "database":  { "status": "ok" }
  },
  "sync": {
    "polling": true,
    "last_sync_age_seconds": 37.832,
    "degraded": true,
    "stale": false,
    "has_state": true
  }
}
```

The backend stayed **up** the whole time: `/api/*` answered `200` with
last-known data, and the health probe correctly reported `503` so the
orchestrator knows the platform is degraded.

---

## 3. Metrics observed during the drill

```
sync_cycles_total{outcome="success"} 114.0
sync_cycles_total{outcome="failed"} 6.0
sse_reconnects_total{reason="unreachable"} 3.0
degraded_mode_activations_total 1.0
degraded_mode 1.0
stale_data 0.0
```

`degraded_mode_activations_total` incremented **once**, not once per failed
poll — the platform distinguishes *entering* a degraded state from *staying*
in it, so the counter is a usable alert signal.

---

## 4. Findings and discrepancies

| # | Finding | How the platform handles it |
|---|---|---|
| F1 | `stale_data` only marks **GET** responses. A `POST` in the same window carries no header. | The client only consults the header after reads, so a stale write is never misreported. |
| F2 | A `stream_disconnect` fault makes the stream answer `503` **at connect time**, not mid-stream. The backend log shows `opening simulator event stream` → `HTTP 200` → later `503`. | The SSE loop classifies a 503 as `reason="fault"` and reconnects with backoff. It never treats the stream as permanently dead. |
| F3 | With a `stream_disconnect` fault active, the **REST poller is unaffected** — `/v1/*` still answers 200. The fault only touches `/v1/stream`. | The REST path stays the source of truth, so a stream fault degrades responsiveness, not correctness. |
| F4 | The `unavailable` fault returns `{"error": {...}}`; domain errors return `{"detail": {...}}`. Both envelopes are live. | The client parses both into one exception hierarchy, so a fault is indistinguishable from a domain error at the call site. |
| F5 | `/v1/health` and every `/admin/*` path **bypass fault injection** entirely. | The health probe stays meaningful during a fault, and `/admin/faults` remains usable to clear the fault — which is how this whole document was produced. |
| F6 | The simulator's `X-Simulator-Stale` header appears on `/v1/*` GETs *only*, and the SSE stream never carries it. | Staleness is tracked from the REST path. The SSE path is a hint and is never used to decide freshness. |
| F7 | The 200-event per-subscriber queue is dropped silently when a subscriber falls behind. Nothing in the response tells you. | Every reconnect triggers a full REST refetch, so a dropped queue cannot cause a missed update. The symptom of a drop is invisible by design — which is exactly why the refetch exists. |
| F8 | The simulator keeps a **15-second keepalive** on an idle stream. Silence is normal, not a disconnect. | The SSE reader has no idle timeout; only a real transport error or a closed connection triggers a reconnect. |

---

## 5. What is still untested

| Gap | Why | When |
|---|---|---|
| Sustained `error_rate` fault at a high rate | The client retries with full jitter, but a long stochastic fault run has not been driven end to end | Day 3 |
| Postgres outage (not just the simulator) | The backend currently logs a persistence failure and keeps serving from cache, but the DB-down path is only unit-tested | Day 3 |
| Multi-replica sync contention | Only one backend instance runs, so the poll-interval jitter is untested in anger | Day 6 |
