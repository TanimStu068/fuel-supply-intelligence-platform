"""A searchable audit trail.

The platform writes three separate audit trails to Postgres:

* :class:`~app.models.Recommendation` — every allocation decision
* :class:`~app.models.DomainEventRow` — the simulator's domain events
* :class:`~app.models.Alert` — the platform's own alerts

They are what a judge asks to see after an incident: *what did the platform
decide, when, and why*. But they are three tables with three shapes, and
reading them means three queries and three joins in the reader's head.

This module presents them as **one** searchable stream: a common
:class:`AuditEntry` shape, a common query, and one endpoint that answers
"show me everything about station X in the last hour".

The search is deliberately simple and honest:

* **Full text** over the fields an operator would search — the entity, the
  kind, the rationale and the message.
* **Facets** on the fields that are worth counting — the kind, the status
  and the severity — so a search can be narrowed and then summarised.
* **A time window**, because an audit question is almost always about a
  period.

Loki is deliberately not used. The audit trail is *already* in Postgres,
and putting a second store in front of it would mean two things that can
disagree. Structured JSON logs cover the "search the raw log lines" half
of the requirement, and the API covers the "search the decisions" half.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import Alert, DomainEventRow, Recommendation


@dataclass(slots=True)
class AuditEntry:
    """One entry in the unified audit stream."""

    #: Which trail it came from: ``decision``, ``event`` or ``alert``.
    source: str
    #: The row's own identifier.
    id: int
    #: When it happened.
    at: datetime
    #: What kind of thing it is.
    kind: str
    #: The status it ended in.
    status: str
    #: The entity it concerns, if any.
    entity_id: str | None
    #: The human-readable explanation.
    message: str
    #: Everything else, as a JSON object.
    payload: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        """A JSON-safe representation."""
        return {
            "source": self.source,
            "id": self.id,
            "at": self.at.isoformat() if self.at else None,
            "kind": self.kind,
            "status": self.status,
            "entity_id": self.entity_id,
            "message": self.message,
            "payload": self.payload,
        }


def _loads(raw: str | None) -> dict[str, Any]:
    """Parse a JSON payload, tolerating anything that is not one."""
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return {"raw": raw}
    return parsed if isinstance(parsed, dict) else {"value": parsed}


def _decision_entry(row: Recommendation) -> AuditEntry:
    """A recommendation (an allocation decision) as an audit entry.

    The ``kind`` of a decision is its risk level, which is what an
    operator filters on — "every critical decision" is the question the
    audit trail is asked most often.
    """
    payload = _loads(row.inputs_snapshot)
    return AuditEntry(
        source="decision",
        id=row.id,
        at=row.created_at,
        kind=row.risk_level,
        status=row.status,
        entity_id=row.station_id or None,
        message=row.rationale or "",
        payload={
            **payload,
            "recommendation_id": row.recommendation_id,
            "station_id": row.station_id,
            "fuel_type": row.fuel_type,
            "quantity": row.quantity,
            "source_depot_id": row.source_depot_id,
            "route_id": row.route_id,
            "confidence": row.confidence,
            "needs_human_review": row.needs_human_review,
            "used_fallback_policy": row.used_fallback_policy,
            "decided_by": row.decided_by,
            "sim_tick": row.sim_tick,
        },
    )


def _event_entry(row: DomainEventRow) -> AuditEntry:
    """A domain event as an audit entry."""
    parameters = _loads(row.parameters)
    return AuditEntry(
        source="event",
        id=row.id,
        at=row.captured_at,
        kind=row.type,
        status=row.status,
        entity_id=parameters.get("entity_id"),
        message=f"{row.type} {row.status.lower()} (ticks {row.start_tick}-{row.end_tick})",
        payload=parameters,
    )


def _alert_entry(row: Alert) -> AuditEntry:
    """An alert as an audit entry."""
    payload = _loads(row.payload)
    return AuditEntry(
        source="alert",
        id=row.id,
        at=row.created_at,
        kind=row.kind,
        status=row.severity,
        entity_id=row.entity_id,
        message=row.message or "",
        payload=payload,
    )


async def read_audit_entries(
    session: AsyncSession,
    *,
    sources: list[str] | None = None,
    kinds: list[str] | None = None,
    statuses: list[str] | None = None,
    entity_id: str | None = None,
    text: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int = 200,
) -> list[AuditEntry]:
    """Read the unified audit stream.

    The three trails are queried independently and merged in Python. That
    is the honest choice here: the tables have genuinely different shapes
    (a decision has a rationale, an event has a tick range, an alert has a
    severity), and a SQL ``UNION`` over three different schemas would need
    a column for every field of every table — mostly nulls.

    The merge is by timestamp, newest first, and the limit is applied
    *after* the merge, so a search over one source does not crowd out the
    other two.
    """
    limit = max(1, min(limit, 1000))
    wanted = set(sources) if sources else {"decision", "event", "alert"}

    entries: list[AuditEntry] = []

    if "decision" in wanted:
        # The decision trail is the ``recommendations`` table — that is
        # where the platform actually writes every allocation decision.
        # (``decision_records`` exists in the schema but nothing writes
        # it, so reading it would give an empty trail.)
        query = select(Recommendation)
        if kinds:
            query = query.where(Recommendation.risk_level.in_(kinds))
        if statuses:
            query = query.where(Recommendation.status.in_(statuses))
        if since is not None:
            query = query.where(Recommendation.created_at >= since)
        if until is not None:
            query = query.where(Recommendation.created_at <= until)
        if entity_id:
            query = query.where(Recommendation.station_id == entity_id)
        if text:
            pattern = f"%{text.lower()}%"
            query = query.where(
                or_(
                    Recommendation.rationale.ilike(pattern),
                    Recommendation.recommendation_id.ilike(pattern),
                    Recommendation.station_id.ilike(pattern),
                    Recommendation.fuel_type.ilike(pattern),
                    Recommendation.status.ilike(pattern),
                    Recommendation.risk_level.ilike(pattern),
                )
            )
        rows = (await session.execute(query)).scalars().all()
        entries.extend(_decision_entry(row) for row in rows)

    if "event" in wanted:
        query = select(DomainEventRow)
        if kinds:
            query = query.where(DomainEventRow.type.in_(kinds))
        if statuses:
            query = query.where(DomainEventRow.status.in_(statuses))
        if since is not None:
            query = query.where(DomainEventRow.captured_at >= since)
        if until is not None:
            query = query.where(DomainEventRow.captured_at <= until)
        if text:
            pattern = f"%{text.lower()}%"
            query = query.where(
                or_(
                    DomainEventRow.type.ilike(pattern),
                    DomainEventRow.status.ilike(pattern),
                )
            )
        rows = (await session.execute(query)).scalars().all()
        entries.extend(_event_entry(row) for row in rows)

    if "alert" in wanted:
        query = select(Alert)
        if kinds:
            query = query.where(Alert.kind.in_(kinds))
        if statuses:
            query = query.where(Alert.severity.in_(statuses))
        if since is not None:
            query = query.where(Alert.created_at >= since)
        if until is not None:
            query = query.where(Alert.created_at <= until)
        if entity_id:
            query = query.where(Alert.entity_id == entity_id)
        if text:
            pattern = f"%{text.lower()}%"
            query = query.where(
                or_(
                    Alert.message.ilike(pattern),
                    Alert.kind.ilike(pattern),
                    Alert.entity_id.ilike(pattern),
                )
            )
        rows = (await session.execute(query)).scalars().all()
        entries.extend(_alert_entry(row) for row in rows)

    # The entity filter applies to every source, so it is applied after
    # the merge rather than in three different places.
    if entity_id:
        entries = [entry for entry in entries if entry.entity_id == entity_id]

    # The facets are applied again here, after the merge.
    #
    # They are already in the SQL — pushing them down is what keeps the
    # query cheap when the trail is large. But the merge is the union of
    # three queries, and the *same* name means different things in each
    # one: ``status`` is a decision's outcome but an alert's severity,
    # and ``kind`` is a decision's kind but an event's type. Applying the
    # filter once, uniformly, over the merged stream is what makes
    # ``?status=critical`` mean "the critical things" rather than "the
    # things whose status happens to be the string 'critical' in the
    # table where that string appears".
    if kinds:
        wanted_kinds = set(kinds)
        entries = [entry for entry in entries if entry.kind in wanted_kinds]

    if statuses:
        wanted_statuses = {status.upper() for status in statuses}
        entries = [entry for entry in entries if entry.status.upper() in wanted_statuses]

    if text:
        needle = text.lower()
        entries = [entry for entry in entries if needle in _searchable_text(entry).lower()]

    # The time window is applied again here, for the same reason: it must
    # mean the same thing for all three sources.
    if since is not None:
        entries = [entry for entry in entries if _at_or_after(entry, since)]
    if until is not None:
        entries = [entry for entry in entries if _at_or_before(entry, until)]

    entries.sort(key=lambda entry: entry.at or datetime.min.replace(tzinfo=UTC), reverse=True)

    # Each trail gets a *fair* share of the page. Without this the
    # busiest trail crowds the others out — there are roughly seventy
    # alerts for every decision, so a merged page was all alerts and the
    # decision trail was invisible even though it had thousands of rows.
    #
    # This runs after every filter, so it only ever splits a set that
    # already matches the query.
    per_source = max(1, limit // max(1, len(wanted)))
    by_source: dict[str, list[AuditEntry]] = {}
    for entry in entries:
        by_source.setdefault(entry.source, []).append(entry)

    merged: list[AuditEntry] = []
    for source_entries in by_source.values():
        merged.extend(source_entries[:per_source])

    merged.sort(key=lambda entry: entry.at or datetime.min.replace(tzinfo=UTC), reverse=True)
    return merged


def _as_utc(moment: datetime) -> datetime:
    """Return the moment with a timezone attached.

    The database hands back naive timestamps in UTC, and a query parameter
    may arrive with or without a timezone. Both are compared as UTC, so
    the comparison is never a ``TypeError`` and never off by the
    offset.
    """
    if moment.tzinfo is None:
        return moment.replace(tzinfo=UTC)
    return moment


def _at_or_after(entry: AuditEntry, moment: datetime) -> bool:
    """Whether an entry happened at or after a moment."""
    if entry.at is None:
        return False
    return _as_utc(entry.at) >= _as_utc(moment)


def _at_or_before(entry: AuditEntry, moment: datetime) -> bool:
    """Whether an entry happened at or before a moment."""
    if entry.at is None:
        return False
    return _as_utc(entry.at) <= _as_utc(moment)


def _searchable_text(entry: AuditEntry) -> str:
    """Everything about an entry that a full-text search should match."""
    parts = [
        entry.kind,
        entry.status,
        entry.entity_id or "",
        entry.message,
        json.dumps(entry.payload, default=str),
    ]
    return " ".join(parts)


async def summarise_audit_entries(
    session: AsyncSession,
    entries: list[AuditEntry],
) -> dict[str, Any]:
    """Summarise a set of entries: how many, and broken down by facet."""
    by_source: dict[str, int] = {}
    by_kind: dict[str, int] = {}
    by_status: dict[str, int] = {}

    for entry in entries:
        by_source[entry.source] = by_source.get(entry.source, 0) + 1
        by_kind[entry.kind] = by_kind.get(entry.kind, 0) + 1
        by_status[entry.status] = by_status.get(entry.status, 0) + 1

    return {
        "count": len(entries),
        "by_source": dict(sorted(by_source.items())),
        "by_kind": dict(sorted(by_kind.items(), key=lambda item: -item[1])),
        "by_status": dict(sorted(by_status.items())),
        "newest": entries[0].at.isoformat() if entries and entries[0].at else None,
        "oldest": entries[-1].at.isoformat() if entries and entries[-1].at else None,
    }
