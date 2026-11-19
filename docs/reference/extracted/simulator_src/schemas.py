from typing import Literal, Any
from pydantic import BaseModel, Field

FuelType = Literal["DIESEL", "PETROL", "OCTANE"]

class AllocationCreate(BaseModel):
    idempotency_key: str = Field(min_length=1, max_length=150)
    source_depot_id: str
    destination_station_id: str
    route_id: str
    fuel_type: FuelType
    quantity: float = Field(gt=0)

class EventCreate(BaseModel):
    type: Literal["demand_spike", "shipment_delay", "route_disruption", "station_outage", "depot_constraint", "supply_shortfall"]
    start_tick: int = Field(ge=0)
    duration_ticks: int = Field(gt=0)
    parameters: dict[str, Any] = Field(default_factory=dict)

class FaultCreate(BaseModel):
    type: Literal["latency", "unavailable", "error_rate", "stale_data", "stream_disconnect"]
    duration_seconds: int = Field(gt=0, le=3600)
    parameters: dict[str, Any] = Field(default_factory=dict)
