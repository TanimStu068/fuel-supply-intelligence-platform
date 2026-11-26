# ADR 0001 — FastAPI + Postgres as the backend stack

**Status:** Accepted
**Date:** Day 1 of 7
**Deciders:** the team

---

## Context

The brief asks for *"an operator-facing application, backend, intelligence
component, deployment, observability, resilience, and load testing"* built
against an organizer-provided simulator that exposes a JSON REST API with two
documented error envelope shapes.

Two things constrain the choice. First, the simulator is a **local, single-tenant
container** — there is no central server to federate with. Second, the platform
has to survive the simulator and the database failing independently, which
means the read path must be able to answer without either of them.

The stack was chosen on Day 1, before the intelligence layer, the decision
engine or the dashboard existed. Everything built afterwards inherited it.

## Decision

**Backend:** Python 3.12 with FastAPI, SQLAlchemy 2.0 async and `asyncpg`,
as a single deployable service.

**Database:** PostgreSQL, reached through the same async stack, holding the
append-only trails — `state_snapshots`, `inventory_snapshots`,
`demand_observations`, `allocations`, `domain_events`, `alerts`,
`recommendations`, `decision_records`, `system_health`, plus the
`demand_forecasts`, `risk_assessments` and `prediction_evaluations` tables
added later.

**Frontend:** React + Vite + TypeScript.

**Repository layout:** a monorepo — `backend/`, `frontend/`, `infra/`,
`docs/`, `scripts/`, `tests/`.

The schema is owned by Alembic; the database URL comes from the environment
so no credential lives in a committed file.

## Alternatives considered

| Alternative | Why not |
|---|---|
| **SQLite** | The simulator already uses SQLite behind a `QueuePool` of five connections plus ten overflow, and it leaks under sustained polling. Putting a second SQLite in the chain would repeat the failure mode the platform spent Day 6 diagnosing. |
| **MongoDB / a document store** | The platform's data is relational and append-only — a demand observation is a row of `(station, fuel, tick)`, and the de-duplication is a unique constraint plus an upsert. A document store would have made the de-duplication an application concern instead of a database guarantee. |
| **Node.js / Express** | Viable, and the frontend is TypeScript. The deciding factor was the async client and the scientific stack: `scikit-learn` for the learned predictor and `asyncpg` for Postgres were both already available in Python, and the forecast runs on a worker thread rather than blocking the event loop. |
| **A streaming platform (Kafka / NATS)** | The simulator has exactly one push channel — SSE — and one write endpoint. A message bus between the simulator and the backend would have added a component that can disagree with the world it describes. |
| **Microservices** | The brief warns against complexity that does not improve the solution, and the hard problems here were correctness ones — a stale snapshot, a leaky benchmark — not distribution ones. Splitting the backend would have made those harder to reason about without making them better. |

## Consequences

**Good.** One language across the intelligence layer, the backend and the
scripts. One dependency set, one Dockerfile, one process to reason about
when something fails. `asyncpg` and `httpx` are both async, so the sync
cycle's nine-endpoint fan-out is one `asyncio` task rather than a thread
pool. Postgres gives real transactions, which turned out to matter: the
decision history and the simulator's allocations have to agree, and the
platform retried `40001` deadlocks rather than papering over them.

**Bad.** Python's GIL means the forecast evaluation is CPU-bound. It is moved
to a worker thread with `asyncio.to_thread` so the event loop keeps serving
requests while it runs — the GIL still applies, but the engine's own work
releases it often enough for the loop to get a look in.

**Bad.** FastAPI's `/health` is a *readiness* probe by default, and it
correctly answers 503 while a dependency is down. The CI smoke test asserted
the wrong thing about this and was green against a broken stack — it booted
the backend with no simulator and no database, then expected `/health` to
return 200. The job now starts the simulator and Postgres and checks the
backend against them.

**Good, but awkward.** Alembic autogenerate diffs the models against the
database *as it finds it*. Two tables that `create_all` had already made were
invisible to the diff, so the generated migration was missing them — a fresh
database would have been missing two tables. The migration was regenerated
against an empty schema.

## Evidence

- [`docs/progress/day1.md`](../progress/day1.md) §1–§2, and the *Bugs the
  verification caught* table
- [`docs/progress/day2.md`](../progress/day2.md) — the eight-table schema
- [`docs/bug-catalog.md`](../bug-catalog.md) B1, B2, B3, B4
- [`docs/architecture.md`](../architecture.md) §1, §2
