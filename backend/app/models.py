"""SQLAlchemy table definitions owned by the participant backend."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Float, Integer, String, Text, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Declarative base."""


class StateSnapshot(Base):
    """One row per operator state snapshot pulled from the simulator.

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
