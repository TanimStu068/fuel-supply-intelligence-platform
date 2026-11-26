# CI/CD, release and rollback

The two GitHub Actions workflows, every job in them, how a release is
published, what the `v1.0.0` tag points at, and the exact commands to roll
back to a previous image.

Everything here was read from the repository: the workflow files themselves,
the git history, the compose files and the existing documentation. **No
workflow was run for this document**, and §7 lists precisely what could not
be verified from the repository — the GHCR push in particular.

---

## 1. The two workflows

```mermaid
flowchart LR
    P["push to main<br/>or a pull request"] --> CI["ci.yml<br/>8 jobs"]
    T["push a v* tag"] --> REL["release.yml<br/>1 job"]
    M["workflow_dispatch"] --> REL

    CI --> L["lint"]
    CI --> TE["test"]
    CI --> F["frontend"]
    CI --> E["frontend-e2e"]
    CI --> D["docker-build<br/>needs: lint, test"]
    CI --> CC["compose-config"]
    CI --> CS["compose-smoke<br/>needs: compose-config"]
    CI --> CV["coverage"]

    REL --> GHCR[("GHCR<br/>3 tags per image")]

    style REL fill:#fef3c7,stroke:#b45309,stroke-width:3px
    style GHCR fill:#fee2e2,stroke:#b91c1c,stroke-width:3px
```

| Workflow | File | Trigger | Jobs |
|---|---|---|---:|
| CI | [`.github/workflows/ci.yml`](../.github/workflows/ci.yml) | `push` to `main`, `pull_request` to `main`, `workflow_dispatch` | 8 |
| Release | [`.github/workflows/release.yml`](../.github/workflows/release.yml) | `push` of a `v*` tag, `workflow_dispatch` | 1 |

Both workflows set a `concurrency` group. They differ in what happens when
two runs overlap:

| Workflow | `cancel-in-progress` | Why |
|---|---|---|
| `ci.yml` | `true` | A newer push makes the older run irrelevant — it is measuring code that is no longer on the branch |
| `release.yml` | `false` | Two releases must not interleave; the second waits for the first |

---

## 2. The CI workflow, job by job

All eight jobs run on `ubuntu-latest`. `ci.yml` declares **no `permissions`
block**, so the job tokens carry the repository's default permissions — the
workflow needs no elevated access, because it pushes nothing.

### 2.1 `lint` — *Lint (ruff)*

| | |
|---|---|
| Working directory | `backend` |
| Steps | checkout → setup Python 3.12 (pip cache) → `pip install ruff` → `ruff check .` → `ruff format --check .` |
| Gate | Both ruff commands must exit 0 |
| Python | `PYTHON_VERSION: "3.12"`, set at the workflow level |

The workflow installs `ruff` on its own rather than installing the project's
`dev` extra, because linting needs nothing from `pyproject.toml` beyond the
configuration.

### 2.2 `test` — *Test (pytest)*

| | |
|---|---|
| Working directory | `backend` |
| Steps | checkout → setup Python 3.12 → `pip install -e ".[dev]"` → `pytest -q --junitxml=pytest-report.xml` |
| Artefact | `backend/pytest-report.xml`, uploaded with `if: always()` and `if-no-files-found: ignore` |

The JUnit report is uploaded even when the job fails, so a failing run still
leaves a readable report behind.

### 2.3 `frontend` — *Frontend (typecheck + build)*

| | |
|---|---|
| Working directory | `frontend` |
| Steps | checkout → setup Node 20 (npm cache) → `npm install` → `npm run typecheck` → `npm run test` → `npm run build` |
| Gate | All three must exit 0 |

`npm run test` is `vitest run` — the 22 component tests. The build is part of
the gate, so a TypeScript error that only appears under `tsc -b` fails the
build rather than the demo.

### 2.4 `frontend-e2e` — *Frontend e2e (playwright)*

| | |
|---|---|
| Working directory | `frontend` |
| Steps | checkout → setup Node 20 → `npm install` → `npm run test:e2e:install` → `npm run build` → `docker compose up -d --wait` → `npm run test:e2e` |
| Artefact | `frontend/playwright-report`, uploaded `if: failure()` |

This is the job that brings the **whole stack** up — the organizer's
simulator, Postgres, the backend and the frontend — because the end-to-end
tests drive a real browser against a real backend. The Playwright config
runs one worker with no retries and retains traces and screenshots on
failure.

### 2.5 `docker-build` — *Docker build*

| | |
|---|---|
| `needs` | `[lint, test]` |
| Steps | checkout → buildx → build the backend image → build the frontend image → **smoke test the backend image** |

Both images are built with `push: false` and `load: true`, so they land in
the runner's local daemon and can be started.

The smoke test is the part worth reading. It does not merely build the
image — it runs it against **real dependencies**:

```bash
docker network create fuel-ci-net || true

docker run -d --name fuel-ci-simulator --network fuel-ci-net \
  -e SIMULATOR_START_MODE=paused \
  asifmahmoud414/bup-fuel-supply-simulator:1.0.0
docker run -d --name fuel-ci-postgres --network fuel-ci-net \
  -e POSTGRES_USER=fuel -e POSTGRES_PASSWORD=fuel -e POSTGRES_DB=fuel \
  postgres:16-alpine
docker run -d --name fuel-backend-ci --network fuel-ci-net -p 18001:8000 \
  -e SIMULATOR_BASE_URL=http://fuel-ci-simulator:8000 \
  -e DATABASE_URL=postgresql+asyncpg://fuel:fuel@fuel-ci-postgres:5432/fuel \
  fuel-supply-backend:ci

for i in $(seq 1 45); do
  if curl -fsS http://127.0.0.1:18001/health > /dev/null 2>&1; then
    break
  fi
  if [ "$i" -eq 45 ]; then
    echo "the backend did not become healthy"
    docker logs fuel-backend-ci
    exit 1
  fi
  sleep 2
done

docker rm -f fuel-backend-ci fuel-ci-simulator fuel-ci-postgres
```

The comment above it explains why the dependencies are needed: *"The
backend's `/health` is a *readiness* probe: it reports 503 while the
simulator or the database are down."* A 200 means the whole stack works, not
just that the process started.

Note the credentials here — `POSTGRES_PASSWORD=fuel` — are for a
throwaway container created and destroyed inside the CI job. They are not a
secret and are not a production credential.

### 2.6 `compose-config` — *Validate docker-compose*

| | |
|---|---|
| Steps | checkout → `docker compose config --quiet` |
| Gate | The compose file must parse |

### 2.7 `compose-smoke` — *Compose smoke test (/health)*

| | |
|---|---|
| `needs` | `[compose-config]` |
| Steps | checkout → `cp .env.example .env` → `docker compose up -d --wait --wait-timeout 300` → check `/health` → check the dashboard → check `/api/state` → run the integration suite → dump the logs on failure → tear down |

The comment above the job calls it *"the one command a judge runs"*, and it
is written to match the README's quick start exactly. The four checks:

| Check | Command | What it proves |
|---|---|---|
| `/health` | `curl` then `python3 scripts/check_health.py /tmp/health.json` | Every component — `backend`, `simulator`, `database` — is `ok`. A 200 with a broken component inside it is a failure |
| The dashboard | `curl -o /dev/null -w '%{http_code}'` | The frontend container serves a 200 |
| `/api/state` | `curl` then `python3 scripts/check_state.py /tmp/state.json` | The state carries a **whole network** — non-empty `stations`, `depots` and `routes`. A healthy backend serving an empty world is a failure |
| The integration suite | `python -m pytest tests/ -m integration -q` | The cross-service suite against the live stack |

On failure it dumps `docker compose logs --no-color`; the teardown is
`if: always()`, so the stack is removed whether the job passed or not.

### 2.8 `coverage` — *Test coverage*

| | |
|---|---|
| Working directory | `backend` |
| Steps | checkout → setup Python 3.12 → `pip install -e ".[dev]"` and `pytest-cov` → `pytest -q --cov=app --cov-report=term-missing --cov-report=json:coverage.json` → `python3 scripts/check_coverage.py backend/coverage.json` |
| Artefact | `backend/coverage.json`, uploaded `if: always()` |

The gate is [`scripts/check_coverage.py`](../scripts/check_coverage.py),
which enforces **seven per-module floors** plus a total floor:

| Module | Floor |
|---|---:|
| `app/decision/optimizer.py` | 90 % |
| `app/decision/service.py` | 90 % |
| `app/decision/validator.py` | 90 % |
| `app/decision/fallback.py` | 90 % |
| `app/intelligence/engine.py` | 90 % |
| `app/simulator_client.py` | 90 % |
| `app/intelligence/forecasting.py` | 85 % |
| **Total** | **80 %** |

The reason for per-module floors rather than one average is in the script's
own docstring: *"A total coverage number hides where the risk is."* An
average lets one module rot while the number stays green.

### 2.9 The job dependency graph

```
lint ─────┐
          ├──► docker-build
test ─────┘

compose-config ──► compose-smoke

frontend          (independent)
frontend-e2e      (independent)
coverage          (independent)
```

**Four of the eight jobs are independent.** Nothing prevents the `coverage`
job from running while `compose-smoke` is still bringing a stack up, and
nothing prevents `frontend-e2e` from starting a second copy of the stack
while `docker-build` is running its own containers. On the GitHub-hosted
runners these are separate machines, so the overlap costs runner-minutes
rather than correctness. On a self-hosted runner it would be a conflict.

---

## 3. The release workflow

One job, `release`, named *Build and publish images*.

| | |
|---|---|
| Runner | `ubuntu-latest` |
| `permissions` | `contents: read`, `packages: write` |
| `env.REGISTRY` | `ghcr.io` |
| `env.IMAGE_NAMESPACE` | `${{ github.repository_owner }}` |
| `env.BACKEND_IMAGE` | `ghcr.io/${{ github.repository_owner }}/fuel-backend` |
| `env.FRONTEND_IMAGE` | `ghcr.io/${{ github.repository_owner }}/fuel-frontend` |
| `concurrency` | `release-${{ github.ref }}`, `cancel-in-progress: false` |

With this repository's owner, the image names are `ghcr.io/tanimstu068/fuel-backend`
and `ghcr.io/tanimstu068/fuel-frontend` — the same defaults hard-coded in
[`docker-compose.release.yml`](../docker-compose.release.yml).

### 3.1 The steps

| # | Step | What it does |
|---|---|---|
| 1 | `actions/checkout@v4` | Checks out the tagged commit |
| 2 | `docker/setup-buildx-action@v3` | Sets up Buildx |
| 3 | `docker/login-action@v3` | Logs in to `ghcr.io` as `${{ github.actor }}` with `secrets.GITHUB_TOKEN` |
| 4 | *Resolve the version tag* | Derives the tag, validates it, writes it to `$GITHUB_OUTPUT` |
| 5 | *Build and push the backend image* | `context: ./backend`, `push: true`, three tags |
| 6 | *Build and push the frontend image* | `context: ./frontend`, `push: true`, three tags |
| 7 | *Summarise what was published* | Writes a Markdown table and the rollback commands to `$GITHUB_STEP_SUMMARY` |

### 3.2 Tag resolution and validation

```bash
if [ "${{ github.event_name }}" = "workflow_dispatch" ]; then
  TAG="${{ inputs.tag }}"
else
  TAG="${GITHUB_REF#refs/tags/}"
fi
if ! echo "$TAG" | grep -Eq '^v[0-9]+\.[0-9]+\.[0-9]+$'; then
  echo "::error::'$TAG' is not a vMAJOR.MINOR.PATCH tag"
  exit 1
fi
```

The validation is a real gate: a tag that is not `vMAJOR.MINOR.PATCH` fails
the job before any image is built. The reason is in
[`docs/deployment.md`](deployment.md) — *"a moving tag that is not a version
is how a rollback target disappears."*

### 3.3 The three tags

Both images get the same three tags:

| Tag | Example | Moves? | Used for |
|---|---|---|---|
| The version | `v1.0.0` | Yes — a careless `git tag -f` can move it | The human-readable name of the release |
| The version plus the commit sha | `v1.0.0-sha-426b5741d5a0da26a1c4092479924520c48bce82` | **No** | **The rollback target** |
| The moving tag | `latest` | Yes — every release overwrites it | "The newest thing we published" |

The `-sha-` tag is the one that matters, and
[`docs/deployment.md`](deployment.md) explains why:

> The `latest` tag is deliberately *not* what a rollback uses. A tag that
> moves cannot be pointed at by a note saying "go back to the good one" —
> the thing it referred to has been overwritten. The `sha` tag cannot move,
> so it is the only one that can be named in a rollback procedure and still
> mean the same image tomorrow.

### 3.4 The build cache

Both build steps use `cache-from: type=gha` and `cache-to: type=gha,mode=max`.
The cache is keyed to the GitHub Actions cache service, so a rebuild of an
unchanged layer is fast — and the cache is scoped to the branch, so a tag
build starts from the cache its branch accumulated.

### 3.5 The release summary

The final step writes a Markdown summary to `$GITHUB_STEP_SUMMARY` listing
the three tags for each image, followed by the rollback commands. This is
where a rollback's "previous sha" is supposed to be read from — the workflow
tells the operator where to look, in the place they will be when they need
it.

---

## 4. The `v1.0.0` tag

| | |
|---|---|
| Tag | `v1.0.0` |
| Type | **Annotated** (`git tag -a`), not lightweight |
| Tagger | `TanimStu068 <tanimmahmud315@gmail.com>` |
| Date | Wed Nov 25 23:12:38 2026 +0600 |
| Points at | commit `426b5741d5a0da26a1c4092479924520c48bce82` — *"fix(day7): stop the platform serving a world that had ended"* |

The annotated tag's message:

```
v1.0.0 — the submission

Seven days, complete. The platform reads the BUP fuel supply simulator,
forecasts demand, recommends explainable allocations for a human to
approve, and stays observable and usable when parts of it fail.

17 of 17 crisis, fault and dependency scenarios recovered automatically.
All three load paths run to 50 users at 0.00% errors.
583 backend tests, 22 Vitest, 12 Playwright, 85% coverage.
The demo dry run executes every step and checks every claim: five
consecutive clean runs.
```

**It is the only tag in the repository.** `git tag -l` returns exactly one
line.

**What it does and does not cover.** The tag is 8 commits behind `main` at
the time of writing, and every commit between them is a documentation
commit — the seven documentation files added after the release. No
application code, no test, and no workflow changed after the tag was cut, so
the tag is a faithful snapshot of the shipped application.

---

## 5. The release procedure

### 5.1 Publishing a release

```bash
# 1. Make sure CI is green on main.
# 2. Tag the commit that is being released.
git tag -a v1.0.0 -m "Release v1.0.0"
git push origin v1.0.0
```

The tag push triggers the release workflow, which builds and pushes both
images with their three tags.

**Read §7 before trusting step 1.** The release workflow does **not**
currently depend on CI being green; `needs:` is empty (§7.1). "Make sure CI
is green" is a manual step a human performs, not an enforced one.

### 5.2 Running the published images instead of building locally

The base [`docker-compose.yml`](../docker-compose.yml) builds the backend and
the frontend from source. A *deployment* wants the opposite — the published
image, built once, tested once, rolled back by pointing at a different tag.
The overlay
[`docker-compose.release.yml`](../docker-compose.release.yml) replaces the
two `build:` blocks with `image:` blocks and changes nothing else:

```yaml
services:
  backend:
    build: !reset null
    image: ${BACKEND_IMAGE:-ghcr.io/tanimstu068/fuel-backend:latest}

  frontend:
    build: !reset null
    image: ${FRONTEND_IMAGE:-ghcr.io/tanimstu068/fuel-frontend:latest}
```

```bash
docker compose -f docker-compose.yml -f docker-compose.release.yml up -d --wait
```

**Verified:** the overlay merges correctly. `docker compose -f
docker-compose.yml -f docker-compose.release.yml config` resolves to
`backend: image='ghcr.io/tanimstu068/fuel-backend:latest' build=None` and
the same for the frontend — the `build` block is genuinely removed, not
merged over.

**One caveat.** `!reset` is a Compose-specific tag and needs a Compose
version that supports it. The local version is **v5.7.1**, where it works.
On an older Compose the overlay would fail to parse rather than silently
fall back to a local build — which is the safer of the two failure modes,
but it is a hard failure.

---

## 6. The rollback procedure

A rollback is "run the previous good image". The procedure is the same for a
bad release and for a bad afternoon.

### 6.1 What you need

The tag of the version to go back to. `docker compose images` shows what is
running now:

```bash
docker compose images
```

```text
CONTAINER     IMAGE                                    TAG
fuel-backend  ghcr.io/tanimstu068/fuel-backend         v1.0.0
```

### 6.2 The procedure — exact commands

**Step 1 — find the previous sha tag.**

```bash
gh api /users/<owner>/packages/container/fuel-backend/versions \
  --jq '.[] | {tags: .metadata.container.tags, created: .created_at}' | head -40
```

The same list for the frontend:

```bash
gh api /users/<owner>/packages/container/fuel-frontend/versions \
  --jq '.[] | {tags: .metadata.container.tags, created: .created_at}' | head -40
```

The GitHub Actions release summary lists them too — it is the output of the
workflow's final step.

**Step 2 — pull the previous images by their sha tag.**

```bash
docker pull ghcr.io/<owner>/fuel-backend:v1.0.0-sha-<previous-sha>
docker pull ghcr.io/<owner>/fuel-frontend:v1.0.0-sha-<previous-sha>
```

For this repository, `<owner>` is `tanimstu068`. Concretely, rolling back
to the commit the `v1.0.0` tag points at:

```bash
docker pull ghcr.io/tanimstu068/fuel-backend:v1.0.0-sha-426b5741d5a0da26a1c4092479924520c48bce82
docker pull ghcr.io/tanimstu068/fuel-frontend:v1.0.0-sha-426b5741d5a0da26a1c4092479924520c48bce82
```

**Step 3 — point `.env` at the previous tags.**

```bash
BACKEND_IMAGE=ghcr.io/<owner>/fuel-backend:v1.0.0-sha-<previous-sha>
FRONTEND_IMAGE=ghcr.io/<owner>/fuel-frontend:v1.0.0-sha-<previous-sha>
```

**Step 4 — recreate the containers with the old images.**

```bash
docker compose -f docker-compose.yml -f docker-compose.release.yml \
  up -d --wait --no-build
```

`--no-build` is **on purpose**. Without it, Compose rebuilds the image from
the working tree and the rollback quietly becomes "the current code", which
is the opposite of what was asked for.

**Step 5 — verify.**

```bash
curl -fsS http://localhost:8001/health | jq '.status, .components'
```

The expected result is `"ok"` and three components all reporting `ok`. The
full post-deploy checklist is in
[`docs/deployment.md`](deployment.md) §3 — six checks, the first three being
the ones that matter.

### 6.3 The alternative: retag `latest`

The release workflow's own summary suggests this shorter form:

```bash
docker pull ghcr.io/<owner>/fuel-backend:v1.0.0-sha-<previous-sha>
docker tag  ghcr.io/<owner>/fuel-backend:v1.0.0-sha-<previous-sha> ghcr.io/<owner>/fuel-backend:latest
docker push ghcr.io/<owner>/fuel-backend:latest
```

**This is worse than the procedure above, and it is worth saying why.** It
publishes a moving tag to point at an old image, which destroys the
property that made the sha tag safe: after this, `latest` no longer means
"the newest release", and the *next* release will overwrite it anyway. It
also needs push access to the registry from wherever the rollback is
performed. The `.env`-and-`--no-build` route leaves the registry untouched.

### 6.4 Rolling back the database

**There is no database migration to reverse.** The schema is created by
SQLAlchemy's `create_all` at startup and there is no Alembic revision to
undo.

That is a deliberate constraint, not an oversight. The platform owns a tiny
schema, and the migration surface a rollback would have to reverse is larger
than the schema itself. If a schema change is ever needed, it ships with an
explicit downgrade path and this section grows a step for it.

> **A caveat worth noting.** The backend's `Dockerfile` runs
> `python -m alembic upgrade head || true` before starting the server, and
> [`backend/migrations`](../backend/migrations) exists. The statement above
> — that there is no migration to reverse — describes the *release policy*:
> the schema is treated as owned by `create_all`, and the Alembic step is
> allowed to fail. If a revision is ever added that changes the schema, the
> "no rollback step needed" claim stops holding.

The Postgres volume is **not** removed by a rollback. The audit trail, the
decision history and the demand history survive, which is the point of
keeping them.

### 6.5 Rolling back to a local build

If the registry is unreachable, the previous working state is in git:

```bash
git checkout v1.0.0
docker compose up -d --wait --build
```

### 6.6 What a rollback does **not** do

* **It does not migrate the database.** See §6.4.
* **It does not reset the simulation.** The simulator's state lives in its
  own volume and survives a deploy. To start the world over, use
  [`scripts/reset_demo.sh`](../scripts/reset_demo.sh).
* **It does not send anything to the simulator.** Rolling back does not
  dispatch fuel. Every allocation still needs a human approval.

---

## 7. What has **not** been verified

This is the important section. The following could not be confirmed from
the repository, because confirming them would require running the workflows
against GitHub.

### 7.1 The GHCR push has never been observed

**Not verified.** No image has been confirmed published to
`ghcr.io/tanimstu068/fuel-backend` or
`ghcr.io/tanimstu068/fuel-frontend`.

The evidence that it *has not* been verified:

| Check | Result |
|---|---|
| Images of this project in the local Docker daemon | **None** under `ghcr.io/tanimstu068/*`. (Two unrelated `ghcr.io` images are present on the host — `ghcr.io/cirruslabs/flutter` and `ghcr.io/mlflow/mlflow` — neither is this project.) |
| This project's images, built locally | `fuel-supply-intelligence-platform-backend:latest` and `fuel-supply-intelligence-platform-frontend:latest` — the local `build:` path, not the registry |
| Workflow run artefacts in the repository | None — `.github/` contains only the two workflow files, no run records |
| A published-image reference in any log or artefact | None |
| The repository's own claim | [`docs/deployment.md`](deployment.md) shows `docker compose images` output listing `ghcr.io/tanimstu068/fuel-backend v1.0.0` — but that is **illustrative text in a documentation example**, not a captured command output |

The workflow *would* push, given a green CI run, a `v*` tag, and a token
with `packages: write`. Whether it has ever done so is **not verified**. The
`v1.0.0` tag exists in git, and pushing it is the documented trigger — but
the repository cannot show whether the push reached the registry.

**What to check, in order:**

```bash
# 1. Does the package exist at all?
gh api /users/tanimstu068/packages/container/fuel-backend/versions

# 2. What tags does it carry?
gh api /users/tanimstu068/packages/container/fuel-backend/versions \
  --jq '.[] | {tags: .metadata.container.tags, created: .created_at}'

# 3. Or just try to pull it.
docker pull ghcr.io/tanimstu068/fuel-backend:v1.0.0
```

### 7.2 The release workflow does **not** depend on CI being green

**Not verified — and, on inspection, not true as the comment claims.**

`release.yml` line 8 says:

> The CI workflow must be green first — it is a `needs:` dependency, not a
> hope.

And line 39 says:

```yaml
needs: [] # filled in below by the guard job
```

Both statements are contradicted by the file itself:

| Check | Result |
|---|---|
| `needs:` in the `release` job | `[]` — **empty** |
| A job named "guard" anywhere in either workflow | **Does not exist** |
| `workflow_run` in `release.yml` | **0 occurrences** |
| `workflow_call` in `release.yml` | **0 occurrences** |
| Any reference to `ci.yml` from `release.yml` | **0 occurrences** |

The `release` job has **no dependency of any kind on the CI workflow**. The
comment says the guard is "filled in below", but there is nothing below it —
the file ends at the summary step.

**What this means in practice.** Pushing a `v*` tag publishes images
whether or not CI has passed. The "make sure CI is green" step in
[`docs/deployment.md`](deployment.md) is a **manual instruction to a human**,
not an enforced gate. It is the right instruction, and it is not the same
thing as an enforced one.

**How it would be fixed**, if it were fixed, is a two-line change — the
`needs:` list would name the CI jobs, and a `workflow_run` trigger would
make the dependency real. Both are recorded here as facts about the current
state, not as a change made by this document.

### 7.3 The CI workflow's own green status

**Not verified.** No CI run record is committed to the repository, and the
run history lives on GitHub's servers. The workflows are syntactically valid
YAML with a structure that matches their intent, and the commands they run
are the same ones documented as passing in
[`docs/progress/day7.md`](progress/day7.md) — but "the CI is green right now"
is not something this repository can answer.

### 7.4 The `v1.0.0` tag was pushed to the remote

**Verified for the local repository; not verified for the remote.** The tag
exists locally and points at `426b574`. Whether the remote has the same tag
object requires a network call to the remote, which was not made.

### 7.5 The `BACKEND_IMAGE` and `FRONTEND_IMAGE` variables are undocumented

**Verified as an absence.** Neither `.env.example` nor `.env` defines
`BACKEND_IMAGE` or `FRONTEND_IMAGE`. The release overlay falls back to
`ghcr.io/tanimstu068/fuel-backend:latest` when they are unset, so the
overlay works without configuration — but the variables a rollback needs to
set are not in the example file, and a reader following
[`docs/deployment.md`](deployment.md) §"Running the published images" will
not find them there.

### 7.6 The `-sha-` tag's exact format

**Verified as written, not as observed.** The workflow builds the tag as
`${{ steps.version.outputs.tag }}-sha-${{ github.sha }}`, where
`github.sha` is the full 40-character commit hash. The rollback commands in
this document use the full hash for that reason. No published tag was seen,
so the exact string in the registry is **not verified** — only the string
the workflow would produce.

---

## 8. Discrepancies

Conflicts found while writing this document. Each is between two files in
the repository, or between a file and the code it describes.

### D1 — The documented CI job names do not match the workflow

[`docs/progress/day7.md`](progress/day7.md) lists the pipeline jobs as:

| `day7.md` says | `ci.yml` actually has |
|---|---|
| `backend-lint` | `lint` |
| `backend-tests` | `test` |
| `backend-coverage` | `coverage` |
| `frontend` | `frontend` ✓ |
| `e2e` | `frontend-e2e` |
| `docker-build` | `docker-build` ✓ |
| `compose-config` | `compose-config` ✓ |
| `compose-smoke` | `compose-smoke` ✓ |

Four names are wrong. The job *functions* described are correct — the day 7
table describes what each job does accurately — but the **identifiers** do
not exist, so the table cannot be used to navigate the Actions UI.

[`docs/requirements-traceability.md`](requirements-traceability.md) gets
this right: it lists *"lint, test, frontend, frontend e2e, docker-build,
compose-config, compose-smoke and coverage"*, which matches the file
exactly. **The most recent verified value is the traceability document's
list**, and it is the one used in §2 of this document.

### D2 — The release workflow's `needs:` comment contradicts its own contents

`release.yml` line 39 reads `needs: [] # filled in below by the guard job`,
and line 8 says the CI must be green first *"it is a `needs:` dependency,
not a hope"*. There is no guard job, and `needs:` is empty. Covered in
§7.2.

### D3 — "85 % coverage" versus the measured 84.4 %

The `v1.0.0` tag message says *"583 backend tests, 22 Vitest, 12
Playwright, 85% coverage"*. The committed
[`coverage.json`](../coverage.json) artifact measures **84.4 %**
(`percent_covered: 84.40193…`), and `README.md` and
[`docs/deliverables-checklist.md`](deliverables-checklist.md) both say
"85 %".

**The most recent verified value is 84.4 %** — it is the measured number
in the committed artifact, and the two prose documents are rounding it up.
The difference is immaterial to the coverage *floor* (80 %), but a tag
message is the one piece of text in the repository that cannot be edited
after the fact.

### D4 — The `deliverables-checklist.md` claims the release workflow publishes the images

[`docs/deliverables-checklist.md`](deliverables-checklist.md) states: *"The
release workflow publishes both images to the GitHub Container Registry with
three tags"*. The **workflow definition** does publish them — the steps are
there and they are correct. What is unverified is that it has ever
**done** so. Covered in §7.1.

### D5 — The `deployment.md` rollback uses `<owner>` while the compose overlay hard-codes the real owner

[`docs/deployment.md`](deployment.md) uses the placeholder `<owner>` in its
rollback commands, while
[`docker-compose.release.yml`](../docker-compose.release.yml) hard-codes
`ghcr.io/tanimstu068/`. Both are correct for their purpose — a
documentation placeholder should stay a placeholder — but a reader copying
the commands has to substitute a value that the compose file already
discloses. This document uses both forms deliberately: `<owner>` for the
general procedure, and the literal `tanimstu068` for the worked example in
§6.2.

---

## 9. Verification commands

Every claim above can be re-checked with these. They are read-only.

```bash
# the two workflows and their jobs
ls .github/workflows/
grep -nE "^  [a-z-]+:$" .github/workflows/ci.yml
grep -nE "^  [a-z-]+:$" .github/workflows/release.yml

# the release job's dependency on CI — expected: no matches
grep -n "needs:" .github/workflows/release.yml
grep -cE "workflow_run|workflow_call|ci\.yml" .github/workflows/release.yml

# the tags in the repository
git tag -l
git tag -l -n99 --format='%(contents)' v1.0.0
git rev-list -n1 v1.0.0

# the three image tags
grep -n "tags:" -A 4 .github/workflows/release.yml

# the release overlay merges correctly
docker compose -f docker-compose.yml -f docker-compose.release.yml config --format json \
  | python3 -c "import json,sys; d=json.load(sys.stdin); \
      [print(s, d['services'][s].get('image'), d['services'][s].get('build')) \
       for s in ('backend','frontend')]"

# the image variables are not in the example environment
grep -c "BACKEND_IMAGE\|FRONTEND_IMAGE" .env.example

# the coverage floors the CI enforces
grep -n "FLOORS\|TOTAL_FLOOR" -A 10 scripts/check_coverage.py
```

---

## 10. Related documents

| Document | What it holds |
|---|---|
| [`docs/deployment.md`](deployment.md) | The publishing and rollback procedure this document expands on, and the post-deploy checks |
| [`docs/runbook.md`](runbook.md) | Operating the platform — and its own list of caveats, including *"There is no automated rollback"* |
| [`docs/deliverables-checklist.md`](deliverables-checklist.md) | Deliverables 7 (Deployment) and the release claims |
| [`docs/requirements-traceability.md`](requirements-traceability.md) | The DevOps rows, including the CI/CD and rollback status |
| [`docs/progress/day7.md`](progress/day7.md) | The release day — the tag, the CI table and the demo that runs itself |
| [`docs/testing-strategy-and-report.md`](testing-strategy-and-report.md) | What the eight CI jobs actually run, and what each one proves |
| [`docker-compose.release.yml`](../docker-compose.release.yml) | The overlay that swaps `build:` for `image:` |
| [`backend/Dockerfile`](../backend/Dockerfile) | The backend image, its healthcheck and its `alembic upgrade head \|\| true` start command |
| [`scripts/check_coverage.py`](../scripts/check_coverage.py) | The seven per-module coverage floors the `coverage` job enforces |
