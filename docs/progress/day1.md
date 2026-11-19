# Day 1 — Running Skeleton

**Date:** Day 1 of 7 · BUP CSE Fest 2026 Hackathon Finals
**Goal:** a running skeleton that talks to the simulator.

---

## Definition of done — status

| Requirement | Status | Evidence |
|---|---|---|
| `docker compose up` works | ✅ | 6 services up; `docker compose ps` all healthy |
| `/health` is green | ✅ | `HTTP 200`, all three components `"ok"` |
| Tests pass | ✅ | 59 backend unit tests + 6 integration tests |
| CI file exists | ✅ | `.github/workflows/ci.yml` — 5 jobs |
| Monorepo layout | ✅ | `/backend` `/frontend` `/infra` `/docs` `/scripts` `/tests` + root README |
| Simulator client, full surface | ✅ | every documented endpoint covered |
| Simulator notes written | ✅ | `docs/simulator-notes.md` — 10 discrepancies |

---

## What was done

### 1. Monorepo

```
backend/    FastAPI service, simulator client, 59 unit tests
frontend/   React + Vite + TypeScript operator dashboard
infra/      Prometheus + Grafana provisioning
docs/       reference PDFs, extracts, simulator notes, this progress log
scripts/    smoke_test.sh, dev_up.sh
tests/      cross-service integration suite
```

### 2. Docker Compose stack

Six services: the published simulator image (starting **paused**), the backend,
Postgres, the frontend, Prometheus and Grafana. Every credential and host port
comes from the environment — `.env.example` documents the contract and `.env`
is gitignored. No secret is hard-coded anywhere.

The backend waits for the simulator and database to report healthy before it
starts, and carries its own healthcheck.

### 3. Simulator client — `backend/app/simulator_client.py`

Covers the **entire** documented surface:

- **Reads:** `/v1/health` `/v1/instance` `/v1/regions` `/v1/depots`
  `/v1/stations` `/v1/routes` `/v1/supply-arrivals` `/v1/events`
  `/v1/allocations` `/v1/demand-history` `/v1/metrics`
  (plus the single-entity `/{entity_id}` variants)
- **Writes:** `POST /v1/allocations`, `POST /v1/allocations/{id}/cancel`
- **Admin:** `run` `pause` `toggle` `step` `reset` `events` `faults`
  `faults/clear` `audit` `stats`

Behaviour that matters:

| Requirement | How it is met |
|---|---|
| Strict validation | A Pydantic model per endpoint with `extra="forbid"` and closed enums. Malformed payloads raise `SimulatorValidationError` and are logged, never passed downstream. |
| Log malformed data | Rejections log the endpoint, the validation error and the offending payload. |
| Timeouts | Separate connect and read timeouts, both configurable. |
| Retry + backoff + jitter | `503 FAULT_INJECTED` and transport errors retry with full-jitter exponential backoff. |
| Both error shapes | `{"error": {...}}` and `{"detail": {...}}` parse into one exception hierarchy. FastAPI's list-shaped `{"detail": [...]}` degrades gracefully. |
| Stale header | `X-Simulator-Stale: true` surfaces as a **sticky** flag, so a snapshot from nine concurrent reads still reports stale if any component was stale. |
| 200 and 201 both succeed | Allocation writes accept both, so an idempotent replay never looks like a failure. |
| 409 → typed exceptions | A dedicated class per code: `RouteMismatchError`, `InsufficientInventoryError`, `DispatchCapacityExceededError`, `CannotCancelError`, … An unknown code falls back to `ConflictError`. |

### 4. Backend endpoints

- **`GET /health`** — backend, simulator and database status. Returns `503`
  when a dependency is down, so it doubles as a container liveness probe.
- **`GET /api/state`** — one validated snapshot of instance, regions, depots,
  stations, routes, supply arrivals, events, allocations and metrics, plus the
  stale flag. Each fetch is persisted to Postgres.

### 5. Tests

60 unit tests with mocked responses (happy path, 503 retry, transport error,
timeout, stale header, malformed payload, every 409 code, both envelope
shapes) plus 6 cross-service integration tests against the live stack.

### 6. CI — `.github/workflows/ci.yml`

`lint` (ruff check + format check) → `test` (pytest + JUnit artifact) →
`docker build` (both images with BuildKit caching, then boots the backend
against a real simulator and Postgres and asserts `/health` returns 200) →
`compose-config`, with a parallel `frontend` job for typecheck and build.

**Latest run: all 5 jobs green.** The `docker build` job reports
`backend healthy after 2s`.

### 7. Simulator notes

`docs/simulator-notes.md` records what the running image actually does, with
the raw observations, the root cause in the simulator's own source where
applicable, and ten documented discrepancies.

---

## Bugs the verification caught

Three defects surfaced during Day 1 that a "does it compile" check would have
missed. Each is now covered by a test.

| Bug | How it hid | Fix |
|---|---|---|
| The backend image **failed to boot** — the client imported a `Fault` model that had been removed | The local suite passed because a stale `__pycache__` entry masked the broken import, and the module was never imported at test time | Fixed the import; added a test that imports `app.main` so a broken import can never pass again |
| The dashboard showed **"Backend unreachable: Failed to fetch"** | The browser blocked the cross-origin call, and Vite inlines `import.meta.env` at build time so the API URL was frozen into the bundle | Added CORS to the backend; the frontend now resolves the API URL at runtime from `public/config.js`, rewritten by the container entrypoint |
| The CI smoke test asserted the **wrong thing** | It booted the backend with no simulator and no database, then expected `/health` to return 200 — but `/health` is a *readiness* probe and correctly returns 503 while a dependency is down | The smoke test now starts the simulator and Postgres on a shared network and checks the backend against them |

The second one is the most valuable: the whole stack was "up" and green by
every automated check, but a judge's browser would have seen an error page.
The fix was only found by loading the dashboard in an actual browser.

---

## Discrepancies found (the headline of the day)

The guide is the source of truth, but the running simulator disagrees with it
in ten places. The client follows the simulator and the guide's promise where
they conflict.

| # | Finding | How we handle it |
|---|---|---|
| D1 | `sim_time` is naive (`2026-01-01T00:00:00`), not `+00:00` as documented | `SimDateTime` anchors offset-less timestamps to UTC on ingest |
| D2 | An idempotent replay answers `201`, not the `200` §9 promises | Both `200` and `201` accepted on writes |
| D3 | Fault rows have `start_wall_time`/`end_wall_time`, not `duration_seconds` | Duration derived from the window |
| D4 | `entity_type`/`entity_id`/`sim_time`/`tick` are `null` on audit rows | Modeled as optional — strict validation caught this |
| D5 | `/admin/audit?limit=0` returns `[]`; the lower bound is not clamped | Client passes the limit through untouched |
| D6 | `/v1/demand-history?limit=0` returns `[]` | Same |
| D7 | `X-Simulator-Stale` is only added to GET responses | Consulted after reads only |
| D8 | Measured ~6.7 ticks/sec against a nominal `SIMULATION_SPEED=8` | Never used as a wall-clock estimate |
| D9 | `GET /admin/stats` exists and is undocumented | Client supports it |
| D10 | The guide skips §4.3; `/v1/regions` is really §4.4 | Cosmetic |

Five questions for the organizers are listed at the end of the notes file.

---

## How to run

### The whole stack

```bash
git clone https://github.com/TanimStu068/fuel-supply-intelligence-platform.git
cd fuel-supply-intelligence-platform
cp .env.example .env          # change the passwords
docker compose up --build
```

| Service | URL |
|---|---|
| Backend API | http://localhost:8001 |
| Simulator | http://localhost:8000 (`/admin` console, `/docs`) |
| Frontend | http://localhost:5173 |
| Prometheus | http://localhost:9090 |
| Grafana | http://localhost:33000 → `admin` / `.env` |

Check it:

```bash
curl -s http://localhost:8001/health | jq
curl -s http://localhost:8001/api/state | jq '.instance, .metrics'
bash scripts/smoke_test.sh        # 24 endpoint checks
```

### Backend only

```bash
cd backend
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest -q
ruff check .
```

### Driving the simulator

It starts **paused**, so every run is deterministic from tick 0.

```bash
curl -s -X POST http://localhost:8000/admin/step    # advance one tick
curl -s -X POST http://localhost:8000/admin/run     # start ticking
curl -s -X POST http://localhost:8000/admin/pause    # stop
curl -s -X POST http://localhost:8000/admin/reset    # wipe and reload the scenario
```

---

## Blockers and risks

**No hard blockers.** The simulator behaves as documented apart from the ten
discrepancies above, all of which are handled.

| Risk | Impact | Mitigation |
|---|---|---|
| The simulator's SQLite DB lives in a named volume, so state survives restarts | A restart is not a reset | `POST /admin/reset` for a deterministic start; document it for judges |
| `SIMULATION_SPEED` is a target, not a rate | Timing assumptions in demos will be off | Measure; never schedule against the nominal value |
| The guide and the simulator disagree on ten points | Silent breakage if we trusted the guide | Every discrepancy has a regression test |
| The SSE queue drops slow subscribers after 200 events | A missed event looks like a state change that never happened | Re-`GET` REST state on every reconnect; not wired yet (Day 2) |
| The simulator is single-tenant and local | No shared state between teams | Fine for the brief, but do not design for multi-tenancy |

### Environment notes

- Ports `8000`, `5432` and `3000` were already taken on the development
  machine by unrelated projects, so the local `.env` moves the simulator to
  `8100`, Postgres to `55432` and Grafana to `33000`. The committed defaults
  are the standard ports.
- `docker-compose.yml` reads every port from the environment, so a conflict is
  resolved in `.env` alone.

---

## Next — Day 2

1. **SSE consumer.** Subscribe to `/v1/stream`, handle the 200-event queue
   drop, and re-`GET` REST state on every reconnect.
2. **State poller.** Background task that keeps `/api/state` fresh and writes
   the history Postgres needs for forecasting.
3. **Demand history ingestion.** Pull `/v1/demand-history` into Postgres so
   the intelligence layer has a real time series.
4. **First intelligence component.** Shortage risk from inventory vs.
   demand rate — the smallest thing that is genuinely useful.
5. **Real schema migrations.** Alembic, instead of `create_all` at startup.
6. **Frontend beyond the skeleton.** Station drill-down, shortage alerts, and
   the decision history view.
