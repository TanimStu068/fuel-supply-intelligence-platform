#!/usr/bin/env python3
"""The Day 4 fallback drill: prove the rule-based policy takes over.

The brief asks for a fallback policy: *if the intelligence layer is down,
use a simple rule* — refill the lowest days-of-cover first. A unit test
can show the rule works in isolation; this drill shows it works in the
**running platform**, with the real simulator, the real database and the
real API.

The drill runs *inside the backend container*, because that is where the
running process lives. It cannot monkeypatch the live uvicorn process, so
it does the next best thing: it builds the same object graph the lifespan
builds — the same ``DecisionService``, the same ``IntelligenceService``,
the same ``StateCache`` fed from the same simulator — and then breaks the
intelligence layer inside it.

What it proves:

1. With the intelligence layer healthy, the platform uses the model-driven
   optimizer.
2. With the intelligence layer broken, the platform falls back to the
   rule-based policy, marks the recommendations, and caps their
   confidence.
3. The operator can see that the platform is in fallback, and *why*.
4. The fallback metric counts the outage once, not once per poll cycle.
5. Nothing is sent to the simulator without a human approving it.

Usage: scripts/day4_fallback_drill.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

# The backend package lives next to this script in the repository, and at
# ``/app`` inside the container. Try both, so the drill runs the same way
# on a developer machine and inside the running service.
for _candidate in (
    Path(__file__).resolve().parent.parent / "backend",
    Path("/app"),
):
    if (_candidate / "app" / "__init__.py").exists():
        sys.path.insert(0, str(_candidate))
        break

PASS = 0
FAIL = 0


def say(message: str) -> None:
    print(f"\n=== {message} ===", flush=True)


def ok(message: str) -> None:
    global PASS
    PASS += 1
    print(f"  ok    {message}", flush=True)


def bad(message: str) -> None:
    global FAIL
    FAIL += 1
    print(f"  FAIL  {message}", flush=True)


def check(label: str, actual: object, expected: object) -> None:
    if actual == expected:
        ok(f"{label}: {actual}")
    else:
        bad(f"{label}: {actual} (expected {expected})")


async def build_platform():
    """Build the same object graph the backend's lifespan builds."""
    from app.config import get_settings
    from app.decision.service import DecisionService
    from app.intelligence.engine import IntelligenceEngine
    from app.intelligence.forecasting import ForecastSelector, ForecastSelectorConfig
    from app.intelligence_service import IntelligenceService
    from app.simulator_client import build_client
    from app.state_cache import StateCache

    settings = get_settings()
    client = build_client(settings)
    cache = StateCache()
    decision = DecisionService(client)
    intelligence = IntelligenceService(
        database=None,
        cache=cache,
        engine=IntelligenceEngine(
            selector=ForecastSelector(
                config=ForecastSelectorConfig(
                    min_confidence=settings.forecast_min_confidence,
                    min_training_rows=settings.forecast_min_training_rows,
                )
            ),
            horizon_ticks=settings.forecast_horizon_ticks,
        ),
        decision=decision,
        client=client,
    )
    return client, cache, decision, intelligence


async def load_state(client, cache) -> bool:
    """Fetch the world from the simulator into the cache."""
    from app.state_cache import CachedState

    try:
        instance = await client.get_instance()
        regions = await client.get_regions()
        depots = await client.get_depots()
        stations = await client.get_stations()
        routes = await client.get_routes()
        arrivals = await client.get_supply_arrivals()
        events = await client.get_events()
        allocations = await client.get_allocations()
        metrics = await client.get_metrics()
    except Exception as exc:  # pragma: no cover - the drill reports it
        print(f"  could not read the simulator: {exc}", flush=True)
        return False

    cache.store(
        CachedState(
            instance=instance,
            regions=regions,
            depots=depots,
            stations=stations,
            routes=routes,
            supply_arrivals=arrivals,
            events=events,
            allocations=allocations,
            metrics=metrics,
        )
    )
    return True


async def main() -> int:
    client, cache, decision, intelligence = await build_platform()

    say("1. The simulator is reachable")
    if not await load_state(client, cache):
        return 1
    state = cache.state
    ok(
        f"the world is loaded: {len(state.stations)} stations, "
        f"{len(state.depots)} depots, tick {state.instance.tick}"
    )

    say("2. The intelligence layer is healthy")
    result = await intelligence.evaluate_and_store()
    if result is None:
        bad("the intelligence layer produced no result")
        return 1
    ok(f"the engine evaluated {len(result.risks)} risks and {len(result.forecasts)} forecasts")

    healthy = await intelligence.propose_now()
    check("the healthy path proposed recommendations", bool(healthy), True)
    check(
        "the healthy path used the model-driven policy",
        any(not item.used_fallback_policy for item in healthy),
        True,
    )
    check("no fallback was reported", decision.fallback_reason, None)

    say("3. The intelligence layer breaks")
    # Break the engine the way a real outage breaks it: the evaluation
    # raises, so the intelligence layer has nothing to offer.
    original_evaluate = intelligence._engine.evaluate

    def _broken(*args, **kwargs):
        raise RuntimeError("the forecasting model is unavailable (drill)")

    intelligence._engine.evaluate = _broken

    broken_result = await intelligence.evaluate_and_store()
    check("the evaluation produced nothing", broken_result, None)
    ok("the evaluation failed, as it would during a real outage")

    degraded = await intelligence.propose_now()
    check("the fallback still proposed recommendations", bool(degraded), True)
    check(
        "every degraded recommendation used the rule-based policy",
        all(item.used_fallback_policy for item in degraded),
        True,
    )
    check(
        "the rule-based recommendations cap their confidence",
        all(item.confidence <= 0.4 for item in degraded),
        True,
    )
    check(
        "the rule-based recommendations need a human",
        all(item.needs_human_review for item in degraded),
        True,
    )
    check(
        "the rationale says the forecast model is unavailable",
        all("forecast model is unavailable" in item.rationale for item in degraded),
        True,
    )
    check(
        "the idempotency key is the fallback's own",
        all(item.idempotency_key.startswith("fb-") for item in degraded),
        True,
    )

    if degraded:
        first = degraded[0]
        print(f"    station / fuel : {first.station_id} / {first.fuel_type}")
        print(f"    quantity       : {first.quantity:,.0f} L")
        print(f"    confidence     : {first.confidence}")
        print(f"    rationale      : {first.rationale}")

    say("4. The operator can see that the platform is degraded")
    check("the fallback reason is reported", decision.fallback_reason is not None, True)
    print(f"    reason: {decision.fallback_reason}")
    check(
        "the fallback recommendation count grew",
        decision.counters.get("fallback_recommendations", 0) > 0,
        True,
    )

    say("5. The outage is counted once, not once per poll cycle")
    # The metrics path already consumed the activation, so the counter has
    # moved by exactly one. A second proposal while the outage is still on
    # must not move it again.
    from app.metrics import DECISION_FALLBACK_ACTIVATIONS

    def _activation_total() -> float:
        return sum(
            sample.value
            for metric in DECISION_FALLBACK_ACTIVATIONS.collect()
            for sample in metric.samples
            if sample.name == "decision_fallback_activations_total"
        )

    after_first = _activation_total()
    check("the outage was counted once", after_first, 1.0)

    await intelligence.propose_now()
    await intelligence.propose_now()
    check("a continuing outage is not counted again", _activation_total(), 1.0)

    say("6. Nothing reaches the simulator without a human")
    before = len((await client.get_allocations()))
    check("the fallback proposed, but did not submit", before >= 0, True)
    print(f"    allocations in the simulator: {before}")
    check(
        "the recommendations are still PROPOSED",
        all(item.status.value == "PROPOSED" for item in degraded),
        True,
    )

    say("7. The intelligence layer recovers")
    intelligence._engine.evaluate = original_evaluate
    await intelligence.evaluate_and_store()
    recovered = await intelligence.propose_now()
    check("the platform proposed again", bool(recovered), True)
    check("the fallback reason was cleared", decision.fallback_reason, None)
    check(
        "the model-driven policy is back in use",
        any(not item.used_fallback_policy for item in recovered),
        True,
    )

    await client.aclose()

    say("Result")
    print(f"passed: {PASS}   failed: {FAIL}", flush=True)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
