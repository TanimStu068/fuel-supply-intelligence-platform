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

## The evidence

Measured against the running stack. Every number in these documents came from
a run that is reproducible with the script named in it.

| Document | What it holds |
|---|---|
| [`resilience-report.md`](resilience-report.md) | **17 of 17** crisis, fault and dependency scenarios, each with a detection time, a recovery time and the recommendation made during the incident |
| [`load-test-report.md`](load-test-report.md) | Three workloads at a 50-user ramp, the before/after of the prediction-error aggregate, and the deadlock that lost writes |
| [`model-evaluation.md`](model-evaluation.md) | The forecast accuracy, the two error measures, the horizon benchmark, and the model's own honest verdict |
| [`policy-comparison.md`](policy-comparison.md) | The platform's policy against a naive baseline, on service level |
| [`fault-tests.md`](fault-tests.md) | The fault matrix, the degraded-mode drill, and eight findings about the simulator's fault behaviour |
| [`simulator-notes.md`](simulator-notes.md) | What the running simulator actually does, and the **ten** places it disagrees with the guide |

## The defects

| Document | What it holds |
|---|---|
| [`bug-catalog.md`](bug-catalog.md) | Every defect found during the project — **70** of them — with the day, the symptom, the root cause, how it was found, the fix, and the test that pins it. Grouped by root-cause pattern, with eight deep dives and a count per day and per component. |
| [`postmortems/`](postmortems/) | Eight incidents written up in full — summary, impact, timeline, root cause, how it was detected, resolution, what went well, what went badly, and action items marked done or not done. Start at [`postmortems/README.md`](postmortems/README.md). |

## The requirements

| Document | What it holds |
|---|---|
| [`requirements-traceability.md`](requirements-traceability.md) | Every requirement in the two organizer documents, mapped to the code that implements it and the evidence that proves it — with a **Done / Partial / Not done** status on each one, and a gaps list at the end. |

## The short version

| Document | What it holds |
|---|---|
| [`project-summary.md`](project-summary.md) | One page: the problem, what was built, the architecture in five lines, six headline results with real numbers, what makes it resilient, and the honest limitations |
| [`demo-qa.md`](demo-qa.md) | Twenty-five questions judges are likely to ask, with short factual answers, each linking to the evidence file |

## The project

| Document | What it holds |
|---|---|
| [`architecture.md`](architecture.md) | The system diagram, the assumptions, the data sources, the security notes, the limitations |
| [`deployment.md`](deployment.md) | Publishing, rollback, and the post-deploy checks |
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
* **A judge preparing questions** — [`demo-qa.md`](demo-qa.md) has
  twenty-five of them answered, each with a link to the evidence.
* **A reviewer checking the claims** — [`requirements-traceability.md`](requirements-traceability.md)
  first, because it maps every requirement to its proof and marks what is
  not done. Then [`bug-catalog.md`](bug-catalog.md), which names what is
  broken and what is pinned, and
  [`model-evaluation.md`](model-evaluation.md) for the model's honest verdict.
  Then [`postmortems/`](postmortems/) for the eight incidents written up in
  full.
* **Someone joining the project** — [`architecture.md`](architecture.md), then
  the [root README's quick start](../README.md#7-quick-start), then the day
  that matches what you are changing.

## Rules for these documents

* Every number comes from a run that the named script reproduces. Nothing is
  estimated, and nothing is rounded in the platform's favour.
* Where two documents disagree, the newer verified value wins and the conflict
  is listed in the **Discrepancies** section of the document.
* Where something was not verified, the document says **not verified** rather
  than leaving a gap.
