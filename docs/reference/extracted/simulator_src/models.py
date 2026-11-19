from datetime import datetime
from sqlalchemy import String, Integer, Float, DateTime, Boolean, JSON, ForeignKey, UniqueConstraint, Text
from sqlalchemy.orm import Mapped, mapped_column
from .db import Base

class SimulationInstance(Base):
    __tablename__ = "simulation_instances"
    id: Mapped[int] = mapped_column(primary_key=True, default=1)
    scenario_id: Mapped[str] = mapped_column(String(100))
    scenario_version: Mapped[str] = mapped_column(String(50), default="1.0")
    seed: Mapped[int] = mapped_column(Integer)
    sim_time: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    tick: Mapped[int] = mapped_column(Integer, default=0)
    tick_minutes: Mapped[int] = mapped_column(Integer, default=15)
    status: Mapped[str] = mapped_column(String(20), default="PAUSED")

class Region(Base):
    __tablename__ = "regions"
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    name: Mapped[str] = mapped_column(String(150))
    demand_factor: Mapped[float] = mapped_column(Float, default=1.0)

class Depot(Base):
    __tablename__ = "depots"
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    name: Mapped[str] = mapped_column(String(150))
    region_id: Mapped[str] = mapped_column(ForeignKey("regions.id"))
    status: Mapped[str] = mapped_column(String(30), default="OPEN")
    dispatch_capacity_per_tick: Mapped[float] = mapped_column(Float, default=10000)
    capacity: Mapped[dict] = mapped_column(JSON)
    inventory: Mapped[dict] = mapped_column(JSON)

class Station(Base):
    __tablename__ = "stations"
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    name: Mapped[str] = mapped_column(String(150))
    region_id: Mapped[str] = mapped_column(ForeignKey("regions.id"))
    status: Mapped[str] = mapped_column(String(30), default="OPEN")
    demand_profile: Mapped[str] = mapped_column(String(80))
    demand_multiplier: Mapped[float] = mapped_column(Float, default=1.0)
    capacity: Mapped[dict] = mapped_column(JSON)
    inventory: Mapped[dict] = mapped_column(JSON)

class Route(Base):
    __tablename__ = "routes"
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    source_depot_id: Mapped[str] = mapped_column(ForeignKey("depots.id"))
    destination_station_id: Mapped[str] = mapped_column(ForeignKey("stations.id"))
    transit_ticks: Mapped[int] = mapped_column(Integer)
    max_shipment: Mapped[float] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String(30), default="AVAILABLE")

class SupplyArrival(Base):
    __tablename__ = "supply_arrivals"
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    depot_id: Mapped[str] = mapped_column(ForeignKey("depots.id"))
    fuel_type: Mapped[str] = mapped_column(String(20))
    quantity: Mapped[float] = mapped_column(Float)
    planned_tick: Mapped[int] = mapped_column(Integer)
    actual_tick: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(30), default="SCHEDULED")

class DemandObservation(Base):
    __tablename__ = "demand_observations"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    station_id: Mapped[str] = mapped_column(ForeignKey("stations.id"))
    fuel_type: Mapped[str] = mapped_column(String(20))
    tick: Mapped[int] = mapped_column(Integer)
    sim_time: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    demand_liters: Mapped[float] = mapped_column(Float)
    served_liters: Mapped[float] = mapped_column(Float)
    unmet_liters: Mapped[float] = mapped_column(Float)

class Allocation(Base):
    __tablename__ = "allocations"
    __table_args__ = (UniqueConstraint("idempotency_key", name="uq_allocation_idempotency"),)
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    idempotency_key: Mapped[str] = mapped_column(String(150))
    source_depot_id: Mapped[str] = mapped_column(ForeignKey("depots.id"))
    destination_station_id: Mapped[str] = mapped_column(ForeignKey("stations.id"))
    route_id: Mapped[str] = mapped_column(ForeignKey("routes.id"))
    fuel_type: Mapped[str] = mapped_column(String(20))
    quantity: Mapped[float] = mapped_column(Float)
    created_tick: Mapped[int] = mapped_column(Integer)
    departure_tick: Mapped[int | None] = mapped_column(Integer, nullable=True)
    expected_arrival_tick: Mapped[int | None] = mapped_column(Integer, nullable=True)
    actual_arrival_tick: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(30), default="PENDING")
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

class DomainEvent(Base):
    __tablename__ = "events"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    type: Mapped[str] = mapped_column(String(50))
    start_tick: Mapped[int] = mapped_column(Integer)
    end_tick: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(30), default="SCHEDULED")
    parameters: Mapped[dict] = mapped_column(JSON, default=dict)

class Fault(Base):
    __tablename__ = "faults"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    type: Mapped[str] = mapped_column(String(50))
    start_wall_time: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    end_wall_time: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    parameters: Mapped[dict] = mapped_column(JSON, default=dict)

class AuditLog(Base):
    __tablename__ = "audit_logs"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    wall_time: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    sim_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    tick: Mapped[int | None] = mapped_column(Integer, nullable=True)
    action: Mapped[str] = mapped_column(String(100))
    entity_type: Mapped[str | None] = mapped_column(String(80), nullable=True)
    entity_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    result: Mapped[str] = mapped_column(String(30), default="OK")
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)
