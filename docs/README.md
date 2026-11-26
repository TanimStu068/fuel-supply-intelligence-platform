# Documentation index

Everything the project documents, and what each document is for.

The project's front door is the [root README](../README.md) — what it is, how to
run it, every endpoint, the rules it holds itself to. This page indexes the
`docs/` tree.

---

## The build log

The seven-day log, one file per day. Each file records what was built, what
the verification caught, and the numbers that were measured — not the numbers
that were hoped for.

| Day | File | What it covers |
|---|---|---|
| 1 | [`progress/day1.md`](progress/day1.md) | The running skeleton, the simulator client, the first discrepancies |
| 2 | [`progress/day2.md`](progress/day2.md) | Postgres + Alembic, the sync service, SSE, the degraded contract, metrics |
| 3 | [`progress/day3.md`](progress/day3.md) | The intelligence layer — forecasting, shortage risk, anomaly detection, the dispatcher |
| 4 | [`progress/day4.md`](progress/day4.md) | The decision engine — the validator, the optimizer, human approval, the fallback |
| 5 | [`progress/day5.md`](progress/day5.md) | The operator dashboard — nine screens, the decision drawer, the crisis console |
| 6 | [`progress/day6.md`](progress/day6.md) | Resilience, observability, the crisis drills and the load test (in three parts) |
| 7 | [`progress/day7.md`](progress/day7.md) | The release, the self-checking demo, the fresh-clone test |

Read as a story rather than a log: [`project-journey.md`](project-journey.md)
narrates the seven days — the goal, what was built, the numbers, the bugs and
the decisions of each day — and ends with the lessons learned.

## The evidence

Measured against the running stack. Every number in these documents came from
a run that is reproducible with the script named in it.

| Document | What it holds |
|---|---|
| [`resilience-report.md`](resilience-report.md) | **17 of 17** crisis, fault and dependency scenarios, each with a detection time, a recovery time and the recommendation made during the incident |
| [`load-test-report.md`](load-test-report.md) | Three workloads at a 50-user ramp, the before/after of the prediction-error aggregate, and the deadlock that lost writes |
| [`model-evaluation.md`](model-evaluation.md) | The forecast accuracy, the two error measures, the horizon benchmark, and the model's own honest verdict |
| [`intelligence-methodology.md`](intelligence-methodology.md) | **The model card** — inputs and data sources, the profile baseline formula, the learned model and its twelve features, confidence intervals, the risk thresholds, anomaly and bottleneck detection, the fallback and the `needs_human_review` rule, retraining, the evaluation method including the leakage fix, why the 34 % / 26.5 % / 3.7 % figures differ, the supply ceiling, and the limitations |
| [`decision-engine.md`](decision-engine.md) | **The allocation engine** — the priority-greedy algorithm and why it is not an LP, the eight local pre-validation rules in the guide's order, route choice and backup routes, splitting above `max_shipment`, transit-time accounting, the recommendation fields, the lifecycle state machine, the what-if simulation, the idempotency key, the handling of every 409, the auto-mode guards, the fallback policy, the audit trail, and the policy-vs-baseline comparison |
| [`api-reference.md`](api-reference.md) | **The backend API** — all 35 routes grouped by area, each with its method, path, purpose, parameters, response fields, status codes and a worked example, plus the common freshness fields (`stale`, `degraded`, `age_seconds`) and every error shape the platform and the simulator use |
| [`configuration-reference.md`](configuration-reference.md) | **Every environment variable** — all **37** backend settings with their default, their validation bounds, their purpose, the effect of changing them and whether the container receives them; the variables for the simulator, Postgres, the frontend and Grafana; the **seven** published ports and the **two** compose profiles; and the gaps between the three sources — including **thirteen** settings that cannot be changed through `.env` under Compose, and `DB_DEADLOCK_RETRIES`, which is documented but silently ignored |
| [`policy-comparison.md`](policy-comparison.md) | The platform's policy against a naive baseline, on service level |
| [`fault-tests.md`](fault-tests.md) | The fault matrix, the degraded-mode drill, and eight findings about the simulator's fault behaviour |
| [`simulator-notes.md`](simulator-notes.md) | What the running simulator actually does, and the **ten** places it disagrees with the guide |
| [`testing-strategy-and-report.md`](testing-strategy-and-report.md) | **The testing strategy and report** — the eight test layers and what each one proves, the counts (**583** backend, **22** Vitest, **12** Playwright) reconciled from the test files themselves, the **84.4 %** coverage against its seven enforced per-module floors, the two flaky tests that were found and fixed, why the on-disk verification scripts exist after the silent edit reverts, and **twelve** known gaps |

## The defects

| Document | What it holds |
|---|---|
| [`bug-catalog.md`](bug-catalog.md) | Every defect found during the project — **70** of them — with the day, the symptom, the root cause, how it was found, the fix, and the test that pins it. Grouped by root-cause pattern, with eight deep dives and a count per day and per component. |
| [`postmortems/`](postmortems/) | Eight incidents written up in full — summary, impact, timeline, root cause, how it was detected, resolution, what went well, what went badly, and action items marked done or not done. Start at [`postmortems/README.md`](postmortems/README.md). |
| [`known-limitations-and-roadmap.md`](known-limitations-and-roadmap.md) | **Every known limitation in one place** — the 25 % supply shortfall, the model's modest advantage, the unbounded `prediction_evaluations`, the simulator's fragile connection pool, the load runs stopped at the CPU guard, the single-tenant and no-authentication positions, and the polling latency floor — each with its impact and its workaround, followed by a prioritized roadmap of **fourteen** items marked as future work |

## The requirements

| Document | What it holds |
|---|---|
| [`requirements-traceability.md`](requirements-traceability.md) | Every requirement in the two organizer documents, mapped to the code that implements it and the evidence that proves it — with a **Done / Partial / Not done** status on each one, and a gaps list at the end. |

## The short version

| Document | What it holds |
|---|---|
| [`project-summary.md`](project-summary.md) | One page: the problem, what was built, the architecture in five lines, six headline results with real numbers, what makes it resilient, and the honest limitations |
| [`demo-qa.md`](demo-qa.md) | Twenty-five questions judges are likely to ask, with short factual answers, each linking to the evidence file |
| [`project-journey.md`](project-journey.md) | **The build log as a narrative** — Days 1 to 7 day by day: the goal, what was built, the key numbers, the bugs found and the decisions taken — followed by **seven lessons learned** on verifying against live behaviour, silent failures, measurement honesty and resource budgeting |

## Operating it

| Document | What it holds |
|---|---|
| [`operator-user-guide.md`](operator-user-guide.md) | **The dashboard, screen by screen** — one section per screen (Overview, Network, Inventory, Risk & alerts, Recommendations, Supply & events, Decision history, Service health, Simulation), how to read a risk level, the eight constraints checked, how to approve or reject, what the `DEGRADED`, `STALE` and `FALLBACK` banners mean, the common tasks, and what the dashboard does not do. With the eleven screenshots the project's own Playwright test captures. |

## The decisions

| Document | What it holds |
|---|---|
| [`adr/`](adr/) | **Thirteen** Architecture Decision Records — the stack, REST as source of truth, the polling floor, human approval, the hybrid forecast, the model gate, idempotency keys, in-SQL aggregation, node-exporter, compose profiles, the decision not to use Loki, the decision not to use RL, and the working-set split. Each states the context, the alternatives rejected and what the decision cost. Start at [`adr/README.md`](adr/README.md). |

## The project

| Document | What it holds |
|---|---|
| [`architecture.md`](architecture.md) | The system diagram, the assumptions, the data sources, the security notes, the limitations |
| [`security-and-threat-model.md`](security-and-threat-model.md) | **The security and threat model** — the assets, the five trust boundaries, the controls that exist (each verified in the code), a twenty-row threat table with impact, mitigation and residual risk, and an honest list of what is **not** protected: no authentication, no authorization, no TLS, no rate limiting, no security headers |
| [`data-model.md`](data-model.md) | Every one of the **twelve** tables with its columns, keys, indexes and purpose; the entity-relationship diagram; the ten de-duplication keys; which tables are actually written versus write-only; the three Alembic migrations; and the retention gaps |
| [`deployment.md`](deployment.md) | Publishing, rollback, and the post-deploy checks |
| [`cicd-release-and-rollback.md`](cicd-release-and-rollback.md) | **The CI/CD, release and rollback** — both GitHub Actions workflows and all **nine** jobs, the three image tags and why the `-sha-` tag is the rollback target, the `v1.0.0` tag and the commit it points at, the rollback procedure with exact commands, and an explicit list of what has **not** been verified — including the GHCR push, the release workflow's empty `needs:`, and the CI's own green status |
| [`runbook.md`](runbook.md) | **Operating the platform** — starting and stopping the core and monitoring profiles, the health checks, a clean reset, the simulator's connection-pool procedure, degraded mode, the ML kill-switch, a database outage, a backend crash, memory pressure on a 7 GB host, and all **twenty-one** Prometheus alerts mapped to their meaning, likely cause and action |
| [`observability-guide.md`](observability-guide.md) | **What the platform measures** — a catalog of all **forty-six** Prometheus metrics with their types, labels and meanings; the **five** dashboards and the question each answers; all **twenty-one** alert rules with their condition, window, severity and what to do; the structured log format and its correlation IDs; how to search `GET /api/audit` and `scripts/search_logs.py`; and the resource budget per container |
| [`demo-script.md`](demo-script.md) | The fourteen demo steps, with the exact command or click for each one and what to say while doing it |
| [`deliverables-checklist.md`](deliverables-checklist.md) | Every required deliverable mapped to the file or screen that proves it |

## Reference

| Path | What it holds |
|---|---|
| [`reference/`](reference/) | The organizer's integration guide and the participant brief (PDF), plus the plain-text extracts and a read-only copy of the simulator's own source |
| [`screenshots/`](screenshots/) | The dashboard screens, captured by `frontend/e2e/screenshots.spec.ts` |
| [`assets/`](assets/) | Rendered diagrams |
| [`host-tuning/`](host-tuning/) | The host memory and CPU budget this machine can afford, and the compose profiles that enforce it |

---

## Reading order

* **A judge with ninety seconds** — the [root README](../README.md), then
  [`project-summary.md`](project-summary.md) for the one-page version, then
  [`resilience-report.md`](resilience-report.md) and the screenshots.
* **A judge wanting the story of the build** —
  [`project-journey.md`](project-journey.md) for the seven days as a
  narrative, then the [progress log](#the-build-log) for the raw record.
* **A judge preparing questions** — [`demo-qa.md`](demo-qa.md) has
  twenty-five of them answered, each with a link to the evidence.
* **A reviewer checking the claims** — [`requirements-traceability.md`](requirements-traceability.md)
  first, because it maps every requirement to its proof and marks what is
  not done. Then [`bug-catalog.md`](bug-catalog.md), which names what is
  broken and what is pinned, and
  [`model-evaluation.md`](model-evaluation.md) for the model's honest verdict.
  Then [`postmortems/`](postmortems/) for the eight incidents written up in
  full. Then [`known-limitations-and-roadmap.md`](known-limitations-and-roadmap.md),
  which collects every limitation with its impact and its workaround, and
  says what is still to be built.
* **Someone joining the project** — [`architecture.md`](architecture.md), then
  the [root README's quick start](../README.md#7-quick-start), then the day
  that matches what you are changing.
* **Someone running the platform** — [`runbook.md`](runbook.md), for the
  commands, the health checks and what to do when a dependency fails.
  [`operator-user-guide.md`](operator-user-guide.md) for the dashboard itself:
  what each screen shows and how to act on it.
* **Someone operating the dashboard** —
  [`operator-user-guide.md`](operator-user-guide.md) first, screen by screen,
  then [`demo-script.md`](demo-script.md) for the same screens in a story
  order.
* **Someone reviewing the instrumentation** —
  [`observability-guide.md`](observability-guide.md), for every metric,
  every dashboard, every alert rule, the log format and the budget.

## Rules for these documents

* Every number comes from a run that the named script reproduces. Nothing is
  estimated, and nothing is rounded in the platform's favour.
* Where two documents disagree, the newer verified value wins and the conflict
  is listed in the **Discrepancies** section of the document.
* Where something was not verified, the document says **not verified** rather
  than leaving a gap.
