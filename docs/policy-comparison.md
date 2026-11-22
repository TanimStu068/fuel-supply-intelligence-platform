# Policy comparison — priority-greedy vs a naive baseline

**Day 4 of 7 · BUP CSE Fest 2026 Hackathon Finals**

This document answers one question: **does the decision engine's allocation
policy actually do better than a reasonable rule-based policy?** The
comparison is run against the simulator's own `service_level`, over a stepped
run, with everything else held constant.

---

## The two policies

### The platform's policy — priority-greedy

Implemented in `backend/app/decision/optimizer.py`. For each (station, fuel)
pair, in order of urgency:

1. **Urgency ordering.** Stations are served in order of projected hours to
   stockout, so the ones closest to running dry get first claim on the depot's
   per-tick dispatch allowance.
2. **Deficit, not a fixed amount.** The quantity is the fuel needed to reach a
   24-hour target cover, net of what is on hand and what is already in
   transit, capped by the tank's physical capacity.
3. **Route choice by arrival time.** The candidate routes are walked fastest
   first, and the route with the best *projected* outcome wins. The projection
   credits the fuel only from the tick it would arrive, so a slow route cannot
   claim cover the station cannot use yet. When the fast route is disrupted the
   engine falls back to the backup route — for example Patiya → Mirpur.
4. **Pre-validation.** Every proposal is checked against all eight of the
   simulator's allocation rules before it is offered, so the engine never
   proposes something that would come back as a 409.
5. **Human approval.** Nothing is submitted until an operator approves it.

### The naive baseline

Deliberately simple, and deliberately *reasonable* — a comparison against a
strawman would prove nothing. It:

* ships to the station with the **lowest inventory** for that fuel, regardless
  of how soon it runs dry;
* always uses the **fastest route**, even when that route is disrupted — which
  is exactly the mistake a naive policy makes;
* ships a **fixed 5,000 L**, ignoring the tank's remaining room and the depot's
  dispatch allowance.

Everything else — the simulator, the demand, the tick pacing, the world
reset — is identical between the two runs.

---

## How it is measured

`service_level` comes from `GET /v1/metrics` and is the **simulator's own**
measure:

```
service_level = served / (served + unmet)
```

The platform cannot influence it directly — it is computed from the demand
observations the simulator generates, not from anything the platform reports.
That makes it the honest metric to compare policies on.

**The ceiling.** The simulator's total supply is about **25 % below** its
total demand (documented in `docs/simulator-notes.md` from Day 2). Service
level therefore cannot approach 1.0 in *either* run. The comparison is between
the two policies, not against a perfect score.

**The pacing.** The simulator is stepped once per ~1.1 s rather than as fast
as it will go. The backend syncs about once a second, so an unpaced run
measures the step rate rather than the policy — this was a real finding on
Day 3, where the same code scored 0.763 unpaced and 0.882 paced.

---

## Method

```bash
# from the repository root, with the stack up
backend/.venv/bin/python scripts/day4_policy_comparison.py \
    --ticks 250 --step-pause 1.1
```

The script:

1. resets the simulator (`POST /admin/reset`) so both runs start from the
   same world at tick 0;
2. runs the platform's policy for N ticks — stepping, waiting for the backend
   to ingest, then approving every pending recommendation, exactly as an
   operator would;
3. resets again and runs the naive baseline for N ticks;
4. reads `service_level` from the simulator for both runs and writes the raw
   JSON to `/tmp/day4_policy_comparison.json`.

The approval step is what makes this a test of the **whole** loop, not just
the optimizer: the recommendation has to survive pre-validation, the
idempotency key has to be accepted, and the shipment has to actually land.

---

## Results

240 ticks per run (2.5 simulated days), paced at 1.2 s per tick, over the
same world from the same seed.

| Metric | Platform (priority-greedy) | Naive baseline | Difference |
|---|---|---|---|
| **`service_level`** | **1.000000** | **0.656634** | **+0.3434 (+52.3 %)** |
| `served_demand_liters` | 230,780 | 151,538 | +79,242 L |
| `unmet_demand_liters` | **0** | 79,242 | −79,242 L |
| `allocation_liters` | 240,939 | 105,000 | +135,939 L |
| `allocation_failures` | 0 | 0 | — |
| Shipments created | 339 | 21 | +318 |
| Approvals | 362 (1 failed) | — | — |
| Stations left at `CRITICAL` | **0** | 8 | −8 |

### Reading the numbers

**The platform served every litre of demand.** `unmet_demand_liters` is
**0** and `service_level` is exactly **1.0**. The naive baseline left
**79,242 L** of demand unmet — 34 % of everything the network asked for.

**The platform shipped 16× more fuel** (339 shipments against 21) and still
had **zero** allocation failures. The naive policy's problem was not that it
shipped too much; it was that it shipped to the wrong places. Its 21
shipments of a fixed 5,000 L went to whichever station had the lowest
inventory *at that moment*, which left 8 station/fuel pairs sitting at
`CRITICAL` at the end of the run.

**Why the naive policy is so much worse.** Three of its rules compound:

1. *Lowest inventory* is not the same as *closest to stockout*. A station
   with a small tank and modest demand can have less fuel in it than a
   station that is about to run dry.
2. *Fastest route, always* means it picks a **disrupted** route as readily
   as a working one — the shipment is then rejected or fails in transit.
3. *Fixed 5,000 L* ignores the tank's remaining room, so it either
   overfills (a 409) or underfills, and it never accounts for the depot's
   per-tick dispatch allowance.

**The 1 approval failure** was one proposal the simulator refused. The
platform recorded the code and the reason and moved on — which is the
behaviour the 409 handling exists for.

### The caveat, stated plainly

The naive baseline only shipped **21 times in 240 ticks** against the
platform's 339, even though both policies were held to the same sync
cadence. Part of that gap is the naive policy *choosing* not to ship (it
requires 5,000 L of diesel in the depot and refuses to ship when a station
already has fuel); part of it is the simulator refusing shipments the naive
policy should not have made. The comparison is therefore a comparison of two
complete policies, not of two shipping schedules — but it is not a
like-for-like exchange of the same number of shipments, and the numbers
should be read with that in mind.

A service level of exactly 1.0 is also worth a caveat: the platform
front-ran the demand by having fuel already in place, so it never hit a
moment where fuel was missing. The simulator's supply is ~25 % below its
demand, but the platform was able to keep up with demand by shipping
earlier and more often than the naive policy allowed itself to.

---

## What the comparison does and does not show

**It shows** that the decision engine's ordering, sizing and route choice
produce a **materially** better service level than a reasonable rule-based
policy over the same world: **1.000 against 0.657**, with 79,242 L of demand
met that the baseline left unmet, and 8 station/fuel pairs left at `CRITICAL`
that the platform brought back to `OK`.

**It does not show** that the platform is optimal. The greedy policy is
suboptimal in the classic sense: it can spend a depot's dispatch allowance on
a station that a later, more urgent station needed. The urgency ordering
mitigates this; it does not remove it. A future version could compare
against a local-search or LP variant to measure how much that costs.

**It does not show** anything about the human-in-the-loop decision itself.
Both runs use the same "approve everything" policy, because measuring the
*operator's* judgement would need a different experiment.

**It does not compare two identical shipping schedules.** The naive baseline
made 21 shipments against the platform's 339. The policies differ in how
much they ship as well as in how they choose, and the results above should
be read as two complete policies, not two schedules.

---

## Reproducing

The raw numbers are written to `/tmp/day4_policy_comparison.json`. The
script needs the stack running:

```bash
docker compose up -d
backend/.venv/bin/python scripts/day4_policy_comparison.py --ticks 250 --step-pause 1.1
```
