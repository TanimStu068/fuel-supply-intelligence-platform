"""Tests for the SSE wire-format parser (guide §6)."""

from __future__ import annotations

from app.sse import SSEParser, parse_sse_block


def test_parses_a_tick_event() -> None:
    block = 'event: simulation.tick\ndata: {"tick": 3, "sim_time": "2026-01-01T00:45:00"}'

    event = parse_sse_block(block)

    assert event is not None
    assert event.event == "simulation.tick"
    assert event.data["tick"] == 3
    assert event.is_tick


def test_ignores_the_connect_comment() -> None:
    assert parse_sse_block(": connected") is None


def test_ignores_the_keepalive_comment() -> None:
    assert parse_sse_block(": keepalive") is None


def test_parses_an_inventory_event() -> None:
    block = (
        "event: inventory.updated\n"
        'data: {"entity_type": "depot", "entity_id": "depot-gazipur", '
        '"inventory": {"DIESEL": 55000}}'
    )

    event = parse_sse_block(block)

    assert event is not None
    assert event.is_inventory
    assert event.entity_id == "depot-gazipur"


def test_parses_an_allocation_event() -> None:
    block = (
        "event: allocation.status_changed\n"
        'data: {"id": 7, "status": "IN_TRANSIT", "quantity": 3000}'
    )

    event = parse_sse_block(block)

    assert event is not None
    assert event.is_allocation
    assert event.data["status"] == "IN_TRANSIT"


def test_parses_a_notice_event() -> None:
    block = 'event: simulator.notice\ndata: {"level": "error", "message": "boom"}'

    event = parse_sse_block(block)

    assert event is not None
    assert event.is_notice
    assert event.data["message"] == "boom"


def test_rejects_a_block_with_invalid_json() -> None:
    assert parse_sse_block("event: simulation.tick\ndata: {not json") is None


def test_rejects_a_block_with_a_json_array() -> None:
    assert parse_sse_block("event: simulation.tick\ndata: [1, 2, 3]") is None


def test_rejects_a_block_without_an_event_name() -> None:
    assert parse_sse_block('data: {"tick": 1}') is None


def test_parser_splits_multiple_events_in_one_chunk() -> None:
    parser = SSEParser()

    events = parser.feed(
        'event: simulation.tick\ndata: {"tick": 1}\n\nevent: simulation.tick\ndata: {"tick": 2}\n\n'
    )

    assert [event.data["tick"] for event in events] == [1, 2]


def test_parser_buffers_an_event_split_across_chunks() -> None:
    parser = SSEParser()

    first = parser.feed('event: simulation.tick\ndata: {"tick":')
    assert first == []

    second = parser.feed(" 42}\n\n")

    assert len(second) == 1
    assert second[0].data["tick"] == 42


def test_parser_ignores_comments_between_events() -> None:
    parser = SSEParser()

    events = parser.feed(
        ': connected\n\nevent: simulation.tick\ndata: {"tick": 5}\n\n: keepalive\n\n'
    )

    assert len(events) == 1
    assert events[0].data["tick"] == 5


def test_parser_handles_crlf_line_endings() -> None:
    parser = SSEParser()

    events = parser.feed('event: simulation.tick\r\ndata: {"tick": 9}\r\n\r\n')

    assert len(events) == 1
    assert events[0].data["tick"] == 9


def test_silence_is_not_a_disconnect() -> None:
    """A quiet stream produces no events but is not an error."""
    parser = SSEParser()

    assert parser.feed("") == []
    assert parser.feed(": keepalive\n\n") == []
    assert parser.pending == ""
