# Deployment

This project is delivered as **Docker images and a Compose file**. There is no
cloud account, no cluster and no orchestrator: the whole platform is seven
containers that a judge can start with one command.

```bash
cp .env.example .env
docker compose up --build
```

This document covers the three questions a deployment raises: how the images are
published, how to roll back, and what to check after a deploy.

---

## 1. How the images are published

The `Release` workflow ([`.github/workflows/release.yml`](../.github/workflows/release.yml))
builds both images and pushes them to the GitHub Container Registry.

| Image | Registry path |
|---|---|
| Backend | `ghcr.io/<owner>/fuel-backend` |
| Frontend | `ghcr.io/<owner>/fuel-frontend` |

Each publish writes **three tags**:

| Tag | Moves? | Use it for |
|---|---|---|
| `v1.0.0` | No, once published | The release as a human refers to it |
| `v1.0.0-sha-<full-sha>` | No, once published | **Rollback** — the only tag that cannot be moved |
| `latest` | Yes, on every release | "The newest thing we published" |

The `latest` tag is deliberately *not* what a rollback uses. A tag that moves
cannot be pointed at by a note saying "go back to the good one" — the thing it
referred to has been overwritten. The `sha` tag cannot move, so it is the only
one that can be named in a rollback procedure and still mean the same image
tomorrow.

### Publishing a release

```bash
# 1. Make sure CI is green on main.
# 2. Tag the commit that is being released.
git tag -a v1.0.0 -m "Release v1.0.0"
git push origin v1.0.0
```

The tag push triggers the release workflow, which builds and pushes both
images. The workflow refuses any tag that is not `vMAJOR.MINOR.PATCH`, because
a moving tag that is not a version is how a rollback target disappears.

### Running the published images instead of building locally

```bash
# In .env, point the compose file at the registry instead of a local build.
BACKEND_IMAGE=ghcr.io/<owner>/fuel-backend:v1.0.0
FRONTEND_IMAGE=ghcr.io/<owner>/fuel-frontend:v1.0.0
```

The `docker-compose.yml` builds the backend and frontend locally by default.
To make the registry the source of truth, use the `docker-compose.release.yml`
overlay, which replaces the `build:` blocks with `image:` blocks:

```bash
docker compose -f docker-compose.yml -f docker-compose.release.yml up -d --wait
```

---

## 2. Rolling back

A rollback is "run the previous good image". The procedure is the same for a
bad release and for a bad afternoon.

### What you need

The tag of the version to go back to. `docker compose images` shows what is
running now:

```bash
docker compose images
```

```text
CONTAINER     IMAGE                                    TAG
fuel-backend  ghcr.io/tanimstu068/fuel-backend         v1.0.0
```

### The procedure

```bash
# 1. Find the previous sha tag.
#    The GitHub Actions release summary lists them, and so does the registry:
gh api /users/<owner>/packages/container/fuel-backend/versions \
  --jq '.[] | {tags: .metadata.container.tags, created: .created_at}' | head -40

# 2. Pull the previous image by its sha tag.
docker pull ghcr.io/<owner>/fuel-backend:v1.0.0-sha-<previous-sha>
docker pull ghcr.io/<owner>/fuel-frontend:v1.0.0-sha-<previous-sha>

# 3. Point .env at the previous tags.
#    BACKEND_IMAGE=ghcr.io/<owner>/fuel-backend:v1.0.0-sha-<previous-sha>
#    FRONTEND_IMAGE=ghcr.io/<owner>/fuel-frontend:v1.0.0-sha-<previous-sha>

# 4. Recreate the containers with the old images.
docker compose up -d --wait --no-build

# 5. Verify.
curl -fsS http://localhost:8001/health | jq '.status, .components'
```

Step 4 is `--no-build` on purpose: without it, Compose rebuilds the image
from the working tree and the rollback quietly becomes "the current code",
which is the opposite of what was asked for.

### Rolling back the database

**There is no database migration in this project.** The schema is created by
SQLAlchemy's `create_all` at startup and there is no Alembic revision to
reverse, so a rollback never has to undo a schema change.

That is a deliberate constraint, not an oversight: the platform owns a tiny
schema, and the migration surface that a rollback would have to reverse is
larger than the schema itself. If a schema change is ever needed, it ships with
an explicit downgrade path and this section grows a step for it.

The Postgres volume is **not** removed by a rollback — the audit trail, the
decision history and the demand history survive, which is the point of keeping
them.

### Rolling back to a local build

If the registry is unreachable, the previous working state is in git:

```bash
git checkout v1.0.0
docker compose up -d --wait --build
```

---

## 3. Checking a deploy

The first three are the ones that matter.

```bash
# 1. The backend is ready. (This is a readiness probe: it is 503 while the
#    simulator or the database are down, so a 200 means the whole stack works.)
curl -fsS http://localhost:8001/health | jq '.status, .components'

# 2. The dashboard is served.
curl -fsSo /dev/null -w '%{http_code}\n' http://localhost:5173/

# 3. The API answers with live data, not a cached empty state.
curl -fsS http://localhost:8001/api/state | jq '.instance.tick, .metrics.service_level'

# 4. The audit trail is queryable.
curl -fsSG http://localhost:8001/api/audit --data-urlencode 'limit=5' | jq '.total_matched, .summary.scope'

# 5. The metrics endpoint is scraped.
curl -fsS http://localhost:8001/metrics | grep -c '^http_requests_total'

# 6. Every container is healthy and no restart loop is running.
docker compose ps
docker inspect fuel-backend --format '{{.RestartCount}} {{.State.OOMKilled}}'
```

`RestartCount` should be `0` and `OOMKilled` should be `false`. A backend that
has restarted is a backend that crashed, and the crash is in
`docker compose logs backend`.

### The smoke test

`tests/test_stack.py` checks the live stack end to end and is what CI runs
against a freshly built image:

```bash
docker compose up -d --wait
pytest tests/ -m integration
```

---

## 4. What a deploy does *not* do

* **It does not migrate the database.** See above — there is nothing to
  migrate, and the volume is preserved.
* **It does not reset the simulation.** The simulator's own state lives in its
  volume and survives a deploy. To start the world over, use
  `scripts/reset_demo.sh` (see [`docs/demo-script.md`](demo-script.md)).
* **It does not send anything to the simulator.** Deploying the platform does
  not dispatch fuel. Every allocation still needs a human approval.
