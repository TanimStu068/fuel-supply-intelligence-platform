"""Strict Pydantic models for every BUP Fuel Supply Simulator response.

Source of truth: docs/reference/BUP_Fuel_Supply_Simulator_Integration_Guide_Final.pdf

Design rules
------------
* Every documented response body has a model. Unknown fields are rejected
  (``extra="forbid"``) so schema drift surfaces as a validation error instead of
  silently poisoning downstream state.
* Enumerations are modelled as ``Literal`` sets, so an unexpected status value
  is a hard failure rather than a free-text string.
* Fuel maps are keyed by :class:`FuelType`; unknown fuel keys fail validation.
* The simulator serialises naive ISO-8601 timestamps (``2026-01-01T00:00:00``)
  even though the guide documents them with a ``+00:00`` offset. Timestamps
  are therefore normalised to UTC via :data:`SimDateTime`.
* The documented ``/admin`` acknowledgement bodies are not specified in the
  guide, so :class:`AdminActionResponse` keeps ``extra="allow"`` and is
  documented as a deliberate looser contract.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, field_validator


def _assume_utc(value: datetime) -> datetime:
    """Treat a timezone-less timestamp as UTC.

    The simulator drops the offset on every ``sim_time`` it serialises. Anchoring
    naive values to UTC keeps downstream comparisons and arithmetic sound.
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


#: A datetime normalised to UTC when the payload omits an offset.
SimDateTime = Annotated[datetime, AfterValidator(_assume_utc)]


class StrictModel(BaseModel):
    """Base model: reject unknown fields, no silent coercion of missing data."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class FuelType(StrEnum):
    """The three fuel categories modelled by the simulator."""

    DIESEL = "DIESEL"
    PETROL = "PETROL"
    OCTANE = "OCTANE"


FuelMap = dict[FuelType, float]


class InstanceStatus(StrEnum):
    PAUSED = "PAUSED"
    RUNNING = "RUNNING"


class DepotStatus(StrEnum):
    OPEN = "OPEN"
    CONSTRAINED = "CONSTRAINED"


class StationStatus(StrEnum):
    OPEN = "OPEN"
    OUTAGE = "OUTAGE"


class RouteStatus(StrEnum):
    AVAILABLE = "AVAILABLE"
    DISRUPTED = "DISRUPTED"


class SupplyArrivalStatus(StrEnum):
    SCHEDULED = "SCHEDULED"
    DELAYED = "DELAYED"
    ARRIVED = "ARRIVED"


class EventStatus(StrEnum):
    SCHEDULED = "SCHEDULED"
    ACTIVE = "ACTIVE"
    RESOLVED = "RESOLVED"


class AllocationStatus(StrEnum):
    PENDING = "PENDING"
    IN_TRANSIT = "IN_TRANSIT"
    ARRIVED = "ARRIVED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class SimulatorEventType(StrEnum):
    """The six injectable crisis event types (guide §7.8)."""

    DEMAND_SPIKE = "demand_spike"
    ROUTE_DISRUPTION = "route_disruption"
    STATION_OUTAGE = "station_outage"
    DEPOT_CONSTRAINT = "depot_constraint"
    SHIPMENT_DELAY = "shipment_delay"
    SUPPLY_SHORTFALL = "supply_shortfall"


class FaultType(StrEnum):
    """The five injectable fault types (guide §7.10)."""

    LATENCY = "latency"
    UNAVAILABLE = "unavailable"
    ERROR_RATE = "error_rate"
    STALE_DATA = "stale_data"
    STREAM_DISCONNECT = "stream_disconnect"


class HealthResponse(StrictModel):
    """GET /v1/health — bypasses fault injection, used as liveness probe."""

    status: Literal["ok", "degraded", "error"]
    database: str
    simulation: SimulationStatus


class SimulationStatus(StrictModel):
    #: The simulator can report ``UNINITIALIZED`` before the scenario is loaded.
    status: InstanceStatus | Literal["UNINITIALIZED"]
    tick: int | None = Field(default=None, ge=0)


class Instance(StrictModel):
    """GET /v1/instance — the single simulation instance."""

    id: int
    scenario_id: str
    scenario_version: str
    seed: int
    sim_time: SimDateTime
    tick: int = Field(ge=0)
    tick_minutes: int = Field(gt=0)
    status: InstanceStatus


class Region(StrictModel):
    """GET /v1/regions."""

    id: str
    name: str
    demand_factor: float = Field(gt=0)


class Depot(StrictModel):
    """GET /v1/depots and GET /v1/depots/{entity_id}."""

    id: str
    name: str
    region_id: str
    status: DepotStatus
    dispatch_capacity_per_tick: float = Field(ge=0)
    capacity: FuelMap
    inventory: FuelMap

    @field_validator("capacity", "inventory")
    @classmethod
    def _non_negative(cls, value: FuelMap) -> FuelMap:
        for fuel, amount in value.items():
            if amount < 0:
                raise ValueError(f"negative {fuel} amount: {amount}")
        return value


class Station(StrictModel):
    """GET /v1/stations and GET /v1/stations/{entity_id}."""

    id: str
    name: str
    region_id: str
    status: StationStatus
    demand_profile: str
    demand_multiplier: float = Field(gt=0)
    capacity: FuelMap
    inventory: FuelMap

    @field_validator("capacity", "inventory")
    @classmethod
    def _non_negative(cls, value: FuelMap) -> FuelMap:
        for fuel, amount in value.items():
            if amount < 0:
                raise ValueError(f"negative {fuel} amount: {amount}")
        return value


class Route(StrictModel):
    """GET /v1/routes."""

    id: str
    source_depot_id: str
    destination_station_id: str
    transit_ticks: int = Field(ge=0)
    max_shipment: float = Field(gt=0)
    status: RouteStatus


class SupplyArrival(StrictModel):
    """GET /v1/supply-arrivals — sorted by planned_tick ascending."""

    id: str
    depot_id: str
    fuel_type: FuelType
    quantity: float = Field(ge=0)
    planned_tick: int = Field(ge=0)
    actual_tick: int | None = None
    status: SupplyArrivalStatus


class SimulatorEvent(StrictModel):
    """GET /v1/events and POST /admin/events."""

    id: int
    type: SimulatorEventType
    start_tick: int = Field(ge=0)
    end_tick: int = Field(ge=0)
    status: EventStatus
    parameters: dict[str, Any] = Field(default_factory=dict)


class Allocation(StrictModel):
    """GET /v1/allocations, POST /v1/allocations, POST /v1/allocations/{id}/cancel."""

    id: int
    idempotency_key: str = Field(min_length=1, max_length=150)
    source_depot_id: str
    destination_station_id: str
    route_id: str
    fuel_type: FuelType
    quantity: float = Field(gt=0)
    created_tick: int = Field(ge=0)
    departure_tick: int | None = None
    expected_arrival_tick: int | None = None
    actual_arrival_tick: int | None = None
    status: AllocationStatus
    failure_reason: str | None = None


class DemandObservation(StrictModel):
    """GET /v1/demand-history — one row per (station, fuel) per tick."""

    id: int
    station_id: str
    fuel_type: FuelType
    tick: int = Field(ge=0)
    sim_time: SimDateTime
    demand_liters: float = Field(ge=0)
    served_liters: float = Field(ge=0)
    unmet_liters: float = Field(ge=0)


class Metrics(StrictModel):
    """GET /v1/metrics — aggregated ground truth since simulation start."""

    served_demand_liters: float = Field(ge=0)
    unmet_demand_liters: float = Field(ge=0)
    service_level: float = Field(ge=0, le=1)
    allocation_liters: float = Field(ge=0)
    allocation_failures: int = Field(ge=0)


class AuditLogEntry(StrictModel):
    """GET /admin/audit — ground-truth audit log, id-desc.

    ``sim_time``, ``tick``, ``entity_type`` and ``entity_id`` are nullable in
    the simulator schema: instance-level actions such as ``admin.run`` and
    ``fault.clear_all`` are audited without an entity reference.
    """

    id: int
    wall_time: SimDateTime
    sim_time: SimDateTime | None = None
    tick: int | None = Field(default=None, ge=0)
    action: str
    entity_type: str | None = None
    entity_id: str | None = None
    result: str
    metadata_json: dict[str, Any] = Field(default_factory=dict)


class FaultsClearedResponse(StrictModel):
    """POST /admin/faults/clear — idempotent."""

    status: Literal["cleared"]


class AdminEvent(BaseModel):
    """An event row as returned by the admin listing endpoints.

    The guide documents the domain event shape (``GET /v1/events``) but not the
    exact admin row shape, so this model requires the core fields and tolerates
    extra bookkeeping columns.
    """

    model_config = ConfigDict(extra="allow")

    id: int
    type: SimulatorEventType
    status: EventStatus
    start_tick: int = Field(ge=0)
    end_tick: int = Field(ge=0)
    parameters: dict[str, Any] = Field(default_factory=dict)


class AdminFault(BaseModel):
    """A fault row as returned by ``GET /admin/faults`` and ``POST /admin/faults``.

    The simulator stores a fault as a wall-clock window rather than a duration,
    so ``duration_seconds`` is **not** echoed back; it is derived from
    ``start_wall_time`` / ``end_wall_time`` when both are present.
    """

    model_config = ConfigDict(extra="allow")

    id: int
    type: FaultType
    active: bool = False
    parameters: dict[str, Any] = Field(default_factory=dict)
    start_wall_time: SimDateTime | None = None
    end_wall_time: SimDateTime | None = None

    @property
    def duration_seconds(self) -> int | None:
        """Derive the requested duration from the stored wall-clock window."""
        if self.start_wall_time is None or self.end_wall_time is None:
            return None
        return int((self.end_wall_time - self.start_wall_time).total_seconds())


class AdminStepResponse(StrictModel):
    """POST /admin/step — advances exactly one tick."""

    tick: int = Field(ge=0)
    sim_time: SimDateTime


class AdminActionResponse(BaseModel):
    """POST /admin/{run,pause,toggle,reset}.

    The simulator answers these with the serialized simulation instance, so
    ``status`` carries the instance status (``RUNNING`` / ``PAUSED``). The
    guide does not pin the body down, hence the permissive contract.
    """

    model_config = ConfigDict(extra="allow")

    status: str | None = None
    message: str | None = None
    tick: int | None = None
    sim_time: SimDateTime | None = None


# --- Request bodies (client -> simulator) --------------------------------


class AllocationCreate(StrictModel):
    """POST /v1/allocations request body (guide §5.1)."""

    idempotency_key: str = Field(min_length=1, max_length=150)
    source_depot_id: str
    destination_station_id: str
    route_id: str
    fuel_type: FuelType
    quantity: float = Field(gt=0)


class EventCreate(StrictModel):
    """POST /admin/events request body (guide §7.7)."""

    type: SimulatorEventType
    start_tick: int = Field(ge=0)
    duration_ticks: int = Field(gt=0)
    parameters: dict[str, Any] = Field(default_factory=dict)


class FaultCreate(StrictModel):
    """POST /admin/faults request body (guide §7.9)."""

    type: FaultType
    duration_seconds: int = Field(gt=0, le=3600)
    parameters: dict[str, Any] = Field(default_factory=dict)


# --- Error envelopes ------------------------------------------------------


class ErrorDetail(StrictModel):
    """The inner payload of both ``{"error": {...}}`` and ``{"detail": {...}}``."""

    code: str
    message: str = ""


class ErrorEnvelope(StrictModel):
    """``{"error": {"code": ..., "message": ...}}`` — used by injected faults."""

    error: ErrorDetail


class DetailEnvelope(StrictModel):
    """``{"detail": {"code": ..., "message": ...}}`` — used by domain errors."""

    detail: ErrorDetail


HealthResponse.model_rebuild()
