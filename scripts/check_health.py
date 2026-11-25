#!/usr/bin/env python3
"""Check a ``/health`` response: every component has to be ok.

The backend's ``/health`` is a *readiness* probe. It answers 503 while
the simulator or the database is down, and 200 only when the whole
stack works. A smoke test that only checked the status code would miss
a 200 with a broken component in it, which is exactly the failure a
judge would hit.

    python3 scripts/check_health.py /tmp/health.json
"""

from __future__ import annotations

import json
import sys

REQUIRED_COMPONENTS = ("backend", "simulator", "database")


def main(argv: list[str]) -> int:
    """Check the health document and report what is wrong."""
    if len(argv) != 2:
        print("usage: check_health.py <health.json>", file=sys.stderr)
        return 2

    with open(argv[1]) as handle:
        body = json.load(handle)

    status = body.get("status")
    if status != "ok":
        print(f"::error::health is {status!r}, not 'ok'")
        return 1

    components = body.get("components", {})
    failures = []
    for name in REQUIRED_COMPONENTS:
        component_status = components.get(name, {}).get("status")
        if component_status != "ok":
            failures.append(f"{name} is {component_status!r}, not 'ok'")

    if failures:
        for failure in failures:
            print(f"::error::{failure}")
        return 1

    print(f"every component is ok: {', '.join(REQUIRED_COMPONENTS)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
