# The deliverables checklist

The brief lists eleven deliverables. This is where each one lives and
what proves it.

Every row names a **file** or a **screen** — something you can open.
Nothing here is "it works"; every row points at the evidence.

---

## 1. A working application

| | |
|---|---|
| **Where** | `docker compose up --build` |
| **Proves it** | `docs/screenshots/overview.png` — the live operator dashboard |
| **Verified by** | [`scripts/demo_dry_run.sh`](../scripts/demo_dry_run.sh) — 16 steps, every one checked, 39 seconds |
| **Fresh-clone proof** | `git clone` → `cp .env.example .env` → `docker compose up --build` → four containers healthy, `/health` ok, `/api/state` serving 4 stations / 2 depots / 6 routes at tick 0 |

**Status: done.** The stack is seven containers in two profiles. The core
profile is the platform; the monitoring profile adds Prometheus, Grafana
and node-exporter.

```bash
cp .env.example .env
docker compose up --build -d --wait
curl -s http://localhost:8001/health | jq
```

---

## 2. A source repository

| | |
|---|---|
| **Where** | This repository |
| **Proves it** | The commit history — one commit per day's work, each message explaining *why* |
| **Also** | [`docs/progress/day1.md`](progress/day1.md) … [`docs/progress/day7.md`](progress/day7.md) — the daily log |

**Status: done.** Seven days, 60+ commits, every day ending with a push.

---

## 3. Simulator integration

| | |
|---|---|
| **Where** | `backend/app/simulator_client.py` (704 lines) |
| **Also** | `backend/app/simulator_models.py`, `backend/app/simulator_errors.py` |
| **Proves it** | [`docs/simulator-notes.md`](simulator-notes.md) — the observed behaviour against the guide, and the **10 discrepancies** found between them |
| **Verified by** | 97 % coverage on the client; 54 tests in `tests/test_simulator_client_errors.py` and `tests/test_simulator_client_stream.py` |

Every documented endpoint is covered: the twelve `/v1` reads, the two
writes, the eight admin endpoints and the SSE stream. Both error
envelope shapes map onto one exception hierarchy, and each documented
409 code has its own exception class.

```bash
grep -n "async def" backend/app/simulator_client.py
```

---

## 4. The intelligence component

| | |
|---|---|
| **Where** | `backend/app/intelligence/` — forecasting, risk, detection, dispatcher |
| **Proves it** | [`docs/model-evaluation.md`](model-evaluation.md) — forecast accuracy over a six-day run, and the **four defects it exposed** |
| **Also** | `docs/screenshots/risk-explanation-drawer.png` — the drawer an operator opens to see why |

**Status: done.** The learned model beats the rule-based baseline by
**34 %** over the forecast horizon (78.1 L vs 118.1 L of total demand
error). A ×2.0 demand spike is detected **1 tick** after it is
injected; a ×3.0 spike moves the risk level `OK` → `WATCH` in **2
ticks**.

```bash
curl -s http://localhost:8001/api/model-status | jq
```

---

## 4b. The decision engine

| | |
|---|---|
| **Where** | `backend/app/decision/` — optimizer, constraints, validator, fallback, builder, world, service |
| **Proves it** | [`docs/policy-comparison.md`](policy-comparison.md) — the policy against a naive baseline on `service_level` |
| **Also** | `docs/screenshots/recommendations.png` — the cards an operator approves |

**Status: done.** The allocator pre-validates every simulator rule
before it proposes anything, ranks alternatives, splits a deficit
above the route's `max_shipment`, and falls back to a days-of-cover
policy when the intelligence layer is down. Coverage: optimizer 94 %,
service 98 %, validator 93 %, fallback 99 %.

```bash
curl -s http://localhost:8001/api/recommendations | jq '.recommendations[0]'
```

---

## 5. The operator interface

| | |
|---|---|
| **Where** | `frontend/src/views/` — nine screens |
| **Proves it** | `docs/screenshots/` — eleven screenshots |
| **Verified by** | 22 Vitest component tests, 12 Playwright end-to-end tests |

**Status: done.** The nine screens are Overview, Network, Inventory,
Risk & alerts, Recommendations, Supply & events, Decision history,
Service health and Simulation. Every view has a loading state and an
error state; the degraded-mode banner shows the age of the cached
data.

| Screen | Screenshot |
|---|---|
| Overview | [`overview.png`](screenshots/overview.png) |
| Network | [`network.png`](screenshots/network.png) |
| Inventory | [`inventory.png`](screenshots/inventory.png) |
| Risk & alerts | [`risk-alerts.png`](screenshots/risk-alerts.png) |
| Explanation drawer | [`risk-explanation-drawer.png`](screenshots/risk-explanation-drawer.png) |
| Recommendations | [`recommendations.png`](screenshots/recommendations.png) |
| Supply & events | [`supply-events.png`](screenshots/supply-events.png) |
| Decision history | [`decision-history.png`](screenshots/decision-history.png) |
| Service health | [`service-health.png`](screenshots/service-health.png) |
| Simulation | [`simulation.png`](screenshots/simulation.png) |
| Responsive, 430 px | [`responsive-mobile.png`](screenshots/responsive-mobile.png) |

---

## 6. The architecture diagram

| | |
|---|---|
| **Where** | [`docs/architecture.md`](architecture.md) §1 — a Mermaid diagram |
| **Also** | [`docs/assets/architecture-mermaid.png`](assets/architecture-mermaid.png) — the rendered image |
| **Proves it** | The diagram renders, and it names every hop: simulator → backend → data/intelligence/decision → dashboard → monitoring |

**Status: done.** `docs/architecture.md` also carries the assumptions,
the data sources, the security notes and the limitations.

---

## 7. Deployment

| | |
|---|---|
| **Where** | `docker-compose.yml`, `docker-compose.release.yml` |
| **CI/CD** | [`.github/workflows/ci.yml`](../.github/workflows/ci.yml) and [`.github/workflows/release.yml`](../.github/workflows/release.yml) |
| **Proves it** | The **compose smoke test** in CI: copy the `.env`, `docker compose up --wait`, check `/health` for every component, check the state, run the integration suite |
| **Documented** | [`docs/deployment.md`](deployment.md) — publishing, rollback, and what a deploy does *not* do |

**Status: done.** CI runs: ruff lint and format, pytest, the frontend
typecheck / unit tests / build, the Playwright end-to-end tests against
the whole stack, both Docker image builds with a live health check
against a real simulator and Postgres, `docker compose config`, the
compose smoke test, and the coverage floors.

The release workflow publishes both images to the GitHub Container
Registry with three tags: the version, the version plus the commit
sha, and `latest`. **The rollback target is the sha tag** — it is the
only one that cannot move after the fact.

```bash
docker compose -f docker-compose.yml -f docker-compose.release.yml up -d --wait
```

---

## 8. Observability evidence

| | |
|---|---|
| **Where** | `infra/prometheus/` (the scrape config and **21 alert rules**), `infra/grafana/provisioning/` (**5 dashboards**) |
| **Proves it** | `docs/screenshots/resilience/` — four Grafana dashboards, captured against the live stack |
| **Also** | `backend/app/metrics.py` — the Prometheus instrumentation; `backend/app/logging_config.py` — structured JSON logs with correlation IDs |
| **Verified by** | Six of the 21 alert rules were **fired and watched**, not just read |

**Status: done.** The dashboards are Application, System, Intelligence,
Integration and Overview. The alert rules sit in four groups.

| Dashboard | Screenshot |
|---|---|
| Application | [`grafana-fuel-application.png`](screenshots/resilience/grafana-fuel-application.png) |
| System | [`grafana-fuel-system.png`](screenshots/resilience/grafana-fuel-system.png) |
| Intelligence | [`grafana-fuel-intelligence.png`](screenshots/resilience/grafana-fuel-intelligence.png) |
| Integration | [`grafana-fuel-integration.png`](screenshots/resilience/grafana-fuel-integration.png) |

```bash
docker compose --profile monitoring up -d --wait
curl -s http://localhost:9090/api/v1/rules | jq '.data.groups[].rules | length'
```

---

## 9. The resilience demonstration

| | |
|---|---|
| **Where** | [`docs/resilience-report.md`](resilience-report.md), [`docs/fault-tests.md`](fault-tests.md) |
| **Proves it** | `scripts/crisis_drill.py` — 14 scenarios, each measured and recorded |
| **Also** | `scripts/day4_fallback_drill.py`, `scripts/degraded_mode_drill.sh` |

**Status: done.** **17 of 17** crisis, fault and dependency scenarios
recovered automatically — a demand spike, a shipment delay, a depot
constraint, a route disruption, a combined crisis, all five fault types,
the simulator's connection-pool exhaustion, a stopped database, and the
prediction layer in three shapes. The report records the **detection
time**, the **recovery time** and the **action taken** for each one.

The five fault types are the ones that break the *platform's* side:
`latency`, `unavailable`, `error_rate`, `stale_data` and
`stream_disconnect`.

```bash
scripts/crisis_drill.py --output docs/resilience-report.md
```

---

## 10. The load-test evidence

| | |
|---|---|
| **Where** | [`docs/load-test-report.md`](load-test-report.md) |
| **Proves it** | `scripts/loadtest/` — the harness and its raw results under `scripts/loadtest/results/` |
| **The numbers** | The p99 of the dashboard read path went from **30 030 ms → 286 ms**, and the container stopped being OOM-killed |

**Status: done.** Three workloads: the dashboard read path, the
recommendation API, and an end-to-end decision request. The report
records the before and after, the bottleneck, and the fix.

The load generator is a standalone `httpx`/`asyncio` script run from the
host — **no extra container** — with a Locust file alongside it for
anyone who prefers that.

---

## 11. The final demo

| | |
|---|---|
| **Where** | [`docs/demo-script.md`](demo-script.md) |
| **Proves it** | [`scripts/demo_dry_run.sh`](../scripts/demo_dry_run.sh) — the fourteen steps, executed and checked |
| **Reset** | [`scripts/reset_demo.sh`](../scripts/reset_demo.sh) — the world back to a known state in seconds |

**Status: done.** The script follows the brief's fourteen-step story
exactly: normal operations → dashboard → demand rising → risk detected →
shortage predicted → recommendation → operator inspects → simulate →
crisis → adaptation → failure injected → monitoring catches it →
fallback → operations continue. Every step has the exact command or
click, and what to say while doing it.

```bash
scripts/reset_demo.sh --wait
scripts/demo_dry_run.sh
```

---

## The supporting material

Not deliverables in themselves, but the evidence behind them.

| Document | What it holds |
|---|---|
| [`docs/progress/day1.md` … `day7.md`](progress/) | The daily log — what was built, what was verified, what was wrong |
| [`docs/architecture.md`](architecture.md) | The diagram, the assumptions, the data sources, the security notes, the limitations |
| [`docs/deployment.md`](deployment.md) | Publishing, rollback, and the post-deploy checks |
| [`docs/simulator-notes.md`](simulator-notes.md) | The simulator's observed behaviour and 10 discrepancies with the guide |
| [`docs/model-evaluation.md`](model-evaluation.md) | Forecast accuracy and the 4 defects the evaluation exposed |
| [`docs/policy-comparison.md`](policy-comparison.md) | The decision policy against a naive baseline |
| [`docs/fault-tests.md`](fault-tests.md) | The fault-injection transcripts |
| [`docs/load-test-report.md`](load-test-report.md) | The three workloads, before and after |
| [`docs/resilience-report.md`](resilience-report.md) | The 14 scenarios, each measured |
| [`docs/host-tuning/`](host-tuning/) | The daemon configuration for log rotation |

---

## The numbers, in one place

| | |
|---|---|
| Backend tests | **583** passing, five clean runs |
| Coverage | **85 %** overall; every named module above 93 % |
| Frontend tests | **22** Vitest component, **12** Playwright end-to-end |
| Containers | 7 (4 core + 3 monitoring) |
| Memory | ~446 MiB in use against a 1.5 GB budget, every container limited |
| API endpoints | 35 routes (33 under `/api`, plus `/`, `/health` and `/ready`) |
| Simulator endpoints covered | 22 of 22 |
| Prometheus alert rules | 21, six of them fired and watched |
| Grafana dashboards | 5 |
| Crisis scenarios recovered | **17 of 17** |
| Load-test p99 improvement | 30 030 ms → 286 ms |
| Forecast error vs the baseline | **34 % better** over the horizon |
| Demand spike detected | **1 tick** after injection |
| Model vs baseline over the horizon | 78.1 L vs 118.1 L of total error |
