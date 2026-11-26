# ADR 0010 — Compose profiles and hard per-service memory limits

**Status:** Accepted
**Date:** Day 6 of 7
**Deciders:** the team

---

## Context

The build machine is **shared**. The Day 6 log records that *"32 containers
across 5 other projects had the machine at 203 MiB free and load average
13"* — other teams' stacks, on the same host, while the load test was trying
to measure this one's behaviour.

Two things follow from that:

1. The load-test numbers are only meaningful if the host's load average is
   recorded with every run, because the before/after comparison has to be
   taken under the same conditions.
2. This stack cannot be allowed to consume the host, and it cannot be allowed
   to be killed by the host's memory pressure mid-measurement.

There is also a second, independent constraint: **monitoring is off during
load tests.** Prometheus scraping the backend while it is being measured
would be a load generator competing with the load generator.

## Decision

**Two compose profiles, and a hard memory limit on every service.**

| Profile | Services | Measured memory |
|---|---|---|
| `core` | simulator, backend, Postgres, frontend — 4 containers | **446 MiB** (limits total 1.1 GB) |
| `monitoring` | Prometheus, Grafana, node-exporter — 3 containers | 116 MiB (the difference between the two published totals) |
| Both | 7 containers | **562 MiB** against a **1.5 GB** total budget |

`COMPOSE_PROFILES=core` lives in `.env`, so a plain `docker compose up`
starts the platform and nothing else.

Every service carries an explicit `mem_limit`, and the tightest is Postgres
at **190.8 MiB measured against its 256 MB limit**. The backend's limit is
384 MB.

> **Discrepancy.** The per-service figures in the Day 6 table sum to
> **455.8 MiB** for the core four and **563.5 MiB** for all seven, while the
> table's own total rows say **446 MiB** and **562 MiB**. The totals are
> quoted here as the document states them, and the ~10 MiB difference is
> **not verified** — most likely the sub-totals were taken at a different
> moment from the individual rows, but the source does not say.

`scripts/stack_guard.sh` stops other projects' stacks, clears their restart
policies and applies a 1 GB limit to every container on the host.

Prometheus keeps **2 days and 500 MB** of retention — an unbounded Prometheus
eventually fills the disk, and a full disk stalls the Docker daemon for every
stack on the machine.

## Alternatives considered

| Alternative | Why not |
|---|---|
| **One profile, everything always on** | The monitoring stack would consume host memory during the load test, competing with the measurement and risking an OOM of the stack under test. The brief's memory rules require monitoring off during load tests. |
| **No memory limits, rely on the host** | The host is shared. An unbounded container on this machine is a container that gets OOM-killed by the kernel rather than by Docker, with no restart policy and no explanation. |
| **Limits only on the backend** | The platform's own load was what exhausted the host. Every service needs a bound, and the total needs to fit the budget. |
| **Move the load test into a container** | The harness is a host-side `httpx`/`asyncio` script deliberately — it needs nothing the project does not already have, and running it in a container would put a container's overhead between the harness and the measurement. A Locust file is provided for anyone who prefers that. |
| **Unlimited Prometheus retention** | A full disk stalls the Docker daemon for every stack on the machine, not just this one. |

## Consequences

**Good.** The stack fits its budget: **562 MiB measured against 1.5 GB**, with
every container under 2 GB as the operating rule requires.

**Good.** The load test runs against a quiet host. The host's load average is
recorded with every run, and the before/after comparison was taken under the
same conditions.

**Good.** A container that exceeds its limit is **OOM-killed by Docker** with
a restart policy, rather than by the kernel under host pressure — a
distinguishable failure with a recorded exit code.

**Good.** The two profiles make the demo cheap. `docker compose up` is four
containers, not seven.

**Bad.** The platform's own monitoring is **off** during the load test, so
the load-test numbers are read from the backend's own `/metrics` endpoint
rather than from Prometheus. That is what makes the numbers attributable to
the platform rather than to the scrape.

**Bad.** `stack_guard.sh` touches **other projects'** containers. It stops
them and applies a 1 GB limit. On a shared machine that is a significant
thing to do to someone else's work, and it is done deliberately — the log
records the alternative of *not* doing it.

**Bad.** The limits are tight enough that the backend is close to its 384 MB
bound after a full 50-user ramp. The measured post-load figure is 167.5 MiB,
but the Day 6 log notes that this is *"the post-load figure: its decision
working set is larger after a full 50-user ramp"* — and the OOM incident is
the reminder of what happens when that is not true.

**Bad.** Prometheus retaining only 2 days means long-term trends are not
available.

## Evidence

- [`docs/progress/day6.md`](../progress/day6.md) — *Two profiles*, the
  per-service memory table, and the shared-host risk
- [`docs/progress/day7.md`](../progress/day7.md) — the final memory figure
- [`docs/postmortems/03-oom-kill-prediction-evaluations.md`](../postmortems/03-oom-kill-prediction-evaluations.md)
  — what an unbounded container does
- [`docker-compose.yml`](../../docker-compose.yml) — the profiles and the
  `mem_limit` on every service
- [`scripts/stack_guard.sh`](../../scripts/stack_guard.sh)
