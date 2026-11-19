"""Regression tests for the discrepancies found against the live simulator.

Each test pins a behaviour observed on the running image that the
integration guide either contradicts or leaves unspecified. See
``docs/simulator-notes.md`` for the raw observations.
"""

from __future__ import annotations

import httpx
import pytest
from pydantic import ValidationError

from app.simulator_models import (
    AdminActionResponse,
    AdminEvent,
    AdminFault,
    Allocation,
    AuditLogEntry,
    Instance,
)


async def test_naive_sim_time_is_anchored_to_utc(client_factory) -> None:
    """D1: the simulator emits ``2026-01-01T00:00:00`` with no offset."""
    payload = {
        "id": 1,
        "scenario_id": "baseline",
        "scenario_version": "1.0",
        "seed": 12345,
        "sim_time": "2026-01-01T00:00:00",
        "tick": 0,
        "tick_minutes": 15,
        "status": "PAUSED",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    client = client_factory(handler)
    instance = await client.get_instance()

    assert instance.sim_time.tzinfo is not None
    assert instance.sim_time.utcoffset().total_seconds() == 0
    assert instance.sim_time.hour == 0


async def test_offset_aware_sim_time_is_preserved(client_factory) -> None:
    """A timestamp that already carries an offset must not be shifted."""
    payload = {
        "id": 1,
        "scenario_id": "baseline",
        "scenario_version": "1.0",
        "seed": 12345,
        "sim_time": "2026-01-01T06:00:00+06:00",
        "tick": 0,
        "tick_minutes": 15,
        "status": "PAUSED",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    client = client_factory(handler)
    instance = await client.get_instance()

    assert instance.sim_time.utcoffset().total_seconds() == 6 * 3600
    assert instance.sim_time.hour == 6


async def test_audit_row_with_null_entity_fields_is_accepted(client_factory) -> None:
    """D4: instance-level audit actions carry ``entity_type: null``."""
    rows = [
        {
            "id": 12,
            "wall_time": "2026-11-19T15:48:33.803958+00:00",
            "sim_time": "2026-01-01T00:30:00",
            "tick": 2,
            "action": "admin.pause",
            "entity_type": None,
            "entity_id": None,
            "result": "OK",
            "metadata_json": {},
        },
        {
            "id": 11,
            "wall_time": "2026-11-19T15:48:33.803958+00:00",
            "sim_time": "2026-01-01T00:30:00",
            "tick": 2,
            "action": "allocation.created",
            "entity_type": "allocation",
            "entity_id": "1",
            "result": "OK",
            "metadata_json": {"quantity": 3000, "fuel_type": "DIESEL"},
        },
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=rows)

    client = client_factory(handler)
    audit = await client.admin_audit()

    assert len(audit) == 2
    assert audit[0].entity_type is None
    assert audit[0].entity_id is None
    assert audit[1].entity_id == "1"


async def test_audit_row_with_null_sim_time_and_tick_is_accepted(client_factory) -> None:
    """D4: ``sim_time`` and ``tick`` are nullable too."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=[
                {
                    "id": 1,
                    "wall_time": "2026-11-19T15:48:33.803958+00:00",
                    "sim_time": None,
                    "tick": None,
                    "action": "fault.clear_all",
                    "entity_type": None,
                    "entity_id": None,
                    "result": "OK",
                    "metadata_json": {},
                }
            ],
        )

    client = client_factory(handler)
    audit = await client.admin_audit()

    assert audit[0].sim_time is None
    assert audit[0].tick is None


def test_fault_duration_is_derived_from_the_wall_clock_window() -> None:
    """D3: the fault row has no ``duration_seconds``; we derive it."""
    from datetime import UTC, datetime, timedelta

    start = datetime(2026, 11, 19, 15, 48, 33, tzinfo=UTC)
    fault = AdminFault.model_validate(
        {
            "id": 3,
            "type": "latency",
            "start_wall_time": start.isoformat(),
            "end_wall_time": (start + timedelta(seconds=30)).isoformat(),
            "active": True,
            "parameters": {"delay_ms": 300},
        }
    )

    assert fault.duration_seconds == 30


async def test_injected_fault_row_round_trips(client_factory) -> None:
    """D3: the live fault row shape must validate."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            201,
            json={
                "id": 3,
                "type": "latency",
                "start_wall_time": "2026-11-19T15:48:33.803958+00:00",
                "end_wall_time": "2026-11-19T15:48:34.803958+00:00",
                "active": True,
                "parameters": {"delay_ms": 300},
            },
        )

    client = client_factory(handler)
    fault = await client.admin_inject_fault(
        {"type": "latency", "duration_seconds": 1, "parameters": {"delay_ms": 300}}
    )

    assert fault.type.value == "latency"
    assert fault.active is True
    assert fault.duration_seconds == 1


async def test_admin_run_returns_the_serialized_instance(client_factory) -> None:
    """``/admin/run`` answers with the instance, not a bare status string."""
    payload = {
        "id": 1,
        "scenario_id": "baseline",
        "scenario_version": "1.0",
        "seed": 12345,
        "sim_time": "2026-01-01T00:30:00",
        "tick": 2,
        "tick_minutes": 15,
        "status": "RUNNING",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    client = client_factory(handler)
    result = await client.admin_run()

    assert result.status == "RUNNING"
    assert result.tick == 2


async def test_audit_limit_zero_returns_no_rows(client_factory) -> None:
    """D5: the simulator does not clamp the lower bound; it returns []."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["limit"] == "0"
        return httpx.Response(200, json=[])

    client = client_factory(handler)
    audit = await client.admin_audit(limit=0)

    assert audit == []


async def test_demand_history_limit_is_passed_through(client_factory) -> None:
    """D6: the client must not silently rewrite the limit."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["limit"] == "5000"
        return httpx.Response(200, json=[])

    client = client_factory(handler)
    rows = await client.get_demand_history(limit=5000)

    assert rows == []


async def test_404_not_found_body_has_no_message_key(client_factory) -> None:
    """The 404 body is ``{"detail":{"code":"NOT_FOUND"}}`` — no message."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": {"code": "NOT_FOUND"}})

    client = client_factory(handler)

    from app.simulator_errors import EntityNotFoundError

    with pytest.raises(EntityNotFoundError) as excinfo:
        await client.get_depot("depot-nope")

    assert excinfo.value.code == "NOT_FOUND"
    assert excinfo.value.message == ""


def test_instance_model_rejects_an_unknown_status() -> None:
    with pytest.raises(ValidationError):
        Instance.model_validate(
            {
                "id": 1,
                "scenario_id": "baseline",
                "scenario_version": "1.0",
                "seed": 12345,
                "sim_time": "2026-01-01T00:00:00",
                "tick": 0,
                "tick_minutes": 15,
                "status": "HALTED",
            }
        )


def test_health_accepts_uninitialized_simulation_status() -> None:
    """The simulator can report ``UNINITIALIZED`` before the scenario loads."""
    from app.simulator_models import HealthResponse

    health = HealthResponse.model_validate(
        {
            "status": "ok",
            "database": "ok",
            "simulation": {"status": "UNINITIALIZED", "tick": None},
        }
    )

    assert health.simulation.status == "UNINITIALIZED"
    assert health.simulation.tick is None


def test_allocation_model_accepts_the_observed_lifecycle() -> None:
    allocation = Allocation.model_validate(
        {
            "id": 1,
            "idempotency_key": "day1-001",
            "source_depot_id": "depot-gazipur",
            "destination_station_id": "station-mirpur",
            "route_id": "route-gazipur-mirpur",
            "fuel_type": "DIESEL",
            "quantity": 3000,
            "created_tick": 0,
            "departure_tick": None,
            "expected_arrival_tick": None,
            "actual_arrival_tick": None,
            "status": "PENDING",
            "failure_reason": None,
        }
    )

    assert allocation.status.value == "PENDING"


def test_admin_event_accepts_the_observed_shape() -> None:
    event = AdminEvent.model_validate(
        {
            "id": 1,
            "type": "demand_spike",
            "status": "SCHEDULED",
            "start_tick": 0,
            "end_tick": 4,
            "parameters": {"region_ids": ["region-dhaka"], "multiplier": 1.8},
        }
    )

    assert event.type.value == "demand_spike"


def test_admin_action_response_is_permissive() -> None:
    """The guide does not pin the admin body down; accept extras."""
    response = AdminActionResponse.model_validate(
        {"status": "RUNNING", "tick": 2, "unexpected": "field"}
    )

    assert response.status == "RUNNING"


def test_audit_log_entry_model_is_strict_about_unknown_columns() -> None:
    with pytest.raises(ValidationError):
        AuditLogEntry.model_validate(
            {
                "id": 1,
                "wall_time": "2026-11-19T15:48:33.803958+00:00",
                "action": "simulation.tick",
                "result": "OK",
                "metadata_json": {},
                "surprise_column": "boom",
            }
        )
