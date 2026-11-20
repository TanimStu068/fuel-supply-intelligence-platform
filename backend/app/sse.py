"""A parser for the simulator's SSE wire format (guide §6).

The simulator sends::

    : connected\\n\\n
    event: simulation.tick\\ndata: {"tick": 3, "sim_time": "..."}\\n\\n
    : keepalive\\n\\n

Three details drive the design:

* **Comments.** A line starting with ``:`` is an SSE comment — the connect
  greeting and the 15-second keepalive. They are ignored, not parsed.
* **Blank line terminates an event.** Fields accumulate until an empty line.
* **Silence is not a disconnect.** The simulator only speaks every 15 seconds
  when nothing happens, so a quiet stream is normal and must never be treated
  as a broken connection.

The parser is a pure function over a byte buffer so it can be unit-tested
without a socket.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

#: Event names the simulator publishes (guide §6.3).
EVENT_SIMULATION_TICK = "simulation.tick"
EVENT_ALLOCATION_STATUS_CHANGED = "allocation.status_changed"
EVENT_INVENTORY_UPDATED = "inventory.updated"
EVENT_SIMULATOR_NOTICE = "simulator.notice"

KNOWN_EVENTS = frozenset(
    {
        EVENT_SIMULATION_TICK,
        EVENT_ALLOCATION_STATUS_CHANGED,
        EVENT_INVENTORY_UPDATED,
        EVENT_SIMULATOR_NOTICE,
    }
)


@dataclass(slots=True)
class StreamEvent:
    """One decoded SSE event."""

    event: str
    data: dict[str, Any]

    @property
    def is_tick(self) -> bool:
        return self.event == EVENT_SIMULATION_TICK

    @property
    def is_allocation(self) -> bool:
        return self.event == EVENT_ALLOCATION_STATUS_CHANGED

    @property
    def is_inventory(self) -> bool:
        return self.event == EVENT_INVENTORY_UPDATED

    @property
    def is_notice(self) -> bool:
        return self.event == EVENT_SIMULATOR_NOTICE

    @property
    def entity_id(self) -> str | None:
        """The entity the event is about, when the payload names one."""
        value = self.data.get("entity_id")
        return value if isinstance(value, str) else None


def parse_sse_block(block: str) -> StreamEvent | None:
    """Parse one complete SSE block (the text between two blank lines).

    Returns ``None`` for comments, empty blocks and blocks whose payload is
    not a JSON object.
    """
    event_name: str | None = None
    data_lines: list[str] = []

    for raw_line in block.split("\n"):
        line = raw_line.rstrip("\r")
        if not line:
            continue
        if line.startswith(":"):
            # Comment: the connect greeting or a keepalive.
            continue
        if ":" not in line:
            continue

        field, _, value = line.partition(":")
        value = value[1:] if value.startswith(" ") else value

        if field == "event":
            event_name = value
        elif field == "data":
            data_lines.append(value)

    if event_name is None or not data_lines:
        return None

    raw_data = "\n".join(data_lines)
    try:
        payload = json.loads(raw_data)
    except ValueError:
        return None

    if not isinstance(payload, dict):
        return None

    return StreamEvent(event=event_name, data=payload)


class SSEParser:
    """Incremental SSE parser fed with chunks of the response body.

    Feed it bytes; it returns the events that became complete. The buffer is
    kept between feeds, so an event split across two TCP reads is still
    decoded correctly.
    """

    def __init__(self) -> None:
        self._buffer = ""

    def feed(self, chunk: str) -> list[StreamEvent]:
        """Feed a chunk of decoded text and return the completed events."""
        self._buffer += chunk
        events: list[StreamEvent] = []

        # Events are separated by a blank line. Normalise CRLF first so the
        # separator check is a simple "\n\n".
        self._buffer = self._buffer.replace("\r\n", "\n").replace("\r", "\n")

        while "\n\n" in self._buffer:
            block, _, self._buffer = self._buffer.partition("\n\n")
            event = parse_sse_block(block)
            if event is not None:
                events.append(event)

        return events

    @property
    def pending(self) -> str:
        """The bytes buffered so far that have not completed an event."""
        return self._buffer
