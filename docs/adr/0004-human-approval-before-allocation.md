# ADR 0004 — Nothing reaches the simulator without a human approval

**Status:** Accepted
**Date:** Day 4 of 7
**Deciders:** the team

---

## Context

The brief sets a guardrail in §26: *"preserve human review for consequential
simulated decisions"*, and §12 lists *"Prediction confidence too low → Human
review requested"* as one of the four resilience rules.

The simulator accepts `POST /v1/allocations` as its only domain write. Once
the platform can call it, the platform can move fuel. That is the most
consequential thing it does, and it is the one action with an irreversible
effect on the world: an allocation is accepted, dispatched, and only then
does it become `ARRIVED` or `FAILED`.

The risk table in the Day 4 log names the danger directly: *"The auto-mode
can approve without a human → A bad automatic dispatch."*

## Decision

**No allocation reaches the simulator without a human approving it.**

The recommendation lifecycle is:

```
PROPOSED ──approve──▶ APPROVED ──submit──▶ SUBMITTED ──▶ ARRIVED
    │                                                       └─▶ FAILED
    └──reject──▶ REJECTED
```

- `simulate` writes **nothing** — it replays the forecast locally.
- `approve` writes.
- `reject` records the reason.
- `POST /api/decision-settings` can turn on an auto-mode, and it is **off by
  default**. When it is on, it still only approves a recommendation that is
  **all** of:
  - `WATCH` level — never `CRITICAL`, because a critical station is exactly
    where a human should look;
  - confidence ≥ 0.85;
  - not flagged `needs_human_review`;
  - not produced by the fallback policy;
  - every constraint satisfied.

Independently of the auto-mode, the **risk engine** flags a recommendation
`needs_human_review` when the forecast behind it carries the flag or its
confidence is below **0.5** — the rule is
`needs_review = forecast.needs_human_review or forecast.confidence < 0.5`.
Risk **level** does not enter that rule; the `CRITICAL` case is handled by
the auto-mode's bar instead, which refuses to approve it at all.

The rule-based fallback policy **never** auto-approves, and says so in its
rationale.

## Alternatives considered

| Alternative | Why not |
|---|---|
| **Auto-approve everything above a risk threshold** | The threshold that matters is not the risk level — it is the *confidence* in the forecast behind it. A high-confidence forecast of a `CRITICAL` station is still a dispatch a human should see. |
| **Auto-approve only `OK`-level recommendations** | Then the auto-mode would only ever dispatch fuel to stations that did not need it, which is exactly the wrong use of a scarce allowance. |
| **No auto-mode at all** | Rejected because a platform that can only be driven one click at a time does not demonstrate a working operations loop, and the brief's suggested demo story has the platform adapting on its own. The narrow auto-mode is the compromise. |
| **Let the simulator decide** | The simulator is not the brain. It executes allocations deterministically; it does not judge whether they should have been made. |

## Consequences

**Good.** The world is never changed by a decision nobody looked at. The
platform's central claim — *"nothing reaches the simulator without a human
saying yes"* — is true by construction, and the fallback drill asserts it:
*"the recommendations are still PROPOSED: True"*.

**Good.** The decision history is a real audit trail: who decided, when, and
what they decided on. `/api/decision-history` serves it from Postgres.

**Good.** The auto-mode's narrowness makes it safe to leave available. It is
a demonstrable capability without being a liability.

**Bad.** The loop is slower. A crisis needs one approval per recommendation,
and the Day 4 policy comparison ran 240 ticks and recorded **362 approvals**
(one of which failed) — the approvals are the pacing constraint on the
evaluation, not the optimizer.

**Bad.** The auto-mode's bar is a *heuristic* on a confidence score, and a
confidence score is only as good as the model behind it. When the model was
retrained and its score moved, so did the auto-mode's behaviour. That is why
the fallback policy is excluded from it outright — the fallback's confidence
is capped at 0.4 precisely because it must not be trusted to dispatch.

**Bad, and unresolved.** The platform's own API has **no authentication**.
Every operator endpoint — including `approve`, which writes to the simulator —
is reachable on the container's port. The *behaviour* is restricted; the
*endpoints* are not. The brief only asks for engineering hygiene rather than
enterprise security, so this is a deliberate trade, but it is a trade and it
is recorded as a gap.

## Evidence

- [`docs/progress/day4.md`](../progress/day4.md) §5, §6 and the risk table —
  *"The auto-mode can approve without a human"*
- [`docs/architecture.md`](../architecture.md) §6 — *Operator-action
  restriction*
- [`docs/postmortems/08-fallback-never-engaged-stale-forecast.md`](../postmortems/08-fallback-never-engaged-stale-forecast.md)
  — the fallback never auto-approves
- [`docs/requirements-traceability.md`](../requirements-traceability.md) §19
  and §Gaps
- [`backend/app/decision/service.py`](../../backend/app/decision/service.py)
  — `_auto_eligible`
