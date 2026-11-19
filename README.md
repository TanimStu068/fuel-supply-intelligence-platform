# Fuel Supply Intelligence & Resilience Platform

Intelligent decision-support and resilience platform for a simulated fuel supply
network. (in association
with Poridhi.io).

> **Status:** Day 1 of 7 complete. Days 2 to 7 are planned below.
> Submission deadline: **26 November 2026**.
> Latest report: [`docs/progress/day1.md`](docs/progress/day1.md)

---

## Table of contents

1. [What this project is](#1-what-this-project-is)
2. [The loop we are building](#2-the-loop-we-are-building)
3. [Architecture](#3-architecture)
4. [Tech stack](#4-tech-stack)
5. [7-day build plan](#5-7-day-build-plan)
6. [Repository layout](#6-repository-layout)
7. [Quick start](#7-quick-start)
8. [Configuration](#8-configuration)
9. [Simulator integration](#9-simulator-integration)
10. [Testing and CI](#10-testing-and-ci)
11. [Required deliverables map](#11-required-deliverables-map)
12. [Evaluation criteria and where we address them](#12-evaluation-criteria-and-where-we-address-them)
13. [Reference material](#13-reference-material)
14. [Rules we hold ourselves to](#14-rules-we-hold-ourselves-to)
15. [Daily workflow](#15-daily-workflow)

---

## 1. What this project is

The organizers provide a deterministic **BUP Fuel Supply Simulator**: 2 regions,
2 depots, 4 stations, 6 routes, 3 fuel types (Diesel, Petrol, Octane) and a
tick-based clock. The simulator is the *world*. It does not predict, optimize or
decide.

This platform is the *brain and cockpit* built on top of it. It:

* observes the fuel network (inventory, demand, supply, routes, events);
* detects emerging shortages and operational risks;
* recommends valid, explainable allocations that a human approves;
* responds to disruptions (demand spikes, delays, route and depot problems);
* stays observable and usable when parts of the system fail.

---

## 2. The loop we are building

```
Observe → Detect → Predict → Decide → Simulate → Act → Monitor → Recover
```

---

## 3. Architecture

┌────────────────────────────────────────────────────────────────────────────┐
│                      OUR PLATFORM (all in Docker Compose)                  │
│                                                                            │
│  ┌──────────────┐      ┌────────────────────────────┐      ┌────────────┐  │
│  │  Frontend    │      │  FastAPI Backend :8001     │      │ Simulator  │  │
│  │  React + TS  │◄────►│  /api /health /metrics     │◄────►│ (Organizer)│  │
│  │  :5173       │      │                            │      │ :8000      │  │
│  │  Operator UI │      │  ├─ Sync service           │      │ REST + SSE │  │
│  │              │      │  ├─ Intelligence layer     │      │ + /admin   │  │
│  │              │      │  ├─ Decision engine        │      │            │  │
│  └──────────────┘      └─────┬────────────────┬─────┘      └────────────┘  │
│                              │                │                            │
│                              ▼                ▼                            │
│                    ┌────────────────────┐  ┌────────────────────┐          │
│                    │  Postgres :5432    │  │  Prometheus :9090  │          │
│                    │  state, history,   │  │  scrapes /metrics  │          │
│                    │  decisions, audit  │  │                    │          │
│                    └────────────────────┘  └──┬─────────────────┘          │
│                                               │                            │
│                                               ▼                            │
│                                            ┌────────────────────┐          │
│                                            │  Grafana :3000     │          │
│                                            │  dashboards, alerts│          │
│                                            └────────────────────┘          │
│                                                                            │
│  Load tests (k6 / Locust)  ──►  Backend API                                │
└────────────────────────────────────────────────────────────────────────────┘

Key principles:

* **REST is the source of truth, SSE is only a hint.** Every event triggers a
  re-fetch of the affected resource.
* **Human in the loop.** Recommendations start as `PROPOSED` and are sent to the
  simulator only after an operator approves them.
* **Fail safe.** Cached state, fallback policies and degraded mode keep the
  operator informed when a dependency fails.

---

## 4. Tech stack

| Layer | Choice |
|---|---|
| Backend | Python 3.12, FastAPI, httpx (async), Pydantic, SQLAlchemy (async) |
| Frontend | React, Vite, TypeScript |
| Database | Postgres (Alembic migrations from Day 2) |
| Observability | Prometheus, Grafana, structured JSON logs |
| Delivery | Docker Compose, GitHub Actions |
| Load testing | k6 or Locust |

---

## 5. 7-day build plan

Progress is logged daily in `docs/progress/dayN.md`. Each day ends with a push
to `main` and an update to the company.

| Day | Date | Focus | Status |
|---|---|---|---|
| 1 | 19 Nov | Foundation, simulator client, repo and CI skeleton | Done |
| 2 | 20 Nov | Data layer, live state sync, logging and metrics | Planned |
| 3 | 21 Nov | Intelligence layer (forecast, shortage prediction, detection) | Planned |
| 4 | 22 Nov | Decision engine, recommendations, simulate and act | Planned |
| 5 | 23 Nov | Operator dashboard | Planned |
| 6 | 24 Nov | Resilience, observability, crisis drills, load testing | Planned |
| 7 | 25 Nov | CI/CD, tests, docs, demo prep, release `v1.0.0` | Planned |
| Buffer | 26 Nov | Fresh-clone test and submission only | Planned |

### Day 1 (19 Nov): Foundation, simulator client, repo and CI skeleton

**Goal:** a running skeleton that talks to the simulator.

- [x] Monorepo layout: `backend`, `frontend`, `infra`, `docs`, `scripts`, `tests`
- [x] Docker Compose stack with 6 services, `.env.example`, no hard-coded secrets
- [x] Async simulator client covering every `/v1` GET, `POST /v1/allocations`,
      cancel, and all `/admin` endpoints
- [x] Strict Pydantic models, retry with backoff and jitter on 503, both error
      shapes handled, stale-data flag, 200/201 idempotent replay, typed 409 errors
- [x] `GET /health` and `GET /api/state`
- [x] Unit tests (60), integration tests (6), smoke checks (24)
- [x] GitHub Actions: lint, test, frontend, docker build, compose-config
- [x] `docs/simulator-notes.md` with observed behavior and 10 documented
      discrepancies against the integration guide

**Definition of done:** `docker compose up` works, `/health` is green, tests
pass, CI file exists.

### Day 2 (20 Nov): Data layer, live state sync, logging and metrics

**Goal:** reliable data ingestion, stored state, and the start of observability.

- [ ] Postgres schema and Alembic migrations: inventory snapshots per tick,
      demand history, allocations mirror, events, alerts, decisions, system health
- [ ] Background sync service: REST polling as source of truth, SSE listener for
      `/v1/stream` (hint only, re-GET on every event), auto-reconnect with
      backoff, full refetch after reconnect, `stream_disconnect` fault handling
- [ ] Stale-data handling: invalidate cache on `X-Simulator-Stale: true` and mark
      data stale in the API
- [ ] Incremental `/v1/demand-history` ingest with de-duplication by
      (station, fuel, tick)
- [ ] Cached-state fallback: serve last-known state with a `degraded` flag and age
      if the simulator is unreachable
- [ ] Structured JSON logging with correlation IDs
- [ ] Prometheus `/metrics`: request rate, latency histogram, error rate,
      simulator call failures, SSE reconnects, stale count, last-sync age
- [ ] Read APIs: `/api/stations`, `/depots`, `/routes`, `/events`,
      `/supply-arrivals`, `/history/{station}`
- [ ] Tests for SSE reconnect, stale handling and degraded mode; real fault test
      via `POST /admin/faults` documented in `docs/fault-tests.md`

**Definition of done:** stop the simulator mid-run and the backend stays up in
degraded mode, then recovers automatically.

### Day 3 (21 Nov): Intelligence layer

**Goal:** a meaningful, explainable intelligence layer in
`backend/app/intelligence/`.

- [ ] Demand forecasting per (station, fuel): profile baseline (daily demand ×
      hour factor × demand multiplier) combined with a learned model trained on
      demand history, with confidence intervals
- [ ] Shortage prediction: hours-to-stockout and stockout probability using
      inventory, forecast demand and in-transit arrivals; risk levels
      `OK / WATCH / CRITICAL`
- [ ] Detection: demand anomalies, abnormal inventory change, bottlenecks
      (dispatch capacity, route disruption, delayed supply, low depot stock)
- [ ] Model-unavailable fallback to a rule-based predictor; low confidence sets
      `needs_human_review=true`
- [ ] `GET /api/alerts` and `GET /api/risk` with the signals behind each score
- [ ] Metrics: prediction error (MAE), confidence, alert rate, fallback count
- [ ] Multi-day stepped run for training and evaluation, documented in
      `docs/model-evaluation.md`, plus a `demand_spike` detection-time test
- [ ] Unit tests for predictor, anomaly detector and fallback

**Definition of done:** `/api/risk` returns sensible, explained risk per station
and fuel, and a `demand_spike` changes the risk within a few ticks.

### Day 4 (22 Nov): Decision engine, recommendations, simulate and act

**Goal:** recommend valid allocations, explain them, and require human approval.

- [ ] Constraint-aware allocator in `backend/app/decision/` that pre-validates
      all simulator rules (route mismatch, depot/station closed, route disrupted,
      route capacity, inventory, dispatch capacity, destination capacity)
- [ ] Route choice among alternatives, shipment splitting above `max_shipment`,
      transit-time awareness
- [ ] Each recommendation includes before/after risk, signals, constraints,
      confidence and 1 to 2 ranked alternatives
- [ ] What-if endpoint `POST /api/recommendations/{id}/simulate` (no simulator write)
- [ ] Approval flow: `PROPOSED` → approve (sends `POST /v1/allocations` with a
      deterministic idempotency key) or reject; every 409 handled with a clear reason
- [ ] Decision history and audit trail reconciled with allocation outcome
- [ ] Optional auto-mode (default OFF) for low-risk, high-confidence actions
- [ ] Fallback policy (refill lowest days-of-cover first) if intelligence is down
- [ ] Metrics: decisions per minute, approval rate, fallbacks, 409 counts by code
- [ ] Tests for every 409 case; policy vs naive baseline compared on
      `service_level` in `docs/policy-comparison.md`

**Definition of done:** the system proposes an allocation, a human approves it,
and it reaches `ARRIVED` with improved risk.

### Day 5 (23 Nov): Operator dashboard

**Goal:** a usable operator dashboard in `/frontend`.

- [ ] Overview: tick, time, status, service level, unmet demand, alerts, health banner
- [ ] Network board: regions, depots, stations, routes with live status colors
- [ ] Inventory view with fill bars and hours-to-stockout
- [ ] Risk and alerts list with explanation drawer and forecast chart with
      confidence band
- [ ] Recommendation cards: impact, alternatives, confidence, Approve / Reject,
      what-if preview
- [ ] Incoming supply and event timeline; decision history with outcomes
- [ ] Service health panel (backend, database, simulator, prediction, decision
      engine, p95 latency, error rate, fallback state)
- [ ] Simulation controls (run / pause / step) and a clearly labeled crisis
      injection panel
- [ ] "Simulated environment. Human review required." banner on recommendations
- [ ] Loading and error states, degraded-mode display with data age, responsive layout
- [ ] Frontend in Compose, smoke tests, screenshots in `docs/screenshots/`

### Day 6 (24 Nov): Resilience, observability, crisis drills, load testing

**Goal:** prove resilience and observability with evidence.

- [ ] Prometheus and Grafana provisioned as code: Application, System,
      Intelligence and Integration dashboards, plus alert rules
- [ ] Circuit breaker, timeouts, malformed-data rejection with alert, ML kill
      switch, cached degraded mode, graceful shutdown, compose health checks
- [ ] `scripts/crisis_drill.py`: baseline, demand spike, shipment delay, depot
      constraint, route disruption, combined crisis, and all five fault types;
      results in `docs/resilience-report.md`
- [ ] Dependency-failure drill (prediction component and database) with recovery
- [ ] Load tests (k6 or Locust) on dashboard API, recommendation API and an
      end-to-end decision request; report avg, p50, p95, p99, throughput, error
      rate, CPU and memory in `docs/load-test-report.md`, with one bottleneck fix
      and before/after numbers
- [ ] Searchable audit, decision and event logs

### Day 7 (25 Nov): CI/CD, tests, docs, demo prep, release

**Goal:** a submission-ready, reproducible, documented project.

- [ ] Fresh-clone test: README followed exactly, one command plus `.env` copy
- [ ] CI/CD: lint, backend tests, frontend tests and build, docker build, compose
      smoke test with `/health`, status badge, tagged images, documented rollback
- [ ] Higher test coverage on decision engine, predictor, client and fallback
- [ ] Polished README and `docs/architecture.md`, assumptions, data sources,
      security notes and limitations
- [ ] `docs/demo-script.md` following the 14-step demo story, with a quick-reset
      script (`POST /admin/reset`)
- [ ] `docs/deliverables-checklist.md` mapping each deliverable to its proof
- [ ] Timed full demo dry run and UI polish
- [ ] Tag `v1.0.0`

### Buffer day (26 Nov)

Fresh-clone verification and submission only. No new features.

### Scope-cut order if time runs short

1. Auto-mode
2. Loki / extra log tooling
3. Extra Grafana panels

Never cut: the load test, the resilience report, or the fresh-clone test.

---

## 6. Repository layout

```
.
├── backend/            FastAPI service
│   ├── app/
│   │   ├── simulator_client.py    async httpx client for every simulator endpoint
│   │   ├── simulator_models.py    strict Pydantic models for every response
│   │   ├── simulator_errors.py    typed exception hierarchy
│   │   ├── main.py                FastAPI app: /health, /api/state, /metrics
│   │   ├── config.py              env-driven settings (no secrets in code)
│   │   ├── db.py / models.py      async SQLAlchemy engine and tables
│   │   ├── metrics.py             Prometheus instrumentation
│   │   ├── intelligence/          (Day 3) forecasting, risk, detection
│   │   └── decision/              (Day 4) allocator, recommendations, approvals
│   └── tests/          pytest suite (mocked simulator responses)
├── frontend/           React + Vite + TypeScript operator dashboard
├── infra/              Prometheus + Grafana provisioning
├── scripts/            helper scripts (crisis drill, load tests, reset)
├── tests/              cross-service / e2e checks
├── docs/
│   ├── reference/      the two organizer PDFs (source of truth) + extracts
│   ├── screenshots/    evidence screenshots per day
│   ├── simulator-notes.md   observed simulator behavior vs. the guide
│   └── progress/       daily progress log (day1.md ... day7.md)
├── docker-compose.yml  the whole stack
└── .env.example        every configuration key, with defaults
```

Files marked with a day number are added as that day's work lands.

---

## 7. Quick start

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

Ports are remapped locally to avoid conflicts. Check the real ones with
`docker compose ps`.

Verify:

```bash
curl -s http://localhost:8001/health | jq
curl -s http://localhost:8001/api/state | jq '.instance, .metrics'
```

The simulator starts **paused**. Advance it deterministically:

```bash
curl -s -X POST http://localhost:8000/admin/step      # one tick
curl -s -X POST http://localhost:8000/admin/run       # start ticking
curl -s -X POST http://localhost:8000/admin/pause     # stop
curl -s -X POST http://localhost:8000/admin/reset     # wipe and reload scenario
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

## 8. Configuration

All configuration is environment-driven; **no secret is hard-coded**. Copy
`.env.example` to `.env` and change the passwords there. `.env` is gitignored.

| Variable | Default | Purpose |
|---|---|---|
| `SIMULATOR_IMAGE` | `asifmahmoud414/bup-fuel-supply-simulator:1.0.0` | Organizer image |
| `SIMULATOR_START_MODE` | `paused` | `paused` or `running` |
| `SIMULATION_SPEED` | `8` | Wall-clock ticks per second while running |
| `TICK_MINUTES` | `15` | Simulated minutes per tick |
| `SIMULATOR_BASE_URL` | `http://simulator:8000` | Backend to simulator |
| `SIMULATOR_REQUEST_TIMEOUT` | `10` | Per-request timeout (seconds) |
| `SIMULATOR_MAX_RETRIES` | `3` | Retries on `503 FAULT_INJECTED` and transport errors |
| `SIMULATOR_RETRY_BASE_DELAY` | `0.2` | Exponential backoff base (seconds) |
| `DATABASE_URL` | `postgresql+asyncpg://…@postgres:5432/fuel` | Postgres DSN |
| `LOG_LEVEL` | `INFO` | Backend log level |
| `POSTGRES_USER` / `POSTGRES_PASSWORD` / `POSTGRES_DB` | `fuel` / *change-me* / `fuel` | Database credentials |

---

## 9. Simulator integration

`backend/app/simulator_client.py` covers the entire documented API surface:

* **Reads:** `/v1/health`, `/v1/instance`, `/v1/regions`, `/v1/depots`,
  `/v1/stations`, `/v1/routes`, `/v1/supply-arrivals`, `/v1/events`,
  `/v1/allocations`, `/v1/demand-history`, `/v1/metrics`
* **Writes:** `POST /v1/allocations`, `POST /v1/allocations/{id}/cancel`
* **Admin:** `run`, `pause`, `toggle`, `step`, `reset`, `events`, `faults`,
  `faults/clear`, `audit`
* **Stream:** `GET /v1/stream` (SSE, consumed from Day 2)

Design guarantees:

* **Strict validation.** Every response is validated against a Pydantic model
  with `extra="forbid"` and closed enums. Malformed payloads are rejected and
  logged, never passed downstream.
* **Typed errors.** Both documented envelope shapes, `{"error": {...}}` and
  `{"detail": {...}}`, map onto one exception hierarchy. Every 409 code has its
  own exception class.
* **Retry with backoff and jitter.** `503 FAULT_INJECTED` and transport errors
  are retried with full-jitter exponential backoff.
* **Staleness is visible.** The `X-Simulator-Stale: true` header is surfaced as
  a sticky flag that survives concurrent fan-out reads.
* **Idempotency is safe.** Both `200` and `201` are accepted on allocation
  writes, so a replay never looks like a failure.

The integration guide contradicts itself in places (tick size, speed, replay
status code). Observed behavior and all 10 discrepancies found so far are in
[`docs/simulator-notes.md`](docs/simulator-notes.md). The brief's "conceptual"
endpoints (`POST /simulate`, `POST /advance-time`) do not exist; the integration
guide is the real API.

---

## 10. Testing and CI

```bash
cd backend
pytest -q          # unit tests, all simulator responses mocked
ruff check .
ruff format --check .
```

The cross-service suite needs the stack running:

```bash
docker compose up -d
pytest tests/ -m integration    # checks against the live simulator
```

It resolves target URLs from the environment first, then `.env`, then the
committed defaults, so it follows a local port remapping.

GitHub Actions runs on every push to `main`: ruff lint and format check, pytest
with a JUnit report, frontend typecheck and build, Docker image builds with a
live backend health check against a real simulator and Postgres, and a
`docker compose config` validation.

Current results (Day 1): 60 backend unit tests, 6 integration tests, 24 smoke
checks, all passing; lint clean; CI green.

---

## 11. Required deliverables map

| # | Deliverable | Where it lives | Status |
|---|---|---|---|
| 1 | Working application | `docker compose up` | Skeleton (Day 1), complete by Day 7 |
| 2 | Source repository | This repo | Ongoing |
| 3 | Simulator integration | `backend/app/simulator_client.py` | Done (Day 1) |
| 4 | Intelligence component | `backend/app/intelligence/` | Day 3 |
| 5 | Operator interface | `frontend/` | Day 5 |
| 6 | Architecture diagram | Section 3 here, `docs/architecture.md` | Draft here, final Day 7 |
| 7 | Deployment | `docker-compose.yml`, CI workflow | Compose done, CI/CD final Day 7 |
| 8 | Observability evidence | Grafana dashboards, `/metrics`, logs | Day 2 and Day 6 |
| 9 | Resilience demonstration | `docs/resilience-report.md` | Day 6 |
| 10 | Load-test evidence | `docs/load-test-report.md` | Day 6 |
| 11 | Final demo | `docs/demo-script.md` | Day 7 |

---

## 12. Evaluation criteria and where we address them

| Criterion | Weight | Our focus |
|---|---|---|
| Working product and UX | 20% | Operator dashboard (Day 5) |
| Intelligence and decision quality | 20% | Forecasting, risk, allocator (Days 3 and 4) |
| Architecture and integration | 15% | Typed simulator client, sync service (Days 1 and 2) |
| DevOps and engineering quality | 15% | Compose, CI/CD, tests (Days 1 and 7) |
| Resilience and incident response | 15% | Fallbacks, degraded mode, crisis drills (Days 2, 4, 6) |
| Observability and performance | 10% | Prometheus, Grafana, load tests (Days 2 and 6) |
| Demo and problem understanding | 5% | Demo script and dry run (Day 7) |

---

## 13. Reference material

* [`docs/reference/BUP_Fuel_Supply_Simulator_Integration_Guide_Final.pdf`](docs/reference/BUP_Fuel_Supply_Simulator_Integration_Guide_Final.pdf): **the API source of truth**
* [`docs/reference/BUP_CSE_FEST_2026_Participant_Facing_Hackathon_Draft.pdf`](docs/reference/BUP_CSE_FEST_2026_Participant_Facing_Hackathon_Draft.pdf): the brief and deliverables
* `docs/reference/extracted/`: plain-text extracts and a read-only copy of the
  simulator's own source, used to verify the contract

---

## 14. Rules we hold ourselves to

* Operate **only** against the simulation environment: no real fuel
  infrastructure, no real credentials, no real dispatches.
* Never modify the simulator image. Judges run against the published image.
* Keep human operators in the loop for consequential decisions.
* Validate external input; reject it and raise an alert rather than crash.
* Keep every important decision inspectable.
* Clearly distinguish simulated results from real-world fuel conditions.
* Document assumptions and any external or generated data.

---

## 15. Daily workflow

1. Run that day's agent prompt in VS Code.
2. Review the diff and run `docker compose up` before accepting.
3. Commit in small logical commits and push to `main`.
4. Write `docs/progress/dayN.md` (done, next, blockers, how to run) and push it.
5. Send the company the daily update with the repo link and evidence.
