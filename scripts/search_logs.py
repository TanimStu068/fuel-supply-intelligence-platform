#!/usr/bin/env python3
"""Search the platform's structured logs.

The backend emits **JSON** logs — one object per line, with a
``correlation_id`` on every line that belongs to a unit of work. That makes
the logs searchable without a second store: ``grep`` and ``jq`` are enough.

This script is the friendly front end to that. It reads the container's
log stream and answers the questions an operator actually asks after an
incident:

* "show me everything for one correlation id"
* "show me every error in the last ten minutes"
* "show me the simulator calls that failed"
* "show me what the platform was doing when the simulator went down"
* "show me one event type between two timestamps"

Usage::

    scripts/search_logs.py                       # the last 200 lines, summarised
    scripts/search_logs.py --level ERROR         # only the errors
    scripts/search_logs.py --correlation cid-abc # one unit of work
    scripts/search_logs.py --event-type simulation.tick --since 2026-11-24T10:00:00Z
    scripts/search_logs.py --contains "circuit"  # full-text search
    scripts/search_logs.py --tail 5000 --json    # machine-readable output
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Any

#: The backend container.
CONTAINER = "fuel-backend"


def read_logs(tail: int) -> list[dict[str, Any]]:
    """Read the backend's logs and parse them as JSON.

    A line that is not JSON is kept as a raw message — the container's
    start-up banner is plain text, and dropping it would hide the fact
    that the service just restarted.
    """
    try:
        completed = subprocess.run(
            ["docker", "logs", CONTAINER, "--tail", str(tail)],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except FileNotFoundError:
        print("docker is not available; the logs cannot be read")
        return []
    except subprocess.TimeoutExpired:
        print("reading the logs timed out")
        return []

    entries: list[dict[str, Any]] = []
    for line in completed.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("{"):
            try:
                entries.append(json.loads(line))
                continue
            except ValueError:
                pass
        entries.append({"level": "RAW", "message": line})
    return entries


def filter_entries(
    entries: list[dict[str, Any]],
    *,
    level: str | None,
    correlation: str | None,
    event_type: str | None,
    contains: str | None,
    since: datetime | None,
    until: datetime | None,
) -> list[dict[str, Any]]:
    """Apply the filters."""
    result = entries
    if level:
        wanted = level.upper()
        result = [
            entry for entry in result if str(entry.get("level", "")).upper() == wanted
        ]
    if correlation:
        result = [
            entry
            for entry in result
            if str(entry.get("correlation_id", "")).casefold() == correlation.casefold()
        ]
    if event_type:
        wanted = event_type.casefold()
        result = [
            entry
            for entry in result
            if str(
                entry.get("event_type") or entry.get("event") or entry.get("kind") or ""
            ).casefold()
            == wanted
        ]
    if contains:
        needle = contains.lower()
        result = [
            entry
            for entry in result
            if needle in json.dumps(entry, default=str).lower()
        ]
    if since is not None:
        kept = []
        for entry in result:
            raw = entry.get("timestamp")
            if not raw:
                continue
            try:
                parsed = datetime.fromisoformat(str(raw))
            except ValueError:
                continue
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
            if parsed >= since:
                kept.append(entry)
        result = kept
    if until is not None:
        kept = []
        for entry in result:
            raw = entry.get("timestamp")
            if not raw:
                continue
            try:
                parsed = datetime.fromisoformat(str(raw))
            except ValueError:
                continue
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
            if parsed <= until:
                kept.append(entry)
        result = kept
    return result


def parse_timestamp(value: str) -> datetime:
    """Parse an ISO-8601 timestamp, treating a missing offset as UTC."""
    parsed = datetime.fromisoformat(value)
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed


def summarise(entries: list[dict[str, Any]]) -> dict[str, Any]:
    """The shape of the log stream."""
    levels = Counter(str(entry.get("level", "UNKNOWN")) for entry in entries)
    loggers = Counter(str(entry.get("logger", "unknown")) for entry in entries)
    messages = Counter(str(entry.get("message", "")) for entry in entries)

    return {
        "total": len(entries),
        "by_level": dict(sorted(levels.items())),
        "by_logger": dict(loggers.most_common(10)),
        "most_common_messages": dict(messages.most_common(10)),
        "correlation_ids": sorted(
            {
                str(e.get("correlation_id"))
                for e in entries
                if e.get("correlation_id") not in (None, "-")
            }
        )[:20],
    }


def main() -> int:
    """Search the logs."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tail", type=int, default=2000, help="how many lines to read")
    parser.add_argument("--level", help="only this level (INFO, WARNING, ERROR)")
    parser.add_argument("--correlation", help="only this correlation id")
    parser.add_argument(
        "--event-type", help="only this structured event/event_type/kind value"
    )
    parser.add_argument("--contains", help="full-text search over the whole record")
    parser.add_argument(
        "--since", help="include records at or after this ISO-8601 time"
    )
    parser.add_argument(
        "--until", help="include records at or before this ISO-8601 time"
    )
    parser.add_argument(
        "--minutes",
        type=int,
        help="only entries from the last N minutes",
    )
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument(
        "--limit", type=int, default=50, help="how many entries to print"
    )
    args = parser.parse_args()

    since = parse_timestamp(args.since) if args.since else None
    until = parse_timestamp(args.until) if args.until else None
    if args.minutes:
        minutes_since = datetime.now(UTC) - timedelta(minutes=args.minutes)
        since = max(filter(None, (since, minutes_since)), default=None)
    if since is not None and until is not None and since > until:
        parser.error("--since must be earlier than or equal to --until")

    entries = read_logs(args.tail)
    entries = filter_entries(
        entries,
        level=args.level,
        correlation=args.correlation,
        event_type=args.event_type,
        contains=args.contains,
        since=since,
        until=until,
    )

    if args.json:
        print(json.dumps({"summary": summarise(entries), "entries": entries}, indent=2))
        return 0

    summary = summarise(entries)
    print(f"=== {summary['total']} entries ===")
    print()
    print("by level:")
    for level, count in summary["by_level"].items():
        print(f"  {level:<10} {count}")
    print()
    print("by logger:")
    for logger, count in summary["by_logger"].items():
        print(f"  {logger:<40} {count}")
    print()
    print("most common messages:")
    for message, count in summary["most_common_messages"].items():
        print(f"  {count:>5}  {message[:90]}")
    print()

    if summary["correlation_ids"]:
        print("correlation ids in this window:")
        for correlation_id in summary["correlation_ids"][:10]:
            print(f"  {correlation_id}")
        print()

    print(f"=== the first {args.limit} entries ===")
    for entry in entries[: args.limit]:
        timestamp = entry.get("timestamp", "-")
        level = entry.get("level", "-")
        correlation_id = entry.get("correlation_id", "-")
        message = entry.get("message", "")
        extra = {
            key: value
            for key, value in entry.items()
            if key not in {"timestamp", "level", "logger", "message", "correlation_id"}
        }
        suffix = f"  {json.dumps(extra, default=str)}" if extra else ""
        print(
            f"{timestamp} {level:<8} {correlation_id:<18} {message[:70]}{suffix[:120]}"
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
