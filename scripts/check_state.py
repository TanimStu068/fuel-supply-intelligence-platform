#!/usr/bin/env python3
"""Check that ``/api/state`` carries a whole network, not an empty one.

A healthy backend serving an empty world is still a failure: the
simulator was not reached, or the sync never completed. The smoke test
has to look at the *content*, not only at the status code.

    python3 scripts/check_state.py /tmp/state.json
"""

from __future__ import annotations

import json
import sys

REQUIRED_SECTIONS = ("stations", "depots", "routes")


def main(argv: list[str]) -> int:
    """Check the state document and report what is missing."""
    if len(argv) != 2:
        print("usage: check_state.py <state.json>", file=sys.stderr)
        return 2

    with open(argv[1]) as handle:
        state = json.load(handle)

    failures = []
    for section in REQUIRED_SECTIONS:
        rows = state.get(section)
        if not rows:
            failures.append(f"the state has no {section}")

    if failures:
        for failure in failures:
            print(f"::error::{failure}")
        return 1

    print(
        f"{len(state['stations'])} stations, "
        f"{len(state['depots'])} depots, "
        f"{len(state['routes'])} routes"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
