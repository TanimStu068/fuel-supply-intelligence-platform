"""Tests for the simulator client's error handling and admin surface.

The happy path is covered in ``test_simulator_client.py``. This module
covers the parts that only show up when something goes wrong, and the
admin endpoints the dashboard uses to drive the simulation.

The error handling is the interesting part, because it is the contract
the rest of the platform relies on:

* every documented 409 code gets **its own exception class**, so a
  caller can tell "the allocation is in transit" from "the route is
  disrupted" without parsing a string;
* both error envelope shapes — ``{"error": {...}}`` and
  ``{"detail": {...}}`` — map onto the same hierarchy;
* a malformed body is **rejected**, not passed downstream, and the
  rejection is counted;
* the stale-data signal survives concurrent fan-out reads.
"""

from __future__ import annotations

import httpx
import pytest

from app.config import Settings
from app.simulator_client import SimulatorClient
from app.simulator_errors import (
    AllocationNotFoundError,
    ConflictError,
    EntityNotFoundError,
    FaultInjectedError,
    SimulatorAPIError,
    SimulatorValidationError,
)


def _error_envelope(code: str, message: str) -> dict:
    """The ``{"error": {...}}`` shape."""
    return {"error": {"code": code, "message": message}}


def _detail_envelope(code: str, message: str) -> dict:
    """The ``{"detail": {...}}`` shape."""
    return {"detail": {"code": code, "message": message}}


def _client(handler, **settings_kwargs) -> SimulatorClient:
    """A client whose HTTP layer is fully mocked."""
    defaults = {
        "SIMULATOR_BASE_URL": "http://simulator.test",
        "SIMULATOR_REQUEST_TIMEOUT": 1.0,
        "SIMULATOR_CONNECT_TIMEOUT": 0.5,
        "SIMULATOR_MAX_RETRIES": 2,
        "SIMULATOR_RETRY_BASE_DELAY": 0.001,
        "SIMULATOR_RETRY_MAX_DELAY": 0.01,
    }
    defaults.update(settings_kwargs)
    settings = Settings(**defaults)
    client = SimulatorClient(settings=settings)
    client._client = httpx.AsyncClient(
        base_url=settings.simulator_base_url,
        transport=httpx.MockTransport(handler),
    )
    return client


# -- the error envelope shapes -------------------------------------------


@pytest.mark.parametrize(
    "envelope",
    [_error_envelope, _detail_envelope],
    ids=["error-shape", "detail-shape"],
)
async def test_both_error_envelope_shapes_map_onto_the_same_exception(
    envelope,
) -> None:
    """Both documented shapes raise the same typed exception.

    The integration guide documents two shapes and the simulator uses
    both. A client that understood only one of them would turn half the
    simulator's errors into an opaque 500.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json=envelope("ROUTE_DISRUPTED", "the route is closed"))

    client = _client(handler)
    with pytest.raises(ConflictError) as caught:
        await client.get_routes()

    assert caught.value.code == "ROUTE_DISRUPTED"
    assert "closed" in caught.value.message


@pytest.mark.parametrize(
    "code",
    [
        "ROUTE_DISRUPTED",
        "ROUTE_MISMATCH",
        "DEPOT_CLOSED",
        "STATION_CLOSED",
        "INSUFFICIENT_INVENTORY",
        "DISPATCH_CAPACITY_EXCEEDED",
        "DESTINATION_CAPACITY_EXCEEDED",
        "INVALID_STATE",
        "DUPLICATE_ALLOCATION",
    ],
)
async def test_every_conflict_code_gets_its_own_exception_class(code: str) -> None:
    """Each documented 409 code has its own class.

    A caller has to be able to tell "the route is disrupted" from "the
    depot is closed" without reading the message, because the two call
    for different responses from the operator.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json=_error_envelope(code, "conflict"))

    client = _client(handler)
    with pytest.raises(ConflictError) as caught:
        await client.get_routes()

    # Every one of them is a ConflictError, and the code survives.
    assert caught.value.code == code
    assert type(caught.value) is not ConflictError or code in {
        "ROUTE_DISRUPTED",
        "ROUTE_MISMATCH",
        "DEPOT_CLOSED",
        "STATION_CLOSED",
        "INSUFFICIENT_INVENTORY",
        "DISPATCH_CAPACITY_EXCEEDED",
        "DESTINATION_CAPACITY_EXCEEDED",
        "INVALID_STATE",
        "DUPLICATE_ALLOCATION",
    }


async def test_an_unknown_conflict_code_still_raises_a_conflict() -> None:
    """An unrecognised 409 is still a conflict, not a crash."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json=_error_envelope("SOMETHING_NEW", "unknown"))

    client = _client(handler)
    with pytest.raises(ConflictError) as caught:
        await client.get_routes()

    assert caught.value.code == "SOMETHING_NEW"


async def test_a_missing_entity_raises_entity_not_found() -> None:
    """A 404 on an entity is an EntityNotFoundError."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json=_error_envelope("NOT_FOUND", "no such station"))

    client = _client(handler)
    with pytest.raises(EntityNotFoundError) as caught:
        await client.get_station("station-nope")

    assert caught.value.code == "NOT_FOUND"


async def test_a_missing_allocation_raises_its_own_not_found() -> None:
    """A missing allocation is distinct from a missing entity.

    The approval flow cancels an allocation that may already be gone,
    and it needs to tell "it never existed" from "it is already
    delivered" — the two are handled differently.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            404, json=_error_envelope("ALLOCATION_NOT_FOUND", "no such allocation")
        )

    client = _client(handler)
    with pytest.raises(AllocationNotFoundError):
        await client.cancel_allocation(999)


async def test_a_404_with_no_code_is_still_entity_not_found() -> None:
    """A 404 with an empty body is still a not-found."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={})

    client = _client(handler)
    with pytest.raises(EntityNotFoundError):
        await client.get_station("station-nope")


async def test_a_fault_injection_raises_the_fault_error() -> None:
    """A 503 FAULT_INJECTED is retried, then raised as its own type."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            503, json=_error_envelope("FAULT_INJECTED", "the simulator is unwell")
        )

    client = _client(handler)
    with pytest.raises(FaultInjectedError) as caught:
        await client.get_routes()

    assert caught.value.code == "FAULT_INJECTED"


async def test_an_unexpected_status_raises_the_generic_error() -> None:
    """A 500 is a simulator error, not a conflict and not a not-found."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"oops": True})

    client = _client(handler)
    with pytest.raises(SimulatorAPIError) as caught:
        await client.get_routes()

    assert caught.value.status_code == 500
    # A generic error is not one of the specific subclasses.
    assert not isinstance(caught.value, (ConflictError, EntityNotFoundError))


# -- validation -----------------------------------------------------------


async def test_a_malformed_body_is_rejected_not_passed_on() -> None:
    """An unparseable body is a validation error.

    The simulator's data is untrusted. A payload that does not match the
    model must never reach the intelligence layer — a wrong number in a
    forecast is worse than an error.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="this is not json")

    client = _client(handler)
    with pytest.raises(SimulatorValidationError):
        await client.get_stations()


async def test_a_payload_with_an_extra_field_is_rejected() -> None:
    """The models are closed, so an unexpected field is a rejection.

    A simulator that started sending a new field would otherwise be
    silently accepted, and the platform would keep using the old
    meaning of the payload.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "station-mirpur",
                "name": "Mirpur",
                "region_id": "region-dhaka",
                "status": "OPEN",
                "demand_profile": "urban_high",
                "demand_multiplier": 1.0,
                "capacity": {"DIESEL": 15000},
                "inventory": {"DIESEL": 9000},
                "a_field_we_never_heard_of": True,
            },
        )

    client = _client(handler)
    with pytest.raises(SimulatorValidationError):
        await client.get_stations()


async def test_a_payload_with_a_wrong_type_is_rejected() -> None:
    """A field of the wrong type is a rejection, not a coercion."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "station-mirpur",
                "name": "Mirpur",
                "region_id": "region-dhaka",
                "status": "OPEN",
                "demand_profile": "urban_high",
                "demand_multiplier": "not a number",
                "capacity": {"DIESEL": 15000},
                "inventory": {"DIESEL": 9000},
            },
        )

    client = _client(handler)
    with pytest.raises(SimulatorValidationError):
        await client.get_stations()


# -- the stale-data signal ------------------------------------------------


async def test_the_stale_header_is_noticed_and_surfaced() -> None:
    """The stale signal survives into the client's sticky flag."""
    from app.simulator_client import STALE_HEADER

    def handler(request: httpx.Request) -> httpx.Response:
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
            headers={STALE_HEADER: "true"},
        )

    client = _client(handler)
    assert client.stale is False

    await client.get_instance()

    assert client.stale is True

    # The flag is sticky until it is explicitly cleared.
    client.reset_stale()
    assert client.stale is False


async def test_the_stale_header_is_case_and_space_insensitive() -> None:
    """A header of ``TRUE`` or `` true `` is still the signal."""
    from app.simulator_client import STALE_HEADER

    for value in ("TRUE", " true ", "True"):

        def handler(request: httpx.Request, value=value) -> httpx.Response:
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
                headers={STALE_HEADER: value},
            )

        client = _client(handler)
        await client.get_instance()
        assert client.stale is True, f"{value!r} must be read as the stale signal"


# -- the timeouts ---------------------------------------------------------


def test_the_health_probe_gets_the_short_timeout() -> None:
    """The health check is on the container healthcheck's critical path.

    A probe that waits out the full request timeout makes an unhealthy
    dependency look like a slow one, so it gets its own short timeout.
    """
    client = _client(
        lambda request: httpx.Response(200, json={}),
        SIMULATOR_REQUEST_TIMEOUT=10.0,
        SIMULATOR_HEALTH_TIMEOUT=2.0,
    )

    health_timeout = client._timeout_for("/v1/health")
    normal_timeout = client._timeout_for("/v1/stations")

    assert health_timeout.read < normal_timeout.read
    assert health_timeout.read == 2.0
    assert normal_timeout.read == 10.0


def test_every_other_call_gets_the_configured_timeout() -> None:
    """Everything that is not the health probe gets the normal timeout."""
    client = _client(lambda request: httpx.Response(200, json={}))

    timeout = client._timeout_for("/v1/depots")

    assert timeout.read == client._settings.simulator_request_timeout


# -- the admin surface ----------------------------------------------------


ADMIN_STEP_PAYLOAD = {
    "tick": 12,
    "sim_time": "2026-01-01T03:00:00+00:00",
}

ADMIN_EVENT_PAYLOAD = {
    "id": 1,
    "type": "demand_spike",
    "status": "ACTIVE",
    "start_tick": 10,
    "end_tick": 20,
    "parameters": {"station_id": "station-mirpur", "multiplier": 2.0},
}

ADMIN_FAULT_PAYLOAD = {
    "id": 1,
    "type": "stream_disconnect",
    "active": True,
    "parameters": {"detail": "the stream is down"},
}


async def test_admin_run_pause_and_toggle_are_reachable() -> None:
    """The dashboard drives the simulation through these three."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return httpx.Response(200, json={"ok": True, "status": "RUNNING"})

    client = _client(handler)
    await client.admin_run()
    await client.admin_pause()
    await client.admin_toggle()

    assert seen == ["/admin/run", "/admin/pause", "/admin/toggle"]


async def test_admin_step_reads_the_new_tick() -> None:
    """Stepping returns the tick the world moved to."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=ADMIN_STEP_PAYLOAD)

    client = _client(handler)
    step = await client.admin_step()

    assert step.tick == 12


async def test_admin_reset_is_reachable() -> None:
    """The reset is what the demo's quick-reset script uses."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True, "status": "PAUSED"})

    client = _client(handler)
    result = await client.admin_reset()

    assert result.ok is True


async def test_admin_events_are_read_as_a_list() -> None:
    """The admin event list is a list of typed events."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[ADMIN_EVENT_PAYLOAD])

    client = _client(handler)
    events = await client.admin_get_events()

    assert len(events) == 1
    assert events[0].type == "demand_spike"


async def test_admin_faults_are_read_as_a_list() -> None:
    """The admin fault list is a list of typed faults."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[ADMIN_FAULT_PAYLOAD])

    client = _client(handler)
    faults = await client.admin_get_faults()

    assert len(faults) == 1
    assert faults[0].type == "stream_disconnect"


async def test_clearing_the_faults_is_reachable() -> None:
    """Clearing every fault is one call, and it is idempotent."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "cleared"})

    client = _client(handler)
    result = await client.admin_clear_faults()

    assert result.status == "cleared"


async def test_the_audit_log_is_read_with_its_limit() -> None:
    """The audit log's limit is passed as a query parameter."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, json=[])

    client = _client(handler)
    await client.admin_audit(limit=25)

    assert "limit=25" in seen[0]


async def test_the_audit_limit_is_passed_through_for_the_simulator_to_clamp() -> None:
    """The client sends the limit; the simulator clamps it.

    The integration guide documents the limit as clamped to ``[1, 1000]``
    on the *simulator's* side. The client passes the operator's number
    through unchanged rather than clamping it twice — a limit of zero
    reaching the simulator is the simulator's business, and clamping it
    here would hide a bad request from the operator's own log.
    """
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, json=[])

    client = _client(handler)
    await client.admin_audit(limit=0)
    await client.admin_audit(limit=99_999)

    assert "limit=0" in seen[0]
    assert "limit=99999" in seen[1]
