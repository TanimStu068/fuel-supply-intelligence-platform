# Configuration reference

Every environment variable the platform reads, where it comes from, what it
does, and what happens when you change it. Every row was read out of the
repository — [`.env.example`](../.env.example),
[`docker-compose.yml`](../docker-compose.yml) and
[`backend/app/config.py`](../backend/app/config.py) — and the three sources
were compared mechanically, so the gaps in §5 are the real ones rather than
an impression.

**The one thing to know first.** The backend's settings are read by
`pydantic-settings` from the **process environment**, and the compose file
passes the backend an **explicit allow-list** of 25 variables in its
`environment:` block. There is **no `env_file:` directive** anywhere in
either compose file. So a variable that is not in that allow-list cannot be
changed through `.env` when the stack runs under Compose — the container
never receives it, and the code falls back to the default baked into
`config.py`. §5 lists the thirteen settings this affects.

---

## 1. Where a variable comes from

```mermaid
flowchart LR
    ENV[".env<br/>(gitignored)"] -->|"docker compose reads it"| CMP["docker-compose.yml"]
    EX[".env.example<br/>(committed)"] -.->|"copy to .env"| ENV
    CMP -->|"environment: block,<br/>explicit allow-list"| BE["backend container<br/>process environment"]
    CMP -->|"environment: block"| SVC["simulator · postgres<br/>frontend · grafana"]
    BE --> CFG["Settings (pydantic-settings)<br/>37 aliased fields"]
    BE --> OSG["os.getenv<br/>CORS_ALLOWED_ORIGINS · LOG_LEVEL"]
    CFG --> APP["the application"]
    OSG --> APP

    style BE fill:#dcfce7,stroke:#15803d,stroke-width:2px
    style ENV fill:#fef3c7,stroke:#b45309
```

| Source | Role |
|---|---|
| [`.env.example`](../.env.example) | The committed contract. 46 variables, all with defaults, no secrets |
| `.env` | The live copy. Gitignored, never committed. The local one holds 25 variables — a subset of the example, all at their default values |
| [`docker-compose.yml`](../docker-compose.yml) | The wiring. 55 `${VAR}` substitutions across 7 services |
| [`docker-compose.release.yml`](../docker-compose.release.yml) | The deployment overlay. Adds `BACKEND_IMAGE` and `FRONTEND_IMAGE` |
| [`backend/app/config.py`](../backend/app/config.py) | The schema. 37 aliased settings with typed defaults and validation bounds |

---

## 2. The backend settings

All 37 aliased fields from `config.py`. "Container" says whether the
variable reaches the backend container under Compose.

### 2.1 Simulator integration

| Variable | Default | Bounds | Purpose | Effect of changing it | Container |
|---|---|---|---|---|---|
| `SIMULATOR_BASE_URL` | `http://simulator:8000` | — | Where the simulator lives | Points the client at a different simulator. **Not in `.env.example`**; compose hard-codes it | ✔ |
| `SIMULATOR_REQUEST_TIMEOUT` | `10.0` | `> 0` | Per-request timeout, seconds | How long a slow simulator call is waited out before it fails | ✔ |
| `SIMULATOR_CONNECT_TIMEOUT` | `3.0` | `> 0` | Connect timeout, seconds | How long a refused connection is waited out | ✔ |
| `SIMULATOR_MAX_RETRIES` | `3` | `0`–`10` | Retries after the first attempt | More retries means a slower failure and more load on a small pool. `0` disables retrying | ✔ |
| `SIMULATOR_RETRY_BASE_DELAY` | `0.2` | `≥ 0` | Backoff base, seconds | The floor of the exponential backoff | ✔ |
| `SIMULATOR_RETRY_MAX_DELAY` | `5.0` | `≥ 0` | Backoff ceiling, seconds | The cap the backoff jitter is drawn under | ✔ |
| `SIMULATOR_HEALTH_TIMEOUT` | `2.0` | `> 0` | Health-probe timeout | On the container healthcheck's critical path — a dead simulator is reported quickly, not waited out | ✔ |
| `CIRCUIT_BREAKER_FAILURES` | `5` | `1`–`100` | Consecutive failures that open the breaker | Lower opens it sooner; higher tolerates more of a single slow call before declaring an outage | ✔ |
| `CIRCUIT_BREAKER_RECOVERY_SECONDS` | `20.0` | `> 0` | How long the breaker stays open | How long before a probe call is let through | ✔ |

### 2.2 Database

| Variable | Default | Bounds | Purpose | Effect of changing it | Container |
|---|---|---|---|---|---|
| `DATABASE_URL` | `postgresql+asyncpg://fuel:fuel@postgres:5432/fuel` | — | The connection URL | Compose builds it from `POSTGRES_USER`/`POSTGRES_PASSWORD`/`POSTGRES_DB`. **The default embeds the credentials `fuel`/`fuel`** | ✔ |
| `DB_POOL_MIN_SIZE` | `1` | `≥ 0` | Minimum pooled connections | The floor of the always-open pool | ✖ |
| `DB_POOL_MAX_SIZE` | `10` | `≥ 1` | Maximum pooled connections | The cap. `max_overflow=0`, so this is the hard ceiling on concurrent database work | ✖ |
| `DB_DEADLOCK_RETRIES` | `3` | `≥ 0` | Retries of a deadlocked transaction | Postgres kills one side of a deadlock with SQLSTATE `40001`; the standard says to retry. **In `.env.example` but not passed to the container** — see §5.2 | ✖ |
| `DB_HEALTH_TIMEOUT` | `3.0` | `> 0` | Database health-probe timeout | How long the readiness probe waits on Postgres | ✖ |

### 2.3 Background sync and the event stream

| Variable | Default | Bounds | Purpose | Effect of changing it | Container |
|---|---|---|---|---|---|
| `SYNC_ENABLED` | `true` | — | Whether the background sync runs | `false` stops the platform polling and listening entirely — it serves only what is already cached | ✔ |
| `SYNC_POLL_INTERVAL` | `5.0` in code / **`15` in `.env.example`** | `> 0` | Seconds between poll cycles | The safety net. **The two sources disagree — see §5.1** | ✔ |
| `SYNC_MIN_INTERVAL` | `3.0` | `> 0` | Floor on the gap between two cycles | The throttle. The simulator publishes a tick event several times a second; without this floor the platform would read the whole world on each one, which is what exhausted the simulator's connection pool | ✔ |
| `SYNC_MAX_CONCURRENCY` | `3` | `1`–`16` | Simulator calls in flight at once | A cycle reads nine endpoints. Unbounded, that is nine connections from a pool of fifteen | ✔ |
| `SSE_RECONNECT_BASE_DELAY` | `1.0` | `> 0` | Backoff base for stream reconnects | The floor of the reconnect delay | ✔ |
| `SSE_RECONNECT_MAX_DELAY` | `30.0` | `> 0` | Backoff ceiling for reconnects | The cap the reconnect delay grows to | ✔ |
| `SSE_READ_TIMEOUT` | `30.0` | `> 0` | How long the stream may stay silent | The simulator sends a keepalive every 15 s, so a stream silent for longer is broken, not quiet. Without a bound the reader waits for ever on a socket that will never answer | ✖ |
| `SSE_STALE_GRACE` | `45.0` | `> 0` | How far the stream may lag before it is dropped | The stream and the REST view are read at different moments, so a few ticks of lag is normal. Short enough to notice a dead stream while it still matters | ✖ |
| `DEMAND_HISTORY_LIMIT` | `500` | `> 0` | Demand observations pulled per ingest cycle | How much history is fetched on each cycle | ✔ |
| `DECISION_HISTORY_LIMIT` | `500` | `≥ 0` | Size of the in-memory recommendation working set | The dashboard polls `/api/recommendations` every few seconds. Unbounded it reached three thousand recommendations and twenty-two megabytes. **`0` disables the bound** — which is what makes the load test's "before" side reproducible | ✖ |

### 2.4 Intelligence

| Variable | Default | Bounds | Purpose | Effect of changing it | Container |
|---|---|---|---|---|---|
| `FORECAST_HORIZON_TICKS` | `96` | `> 0` | How many ticks ahead the forecast reaches | 96 ticks is one simulated day at 15-minute ticks. A six-hour horizon would see a station running dry only six hours before it happens | ✔ |
| `FORECAST_MIN_CONFIDENCE` | `0.35` | `0`–`1` | Confidence the learned model must reach | Raising it demotes the model to the rule-based predictor more often | ✖ |
| `FORECAST_MIN_TRAINING_ROWS` | `30` | `> 0` | Rows the model needs before it is trusted | Below this the platform uses the rule-based predictor and says so. A cold start is a rule-based start | ✖ |
| `FORECAST_HISTORY_LIMIT` | `5000` | `> 0` | Stored observations read per evaluation | How much history the forecaster sees | ✖ |
| `MODEL_TRAIN_INTERVAL` | `300` | `> 0` | Seconds between retrainings | The trainer's schedule. Five minutes is right for production and wrong for a demo — the demo asks for a retrain explicitly | ✔ |
| `PREDICTION_ERROR_AGGREGATE_IN_DATABASE` | `true` | — | Compute the error aggregate in SQL or Python | `false` reproduces the "before" side of the load test's bottleneck — the p99 was ten seconds long when the aggregate was read in Python | ✔ |
| `ERROR_METRIC_WINDOW_TICKS` | `500` | `≥ 0` | How far back the error metric looks | Bounding the query to the recent past turns an unbounded table scan into a bounded index scan. **`0` aggregates the whole table** — the other "before" case | ✖ |
| `DISPATCH_ENABLED` | `true` | — | Whether the platform dispatches fuel in response to risk | Turning it off stops allocations being submitted to the simulator | ✔ |

### 2.5 Observability and lifecycle

| Variable | Default | Bounds | Purpose | Effect of changing it | Container |
|---|---|---|---|---|---|
| `LOG_LEVEL` | `INFO` | — | Log level | Read twice: by `Settings`, and directly by `os.getenv` in [`logging_config.py`](../backend/app/logging_config.py) — the two agree | ✔ |
| `LOG_FORMAT` | `json` | — | Log format | Only `json` is implemented. Changing it has no effect | ✖ |
| `METRICS_ENABLED` | `true` | — | Whether the metrics middleware runs | `false` stops the request counters and the `/metrics` route's data | ✔ |
| `METRICS_PATH` | `/metrics` | — | The metrics route path | The Prometheus scrape target is hard-coded to `/metrics` in the Prometheus config, so changing this would break the scrape | ✖ |
| `SHUTDOWN_GRACE_SECONDS` | `15.0` | `> 0` | Drain time on SIGTERM | Bounded so a wedged dependency cannot hold the container open until Docker escalates to SIGKILL. Compose's `stop_grace_period` is 30 s, longer than the drain | ✔ |

### 2.6 The three settings with no alias

These have no environment alias, so they are **not** configurable from the
environment at all — their value is fixed in the class body.

| Field | Default | Purpose | Effect of changing it |
|---|---|---|---|
| `app_name` | `Fuel Supply Intelligence & Resilience Platform` | The service name the API reports | Served by `GET /` and by `/health` as `"service"` |
| `app_version` | `0.1.0` | The version the API reports | Served by `GET /` and by `/health` as `"version"`. **Not read from the git tag** — the image and the API can disagree. The published `v1.0.0` release and the API's reported `0.1.0` are two different numbers |
| `debug` | `False` | FastAPI's debug flag | **Not read anywhere in the application.** It is a field on the settings class and nothing consumes it |

---

## 3. The variables that are not backend settings

These configure the other services, or Compose itself.

### 3.1 The simulator

| Variable | Default | Service | Purpose | Effect of changing it |
|---|---|---|---|---|
| `SIMULATOR_IMAGE` | `asifmahmoud414/bup-fuel-supply-simulator:1.0.0` | simulator | Which simulator image to run | The organizer's published image. The simulator's own code is never modified by this project |
| `SIMULATOR_START_MODE` | `paused` | simulator | `paused` or `running` | `paused` is the default so a stack comes up without the world running away. `running` starts the clock |
| `SIMULATION_SPEED` | `8` | simulator | Wall-clock ticks per second | **Not a wall-clock estimate.** The runner sleeps `1 / SIMULATION_SPEED` *between* ticks and the tick itself takes real time, so the achievable rate is always below the nominal one. Measured: 8 configured → 6.67 ticks/second, so one simulated day is ~14.4 s of wall clock. Measure it, don't compute it |
| `TICK_MINUTES` | `15` | simulator | Simulated minutes per tick | Env-only and applied at startup, overwriting whatever the scenario YAML says. 96 ticks = 24 h because the demand model divides daily litres by 96. Changing it changes the **size** of each demand step, not the number of ticks per day |
| `SIMULATOR_PORT` | `8000` | simulator | The published port | The host port the simulator is reachable on |

### 3.2 The database

| Variable | Default | Service | Purpose | Effect of changing it |
|---|---|---|---|---|
| `POSTGRES_USER` | `fuel` | postgres | The database user | Also interpolated into `DATABASE_URL` |
| `POSTGRES_PASSWORD` | `change-me-in-env` | postgres | The database password | Deliberately obviously fake: a stack started without a `.env` is *loudly* insecure rather than quietly insecure. Also interpolated into `DATABASE_URL` |
| `POSTGRES_DB` | `fuel` | postgres | The database name | Also interpolated into `DATABASE_URL` |
| `POSTGRES_PORT` | `5432` | postgres | The published port | The host port Postgres is reachable on |

### 3.3 The frontend

| Variable | Default | Service | Purpose | Effect of changing it |
|---|---|---|---|---|
| `FRONTEND_PORT` | `5173` | frontend | The published port | The host port the dashboard is served on. **Also interpolated into `API_BASE_URL`**, so changing it moves both together |
| `API_BASE_URL` | `http://localhost:${BACKEND_PORT:-8001}` | frontend | Where the dashboard finds the API | Read by [`frontend/docker-entrypoint.sh`](../frontend/docker-entrypoint.sh), which rewrites `dist/config.js` at container start. Vite inlines `import.meta.env` at build time, so this has to be runtime configuration — one image works against any backend address |

### 3.4 Monitoring

| Variable | Default | Service | Purpose | Effect of changing it |
|---|---|---|---|---|
| `PROMETHEUS_PORT` | `9090` | prometheus | The published port | Prometheus's own web port |
| `GRAFANA_PORT` | `3000` | grafana | The published port | Grafana's own web port |
| `NODE_EXPORTER_PORT` | `9100` | node-exporter | The published port | node-exporter's own port |
| `GRAFANA_ADMIN_USER` | `admin` | grafana | The Grafana admin user | Passed as `GF_SECURITY_ADMIN_USER` |
| `GRAFANA_ADMIN_PASSWORD` | `change-me-in-env` | grafana | The Grafana admin password | Passed as `GF_SECURITY_ADMIN_PASSWORD`. Grafana is the only service here with a real credential to protect |

### 3.5 Compose itself and the resource budget

| Variable | Default | Purpose | Effect of changing it |
|---|---|---|---|
| `COMPOSE_PROFILES` | `core` | Which profile starts | `core` is the platform alone. The monitoring stack is added with `--profile core --profile monitoring`. **Not referenced by the compose file itself** — Compose reads it directly |
| `CORS_ALLOWED_ORIGINS` | `http://localhost:5173,http://localhost:3000,http://127.0.0.1:5173,http://localhost:4173,http://127.0.0.1:4173` | The browser origins the API accepts | Read with `os.getenv`, not through `Settings`. Port 4173 is the Playwright preview server's port; without it the browser rejects every request and the dashboard shows the backend as unreachable |

**The resource budget.** Every container carries a memory limit, a CPU limit
and a PID limit. These are the host-level contract — the host has 7.1 GB of
RAM and is shared with other projects' stacks.

| Variable | Default | Service | Effect of changing it |
|---|---|---|---|
| `SIMULATOR_MEM_LIMIT` / `SIMULATOR_CPUS` | `384m` / `0.75` | simulator | The simulator is the organizer's image; the ceiling stops a leak reaching the host |
| `BACKEND_MEM_LIMIT` / `BACKEND_CPUS` | `384m` / `1.0` | backend | The platform has been seen holding ~250 MiB |
| `POSTGRES_MEM_LIMIT` / `POSTGRES_CPUS` | `256m` / `0.75` | postgres | |
| `FRONTEND_MEM_LIMIT` / `FRONTEND_CPUS` | `64m` / `0.25` | frontend | It is a static bundle; saying so stops it competing with the backend |
| `PROMETHEUS_MEM_LIMIT` / `PROMETHEUS_CPUS` | `192m` / `0.5` | prometheus | Retention is separately bounded to 2 days and 500 MB |
| `GRAFANA_MEM_LIMIT` / `GRAFANA_CPUS` | `192m` / `0.5` | grafana | |
| `NODE_EXPORTER_MEM_LIMIT` / `NODE_EXPORTER_CPUS` | `32m` / `0.1` | node-exporter | It reads the host's counters and publishes almost nothing |

### 3.6 The release overlay only

| Variable | Default | Purpose | Effect of changing it |
|---|---|---|---|
| `BACKEND_IMAGE` | `ghcr.io/tanimstu068/fuel-backend:latest` | Which published backend image to run | **Not in `.env.example`** — see §5.3. Used by `docker-compose.release.yml` only |
| `FRONTEND_IMAGE` | `ghcr.io/tanimstu068/fuel-frontend:latest` | Which published frontend image to run | **Not in `.env.example`** — see §5.3 |

---

## 4. Ports and profiles

### 4.1 The published ports

Every port is published as `"${VAR:-default}:<container-port>"`. **None is
bound to a loopback address** — Docker publishes on `0.0.0.0` by default, so
every one of them is reachable from the host's network.

| Service | Host port | Container port | Variable | Default |
|---|---|---|---|---|
| simulator | `SIMULATOR_PORT` | 8000 | — | 8000 |
| backend | `BACKEND_PORT` | 8000 | — | 8001 |
| postgres | `POSTGRES_PORT` | 5432 | — | 5432 |
| frontend | `FRONTEND_PORT` | 5173 | — | 5173 |
| prometheus | `PROMETHEUS_PORT` | 9090 | — | 9090 |
| grafana | `GRAFANA_PORT` | 3000 | — | 3000 |
| node-exporter | `NODE_EXPORTER_PORT` | 9100 | — | 9100 |

The backend listens on 8000 *inside* the container and is published on
8001 *outside* it — the container's healthcheck uses `127.0.0.1:8000`, so
the internal port cannot change.

### 4.2 The two profiles

| Profile | Services | Stated budget | Started by |
|---|---|---|---|
| `core` | simulator, backend, postgres, frontend | ~1.1 GB | `COMPOSE_PROFILES=core`, so a plain `docker compose up` |
| `monitoring` | prometheus, grafana, node-exporter | ~0.4 GB | `--profile core --profile monitoring` |

```bash
# the platform alone
docker compose up -d --wait

# the platform plus the dashboards
docker compose --profile core --profile monitoring up -d --wait
```

The split exists because of the host, not the application: the platform used
to be the reason the shared desktop froze, because the scheduler had no
budget to work with. **The monitoring profile is switched off during a load
test** — it competes for the same cores the backend is trying to use.

### 4.3 The published port and the simulator's own pool

The simulator's connection pool holds fifteen connections. The backend's
fan-out is bounded at `SYNC_MAX_CONCURRENCY=3` and the poll floor at
`SYNC_MIN_INTERVAL=3`. Raising the concurrency without lowering the poll
rate is what exhausts the pool — that is the incident written up in
[`docs/postmortems/04-simulator-connection-pool-exhaustion.md`](postmortems/04-simulator-connection-pool-exhaustion.md).

---

## 5. Gaps between the three sources

These are the discrepancies, found by comparing the three sources
mechanically. None of them is a runtime failure — every unset variable falls
back to its documented default — but each is a difference between what the
documentation promises and what the code does.

### 5.1 `SYNC_POLL_INTERVAL` has two different defaults

| Source | Default |
|---|---|
| `config.py` line 100 | `5.0` |
| `.env.example` line 36 | `15` |
| `docker-compose.yml` | `15` (passed as `${SYNC_POLL_INTERVAL:-15}`) |

**The most recent verified value is 15.** The compose file and `.env.example`
agree on 15, and the compose file is what actually determines the running
value. The `5.0` in `config.py` is the fallback for a backend run *outside*
Compose.

This is a real disagreement, but a benign one: the two values differ in
their *documentation* — `config.py` still carries the comment *"Seconds
between two REST poll cycles"* without the reasoning, while `.env.example`
explains that the poll is the safety net and the SSE stream is the hint. The
effective interval under Compose is **15 seconds**.

### 5.2 Thirteen backend settings cannot be changed through `.env`

The compose `environment:` block for the backend lists **25** variables. The
`Settings` class has **37** aliased fields. The thirteen that are not passed
into the container always take their `config.py` default when the stack runs
under Compose.

| Setting | Default | In `.env.example`? | In the container? |
|---|---|---|---|
| `DB_POOL_MIN_SIZE` | `1` | ✖ | ✖ |
| `DB_POOL_MAX_SIZE` | `10` | ✖ | ✖ |
| `DB_DEADLOCK_RETRIES` | `3` | **✔** | **✖** |
| `DB_HEALTH_TIMEOUT` | `3.0` | ✖ | ✖ |
| `DECISION_HISTORY_LIMIT` | `500` | ✖ | ✖ |
| `SSE_READ_TIMEOUT` | `30.0` | ✖ | ✖ |
| `SSE_STALE_GRACE` | `45.0` | ✖ | ✖ |
| `FORECAST_MIN_CONFIDENCE` | `0.35` | ✖ | ✖ |
| `FORECAST_MIN_TRAINING_ROWS` | `30` | ✖ | ✖ |
| `FORECAST_HISTORY_LIMIT` | `5000` | ✖ | ✖ |
| `ERROR_METRIC_WINDOW_TICKS` | `500` | ✖ | ✖ |
| `LOG_FORMAT` | `json` | ✖ | ✖ |
| `METRICS_PATH` | `/metrics` | ✖ | ✖ |

**`DB_DEADLOCK_RETRIES` is the one that matters.** It is documented in
`.env.example` — so a reader will reasonably believe setting it to `0` turns
off deadlock retrying — but it is **not** in the compose `environment:`
block, so the container never receives it and the value in `.env` is
**silently ignored**. The effective value is always the `config.py` default
of `3`.

The same is true of the other twelve, but they are absent from
`.env.example` as well, so nothing in the documentation promises they are
settable. **`DB_DEADLOCK_RETRIES` is the only one where the documentation
and the wiring disagree.**

There is a way to change any of them — run the backend with the variable
passed directly, or add it to the compose `environment:` block — but as the
files stand, no `.env` value reaches the container for these thirteen.

### 5.3 Two variables are used by the compose file but absent from `.env.example`

| Variable | Used by | Default |
|---|---|---|
| `BACKEND_IMAGE` | `docker-compose.release.yml` | `ghcr.io/tanimstu068/fuel-backend:latest` |
| `FRONTEND_IMAGE` | `docker-compose.release.yml` | `ghcr.io/tanimstu068/fuel-frontend:latest` |

These are the variables a **rollback** needs to set — see
[`docs/cicd-release-and-rollback.md`](cicd-release-and-rollback.md) §6.2 —
and they are the ones a reader will not find in `.env.example`. The same
gap is noted in that document's §7.5.

### 5.4 Two variables are in `.env.example` but referenced nowhere in the compose file

| Variable | Why |
|---|---|
| `COMPOSE_PROFILES` | **Not a gap.** Compose reads `COMPOSE_PROFILES` from the environment itself, before it parses the file. It is in `.env.example` for exactly that reason |
| `DB_DEADLOCK_RETRIES` | **A real gap** — see §5.2 |

### 5.5 `COMPOSE_PROFILES` and the two profiles

`COMPOSE_PROFILES=core` in `.env` means a plain `docker compose up` starts
the four core services. The `monitoring` profile has to be asked for
explicitly. Both are named in the compose file's own header comment.

### 5.6 Two settings are read outside the `Settings` class

| Variable | Where | Why |
|---|---|---|
| `CORS_ALLOWED_ORIGINS` | `os.getenv` in [`backend/app/main.py`](../backend/app/main.py) | Read at module import time, before `Settings` is instantiated, because the CORS middleware is added to the app at construction |
| `LOG_LEVEL` | `os.getenv` in [`backend/app/logging_config.py`](../backend/app/logging_config.py), **and** in `Settings` | The logging configuration is installed before the app is built. The two reads use the same default, so they agree |

`CORS_ALLOWED_ORIGINS` is the only variable in the platform that has no
`Settings` field at all.

### 5.7 The local `.env` is a subset, not a copy

The working `.env` holds **25** variables; `.env.example` holds **46**. The
twenty-one that are absent from the live `.env` are all at their default
values, so the running behaviour is unchanged. The absent ones are the
resource ceilings, `CORS_ALLOWED_ORIGINS`, `DB_DEADLOCK_RETRIES`,
`PREDICTION_ERROR_AGGREGATE_IN_DATABASE`, `SHUTDOWN_GRACE_SECONDS`,
`SIMULATOR_HEALTH_TIMEOUT` and the two circuit-breaker values.

---

## 6. Summary table

| | Count |
|---|---:|
| Variables in `.env.example` | 46 |
| Variables in the local `.env` | 25 |
| `${VAR}` substitutions across both compose files | 55 |
| Aliased settings in `config.py` | 37 |
| Settings passed into the backend container by Compose | 25 |
| Settings **not** reachable through `.env` under Compose | 13 |
| Variables in `.env.example` but not used by any compose file | 2 |
| Variables used by a compose file but not in `.env.example` | 2 |
| Published ports | 7 |
| Compose profiles | 2 |

---

## 7. Verification commands

Every claim above can be re-checked with these. They are read-only.

```bash
# the three sources, counted
grep -cE '^[A-Z][A-Z0-9_]*=' .env.example                  # 46
grep -c 'alias="' backend/app/config.py                     # 37
grep -oE '\$\{[A-Z][A-Z0-9_]*' docker-compose.yml docker-compose.release.yml \
  | sed 's/.*://' | sort -u | wc -l                         # 55

# the settings the backend container receives
docker compose config --format json \
  | python3 -c "import json,sys; d=json.load(sys.stdin); \
      print(sorted(d['services']['backend']['environment']))"

# the variables with no Settings field
grep -rn "os.getenv" backend/app/ --include='*.py'

# there is no env_file directive — the environment: block is the only channel
grep -c "env_file" docker-compose.yml docker-compose.release.yml

# the profiles and the ports
grep -nE "profiles:" docker-compose.yml
grep -nE '\$\{[A-Z0-9_]+:-[0-9]+\}:' docker-compose.yml

# the settings that cannot be set through .env
grep -oE 'alias="[A-Z_]+"' backend/app/config.py | sed 's/alias="//; s/"//' | sort > /tmp/cfg.txt
docker compose config --format json \
  | python3 -c "import json,sys; d=json.load(sys.stdin); \
      print(sorted(set(open('/tmp/cfg.txt').read().split()) - set(d['services']['backend']['environment'])))"
```

---

## 8. Related documents

| Document | What it holds |
|---|---|
| [`.env.example`](../.env.example) | The committed contract — 46 variables with their defaults |
| [`docker-compose.yml`](../docker-compose.yml) | The wiring — 7 services, 2 profiles, the resource budget, and every `environment:` block |
| [`docker-compose.release.yml`](../docker-compose.release.yml) | The deployment overlay — swaps `build:` for `image:` |
| [`backend/app/config.py`](../backend/app/config.py) | The schema — 37 settings, their defaults and their validation bounds |
| [`docs/deployment.md`](deployment.md) | Publishing and rollback, including where the image variables fit |
| [`docs/cicd-release-and-rollback.md`](cicd-release-and-rollback.md) | The workflows, the image tags, and the rollback that sets `BACKEND_IMAGE` |
| [`docs/runbook.md`](runbook.md) | Operating the platform — the degraded mode, the kill-switch, the resource budget |
| [`docs/observability-guide.md`](observability-guide.md) | The metrics, the alerts and the resource budget per container |
| [`docs/simulator-notes.md`](simulator-notes.md) | What the running simulator actually does — including how `TICK_MINUTES` and `SIMULATION_SPEED` behave |
| [`docs/testing-strategy-and-report.md`](testing-strategy-and-report.md) | What the CI jobs run, and the coverage floors the `coverage` job enforces |
