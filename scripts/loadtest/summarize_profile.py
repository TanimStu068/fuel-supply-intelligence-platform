#!/usr/bin/env python3
"""Summarise a py-spy speedscope profile: where the self-time actually goes.

The speedscope format keeps the frames in a shared table and the samples as
lists of **frame indices**, root first. The frame that was executing is the
last index in the sample.
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path


def _label(frame: dict) -> str:
    name = frame.get("name", "?")
    file = frame.get("file")
    if not file:
        return name
    return f"{name} ({file.split('/')[-1]}:{frame.get('line', '?')})"


def main() -> int:
    path = Path(sys.argv[1] if len(sys.argv) > 1 else ".tmp-pyspy/prof-scoped.json")
    data = json.loads(path.read_text())
    shared = data.get("shared", {}).get("frames", [])

    self_time: dict[str, int] = defaultdict(int)
    total_time: dict[str, int] = defaultdict(int)
    total = 0

    for profile in data.get("profiles", []):
        for sample in profile.get("samples", []):
            if not sample:
                continue
            total += 1
            leaf = shared[sample[-1]] if sample[-1] < len(shared) else {"name": "?"}
            self_time[_label(leaf)] += 1
            for index in dict.fromkeys(sample):
                frame = shared[index] if index < len(shared) else {"name": "?"}
                total_time[_label(frame)] += 1

    if not total:
        print("no samples")
        return 1

    print(f"samples: {total}\n")
    print("=== SELF TIME (the frame actually running) ===")
    for name, count in sorted(self_time.items(), key=lambda kv: -kv[1])[:28]:
        print(f"{100 * count / total:6.2f}%  {count:5d}  {name}")

    print("\n=== TOTAL TIME (on the stack) ===")
    for name, count in sorted(total_time.items(), key=lambda kv: -kv[1])[:45]:
        print(f"{100 * count / total:6.2f}%  {count:5d}  {name}")
    return 0


sys.exit(main())
