# ADR 0011 — No Loki; the audit trail lives in Postgres

**Status:** Accepted
**Date:** Day 6 of 7
**Deciders:** the team

---

## Context

The brief's observability requirement lists four layers, and the fourth is
**logs**: *"important actions, integration failures, decision events,
recoveries"*. It suggests a stack that *"may include Prometheus, Grafana,
OpenTelemetry, Loki, ELK, Jaeger, or equivalent tools"* and leaves the
choice open.

The platform already has two places where a trail could live:

- **Postgres**, holding `decision_records`, `alerts` and `domain_events` —
  the durable, queryable history.
- **The backend's structured JSON logs**, where every line carries a
  `correlation_id` so one unit of work can be traced end to end.

A third option — shipping the logs into Loki — would put a second store in
front of a trail that already exists.

There is also a measured constraint: the whole stack has a 1.5 GB budget on
a shared host, and the monitoring profile is optional.

## Decision

**Do not use Loki.**

The audit trail is already in Postgres, and putting a second store in front
of it would mean two things that can disagree. The structured-log half of
the requirement is covered by
[`scripts/search_logs.py`](../../scripts/search_logs.py), which searches the
backend's JSON logs:

```bash
scripts/search_logs.py --level ERROR --minutes 10
scripts/search_logs.py --correlation sync-020a8b6e00e4
scripts/search_logs.py --contains "deadlock"
```

`GET /api/audit` presents the three Postgres trails — decisions, domain
events and alerts — as **one searchable stream**, so an operator can ask
*"what happened to this station?"* once instead of joining three tables by
hand.

## Alternatives considered

| Alternative | Why not |
|---|---|
| **Loki for the logs** | A second store in front of a trail that already exists, with the corresponding risk of the two disagreeing. It also costs memory on a host with a 1.5 GB budget. |
| **ELK / Elasticsearch** | The same objection, at a much larger cost — a JVM-based index on a shared host with 203 MiB free. |
| **Postgres for the logs as well** | Possible, but the logs are high-volume and low-value compared to the decision trail, and writing them twice would put write load on the database during exactly the moments the platform is already under stress. |
| **The logs on disk only, with no search tool** | Rejected: `scripts/search_logs.py` gives the same answer with no extra service, and the correlation id makes an end-to-end trace possible. |
| **OpenTelemetry tracing** | Optional in the brief, and the platform's distributed-tracing story is a single process — the correlation id carries that job. |

## Consequences

**Good.** One searchable place for the durable history, and one tool for the
logs. The live endpoint reports **333 decisions, 333 alerts and 40 events**
in a single search.

**Good.** No extra container, no extra memory budget, no second thing that
can disagree with the first.

**Good.** The audit trail is **durable**. Logs on disk are lost when a
container is replaced; the decision history is in Postgres and is served from
`/api/decision-history`.

**Bad.** The logs are only as durable as the container's filesystem. A
container replaced during an incident loses them. The correlation id is what
ties what survives to what it was part of.

**Bad.** `scripts/search_logs.py` reads the container's log files, so it
needs shell access to the running container. It is a tool for an operator at
a terminal, not a dashboard panel.

**Bad.** There is **no log aggregation across containers**. Each container's
logs are searched separately.

**Bad, and fixed late.** The audit endpoint originally read the `decisions`
table, which the schema had but **nothing ever wrote to** — the platform's
decisions go to `recommendations`. The "decision" half of the searchable
trail was permanently empty and a search over it silently returned nothing.
The module now reads the table that is actually written, and a test named
`test_the_decision_trail_reads_the_table_that_is_actually_written` fails if
it ever goes back.

**Bad.** The `decision_records` table **grows without bound**. It is indexed
on status, station and tick; the Day 4 log notes *"Pruning is Day 7 work"*,
and no retention job was written.

## Evidence

- [`docs/progress/day6.md`](../progress/day6.md) — the Loki decision, the
  333/333/40 live figures, and the empty-table bug
- [`docs/progress/day4.md`](../progress/day4.md) — the decision history
  growing without bound
- [`docs/architecture.md`](../architecture.md) §3 — the data sources
- [`scripts/search_logs.py`](../../scripts/search_logs.py)
- [`backend/app/logging_config.py`](../../backend/app/logging_config.py) — the
  structured JSON logs and the correlation id
