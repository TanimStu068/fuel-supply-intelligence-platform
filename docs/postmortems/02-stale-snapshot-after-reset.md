# Postmortem: the platform served a world that had ended

**Incident:** after a simulator reset, the platform served the previous run's
numbers as if they were current
**Day:** 7 of 7 (the release and the self-checking demo)
**Severity:** high
**Status:** resolved and verified, including on a fresh clone

---

## Summary

`POST /admin/reset` on the simulator starts the world again at tick 0. The
platform's cache still held the **previous run's** snapshot — tick 892, a world
that no longer existed — and it served it as fresh.

```text
=== BEFORE reset ===
{"tick":893,"sim_time":"2026-01-10T07:15:00"}
{"tick":892,"sim_time":"2026-01-10T07:00:00Z","degraded":false,"age_seconds":0.12}

=== POST /admin/reset ===
{"tick":0,"status":"PAUSED"}

=== IMMEDIATELY after (within 1s) ===
{"tick":892,"sim_time":"2026-01-10T07:00:00Z","degraded":false,"age_seconds":0.2}
```

Read that again. The world was at tick **0**. The platform reported tick
**892**, said `degraded: false`, and said the data was **0.2 seconds** old. The
age was technically true — the cache *was* refreshed 0.2 s ago — but it was
refreshed with the numbers of a run that had ended. The platform was not
stale. It was **wrong**, and it looked fresh.

---

## Impact

This is the one defect in the project that contradicts the platform's central
claim: *"Every number the dashboard shows comes from the live simulator.
Nothing is fabricated."* The platform was serving numbers from a run that had
been wiped, and reporting them as 0.2 seconds old.

| | Before | After |
|---|---|---|
| The state served one second after a reset | tick 892, `degraded: false`, age 0.2 s | **503, no state** |
| The state served two seconds after a reset | tick 892, `degraded: false` | **the new world** |
| A reset done from the simulator's own console | not noticed | **noticed within 2 s** |

The window was bounded — the next sync cycle replaced it — but during that
window an operator would have seen a coherent, confident, wrong network.

---

## Timeline

The documented order of events, from [`day7.md`](../progress/day7.md). No
timestamps are invented here.

1. The simulator ran for a long time; the platform held its state at tick 892.
2. The platform's own **database** half of a reset was already handled: it
   noticed the clock had gone backwards and dropped the stored demand history,
   so the forecaster would not read two runs as one series.
3. `POST /admin/reset` was issued. The simulator's world ended and restarted at
   tick 0.
4. The platform's **cache** half was never covered. The cache kept the old
   snapshot and kept serving it as fresh.
5. The demo dry run — which executes every command in the demo, in order, and
   checks that each one produced what the demo claims — reported:
   `1 step(s) did not produce what the demo script claims.`
6. The failure was traced to the reset step: the state after the reset was the
   state from before it.
7. A **second bug** was found hiding inside the first: the event stream's tick
   tracker was not reset, so the platform would have decided the new world's
   stream was permanently stale and reconnected, for ever.
8. The fix was put in the `StateCache`, where the invariant lives.
9. The fix was verified on the running stack, and then again on a **fresh
   clone** against the tagged commit, because a fix that only works in the
   working tree is not a fix.

---

## Root cause

### The invariant was in the wrong place, and only half of it was implemented

Two halves of the same event — a simulator reset — were handled in two
different places, and only one of them remembered:

| Half | Handled by | State before the fix |
|---|---|---|
| The **database** history | The sync service's history-wipe | Already correct — the clock going backwards dropped the stored demand history |
| The **cache** | Nothing | The old snapshot was served as fresh |

The cache's contract was *"replace the snapshot wholesale and keep the
freshness metadata accurate"*. It had no concept of a **rewound** clock, so a
snapshot from tick 0 arriving behind a snapshot from tick 892 looked like a
slightly stale refresh rather than a different world.

### The second bug: the stream's tick tracker

The event stream's tick tracker is the highest tick the stream has carried.
After a reset the stream carries the *new* world's ticks, but the tracker still
said 892 — so the platform would decide the stream was permanently stale, drop
it, and reconnect, for ever. The tracker has to be reset with the cache.

---

## How it was detected

By the **demo dry run** — [`scripts/demo_dry_run.sh`](../../scripts/demo_dry_run.sh).
The dry run is not a transcript of the demo; it *executes* every command in the
demo, in order, against the running stack, and checks that each one produced
what the demo script claims.

It reported one failure, and it turned out to be the most important bug of the
week.

No unit test could have found it. The cache behaved correctly according to its
own contract; the contract was incomplete.

---

## Resolution

The invariant now lives in [`StateCache`](../../backend/app/state_cache.py),
because that is where it belongs: ***the cache never serves a world that no
longer exists.***

| # | Fix | Where |
|---|---|---|
| 1 | A snapshot whose tick is **behind** the cached one means the world was reset. The snapshot is still stored — it *is* the current world — but the rewind is recorded, so the caller can react. | `backend/app/state_cache.py` — `StateCache.store()` sets `rewound` |
| 2 | `SyncService.forget_the_previous_world()` drops the cached snapshot and resets the event stream's tick tracker. `/api/state` then answers **503 "no simulator state available yet"** until the next cycle fetches the new world — which is exactly what it says. | `backend/app/sync_service.py` |
| 3 | The platform's own reset endpoints (`POST /admin/reset` and `POST /api/simulation/reset`) call it, so the window does not open at all when the platform does the resetting. | `backend/app/main.py` |
| 4 | When the reset comes from **outside** — the simulator's own admin console, or the organizers' scripts — the simulator announces it on the stream (`event: simulator.notice, data: {"message": "Simulation reset"}`). The platform handles that notice by dropping the world and re-reading it. | `backend/app/sync_service.py` — `_handle_notice()` |

### Verification on a fresh clone

Run against the tagged commit, not the working tree:

```text
=== BEFORE ===
{"tick":302}
{"tick":288,"degraded":false,"age_seconds":2.183}

=== POST /admin/reset ===
{"tick":0,"status":"PAUSED"}

=== IMMEDIATELY after ===
HTTP 503
{"detail":"no simulator state available yet"}
```

The platform refuses to serve the world that just ended, and it serves the new
world **one second** later.

---

## What went well

- **The demo dry run checked claims, not commands.** It asked "did this step
  produce what the demo says it produces?" rather than "did this command
  exit 0?". That distinction found this and nothing else would have.
- **The database half of the reset was already right.** The fix did not have to
  invent an approach; it had to make the cache obey the same discipline.
- **The fix is an invariant, not a patch.** "The cache never serves a world
  that no longer exists" is a property of the component, and it is now
  enforced where the component lives.
- **It was verified on a fresh clone.** Against the tagged commit, so the fix
  is in the released artifact and not only in the working tree.

## What went badly

- **Freshness and relevance were treated as the same property.** The platform
  reported "0.2 seconds old" accurately and it did not matter. The number was
  accurate about the wrong thing.
- **The same event was handled in two places, and only one remembered.** This
  is the classic shape of a partial fix, and it is what let the defect survive
  a day of testing.
- **A second defect was hiding inside the first.** Fixing only the cache would
  have produced a platform that served the right world and then tore down a
  healthy stream every few seconds forever.
- **A reset from outside the platform was not handled at all.** The
  organizers will reset the simulator between demo runs. The platform has to
  notice when someone else does it.
- **The defect surfaced on the last day.** It had been present through the
  whole build, and the entire scoring rubric turns on "nothing is fabricated".

---

## Action items

| # | Action | Status |
|---|---|---|
| 1 | Record a rewind when a snapshot arrives behind the cached one | **Done** — `backend/app/state_cache.py` |
| 2 | `forget_the_previous_world()` drops the cache and the stream trackers | **Done** — `backend/app/sync_service.py` |
| 3 | The platform's own reset endpoints call it, so the window never opens | **Done** |
| 4 | Handle the simulator's `simulator.notice` stream event, so an external reset is noticed | **Done** |
| 5 | Regression tests for the rewind, the tracker reset and the 503 | **Done** — `backend/tests/test_sync_service.py::test_forgetting_the_previous_world_drops_the_cached_state`, `::test_forgetting_the_previous_world_resets_the_stream_tracker`, `::test_a_rewound_world_resets_the_stream_tracker` |
| 6 | Verify the fix on a fresh clone against the tagged commit | **Done** — recorded in the addendum of [`day7.md`](../progress/day7.md) |
| 7 | Extend the demo dry run's checks to cover the reset path's *output*, not only its exit status | **Done** — the dry run is what caught it |
| 8 | A dedicated test that the API answers 503 (not a stale 200) in the window between a reset and the next sync | **Partly done** — the behaviour is pinned by the sync-service tests above, but there is no single test that asserts the API's 503 response body after an external reset |
