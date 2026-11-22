"""Persistence layer: everything that writes to Postgres.

Each function is idempotent where it can be, so a replayed sync cycle does not
duplicate rows. The uniqueness constraints on the tables do the heavy lifting;
we use ``ON CONFLICT DO UPDATE`` so a replayed tick refreshes the values
instead of raising.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from .models import (
    Alert,
    AllocationRow,
    DecisionRecord,
    DemandForecast,
    DemandObservationRow,
    DomainEventRow,
    InventorySnapshot,
    PredictionEvaluation,
    RiskAssessmentRow,
    StateSnapshot,
    SystemHealth,
)
from .simulator_models import (
    Allocation,
    Depot,
    Metrics,
    SimulatorEvent,
    Station,
)

logger = logging.getLogger(__name__)


# -- state snapshots --------------------------------------------------------


async def write_state_snapshot(
    session: AsyncSession,
    *,
    sim_tick: int,
    sim_time: datetime,
    sim_status: str,
    scenario_id: str,
    metrics: Metrics,
    stale: bool,
    payload: dict[str, Any] | None = None,
) -> None:
    """Append one row describing a sync cycle."""
    session.add(
        StateSnapshot(
            sim_tick=sim_tick,
            sim_time=sim_time,
            sim_status=sim_status,
            scenario_id=scenario_id,
            service_level=metrics.service_level,
            unmet_demand_liters=metrics.unmet_demand_liters,
            served_demand_liters=metrics.served_demand_liters,
            allocation_liters=metrics.allocation_liters,
            allocation_failures=metrics.allocation_failures,
            stale=stale,
            payload=json.dumps(payload or {}, default=str),
        )
    )


# -- inventory --------------------------------------------------------------


async def write_inventory_snapshots(
    session: AsyncSession,
    *,
    sim_tick: int,
    sim_time: datetime,
    stations: Iterable[Station],
    depots: Iterable[Depot],
) -> int:
    """Persist station and depot inventory for one tick.

    Returns the number of rows written (new or updated).
    """
    rows: list[dict[str, Any]] = []

    for station in stations:
        for fuel, quantity in station.inventory.items():
            rows.append(
                {
                    "entity_type": "station",
                    "entity_id": station.id,
                    "sim_tick": sim_tick,
                    "sim_time": sim_time,
                    "fuel_type": fuel.value,
                    "quantity": quantity,
                    "capacity": station.capacity.get(fuel),
                    "status": station.status.value,
                }
            )

    for depot in depots:
        for fuel, quantity in depot.inventory.items():
            rows.append(
                {
                    "entity_type": "depot",
                    "entity_id": depot.id,
                    "sim_tick": sim_tick,
                    "sim_time": sim_time,
                    "fuel_type": fuel.value,
                    "quantity": quantity,
                    "capacity": depot.capacity.get(fuel),
                    "status": depot.status.value,
                }
            )

    if not rows:
        return 0

    statement = pg_insert(InventorySnapshot).values(rows)
    statement = statement.on_conflict_do_update(
        index_elements=[
            InventorySnapshot.entity_type,
            InventorySnapshot.entity_id,
            InventorySnapshot.sim_tick,
            InventorySnapshot.fuel_type,
        ],
        set_={
            "quantity": statement.excluded.quantity,
            "capacity": statement.excluded.capacity,
            "status": statement.excluded.status,
            "sim_time": statement.excluded.sim_time,
        },
    )
    await session.execute(statement)
    return len(rows)


# -- demand history ---------------------------------------------------------


async def write_demand_observations(
    session: AsyncSession,
    observations: Iterable[Any],
) -> tuple[int, int]:
    """Upsert demand observations, de-duplicated by (station, fuel, tick).

    Returns ``(inserted, duplicates)``.
    """
    rows = [
        {
            "simulator_id": observation.id,
            "station_id": observation.station_id,
            "fuel_type": observation.fuel_type.value,
            "tick": observation.tick,
            "sim_time": observation.sim_time,
            "demand_liters": observation.demand_liters,
            "served_liters": observation.served_liters,
            "unmet_liters": observation.unmet_liters,
        }
        for observation in observations
    ]

    if not rows:
        return (0, 0)

    statement = pg_insert(DemandObservationRow).values(rows)
    statement = statement.on_conflict_do_update(
        constraint="uq_demand_observations_station_fuel_tick",
        set_={
            "demand_liters": statement.excluded.demand_liters,
            "served_liters": statement.excluded.served_liters,
            "unmet_liters": statement.excluded.unmet_liters,
            "sim_time": statement.excluded.sim_time,
        },
    )
    result = await session.execute(statement)
    # Postgres reports the number of rows the statement touched; the rows
    # that already existed were updated rather than inserted.
    touched = result.rowcount or 0
    return (touched, 0)


async def count_demand_observations(session: AsyncSession) -> int:
    """Return the number of stored demand observations."""
    result = await session.execute(
        select(DemandObservationRow.id).order_by(DemandObservationRow.id.desc()).limit(1)
    )
    return len(result.all())


async def max_demand_tick(session: AsyncSession) -> int | None:
    """Return the highest tick we already hold a demand observation for."""
    result = await session.execute(
        select(DemandObservationRow.tick).order_by(DemandObservationRow.tick.desc()).limit(1)
    )
    row = result.first()
    return row[0] if row else None


async def truncate_demand_history(session: AsyncSession) -> int:
    """Delete every stored demand observation.

    Used when the simulator is reset. A reset wipes the simulator's world
    and restarts the clock at tick 0, so the stored history would otherwise
    mix two different timelines: the old run's tick 400 and the new run's
    tick 1 would share a tick number, and the forecaster would read them
    as one continuous series.
    """
    result = await session.execute(DemandObservationRow.__table__.delete())
    return result.rowcount or 0


# -- ledger mirrors ---------------------------------------------------------


async def write_allocations(
    session: AsyncSession,
    allocations: Iterable[Allocation],
) -> int:
    """Upsert the allocation ledger mirror."""
    rows = [
        {
            "simulator_id": allocation.id,
            "idempotency_key": allocation.idempotency_key,
            "source_depot_id": allocation.source_depot_id,
            "destination_station_id": allocation.destination_station_id,
            "route_id": allocation.route_id,
            "fuel_type": allocation.fuel_type.value,
            "quantity": allocation.quantity,
            "created_tick": allocation.created_tick,
            "departure_tick": allocation.departure_tick,
            "expected_arrival_tick": allocation.expected_arrival_tick,
            "actual_arrival_tick": allocation.actual_arrival_tick,
            "status": allocation.status.value,
            "failure_reason": allocation.failure_reason,
        }
        for allocation in allocations
    ]

    if not rows:
        return 0

    statement = pg_insert(AllocationRow).values(rows)
    statement = statement.on_conflict_do_update(
        index_elements=[AllocationRow.simulator_id],
        set_={
            "status": statement.excluded.status,
            "departure_tick": statement.excluded.departure_tick,
            "expected_arrival_tick": statement.excluded.expected_arrival_tick,
            "actual_arrival_tick": statement.excluded.actual_arrival_tick,
            "failure_reason": statement.excluded.failure_reason,
            "quantity": statement.excluded.quantity,
        },
    )
    await session.execute(statement)
    return len(rows)


async def write_domain_events(
    session: AsyncSession,
    events: Iterable[SimulatorEvent],
) -> int:
    """Upsert the domain event mirror."""
    rows = [
        {
            "simulator_id": event.id,
            "type": event.type.value,
            "start_tick": event.start_tick,
            "end_tick": event.end_tick,
            "status": event.status.value,
            "parameters": json.dumps(event.parameters, default=str),
        }
        for event in events
    ]

    if not rows:
        return 0

    statement = pg_insert(DomainEventRow).values(rows)
    statement = statement.on_conflict_do_update(
        index_elements=[DomainEventRow.simulator_id],
        set_={
            "status": statement.excluded.status,
            "end_tick": statement.excluded.end_tick,
            "parameters": statement.excluded.parameters,
        },
    )
    await session.execute(statement)
    return len(rows)


# -- forecasts, risk and evaluation ----------------------------------------


async def write_demand_forecast(
    session: AsyncSession,
    *,
    station_id: str,
    fuel_type: str,
    sim_tick: int,
    sim_time: datetime,
    model: str,
    confidence: float,
    needs_human_review: bool,
    fallback_reason: str | None,
    horizon_ticks: int,
    total_demand: float,
    lower_total: float,
    upper_total: float,
    payload: dict[str, Any],
) -> None:
    """Upsert one forecast row."""
    statement = pg_insert(DemandForecast).values(
        station_id=station_id,
        fuel_type=fuel_type,
        sim_tick=sim_tick,
        sim_time=sim_time,
        model=model,
        confidence=confidence,
        needs_human_review=needs_human_review,
        fallback_reason=fallback_reason,
        horizon_ticks=horizon_ticks,
        total_demand_liters=total_demand,
        lower_total_liters=lower_total,
        upper_total_liters=upper_total,
        payload=json.dumps(payload, default=str),
    )
    statement = statement.on_conflict_do_update(
        constraint="uq_demand_forecasts_station_fuel_tick",
        set_={
            "model": statement.excluded.model,
            "confidence": statement.excluded.confidence,
            "needs_human_review": statement.excluded.needs_human_review,
            "fallback_reason": statement.excluded.fallback_reason,
            "total_demand_liters": statement.excluded.total_demand_liters,
            "lower_total_liters": statement.excluded.lower_total_liters,
            "upper_total_liters": statement.excluded.upper_total_liters,
            "payload": statement.excluded.payload,
        },
    )
    await session.execute(statement)


async def write_risk_assessment(
    session: AsyncSession,
    *,
    station_id: str,
    fuel_type: str,
    sim_tick: int,
    risk_level: str,
    risk_score: float,
    hours_to_stockout: float | None,
    stockout_probability: float,
    needs_human_review: bool,
    recommendation: str,
    payload: dict[str, Any],
) -> None:
    """Upsert one risk assessment row."""
    statement = pg_insert(RiskAssessmentRow).values(
        station_id=station_id,
        fuel_type=fuel_type,
        sim_tick=sim_tick,
        risk_level=risk_level,
        risk_score=risk_score,
        hours_to_stockout=hours_to_stockout,
        stockout_probability=stockout_probability,
        needs_human_review=needs_human_review,
        recommendation=recommendation,
        payload=json.dumps(payload, default=str),
    )
    statement = statement.on_conflict_do_update(
        constraint="uq_risk_assessments_station_fuel_tick",
        set_={
            "risk_level": statement.excluded.risk_level,
            "risk_score": statement.excluded.risk_score,
            "hours_to_stockout": statement.excluded.hours_to_stockout,
            "stockout_probability": statement.excluded.stockout_probability,
            "needs_human_review": statement.excluded.needs_human_review,
            "recommendation": statement.excluded.recommendation,
            "payload": statement.excluded.payload,
        },
    )
    await session.execute(statement)


async def write_prediction_evaluation(
    session: AsyncSession,
    *,
    station_id: str,
    fuel_type: str,
    target_tick: int,
    model: str,
    predicted_demand: float,
    actual_demand: float,
    lower_bound: float,
    upper_bound: float,
) -> None:
    """Upsert one scored prediction."""
    statement = pg_insert(PredictionEvaluation).values(
        station_id=station_id,
        fuel_type=fuel_type,
        target_tick=target_tick,
        model=model,
        predicted_demand=predicted_demand,
        actual_demand=actual_demand,
        absolute_error=abs(predicted_demand - actual_demand),
        lower_bound=lower_bound,
        upper_bound=upper_bound,
        within_interval=lower_bound <= actual_demand <= upper_bound,
    )
    statement = statement.on_conflict_do_update(
        constraint="uq_prediction_evaluations_station_fuel_tick",
        set_={
            "model": statement.excluded.model,
            "predicted_demand": statement.excluded.predicted_demand,
            "actual_demand": statement.excluded.actual_demand,
            "absolute_error": statement.excluded.absolute_error,
            "lower_bound": statement.excluded.lower_bound,
            "upper_bound": statement.excluded.upper_bound,
            "within_interval": statement.excluded.within_interval,
        },
    )
    await session.execute(statement)


async def read_demand_history(
    session: AsyncSession,
    *,
    limit: int = 5000,
) -> list[DemandObservationRow]:
    """Read the stored demand history, oldest first.

    The forecaster needs the series in order, and the limit keeps the query
    bounded no matter how large the table has grown.
    """
    result = await session.execute(
        select(DemandObservationRow).order_by(DemandObservationRow.tick.desc()).limit(limit)
    )
    rows = list(result.scalars())
    rows.reverse()
    return rows


# -- alerts -----------------------------------------------------------------


async def write_alert(
    session: AsyncSession,
    *,
    kind: str,
    severity: str,
    entity_type: str | None,
    entity_id: str | None,
    sim_tick: int,
    message: str,
    payload: dict[str, Any] | None = None,
) -> bool:
    """Write an alert, skipping it if the same alert already exists.

    Returns True when a new alert row was created.
    """
    statement = (
        pg_insert(Alert)
        .values(
            kind=kind,
            severity=severity,
            entity_type=entity_type,
            entity_id=entity_id,
            sim_tick=sim_tick,
            message=message,
            resolved=False,
            payload=json.dumps(payload or {}, default=str),
        )
        .on_conflict_do_nothing(constraint="uq_alerts_kind_entity_tick")
    )
    result = await session.execute(statement)
    return bool(result.rowcount)


async def write_decision_record(
    session: AsyncSession,
    *,
    sim_tick: int,
    kind: str,
    status: str,
    rationale: str | None = None,
    idempotency_key: str | None = None,
    payload: dict[str, Any] | None = None,
) -> None:
    """Append a decision audit row (empty until the decision engine lands)."""
    session.add(
        DecisionRecord(
            sim_tick=sim_tick,
            kind=kind,
            idempotency_key=idempotency_key,
            status=status,
            rationale=rationale,
            payload=json.dumps(payload or {}, default=str),
        )
    )


# -- health -----------------------------------------------------------------


async def write_system_health(
    session: AsyncSession,
    *,
    component: str,
    status: str,
    detail: str | None = None,
    correlation_id: str | None = None,
) -> None:
    """Upsert the health row of one component."""
    insert = pg_insert(SystemHealth).values(
        component=component,
        status=status,
        detail=detail,
        correlation_id=correlation_id,
    )
    statement = insert.on_conflict_do_update(
        index_elements=[SystemHealth.component],
        set_={
            "status": insert.excluded.status,
            "detail": insert.excluded.detail,
            "correlation_id": insert.excluded.correlation_id,
            "checked_at": insert.excluded.checked_at,
        },
    )
    await session.execute(statement)
