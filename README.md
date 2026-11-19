# Fuel Supply Intelligence & Resilience Platform

Intelligent decision-support and resilience platform for a simulated fuel supply
network. Built for the **BUP CSE Fest 2026** Hackathon Finals.

> **Status:** Day 1 — running skeleton that talks to the official BUP Fuel
> Supply Simulator. See [`docs/progress/day1.md`](docs/progress/day1.md).

---

## The loop we are building

```
Observe → Detect → Predict → Decide → Simulate → Act → Monitor → Recover
```

The organizer-provided simulator is the *world*. Our platform is the brain: it
observes the world, detects risk, decides how to allocate constrained fuel, and
stays usable when things break.

---

## Repository layout

```
.
├── backend/            FastAPI service — simulator client, state API, health
│   ├── app/
│   │   ├── simulator_client.py    async httpx client for every simulator endpoint
│   │   ├── simulator_models.py    strict Pydantic models for every response
│   │   ├── simulator_errors.py    typed exception hierarchy
│   │   ├── main.py                FastAPI app: /health, /api/state, /metrics
│   │   ├── config.py              env-driven settings (no secrets in code)
│   │   ├── db.py / models.py      async SQLAlchemy engine and tables
│   │   └── metrics.py             Prometheus instrumentation
│   └── tests/          pytest suite (mocked simulator responses)
├── frontend/           React + Vite + TypeScript operator dashboard
├── infra/              Prometheus + Grafana provisioning
├── scripts/            developer helper scripts
├── tests/              cross-service / e2e checks
├── docs/
│   ├── reference/      the two organizer PDFs (source of truth) + extracts
│   ├── simulator-notes.md   observed simulator behaviour vs. the guide
│   └── progress/       daily progress log
├── docker-compose.yml  the whole stack
└── .env.example        every configuration key, with defaults
```

---

## Quick start

### Prerequisites

* Docker with the Compose plugin
* Python 3.12+ (only for running the backend outside a container)
* Node 20+ (only for frontend development)

### Run the stack

```bash
cp .env.example .env          # then edit the passwords in .env
docker compose up --build
```

| Service | URL | Notes |
|---|---|---|
| Backend API | http://localhost:8001 | `/health`, `/api/state`, `/docs`, `/metrics` |
| Simulator | http://localhost:8000 | Organizer image; `/admin` console, `/docs` |
| Frontend | http://localhost:5173 | Operator dashboard |
| Prometheus | http://localhost:9090 | Scrapes the backend `/metrics` |
| Grafana | http://localhost:3000 | Provisioned dashboard, login `admin` / `.env` |

Verify:

```bash
curl -s http://localhost:8001/health | jq
curl -s http://localhost:8001/api/state | jq '.instance, .metrics'
```

The simulator starts **paused**. Advance it deterministically:

```bash
curl -s -X POST http://localhost:8000/admin/step      # one tick
curl -s -X POST http://localhost:8000/admin/run       # start ticking
curl -s -X POST http://localhost:8000/admin/pause      # stop
```

### Develop the backend locally

```bash
cd backend
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
SIMULATOR_BASE_URL=http://localhost:8000 uvicorn app.main:app --reload
```

### Develop the frontend locally

```bash
cd frontend
npm install
npm run dev
```

---

## Configuration

All configuration is environment-driven; **no secret is hard-coded**. Copy
`.env.example` to `.env` and change the passwords there. `.env` is gitignored.

| Variable | Default | Purpose |
|---|---|---|
| `SIMULATOR_IMAGE` | `asifmahmoud414/bup-fuel-supply-simulator:1.0.0` | Organizer image |
| `SIMULATOR_START_MODE` | `paused` | `paused` or `running` |
| `SIMULATION_SPEED` | `8` | Wall-clock ticks per second while running |
| `TICK_MINUTES` | `15` | Simulated minutes per tick |
| `SIMULATOR_BASE_URL` | `http://simulator:8000` | Backend → simulator |
| `SIMULATOR_REQUEST_TIMEOUT` | `10` | Per-request timeout (seconds) |
| `SIMULATOR_MAX_RETRIES` | `3` | Retries on `503 FAULT_INJECTED` and transport errors |
| `SIMULATOR_RETRY_BASE_DELAY` | `0.2` | Exponential backoff base (seconds) |
| `DATABASE_URL` | `postgresql+asyncpg://…@postgres:5432/fuel` | Postgres DSN |
| `LOG_LEVEL` | `INFO` | Backend log level |
| `POSTGRES_USER` / `POSTGRES_PASSWORD` / `POSTGRES_DB` | `fuel` / *change-me* / `fuel` | Database credentials |

---

## Simulator integration

`backend/app/simulator_client.py` covers the entire documented API surface:

* **Reads** — `/v1/health`, `/v1/instance`, `/v1/regions`, `/v1/depots`,
  `/v1/stations`, `/v1/routes`, `/v1/supply-arrivals`, `/v1/events`,
  `/v1/allocations`, `/v1/demand-history`, `/v1/metrics`
* **Writes** — `POST /v1/allocations`, `POST /v1/allocations/{id}/cancel`
* **Admin** — `run`, `pause`, `toggle`, `step`, `reset`, `events`, `faults`,
  `faults/clear`, `audit`

Design guarantees:

* **Strict validation.** Every response is validated against a Pydantic model
  with `extra="forbid"` and closed enums. Malformed payloads are rejected and
  logged, never passed downstream.
* **Typed errors.** Both documented envelope shapes — `{"error": {...}}` and
  `{"detail": {...}}` — map onto one exception hierarchy. Every 409 code has
  its own exception class.
* **Retry with backoff and jitter.** `503 FAULT_INJECTED` and transport errors
  are retried with full-jitter exponential backoff.
* **Staleness is visible.** The `X-Simulator-Stale: true` header is surfaced as
  a sticky flag that survives concurrent fan-out reads.
* **Idempotency is safe.** Both `200` and `201` are accepted on allocation
  writes, so a replay never looks like a failure.

Observed behaviour and every discrepancy we found against the guide are written
up in [`docs/simulator-notes.md`](docs/simulator-notes.md).

---

## Testing and CI

```bash
cd backend
pytest -q          # 59 tests, all simulator responses mocked
ruff check .
ruff format --check .
```

GitHub Actions runs on every push to `main`: ruff lint + format check, pytest
with a JUnit report, frontend typecheck + build, both Docker image builds with
a live backend health check, and a `docker compose config` validation.

---

## Reference material

* [`docs/reference/BUP_Fuel_Supply_Simulator_Integration_Guide_Final.pdf`](docs/reference/BUP_Fuel_Supply_Simulator_Integration_Guide_Final.pdf) — **the API source of truth**
* [`docs/reference/BUP_CSE_FEST_2026_Participant_Facing_Hackathon_Draft.pdf`](docs/reference/BUP_CSE_FEST_2026_Participant_Facing_Hackathon_Draft.pdf) — the brief and deliverables
* `docs/reference/extracted/` — plain-text extracts and a read-only copy of the
  simulator's own source, used to verify the contract

---

## Rules we are holding ourselves to

* Operate **only** against the simulation environment — no real fuel
  infrastructure, no real credentials, no real dispatches.
* Never modify the simulator image. Judges run against the published image.
* Human operators stay in the loop for consequential decisions.
* Validate external input; reject it and raise an alert rather than crash.
* Keep every important decision inspectable.
