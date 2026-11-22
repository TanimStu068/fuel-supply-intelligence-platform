"""SQLAlchemy table definitions owned by the participant backend.

Day 2 grew this from two tables to a full schema:

``state_snapshots``       one row per sync cycle (the instance + metrics row)
``inventory_snapshots``   station and depot inventory per tick
``demand_observations``   the demand time series, de-duplicated
``allocations``           a mirror of the simulator's allocation ledger
``domain_events``         a mirror of the simulator's domain events
``alerts``                platform-generated alerts (shortage, outage, …)
``decision_records``      the decision audit trail (empty until Day 5+)
``system_health``         the latest health of every component we depend on
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Declarative base."""


class StateSnapshot(Base):
    """One row per sync cycle pulled from the simulator.

    Keeping a history (rather than a single mutable row) gives us a replay
    trail for later forecasting work and proves the Postgres dependency is
    genuinely exercised by the read path.
    """

    __tablename__ = "state_snapshots"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    captured_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    sim_tick: Mapped[int] = mapped_column(Integer, index=True)
    sim_time: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    sim_status: Mapped[str] = mapped_column(String(32))
    scenario_id: Mapped[str] = mapped_column(String(64))
    service_level: Mapped[float] = mapped_column(Float)
    unmet_demand_liters: Mapped[float] = mapped_column(Float)
    served_demand_liters: Mapped[float] = mapped_column(Float)
    allocation_liters: Mapped[float] = mapped_column(Float)
    allocation_failures: Mapped[int] = mapped_column(Integer)
    stale: Mapped[bool] = mapped_column(default=False)
    payload: Mapped[str] = mapped_column(Text)


class InventorySnapshot(Base):
    """Inventory of one station or depot at one simulation tick.

    ``entity_type`` is ``"station"`` or ``"depot"``. The unique constraint on
    ``(entity_type, entity_id, sim_tick, fuel_type)`` makes the ingest
    idempotent: replaying the same tick updates nothing.
    """

    __tablename__ = "inventory_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "entity_type",
            "entity_id",
            "sim_tick",
            "fuel_type",
            name="uq_inventory_snapshots_entity_tick_fuel",
        ),
        Index("ix_inventory_snapshots_entity", "entity_type", "entity_id", "sim_tick"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    captured_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    entity_type: Mapped[str] = mapped_column(String(16), index=True)
    entity_id: Mapped[str] = mapped_column(String(64), index=True)
    sim_tick: Mapped[int] = mapped_column(Integer, index=True)
    sim_time: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    fuel_type: Mapped[str] = mapped_column(String(16))
    quantity: Mapped[float] = mapped_column(Float)
    capacity: Mapped[float | None] = mapped_column(Float, nullable=True)
    status: Mapped[str | None] = mapped_column(String(32), nullable=True)


class DemandObservationRow(Base):
    """One demand observation: ``(station, fuel)`` at one tick.

    De-duplicated by ``(station_id, fuel_type, tick)`` — the simulator
    guarantees one row per that key per tick, and so do we.
    """

    __tablename__ = "demand_observations"
    __table_args__ = (
        UniqueConstraint(
            "station_id",
            "fuel_type",
            "tick",
            name="uq_demand_observations_station_fuel_tick",
        ),
        Index("ix_demand_observations_station_tick", "station_id", "tick"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    captured_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    simulator_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    station_id: Mapped[str] = mapped_column(String(64), index=True)
    fuel_type: Mapped[str] = mapped_column(String(16))
    tick: Mapped[int] = mapped_column(Integer, index=True)
    sim_time: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    demand_liters: Mapped[float] = mapped_column(Float)
    served_liters: Mapped[float] = mapped_column(Float)
    unmet_liters: Mapped[float] = mapped_column(Float)


class AllocationRow(Base):
    """Mirror of the simulator's allocation ledger.

    The simulator is the source of truth; this table is our read-optimised
    copy so the dashboard and the decision engine never block on it.
    """

    __tablename__ = "allocations"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    captured_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    simulator_id: Mapped[int] = mapped_column(Integer, unique=True, index=True)
    idempotency_key: Mapped[str] = mapped_column(String(150), index=True)
    source_depot_id: Mapped[str] = mapped_column(String(64), index=True)
    destination_station_id: Mapped[str] = mapped_column(String(64), index=True)
    route_id: Mapped[str] = mapped_column(String(64), index=True)
    fuel_type: Mapped[str] = mapped_column(String(16))
    quantity: Mapped[float] = mapped_column(Float)
    created_tick: Mapped[int] = mapped_column(Integer, index=True)
    departure_tick: Mapped[int | None] = mapped_column(Integer, nullable=True)
    expected_arrival_tick: Mapped[int | None] = mapped_column(Integer, nullable=True)
    actual_arrival_tick: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(32), index=True)
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)


class DomainEventRow(Base):
    """Mirror of the simulator's domain events (demand spikes, outages, …)."""

    __tablename__ = "domain_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    captured_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    simulator_id: Mapped[int] = mapped_column(Integer, unique=True, index=True)
    type: Mapped[str] = mapped_column(String(64), index=True)
    start_tick: Mapped[int] = mapped_column(Integer)
    end_tick: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(32), index=True)
    parameters: Mapped[str] = mapped_column(Text, default="{}")


class Alert(Base):
    """A platform-generated alert (shortage risk, outage, degraded sync).

    Alerts are deduplicated on ``(kind, entity_id, sim_tick)`` so a poller
    that runs every few seconds does not write the same alert repeatedly.
    """

    __tablename__ = "alerts"
    __table_args__ = (
        UniqueConstraint("kind", "entity_id", "sim_tick", name="uq_alerts_kind_entity_tick"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    kind: Mapped[str] = mapped_column(String(64), index=True)
    severity: Mapped[str] = mapped_column(String(16), index=True)
    entity_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    entity_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    sim_tick: Mapped[int] = mapped_column(Integer, index=True)
    message: Mapped[str] = mapped_column(Text)
    resolved: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    payload: Mapped[str | None] = mapped_column(Text, nullable=True)


class Recommendation(Base):
    """One proposed allocation, and everything that happened to it.

    This is the decision history and the audit trail in one table. It
    records *who* decided, *when*, the exact inputs the recommendation was
    made from (as a JSON snapshot), the simulator's response, and the
    outcome once the allocation's status is known.

    ``inputs_snapshot`` is the important column: it holds the signals and
    the constraint checks exactly as the operator saw them, so a decision
    can be explained months later without re-running the model.
    """

    __tablename__ = "recommendations"
    __table_args__ = (
        UniqueConstraint("recommendation_id", name="uq_recommendations_id"),
        Index("ix_recommendations_status", "status"),
        Index("ix_recommendations_station", "station_id", "sim_tick"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    #: The engine's own identifier for the recommendation.
    recommendation_id: Mapped[str] = mapped_column(String(150), index=True)
    station_id: Mapped[str] = mapped_column(String(64), index=True)
    fuel_type: Mapped[str] = mapped_column(String(16))
    source_depot_id: Mapped[str] = mapped_column(String(64))
    route_id: Mapped[str] = mapped_column(String(64))
    quantity: Mapped[float] = mapped_column(Float)
    #: PROPOSED / APPROVED / SUBMITTED / ARRIVED / FAILED / REJECTED.
    status: Mapped[str] = mapped_column(String(32), index=True)
    risk_level: Mapped[str] = mapped_column(String(16))
    hours_to_stockout: Mapped[float | None] = mapped_column(Float, nullable=True)
    confidence: Mapped[float] = mapped_column(Float)
    needs_human_review: Mapped[bool] = mapped_column(Boolean, default=True)
    #: True when the fallback policy produced this recommendation.
    used_fallback_policy: Mapped[bool] = mapped_column(Boolean, default=False)
    idempotency_key: Mapped[str] = mapped_column(String(150))
    rationale: Mapped[str] = mapped_column(Text)
    #: The tick the recommendation was made against.
    sim_tick: Mapped[int] = mapped_column(Integer, index=True)
    #: Who decided, and when.
    decided_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: The simulator's allocation id, once it has one.
    allocation_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    #: The simulator's error code, when it rejected the allocation.
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: The signals, constraints, projection and alternatives, as JSON.
    inputs_snapshot: Mapped[str] = mapped_column(Text)
    #: The simulator's response, as JSON.
    simulator_response: Mapped[str | None] = mapped_column(Text, nullable=True)


class DecisionRecord(Base):
    """Audit trail of decisions the platform takes (filled in later days)."""

    __tablename__ = "decision_records"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    sim_tick: Mapped[int] = mapped_column(Integer, index=True)
    kind: Mapped[str] = mapped_column(String(64), index=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(150), index=True)
    status: Mapped[str] = mapped_column(String(32))
    rationale: Mapped[str | None] = mapped_column(Text)
    payload: Mapped[str | None] = mapped_column(Text)


class SystemHealth(Base):
    """The latest health of every component the platform depends on.

    One row per component. Written on every sync cycle so an operator can
    see, after the fact, when the simulator started failing.
    """

    __tablename__ = "system_health"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    checked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    component: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    status: Mapped[str] = mapped_column(String(32), index=True)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    correlation_id: Mapped[str | None] = mapped_column(String(64), nullable=True)


class DemandForecast(Base):
    """One forecast for one (station, fuel) pair at one evaluation tick.

    The forecast's points and its confidence interval are stored as JSON so
    the exact numbers that were shown to an operator can be reproduced
    later. ``model`` records which predictor produced it, and
    ``needs_human_review`` records whether it was flagged for a human.
    """

    __tablename__ = "demand_forecasts"
    __table_args__ = (
        UniqueConstraint(
            "station_id",
            "fuel_type",
            "sim_tick",
            name="uq_demand_forecasts_station_fuel_tick",
        ),
        Index("ix_demand_forecasts_model", "model"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    station_id: Mapped[str] = mapped_column(String(64), index=True)
    fuel_type: Mapped[str] = mapped_column(String(16))
    sim_tick: Mapped[int] = mapped_column(Integer, index=True)
    sim_time: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    #: Which predictor produced this forecast: ``baseline`` or ``learned``.
    model: Mapped[str] = mapped_column(String(16))
    #: 0..1.
    confidence: Mapped[float] = mapped_column(Float)
    needs_human_review: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    fallback_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    horizon_ticks: Mapped[int] = mapped_column(Integer)
    total_demand_liters: Mapped[float] = mapped_column(Float)
    lower_total_liters: Mapped[float] = mapped_column(Float)
    upper_total_liters: Mapped[float] = mapped_column(Float)
    #: The forecast points and the interval, as JSON.
    payload: Mapped[str] = mapped_column(Text)


class RiskAssessmentRow(Base):
    """One risk assessment for one (station, fuel) pair at one tick.

    The signals that drove the score are stored alongside it, so a risk can
    be explained after the fact without re-running the model.
    """

    __tablename__ = "risk_assessments"
    __table_args__ = (
        UniqueConstraint(
            "station_id",
            "fuel_type",
            "sim_tick",
            name="uq_risk_assessments_station_fuel_tick",
        ),
        Index("ix_risk_assessments_level", "risk_level"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    station_id: Mapped[str] = mapped_column(String(64), index=True)
    fuel_type: Mapped[str] = mapped_column(String(16))
    sim_tick: Mapped[int] = mapped_column(Integer, index=True)
    risk_level: Mapped[str] = mapped_column(String(16), index=True)
    risk_score: Mapped[float] = mapped_column(Float)
    hours_to_stockout: Mapped[float | None] = mapped_column(Float, nullable=True)
    stockout_probability: Mapped[float] = mapped_column(Float)
    needs_human_review: Mapped[bool] = mapped_column(Boolean, default=False)
    recommendation: Mapped[str] = mapped_column(Text)
    #: The signals that drove the score, as JSON.
    payload: Mapped[str] = mapped_column(Text)


class PredictionEvaluation(Base):
    """A scored prediction, compared against what actually happened.

    This is how the prediction error is measured: when a forecast for tick
    T is made, and the demand for tick T is observed, the difference is
    recorded here.
    """

    __tablename__ = "prediction_evaluations"
    __table_args__ = (
        UniqueConstraint(
            "station_id",
            "fuel_type",
            "target_tick",
            name="uq_prediction_evaluations_station_fuel_tick",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    station_id: Mapped[str] = mapped_column(String(64), index=True)
    fuel_type: Mapped[str] = mapped_column(String(16))
    target_tick: Mapped[int] = mapped_column(Integer, index=True)
    model: Mapped[str] = mapped_column(String(16), index=True)
    predicted_demand: Mapped[float] = mapped_column(Float)
    actual_demand: Mapped[float] = mapped_column(Float)
    absolute_error: Mapped[float] = mapped_column(Float)
    lower_bound: Mapped[float] = mapped_column(Float)
    upper_bound: Mapped[float] = mapped_column(Float)
    #: True when the actual demand landed inside the confidence interval.
    within_interval: Mapped[bool] = mapped_column(Boolean, default=False)
