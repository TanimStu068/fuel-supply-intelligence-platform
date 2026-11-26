# Postmortem: the Postgres deadlock that lost writes

**Incident:** the sync service and an operator's approval deadlocked on each
other, and 35 writes were silently lost in a single load-test run
**Day:** 6 of 7 (Part 3 — load testing)
**Severity:** high
**Status:** resolved and measured

---

## Summary

Underneath a 24.5 % error rate in the end-to-end decision workload, the backend
logs showed `deadlock detected` on Postgres inserts. **The sync service
persisting the decision history and an operator approving a recommendation were
deadlocking on each other.**

Postgres kills one side of a deadlock and raises `40001`, which the SQL
standard says the application should **retry**. Three write paths were
bypassing the retrying helper and using a plain session: the recommendation
write, the snapshot write, and the allocation refresh.

Before the fix, **35 deadlocks in one load-test run were lost** — the write
simply failed and was logged as a warning.

---

## Impact

| | Before | After |
|---|---|---|
| Deadlocks in a run | 35 | 2 |
| **Writes lost** | **35** | **0** |
| Retry succeeded | — | 2 of 2 |

The two remaining deadlocks are not failures: they are the retry mechanism
working. Both were retried and both succeeded, and the run's error rate was
**0.00 %**.

The headline 24.5 % error rate was **mostly not a bug**. Most of those errors
were `409 Conflict` — the approve endpoint correctly refusing to dispatch the
same allocation twice. *"A platform that refuses a duplicate dispatch is
behaving correctly, so the load test counts them separately: the final run
recorded **2,619 conflicts and a 0.00% error rate**. A test that counts a
correct refusal as a failure is measuring the test, not the platform."*

The real defect was underneath: 35 writes that were supposed to be durable
were not.

---

## Timeline

The documented order of events, from
[`docs/load-test-report.md`](../load-test-report.md).

1. The end-to-end decision workload was run: the operator's approve path, with
   the sync service running at the same time.
2. It failed at a **24.5 % error rate**.
3. Most of those errors were `409 Conflict` — the approve endpoint refusing to
   dispatch the same allocation twice. That is correct behaviour, and the
   harness was counting it as a failure.
4. The harness was changed to count conflicts on their own column rather than
   as failures.
5. Underneath them, the backend logs showed `deadlock detected` on Postgres
   inserts.
6. The two writers were identified: the sync service persisting the decision
   history, and an operator approving a recommendation.
7. The mechanism was understood: Postgres detects a deadlock when two
   transactions each hold a lock the other wants, kills one of them, and
   raises `40001`.
8. Three write paths were found to be bypassing the retrying helper and using a
   plain session: the recommendation write, the snapshot write, and the
   allocation refresh.
9. All three were moved onto `Database.write()`.
10. The run was repeated.

---

## Root cause

### The retry existed — three write paths did not use it

[`backend/app/db.py`](../../backend/app/db.py) already had a `Database.write()`
helper that retried a deadlock with a bounded backoff. It was correct, and it
was documented as following the standard's own instruction rather than
papering over the problem:

> **Deadlocks are retried.** Postgres detects a deadlock when two transactions
> each hold a lock the other wants, and it kills one of them. With several
> writers — the sync service persisting the decision history while an operator
> approves a recommendation — that happens regularly under load, and the error
> it raises is `40001`, which the SQL standard says the application should
> retry.

The defect was not a missing helper. It was that three call sites still used a
plain session, so a transient, retryable error was treated as a permanent
failure.

### The failure was swallowed by design, and that hid it

The write paths wrapped their work in `try/except` and logged a warning:

```python
try:
    await self._database.write(work)
except Exception as exc:
    logger.warning("could not persist snapshot to postgres", extra={...})
```

That behaviour is **correct** — the platform must keep serving when Postgres
has a hiccup. But it means a deadlock was indistinguishable from a genuine
failure at the call site, and the write was simply gone. 35 of them.

### The whole unit of work has to be retried, not the statement

The fix retries the **whole** unit of work, not just the failing statement,
because Postgres leaves an aborted transaction unusable — every further
statement in it fails until it is rolled back. Retrying only the statement
would therefore fail for a different reason and hide the real one. The work is
rolled back before the retry, so it starts from a clean state, and the retries
are bounded by `DB_DEADLOCK_RETRIES` (default 3).

---

## How it was detected

By the **load test**, and specifically by reading the backend's logs rather
than only the harness's summary. The harness reported a 24.5 % error rate; the
*logs* showed `deadlock detected`.

This is the same shape of finding as the OOM kill: the aggregate number said
"slow", and the per-step measurement said where.

---

## Resolution

| # | Fix | Where |
|---|---|---|
| 1 | The recommendation write, the snapshot write and the allocation refresh all go through `Database.write()` | `backend/app/sync_service.py`, `backend/app/intelligence_service.py` |
| 2 | `Database.write()` retries `40001` — a deadlock or a lock timeout — with a bounded backoff, rolling back the whole unit of work first | `backend/app/db.py` |
| 3 | `_is_deadlock()` recognises the error by `sqlstate`, by the asyncpg exception class and by the message | `backend/app/db.py` |
| 4 | The load harness counts `409 Conflict` separately, so a correct refusal is not reported as a failure | `scripts/loadtest/async_loadtest.py` |

`DB_DEADLOCK_RETRIES` is configurable in
[`backend/app/config.py`](../../backend/app/config.py) and defaults to 3.

---

## What went well

- **The correct helper already existed.** The fix was to route three call
  sites to it, not to invent a retry mechanism.
- **The helper's docstring explained *why* retrying is right**, which made the
  decision to use it defensible rather than superstitious.
- **The whole unit of work is retried**, not the failing statement — the
  subtlety that would otherwise have produced a retry loop that always failed.
- **The harness was corrected as well as the code.** Counting a correct
  refusal as a failure was measuring the test, not the platform.
- **The result is measured on both sides**: 35 lost writes → 0.

## What went badly

- **Three call sites had quietly opted out.** The helper existed; using it was
  not enforced by the type system, the linter or the architecture.
- **The failure mode was silent by design.** The `try/except` that keeps the
  platform alive also made 35 lost writes indistinguishable from 35 successful
  ones, as far as the caller was concerned.
- **The 24.5 % error rate pointed at the wrong thing.** The conflicts were the
  loud symptom; the deadlocks were the quiet one underneath.
- **The deadlock was a *correctness* bug found by a *performance* exercise.**
  Nothing about the deadlock was visible in latency.

---

## Action items

| # | Action | Status |
|---|---|---|
| 1 | Route the recommendation write, the snapshot write and the allocation refresh through `Database.write()` | **Done** |
| 2 | `Database.write()` retries `40001` with a bounded backoff, rolling back the whole unit of work first | **Done** — `backend/app/db.py` |
| 3 | `DB_DEADLOCK_RETRIES` configurable, default 3 | **Done** — `backend/app/config.py` |
| 4 | The harness counts `409 Conflict` separately from errors | **Done** |
| 5 | Regression tests for the retry, the bound and the rollback | **Done** — `backend/tests/test_db_deadlock.py::test_a_deadlock_is_retried`, `::test_a_deadlock_is_retried_a_few_times_then_raised`, `::test_the_retry_rolls_the_failed_transaction_back` |
| 6 | Make it hard to write to the database *without* `Database.write()` — for example, remove the plain `session()` accessor from the write path, or lint for it | **Not done** — [`backend/app/db.py`](../../backend/app/db.py) still exposes `session()` for reads, with the distinction documented in its docstring |
| 7 | A metric for writes lost to a failed write, so a silent loss is visible without reading the logs | **Not done** — the platform logs a warning, but there is no counter for it |
| 8 | Investigate *why* the two writers deadlock — the ordering of their locks — rather than only retrying | **Not done** — the retry follows the SQL standard's instruction, but the lock ordering is not pinned |
