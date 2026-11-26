# Documentation index

This index covers the documents and supporting evidence in `docs/`.

## Start here

- [`README.md`](README.md) — this index of project documentation.
- [`project-summary.md`](project-summary.md) — a concise overview of the project, results, and limitations.
- [`demo-qa.md`](demo-qa.md) — factual answers to likely demonstration questions.
- [`deliverables-checklist.md`](deliverables-checklist.md) — deliverables and their evidence.
- [`glossary.md`](glossary.md) — simulator, platform, and evidence terminology.

## Architecture and design

- [`architecture.md`](architecture.md) — system architecture, data sources, assumptions, and limitations.
- [`api-reference.md`](api-reference.md) — backend routes, parameters, responses, and errors.
- [`data-model.md`](data-model.md) — database tables, relationships, and migrations.
- [`adr/README.md`](adr/README.md) — index of architecture decision records.
- [`adr/0001-fastapi-postgres-stack.md`](adr/0001-fastapi-postgres-stack.md) — FastAPI and Postgres stack decision.
- [`adr/0002-rest-source-of-truth-sse-as-hint.md`](adr/0002-rest-source-of-truth-sse-as-hint.md) — REST source of truth and SSE notification decision.
- [`adr/0003-polling-floor-and-bounded-fanout.md`](adr/0003-polling-floor-and-bounded-fanout.md) — polling floor and bounded fanout decision.
- [`adr/0004-human-approval-before-allocation.md`](adr/0004-human-approval-before-allocation.md) — human approval for allocations decision.
- [`adr/0005-hybrid-forecast-with-rule-based-fallback.md`](adr/0005-hybrid-forecast-with-rule-based-fallback.md) — learned forecast and rule-based fallback decision.
- [`adr/0006-gate-model-on-held-out-one-step-comparison.md`](adr/0006-gate-model-on-held-out-one-step-comparison.md) — held-out model comparison gate decision.
- [`adr/0007-deterministic-idempotency-keys.md`](adr/0007-deterministic-idempotency-keys.md) — deterministic idempotency key decision.
- [`adr/0008-aggregate-in-the-database.md`](adr/0008-aggregate-in-the-database.md) — database aggregation decision.
- [`adr/0009-node-exporter-instead-of-cadvisor.md`](adr/0009-node-exporter-instead-of-cadvisor.md) — node-exporter choice decision.
- [`adr/0010-compose-profiles-and-memory-limits.md`](adr/0010-compose-profiles-and-memory-limits.md) — Compose profiles and memory limits decision.
- [`adr/0011-no-loki-audit-in-postgres.md`](adr/0011-no-loki-audit-in-postgres.md) — Postgres audit storage and no-Loki decision.
- [`adr/0012-no-reinforcement-learning.md`](adr/0012-no-reinforcement-learning.md) — decision not to use reinforcement learning.
- [`adr/0013-in-memory-working-set-with-postgres-audit-trail.md`](adr/0013-in-memory-working-set-with-postgres-audit-trail.md) — in-memory working set and Postgres audit trail decision.
- [`assets/architecture.png`](assets/architecture.png) — rendered system architecture diagram.
- [`assets/architecture-mermaid.png`](assets/architecture-mermaid.png) — rendered Mermaid architecture diagram.

## Intelligence and decisions

- [`intelligence-methodology.md`](intelligence-methodology.md) — model inputs, methods, evaluation, fallback, and limitations.
- [`model-evaluation.md`](model-evaluation.md) — forecast evaluation and the reported evaluation findings.
- [`decision-engine.md`](decision-engine.md) — allocation rules, recommendation lifecycle, and fallback policy.
- [`policy-comparison.md`](policy-comparison.md) — comparison of the platform policy with a baseline.

## Operations

- [`operator-user-guide.md`](operator-user-guide.md) — dashboard screens and operator workflows.
- [`runbook.md`](runbook.md) — operating procedures and responses to service issues.
- [`deployment.md`](deployment.md) — deployment, publishing, and post-deployment checks.
- [`cicd-release-and-rollback.md`](cicd-release-and-rollback.md) — CI/CD, release, and rollback procedures.
- [`configuration-reference.md`](configuration-reference.md) — service settings and their effects.
- [`observability-guide.md`](observability-guide.md) — metrics, dashboards, alerts, logs, and resource budgets.
- [`simulator-notes.md`](simulator-notes.md) — observed simulator behaviour compared with its integration guide.
- [`demo-script.md`](demo-script.md) — ordered demonstration steps and operator actions.
- [`host-tuning/daemon.json`](host-tuning/daemon.json) — Docker daemon log-rotation configuration.

## Quality and evidence

- [`requirements-traceability.md`](requirements-traceability.md) — requirements mapped to implementation and evidence.
- [`testing-strategy-and-report.md`](testing-strategy-and-report.md) — test layers, results, coverage, and known gaps.
- [`fault-tests.md`](fault-tests.md) — fault-test matrix and observed findings.
- [`resilience-report.md`](resilience-report.md) — measured resilience scenarios and outcomes.
- [`load-test-report.md`](load-test-report.md) — load-test workloads, results, and bottleneck changes.
- [`bug-catalog.md`](bug-catalog.md) — recorded defects, causes, fixes, and regression tests.
- [`security-and-threat-model.md`](security-and-threat-model.md) — assets, trust boundaries, controls, threats, and residual risks.
- [`screenshots/day1/day1_fronted.png`](screenshots/day1/day1_fronted.png) — Day 1 dashboard screenshot.
- [`screenshots/day1/day1_screenshots_api_docs.png`](screenshots/day1/day1_screenshots_api_docs.png) — Day 1 API documentation screenshot.
- [`screenshots/decision-history.png`](screenshots/decision-history.png) — decision history screen.
- [`screenshots/inventory.png`](screenshots/inventory.png) — inventory screen.
- [`screenshots/network.png`](screenshots/network.png) — network screen.
- [`screenshots/overview.png`](screenshots/overview.png) — overview screen.
- [`screenshots/recommendations.png`](screenshots/recommendations.png) — recommendations screen.
- [`screenshots/responsive-mobile.png`](screenshots/responsive-mobile.png) — responsive dashboard screenshot.
- [`screenshots/risk-alerts.png`](screenshots/risk-alerts.png) — risk and alerts screen.
- [`screenshots/risk-explanation-drawer.png`](screenshots/risk-explanation-drawer.png) — risk explanation drawer.
- [`screenshots/service-health.png`](screenshots/service-health.png) — service health screen.
- [`screenshots/simulation.png`](screenshots/simulation.png) — simulation controls screen.
- [`screenshots/supply-events.png`](screenshots/supply-events.png) — supply and events screen.
- [`screenshots/resilience/grafana-fuel-application.png`](screenshots/resilience/grafana-fuel-application.png) — Grafana application dashboard.
- [`screenshots/resilience/grafana-fuel-integration.png`](screenshots/resilience/grafana-fuel-integration.png) — Grafana integration dashboard.
- [`screenshots/resilience/grafana-fuel-intelligence.png`](screenshots/resilience/grafana-fuel-intelligence.png) — Grafana intelligence dashboard.
- [`screenshots/resilience/grafana-fuel-system.png`](screenshots/resilience/grafana-fuel-system.png) — Grafana system dashboard.

## Project story

- [`project-journey.md`](project-journey.md) — narrative of the seven-day project and lessons learned.
- [`known-limitations-and-roadmap.md`](known-limitations-and-roadmap.md) — known limitations, workarounds, and future work.
- [`progress/day1.md`](progress/day1.md) — Day 1 progress record.
- [`progress/day2.md`](progress/day2.md) — Day 2 progress record.
- [`progress/day3.md`](progress/day3.md) — Day 3 progress record.
- [`progress/day4.md`](progress/day4.md) — Day 4 progress record.
- [`progress/day5.md`](progress/day5.md) — Day 5 progress record.
- [`progress/day6.md`](progress/day6.md) — Day 6 progress record.
- [`progress/day7.md`](progress/day7.md) — Day 7 progress record.
- [`postmortems/README.md`](postmortems/README.md) — index of incident postmortems.
- [`postmortems/01-silent-dead-sse-feed.md`](postmortems/01-silent-dead-sse-feed.md) — SSE feed incident postmortem.
- [`postmortems/02-stale-snapshot-after-reset.md`](postmortems/02-stale-snapshot-after-reset.md) — stale snapshot after reset incident postmortem.
- [`postmortems/03-oom-kill-prediction-evaluations.md`](postmortems/03-oom-kill-prediction-evaluations.md) — prediction evaluation memory incident postmortem.
- [`postmortems/04-simulator-connection-pool-exhaustion.md`](postmortems/04-simulator-connection-pool-exhaustion.md) — simulator connection-pool incident postmortem.
- [`postmortems/05-alembic-autogenerate-skipped-tables.md`](postmortems/05-alembic-autogenerate-skipped-tables.md) — Alembic autogenerate incident postmortem.
- [`postmortems/06-postgres-deadlock-lost-writes.md`](postmortems/06-postgres-deadlock-lost-writes.md) — Postgres deadlock and lost writes incident postmortem.
- [`postmortems/07-benchmark-leaked-the-future.md`](postmortems/07-benchmark-leaked-the-future.md) — benchmark data leakage incident postmortem.
- [`postmortems/08-fallback-never-engaged-stale-forecast.md`](postmortems/08-fallback-never-engaged-stale-forecast.md) — stale forecast fallback incident postmortem.
- [`reference/BUP_CSE_FEST_2026_Participant_Facing_Hackathon_Draft.pdf`](reference/BUP_CSE_FEST_2026_Participant_Facing_Hackathon_Draft.pdf) — participant-facing hackathon brief.
- [`reference/BUP_Fuel_Supply_Simulator_Integration_Guide_Final.pdf`](reference/BUP_Fuel_Supply_Simulator_Integration_Guide_Final.pdf) — simulator integration guide.
- [`reference/extracted/integration_guide.txt`](reference/extracted/integration_guide.txt) — text extracted from the integration guide.
- [`reference/extracted/participant_brief.txt`](reference/extracted/participant_brief.txt) — text extracted from the participant brief.
- [`reference/extracted/simulator_src/__init__.py`](reference/extracted/simulator_src/__init__.py) — extracted simulator source package initializer.
- [`reference/extracted/simulator_src/bus.py`](reference/extracted/simulator_src/bus.py) — extracted simulator event bus source.
- [`reference/extracted/simulator_src/config.py`](reference/extracted/simulator_src/config.py) — extracted simulator configuration source.
- [`reference/extracted/simulator_src/db.py`](reference/extracted/simulator_src/db.py) — extracted simulator database source.
- [`reference/extracted/simulator_src/engine.py`](reference/extracted/simulator_src/engine.py) — extracted simulator engine source.
- [`reference/extracted/simulator_src/faults.py`](reference/extracted/simulator_src/faults.py) — extracted simulator fault source.
- [`reference/extracted/simulator_src/main.py`](reference/extracted/simulator_src/main.py) — extracted simulator application entry point.
- [`reference/extracted/simulator_src/models.py`](reference/extracted/simulator_src/models.py) — extracted simulator data models.
- [`reference/extracted/simulator_src/scenario.py`](reference/extracted/simulator_src/scenario.py) — extracted simulator scenario source.
- [`reference/extracted/simulator_src/schemas.py`](reference/extracted/simulator_src/schemas.py) — extracted simulator schemas.
