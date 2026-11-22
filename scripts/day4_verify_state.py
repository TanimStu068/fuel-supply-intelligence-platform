#!/usr/bin/env python3
"""Check that the Day 4 fallback and split work is actually on disk.

Several edits in this session were silently reverted by an external
process. The unit tests catch a missing behaviour, but only once they
fail — and a *reverted* test edit can hide a real defect behind a green
suite. This script asserts the specific facts the implementation is
supposed to hold, straight from the source files, so a silent revert is
caught before it reaches a commit.

Usage: python scripts/day4_verify_state.py
"""

from __future__ import annotations

import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent / "backend"

PASS = 0
FAIL = 0


def check(label: str, condition: bool) -> None:
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  ok    {label}")
    else:
        FAIL += 1
        print(f"  FAIL  {label}")


def read(relative: str) -> str:
    return (BACKEND / relative).read_text()


def main() -> int:
    print("=== the decision package exports ===")
    package = read("app/decision/__init__.py")
    check("FallbackPolicy is exported", '"FallbackPolicy"' in package)
    check("FallbackRisk is exported", '"FallbackRisk"' in package)
    check("StationProfile is exported", '"StationProfile"' in package)
    check("StationProfile is imported", "from .fallback import" in package)

    print("\n=== the optimizer splits oversized deficits ===")
    optimizer = read("app/decision/optimizer.py")
    check("_propose_for returns a list", "-> list[Recommendation]:" in optimizer)
    check("the split helper exists", "def _split(" in optimizer)
    check("the split helper is used", "return self._split(" in optimizer)
    check("the leg count is capped", "max_split_legs" in optimizer)
    check(
        "the idempotency key carries the leg",
        'suffix = f"-{leg}" if leg else ""' in optimizer,
    )
    check(
        "the optimizer has no dead fallback branch",
        "def _confidence(\n        self, risk: RiskAssessment, projection: RiskProjection, fallback: bool"
        not in optimizer,
    )

    print("\n=== the fallback policy is the only rule-based policy ===")
    fallback = read("app/decision/fallback.py")
    check("the fallback module exists", "class FallbackPolicy" in fallback)
    check(
        "the fallback marks its recommendations",
        "used_fallback_policy=True" in fallback,
    )
    check(
        "the fallback caps its confidence",
        "confidence=0.4" in fallback,
    )
    check(
        "the fallback timestamps are timezone-aware",
        "datetime.now(UTC)" in fallback,
    )

    print("\n=== the recommendation timestamps are timezone-aware ===")
    models = read("app/decision/models.py")
    check("the model imports UTC", "from datetime import UTC, datetime" in models)
    check(
        "the default timestamp is timezone-aware",
        "datetime.now(UTC)" in models,
    )

    print("\n=== the fallback activation is counted once per outage ===")
    service = read("app/decision/service.py")
    check("the activation latch exists", "self._fallback_counted = False" in service)
    check(
        "the activation is consumed once",
        "def consume_fallback_activation" in service,
    )
    check(
        "the fallback reason is sticky",
        "def set_fallback_reason" in service,
    )
    check(
        "the fallback recommendations are counted",
        '"fallback_recommendations"' in service,
    )

    print("\n=== the intelligence layer drops a stale result ===")
    intelligence = read("app/intelligence_service.py")
    check(
        "a failed evaluation clears the last result",
        "self._last_result = None" in intelligence,
    )
    check("the shared propose path exists", "async def propose_now(" in intelligence)
    check(
        "the metrics path consumes the activation",
        "consume_fallback_activation()" in intelligence,
    )
    check(
        "the metric label is bounded",
        "def _fallback_label(" in intelligence,
    )

    print("\n=== the API uses the shared propose path ===")
    main_module = read("app/main.py")
    check(
        "the propose endpoint calls propose_now",
        "await service.propose_now()" in main_module,
    )
    check(
        "the propose endpoint no longer calls the optimizer directly",
        "decision.optimizer.propose(" not in main_module,
    )
    check(
        "the settings endpoint reports the fallback policy",
        '"fallback_policy"' in main_module,
    )

    print(f"\n=== Result ===\npassed: {PASS}   failed: {FAIL}")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
