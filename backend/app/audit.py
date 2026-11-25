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

**The counting happens in the database.** The first version of this module
read every matching row into Python and counted it there. The trail holds
tens of thousands of rows and a hundred megabytes of JSON, so that took
eighteen seconds and returned a page of two hundred — a search that an
operator gives up on. The same lesson the load test taught about the
prediction-error aggregate: the data is already in Postgres, so Postgres
should do the counting. The filters are pushed down into SQL, the counts
come back as aggregates, and only the rows that are actually returned
cross into the application.

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

from sqlalchemy import cast, func, or_, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession

from .models import Alert, DomainEventRow, Recommendation

#: The largest page the endpoint will serve.
MAX_LIMIT = 1000


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


def _like_pattern(needle: str) -> str:
    """Escape a full-text needle so it is matched literally.

    ``LIKE`` treats ``%`` and ``_`` as wildcards. A search for "100%"
    must find the percent sign and not match a hundred characters, so the
    needle is escaped and the escape character is declared in the query.
    """
    escaped = needle.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _text_clause(columns: tuple[Any, ...], needle: str) -> Any:
    """A case-insensitive full-text clause over the given columns."""
    pattern = _like_pattern(needle)
    return or_(*(column.ilike(pattern, escape="\\") for column in columns))


def _decision_entry(row: Any) -> AuditEntry:
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


def _event_entry(row: Any) -> AuditEntry:
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


def _alert_entry(row: Any) -> AuditEntry:
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


# -- the three trails --------------------------------------------------------
#
# Each trail is described once, and the description drives both the page
# query and the count query. That is what keeps the counts and the page
# describing the *same* set of rows: a filter that is pushed into the page
# query but not into the count query would report a total that does not
# match what was returned.


@dataclass(frozen=True)
class _Trail:
    """How one audit trail is queried."""

    source: str
    #: The table the trail lives in.
    table: Any
    #: The column holding the row's timestamp.
    time_column: Any
    #: The column holding the trail's own "kind".
    kind_column: Any
    #: The column holding the trail's own "status".
    status_column: Any
    #: The column holding the entity, if the trail has one as a column.
    entity_column: Any
    #: The columns a full-text search reads.
    text_columns: tuple[Any, ...]
    #: The columns the page query selects.
    page_columns: tuple[Any, ...]
    #: How a row becomes an entry.
    to_entry: Any


_DECISIONS = _Trail(
    source="decision",
    table=Recommendation.__table__,
    time_column=Recommendation.created_at,
    kind_column=Recommendation.risk_level,
    status_column=Recommendation.status,
    entity_column=Recommendation.station_id,
    text_columns=(
        Recommendation.rationale,
        Recommendation.recommendation_id,
        Recommendation.station_id,
        Recommendation.fuel_type,
        Recommendation.status,
        Recommendation.risk_level,
        Recommendation.inputs_snapshot,
    ),
    page_columns=(
        Recommendation.id,
        Recommendation.created_at,
        Recommendation.recommendation_id,
        Recommendation.station_id,
        Recommendation.fuel_type,
        Recommendation.source_depot_id,
        Recommendation.route_id,
        Recommendation.quantity,
        Recommendation.status,
        Recommendation.risk_level,
        Recommendation.confidence,
        Recommendation.needs_human_review,
        Recommendation.used_fallback_policy,
        Recommendation.decided_by,
        Recommendation.sim_tick,
        Recommendation.rationale,
        Recommendation.inputs_snapshot,
    ),
    to_entry=_decision_entry,
)

_EVENTS = _Trail(
    source="event",
    table=DomainEventRow.__table__,
    time_column=DomainEventRow.captured_at,
    kind_column=DomainEventRow.type,
    status_column=DomainEventRow.status,
    # An event's entity lives inside its JSON parameters, so the filter
    # reaches into the document rather than into a column.
    entity_column=cast(DomainEventRow.parameters, JSONB).op("->>")("entity_id"),
    text_columns=(
        DomainEventRow.type,
        DomainEventRow.status,
        DomainEventRow.parameters,
    ),
    page_columns=(
        DomainEventRow.id,
        DomainEventRow.captured_at,
        DomainEventRow.type,
        DomainEventRow.start_tick,
        DomainEventRow.end_tick,
        DomainEventRow.status,
        DomainEventRow.parameters,
    ),
    to_entry=_event_entry,
)

_ALERTS = _Trail(
    source="alert",
    table=Alert.__table__,
    time_column=Alert.created_at,
    kind_column=Alert.kind,
    status_column=Alert.severity,
    entity_column=Alert.entity_id,
    text_columns=(
        Alert.message,
        Alert.kind,
        Alert.entity_id,
        Alert.payload,
    ),
    page_columns=(
        Alert.id,
        Alert.created_at,
        Alert.kind,
        Alert.severity,
        Alert.entity_type,
        Alert.entity_id,
        Alert.sim_tick,
        Alert.message,
        Alert.payload,
    ),
    to_entry=_alert_entry,
)

#: Every trail, keyed by the name the API uses for it.
TRAILS: dict[str, _Trail] = {
    _DECISIONS.source: _DECISIONS,
    _EVENTS.source: _EVENTS,
    _ALERTS.source: _ALERTS,
}


def _clauses(
    trail: _Trail,
    *,
    kinds: list[str] | None,
    statuses: list[str] | None,
    entity_id: str | None,
    text: str | None,
    since: datetime | None,
    until: datetime | None,
) -> list[Any]:
    """The WHERE clauses for one trail.

    Every filter the API offers is pushed down here, so the database does
    the filtering and the application never sees a row that does not
    match. The same filters are applied again in Python after the merge —
    see :func:`read_audit_entries` — because ``status`` means a decision's
    outcome in one trail and an alert's severity in another.
    """
    clauses: list[Any] = []
    if kinds:
        clauses.append(trail.kind_column.in_(kinds))
    if statuses:
        # The comparison is case-insensitive on both sides: a decision's
        # status is "submitted" and an alert's severity is "critical", and
        # an operator types them in either case.
        wanted = {status.upper() for status in statuses}
        clauses.append(func.upper(trail.status_column).in_(wanted))
    if entity_id:
        clauses.append(trail.entity_column == entity_id)
    if text:
        clauses.append(_text_clause(trail.text_columns, text))
    if since is not None:
        clauses.append(trail.time_column >= since)
    if until is not None:
        clauses.append(trail.time_column <= until)
    return clauses


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
    """Read one page of the unified audit stream.

    The three trails are queried independently and merged in Python. That
    is the honest choice here: the tables have genuinely different shapes
    (a decision has a rationale, an event has a tick range, an alert has a
    severity), and a SQL ``UNION`` over three different schemas would need
    a column for every field of every table — mostly nulls.

    The merge is by timestamp, newest first, and the limit is applied
    *after* the merge, so a search over one source does not crowd out the
    other two.
    """
    limit = max(1, min(limit, MAX_LIMIT))
    wanted = set(sources) if sources else set(TRAILS)

    entries: list[AuditEntry] = []

    for source in wanted:
        trail = TRAILS.get(source)
        if trail is None:
            continue
        clauses = _clauses(
            trail,
            kinds=kinds,
            statuses=statuses,
            entity_id=entity_id,
            text=text,
            since=since,
            until=until,
        )
        query = (
            select(*trail.page_columns)
            .select_from(trail.table)
            .where(*clauses)
            .order_by(trail.time_column.desc())
            .limit(limit)
        )
        rows = (await session.execute(query)).all()
        entries.extend(trail.to_entry(row) for row in rows)

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
    # The share is divided among the trails that actually have rows. A
    # search that matched one trail and nothing else gets the whole
    # page: splitting it three ways would answer "one thing matches"
    # with a page of one for no reason.
    #
    # This runs after every filter, so it only ever splits a set that
    # already matches the query.
    by_source: dict[str, list[AuditEntry]] = {}
    for entry in entries:
        by_source.setdefault(entry.source, []).append(entry)

    per_source = max(1, limit // max(1, len(by_source)))
    merged: list[AuditEntry] = []
    for source_entries in by_source.values():
        merged.extend(source_entries[:per_source])

    merged.sort(key=lambda entry: entry.at or datetime.min.replace(tzinfo=UTC), reverse=True)
    return merged


async def summarise_audit_entries(
    session: AsyncSession,
    entries: list[AuditEntry] | None = None,
    *,
    sources: list[str] | None = None,
    kinds: list[str] | None = None,
    statuses: list[str] | None = None,
    entity_id: str | None = None,
    text: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
) -> dict[str, Any]:
    """Summarise a search: how many, and broken down by facet.

    The counts come from the database, not from the page. A page of two
    hundred rows cannot tell an operator that there are twelve thousand
    alerts behind them, and a summary that reports "200" for a trail of
    twelve thousand is worse than no summary at all.

    ``COUNT`` is grouped by kind and status, and the oldest and newest
    moments are the minimum and maximum over those groups — so the
    whole summary is three aggregate queries.
    """
    wanted = set(sources) if sources else set(TRAILS)

    by_source: dict[str, int] = {}
    by_kind: dict[str, int] = {}
    by_status: dict[str, int] = {}
    total = 0
    oldest: datetime | None = None
    newest: datetime | None = None

    for source in wanted:
        trail = TRAILS.get(source)
        if trail is None:
            continue
        clauses = _clauses(
            trail,
            kinds=kinds,
            statuses=statuses,
            entity_id=entity_id,
            text=text,
            since=since,
            until=until,
        )
        query = (
            select(
                trail.kind_column,
                trail.status_column,
                func.count(),
                func.min(trail.time_column),
                func.max(trail.time_column),
            )
            .select_from(trail.table)
            .where(*clauses)
            .group_by(trail.kind_column, trail.status_column)
        )
        rows = (await session.execute(query)).all()
        if not rows:
            continue
        trail_total = 0
        for kind, status, count, first, last in rows:
            trail_total += int(count)
            by_kind[kind] = by_kind.get(kind, 0) + int(count)
            by_status[status] = by_status.get(status, 0) + int(count)
            if first is not None:
                oldest = first if oldest is None else min(oldest, _as_utc(first))
            if last is not None:
                newest = last if newest is None else max(newest, _as_utc(last))
        by_source[source] = trail_total
        total += trail_total

    return {
        "count": total,
        "by_source": dict(sorted(by_source.items())),
        "by_kind": dict(sorted(by_kind.items(), key=lambda item: -item[1])),
        "by_status": dict(sorted(by_status.items())),
        "newest": newest.isoformat() if newest else None,
        "oldest": oldest.isoformat() if oldest else None,
    }


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
