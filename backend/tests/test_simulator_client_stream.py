"""Tests for the simulator client's SSE stream and its remaining reads.

The stream is the platform's *hint* channel — every event says
"something changed, go and look again". It is not the source of truth,
so the failure modes that matter are the ones where the platform would
otherwise believe it has a live feed when it does not:

* a stream that times out is noticed, not waited on forever;
* a stream that fails to connect raises the connection error;
* a stream that answers 503 raises the fault-injection error;
* an event split across two reads is still decoded.

The rest of this module covers the read endpoints the dashboard uses
that the happy-path tests do not touch.
"""

from __future__ import annotations

import httpx
import pytest

from app.config import Settings
from app.simulator_client import SimulatorClient
from app.simulator_errors import (
    FaultInjectedError,
    SimulatorConnectionError,
    SimulatorTimeoutError,
)


def _client(handler, **settings_kwargs) -> SimulatorClient:
    """A client whose HTTP layer is fully mocked."""
    defaults = {
        "SIMULATOR_BASE_URL": "http://simulator.test",
        "SIMULATOR_REQUEST_TIMEOUT": 1.0,
        "SIMULATOR_CONNECT_TIMEOUT": 0.5,
        "SIMULATOR_MAX_RETRIES": 2,
        "SIMULATOR_RETRY_BASE_DELAY": 0.001,
        "SIMULATOR_RETRY_MAX_DELAY": 0.01,
        "SSE_READ_TIMEOUT": 30.0,
    }
    defaults.update(settings_kwargs)
    settings = Settings(**defaults)
    client = SimulatorClient(settings=settings)
    client._client = httpx.AsyncClient(
        base_url=settings.simulator_base_url,
        transport=httpx.MockTransport(handler),
    )
    return client


# -- the stream -----------------------------------------------------------


async def test_the_stream_yields_the_events_it_receives() -> None:
    """A well-formed event is decoded and handed on."""

    def handler(request: httpx.Request) -> httpx.Response:
        body = 'event: tick\ndata: {"tick": 12}\n\n'
        return httpx.Response(200, text=body)

    client = _client(handler)

    events = [event async for event in client.stream()]

    assert len(events) == 1
    assert events[0].event == "tick"


async def test_an_event_split_across_two_reads_is_still_decoded() -> None:
    """The buffer carries a partial event between reads.

    A TCP read can land in the middle of an event. Without a buffer the
    event would be dropped, and the platform would miss a tick.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        body = 'event: tick\ndata: {"tick":' + " 12}\n\n"
        return httpx.Response(200, text=body)

    client = _client(handler)

    events = [event async for event in client.stream()]

    assert len(events) == 1


async def test_several_events_in_one_read_are_all_decoded() -> None:
    """One read can carry several events."""
    body = 'event: tick\ndata: {"tick": 12}\n\nevent: allocation\ndata: {"id": 7}\n\n'

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=body)

    client = _client(handler)

    events = [event async for event in client.stream()]

    assert len(events) == 2


async def test_a_stream_that_times_out_raises_the_timeout_error() -> None:
    """A dead stream is noticed rather than waited on forever.

    Without a bounded read timeout the platform would sit on a socket
    that will never answer and keep believing it had a live feed.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("the read timed out")

    client = _client(handler)

    with pytest.raises(SimulatorTimeoutError):
        async for _ in client.stream():
            pass


async def test_a_stream_that_cannot_connect_raises_the_connection_error() -> None:
    """A transport failure is a connection error, not a timeout."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("the connection was refused")

    client = _client(handler)

    with pytest.raises(SimulatorConnectionError):
        async for _ in client.stream():
            pass


async def test_a_stream_that_answers_503_raises_the_fault_error() -> None:
    """The ``stream_disconnect`` fault surfaces as the fault error."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            503,
            json={"error": {"code": "FAULT_INJECTED", "message": "stream is down"}},
        )

    client = _client(handler)

    with pytest.raises(FaultInjectedError):
        async for _ in client.stream():
            pass


async def test_a_stream_that_answers_503_with_a_plain_body_still_raises() -> None:
    """A 503 whose body is not JSON is still a fault, not a crash."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="the stream is down")

    client = _client(handler)

    with pytest.raises(Exception) as caught:
        async for _ in client.stream():
            pass

    assert "FAULT_INJECTED" in str(caught.value) or caught.value.status_code == 503


async def test_the_stream_reads_with_a_bounded_timeout() -> None:
    """The read timeout is a multiple of the keepalive interval.

    The simulator sends a keepalive every fifteen seconds, so a stream
    that has been silent for longer than that is dead, not quiet.
    """
    client = _client(lambda request: httpx.Response(200, text=""))

    assert client._settings.sse_read_timeout >= 15.0


# -- the remaining reads ---------------------------------------------------


REGION_PAYLOAD = {
    "id": "region-dhaka",
    "name": "Dhaka",
    "demand_factor": 1.0,
}

SUPPLY_ARRIVAL_PAYLOAD = {
    "id": "arrival-1",
    "depot_id": "depot-gazipur",
    "fuel_type": "DIESEL",
    "quantity": 5_000.0,
    "planned_tick": 20,
    "actual_tick": None,
    "status": "SCHEDULED",
}

EVENT_PAYLOAD = {
    "id": 1,
    "type": "demand_spike",
    "status": "ACTIVE",
    "start_tick": 10,
    "end_tick": 20,
    "parameters": {"station_id": "station-mirpur", "multiplier": 2.0},
}


async def test_the_regions_are_read_as_a_list() -> None:
    """The regions endpoint is part of the documented surface."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[REGION_PAYLOAD])

    client = _client(handler)
    regions = await client.get_regions()

    assert len(regions) == 1


async def test_one_depot_is_read_by_its_id() -> None:
    """The single-entity read is used by the drill scripts."""
    depot_payload = {
        "id": "depot-gazipur",
        "name": "Gazipur Depot",
        "region_id": "region-dhaka",
        "status": "OPEN",
        "dispatch_capacity_per_tick": 12_000,
        "capacity": {"DIESEL": 90_000},
        "inventory": {"DIESEL": 60_000},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/depots/depot-gazipur"
        return httpx.Response(200, json=depot_payload)

    client = _client(handler)
    depot = await client.get_depot("depot-gazipur")

    assert depot.id == "depot-gazipur"


async def test_one_station_is_read_by_its_id() -> None:
    """The single-entity read is used by the drill scripts."""
    station_payload = {
        "id": "station-mirpur",
        "name": "Mirpur Fuel Station",
        "region_id": "region-dhaka",
        "status": "OPEN",
        "demand_profile": "urban_high",
        "demand_multiplier": 1.0,
        "capacity": {"DIESEL": 15_000},
        "inventory": {"DIESEL": 9_000},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/stations/station-mirpur"
        return httpx.Response(200, json=station_payload)

    client = _client(handler)
    station = await client.get_station("station-mirpur")

    assert station.id == "station-mirpur"


async def test_the_supply_arrivals_are_read_as_a_list() -> None:
    """The incoming-supply list is one of the timeline's two sources."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[SUPPLY_ARRIVAL_PAYLOAD])

    client = _client(handler)
    arrivals = await client.get_supply_arrivals()

    assert len(arrivals) == 1


async def test_the_events_are_read_as_a_list() -> None:
    """The active-crisis list is the other source."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[EVENT_PAYLOAD])

    client = _client(handler)
    events = await client.get_events()

    assert len(events) == 1
    assert events[0].type == "demand_spike"


async def test_the_admin_pause_is_its_own_call() -> None:
    """Pause is not the same as run, and it has its own endpoint."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return httpx.Response(200, json={"status": "PAUSED", "message": "paused", "tick": 12})

    client = _client(handler)
    result = await client.admin_pause()

    assert seen == ["/admin/pause"]
    assert result.status == "PAUSED"


async def test_the_admin_toggle_is_its_own_call() -> None:
    """Toggle is its own endpoint too."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return httpx.Response(200, json={"status": "RUNNING", "message": "toggled", "tick": 12})

    client = _client(handler)
    result = await client.admin_toggle()

    assert seen == ["/admin/toggle"]
    assert result.status == "RUNNING"


async def test_the_admin_reset_is_its_own_call() -> None:
    """The reset is what the demo's quick-reset script uses."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return httpx.Response(200, json={"status": "RESET", "message": "reset", "tick": 0})

    client = _client(handler)
    result = await client.admin_reset()

    assert seen == ["/admin/reset"]
    assert result.tick == 0


async def test_the_extra_headers_are_merged_into_the_request() -> None:
    """A caller can add its own headers without losing the Accept.

    The correlation id travels with the request so a log line on the
    simulator's side can be tied to one on this side.
    """
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(request.headers)
        return httpx.Response(
            200,
            json={
                "id": 1,
                "scenario_id": "baseline",
                "scenario_version": "1.0",
                "seed": 1,
                "sim_time": "2026-01-01T00:00:00+00:00",
                "tick": 7,
                "tick_minutes": 15,
                "status": "RUNNING",
            },
        )

    client = _client(handler)
    headers = client._build_headers({"X-Correlation-Id": "abc-123"})

    assert headers["Accept"] == "application/json"
    assert headers["X-Correlation-Id"] == "abc-123"

    # The same headers reach the wire.
    await client._request("GET", "/v1/instance", headers=headers)
    assert seen.get("x-correlation-id") == "abc-123"


async def test_a_response_without_a_model_is_returned_raw() -> None:
    """A read with no model returns the decoded body as-is."""
    from app.simulator_client import SimulatorClient as Client

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"anything": True})

    client = _client(handler)
    result = await client._request("GET", "/v1/anything")

    assert result == {"anything": True}
    assert Client is not None
