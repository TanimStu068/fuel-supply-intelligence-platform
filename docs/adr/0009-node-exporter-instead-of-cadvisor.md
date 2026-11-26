# ADR 0009 — node-exporter instead of cAdvisor

**Status:** Accepted
**Date:** Day 6 of 7
**Deciders:** the team

---

## Context

The brief requires an observability layer covering **system** metrics: *"CPU,
memory, resource utilization"*. The platform's System dashboard needs a
per-container view, and the usual answer is cAdvisor.

cAdvisor does not work in this environment. Its `/api/v1.3/docker` endpoint
returns `{}`, so Prometheus received **one host-level series with the id `/`
and no per-container breakdown** — the metrics looked like they were being
collected, and they were useless. It also cost **384 MB**, on a shared host
where the whole stack had a 1.5 GB budget.

## Decision

**Remove cAdvisor. Use node-exporter for the host, and the backend's own
cgroup reading for the containers.**

| | cAdvisor | node-exporter |
|---|---|---|
| Memory | 384 MB | **32 MB** |
| Per-container view | empty in this environment | not offered (by design) |
| Host CPU and memory | broken | **working** |
| Disk, network, load average | partial | **working** |

The per-container view that cAdvisor was supposed to provide is served by the
**backend's own cgroup**: it reads `memory.max` and `memory.current` from
`/sys/fs/cgroup` and publishes `container_memory_limit_bytes`,
`container_memory_usage_bytes` and `container_memory_fraction`.

The load test reads the same numbers from the container's own cgroup — the
same counter Docker enforces when it decides to kill a process — rather than
from `docker stats`.

## Alternatives considered

| Alternative | Why not |
|---|---|
| **Fix cAdvisor** | It cannot see this environment's containers. The failure is environmental, and the fix is not in the project's hands. |
| **Use `docker stats` as the source** | It is a CLI, not a scrapeable endpoint, and it is not available inside the container. The cgroup files are. |
| **Prometheus's cAdvisor exporter** | Same underlying problem: it reads the same Docker API that returns `{}`. |
| **No per-container metrics at all** | Rejected, because the cgroup reading costs almost nothing — the backend already runs inside the container it is measuring — and it is what makes the "how close is this container to being killed" alert possible. |
| **node-exporter plus the Docker API** | The Docker API is what returns `{}`. |

## Consequences

**Good.** The System dashboard shows **working** host CPU and memory, disk,
network and load average.

**Good.** The per-container view is *more* accurate than cAdvisor's would
have been. `container_memory_fraction` is the number Docker actually
enforces when it decides to kill the process — so the alert that fires is
*"this container is close to being OOM-killed"*, and it is **correct** rather
than absent.

**Good.** 352 MB saved on a host shared with five other Docker stacks.

**Bad.** The per-container view is **only** memory. CPU per container is not
published. The Day 6 risk table accepts this: *"cAdvisor cannot see the
containers → No per-container CPU or memory in Prometheus → The System
dashboard shows the host's CPU and memory; the load test reads the
container's vitals from `docker stats`."* The load test compensates by
sampling the cgroup directly.

**Bad.** The per-container series exist only while the **backend** is
running. The simulator's, Postgres's and the frontend's memory are visible
only in `docker stats` or the compose output, not in Prometheus.

**Bad.** The cgroup files are a Linux interface. The measurement does not
port to a non-Linux host.

## Evidence

- [`docs/progress/day6.md`](../progress/day6.md) — *cAdvisor is gone*, the
  comparison table, the 32 MB figure, and the risk table
- [`docs/load-test-report.md`](../load-test-report.md) — the cgroup sampling
  in the harness
- [`infra/prometheus/prometheus.yml`](../../infra/prometheus/prometheus.yml)
  — the scrape configuration, with no cadvisor job
- [`backend/app/metrics.py`](../../backend/app/metrics.py) — the cgroup
  reader
