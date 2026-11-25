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
from sqlalchemy.sql.util import ClauseAdapter

from .models import Alert, DomainEventRow, Recommendation

#: The largest page the endpoint will serve.
MAX_LIMIT = 1000

#: The smallest number of rows a full-text scan will look at before it
#: gives up. A search that matches nothing must not walk the whole
#: trail, and a needle that matches everything must not fetch the whole
#: trail either.
MIN_SCAN_WINDOW_ROWS = 5000

#: How many times the page size the scan window is allowed to be.
#:
#: The window is the newest slice of a trail that a full-text search
#: looks at. It has to be comfortably larger than the page the operator
#: asked for, because the filter is applied *after* the slice is taken:
#: a window of exactly the page size would return fewer rows than the
#: page asked for whenever the newest rows do not all match.
SCAN_WINDOW_PAGE_FACTOR = 20


def scan_window(limit: int) -> int:
    """How many rows a full-text search looks at.

    One definition, used by the page **and** the summary, so the two
    always describe the same set of rows. If they used different windows
    the summary could count rows the page never showed, and the count
    would be a number the operator cannot reproduce from the answer.
    """
    return max(limit * SCAN_WINDOW_PAGE_FACTOR, MIN_SCAN_WINDOW_ROWS)


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


def _adapt(clause: Any, source: Any) -> Any:
    """Rewrite a clause so it reads from ``source`` instead of the table.

    ``source`` is the bounded subquery that the full-text scan selects
    from. Every clause has to be rewritten against it, because a clause
    that still points at the table would join the two back together and
    the whole table would be read again — which is the bug the subquery
    exists to fix.

    :class:`ClauseAdapter` is SQLAlchemy's own mechanism for this, and
    it is used here rather than a hand-written column lookup because not
    every clause is a column. The events trail's entity filter is an
    expression that reaches into a JSON document
    (``parameters->>'entity_id'``), and an expression has no column name
    to look up — the hand-written version raised ``KeyError: None`` and
    turned every full-text search that also filtered by entity into a
    500.
    """
    return ClauseAdapter(source).traverse(clause)


def _clauses(
    trail: _Trail,
    *,
    kinds: list[str] | None,
    statuses: list[str] | None,
    entity_id: str | None,
    text: str | None,
    since: datetime | None,
    until: datetime | None,
    source: Any | None = None,
) -> list[Any]:
    """The WHERE clauses for one trail.

    Every filter the API offers is pushed down here, so the database does
    the filtering and the application never sees a row that does not
    match. The same filters are applied again in Python after the merge —
    see :func:`read_audit_entries` — because ``status`` means a decision's
    outcome in one trail and an alert's severity in another.

    ``source`` overrides which table the columns are read from. It is the
    bounded subquery in the full-text search: without it the clause
    would still point at the table, and the query would join the two
    back together and read every row anyway.

    The remapping goes through SQLAlchemy's own :class:`ClauseAdapter`
    rather than a hand-written ``column.key`` lookup. The adapter
    rewrites *any* clause against the new FROM — including the ones
    that are not simple columns. The events trail's entity filter is
    not a column at all: it reaches into the JSON document
    (``parameters->>'entity_id'``), and a name-based lookup cannot
    express that. It failed with ``KeyError: None`` and turned every
    full-text search that also filtered by entity into a 500.

    **Each filter is built as a whole clause and then adapted**, rather
    than adapting each column and assembling the clause afterwards. It
    matters: a clause that still mentions the table is not merely
    untidy — SQLAlchemy adds the table to the FROM to satisfy it, and
    the query becomes a cartesian product between the window and the
    whole table. That is not a slow query, it is a wrong one: the
    decision trail's count took **147 seconds** that way.
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

    if source is None:
        return clauses
    return [_adapt(clause, source) for clause in clauses]


async def _search_by_scan(
    session: AsyncSession,
    trail: _Trail,
    *,
    kinds: list[str] | None,
    statuses: list[str] | None,
    entity_id: str | None,
    text: str | None,
    since: datetime | None,
    until: datetime | None,
    limit: int,
) -> list[Any]:
    """Find the rows matching a full-text search by walking the newest ones.

    **Why not let the index do it.** A trigram index finds the rows that
    contain the needle quickly, but it then has to *fetch* every one of
    them. The audit trail's needle is often common — "disrupted" appears
    in the alternatives of every recommendation the platform made — so
    the index returned all 61,951 rows and the heap scan fetched all
    61,951 of them, to sort them by time and keep two hundred. That was
    **2,319 ms**.

    The rows the operator wants are the **newest** ones, so the search
    takes the newest rows first and filters them afterwards. The same
    query under this plan took **13 ms** — a hundred and seventy-eight
    times less work, because it never touches the rows that would have
    been discarded.

    The shape matters twice over:

    * the filter is applied in an **outer** query over a bounded
      **inner** query, because written as one statement the planner
      prefers the trigram index and fetches every match anyway; and
    * the filter's columns point at the **inner** query, because if they
      point at the table the two are joined back together and the whole
      table is read again — which is the bug this shape exists to fix.

    The walk is bounded: it gives up after ``SCAN_WINDOW_ROWS`` rows, so
    a needle that matches nothing in the newest rows costs one bounded
    scan rather than a full one.
    """
    recent, clauses = _scan_window(
        trail,
        kinds=kinds,
        statuses=statuses,
        entity_id=entity_id,
        text=text,
        since=since,
        until=until,
        limit=limit,
    )
    query = (
        select(*(column for column in recent.c))
        .where(*clauses)
        .order_by(recent.c[trail.time_column.key].desc())
        .limit(limit)
    )
    return list((await session.execute(query)).all())


async def _count_by_scan(
    session: AsyncSession,
    trail: _Trail,
    *,
    kinds: list[str] | None,
    statuses: list[str] | None,
    entity_id: str | None,
    text: str | None,
    since: datetime | None,
    until: datetime | None,
    limit: int,
) -> list[Any]:
    """Count the matches of a full-text search, over the same window.

    This is the summary's half of the windowed search, and it exists
    because the summary used to count the **whole table** while the
    page showed the newest slice. Two things were wrong with that.

    The count was **inconsistent**: the summary described a different
    set of rows than the page returned, so the total could not be
    reproduced from the answer. And the count was **the slow part**: the
    decision trail's ``inputs_snapshot`` is a 2.4 KB JSON document per
    row, so reading all 61,951 of them to find the matches took
    **2,171 ms** — while the windowed page took **28 ms**.

    The window is the same one the page uses, so the two agree by
    construction rather than by coincidence.
    """
    recent, clauses = _scan_window(
        trail,
        kinds=kinds,
        statuses=statuses,
        entity_id=entity_id,
        text=text,
        since=since,
        until=until,
        limit=limit,
    )
    kind_column = recent.c[trail.kind_column.key]
    status_column = recent.c[trail.status_column.key]
    time_column = recent.c[trail.time_column.key]
    query = (
        select(
            kind_column,
            status_column,
            func.count(),
            func.min(time_column),
            func.max(time_column),
        )
        .select_from(recent)
        .where(*clauses)
        .group_by(kind_column, status_column)
    )
    return list((await session.execute(query)).all())


def _scan_window(
    trail: _Trail,
    *,
    kinds: list[str] | None,
    statuses: list[str] | None,
    entity_id: str | None,
    text: str | None,
    since: datetime | None,
    until: datetime | None,
    limit: int,
) -> tuple[Any, list[Any]]:
    """The bounded window of rows a full-text search looks at.

    Returns the subquery and the clauses that filter it. The page and
    the count both build it here, so they cannot drift apart.
    """
    window = scan_window(limit)
    recent = (
        select(*trail.page_columns)
        .select_from(trail.table)
        .order_by(trail.time_column.desc())
        .limit(window)
        .subquery()
    )
    clauses = _clauses(
        trail,
        kinds=kinds,
        statuses=statuses,
        entity_id=entity_id,
        text=text,
        since=since,
        until=until,
        source=recent,
    )
    return recent, clauses


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
        if text:
            # A full-text search is scanned over the newest rows rather
            # than matched with an index. See ``_search_by_scan``.
            rows = await _search_by_scan(
                session,
                trail,
                kinds=kinds,
                statuses=statuses,
                entity_id=entity_id,
                text=text,
                since=since,
                until=until,
                limit=limit,
            )
        else:
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
    limit: int = 200,
) -> dict[str, Any]:
    """Summarise a search: how many, and broken down by facet.

    The counts come from the database, not from the page. A page of two
    hundred rows cannot tell an operator that there are twelve thousand
    alerts behind them, and a summary that reports "200" for a trail of
    twelve thousand is worse than no summary at all.

    ``COUNT`` is grouped by kind and status, and the oldest and newest
    moments are the minimum and maximum over those groups — so the
    whole summary is three aggregate queries.

    **A full-text search counts the same window the page shows.**
    This is the part that was wrong. The count query was left on the
    unbounded whole-table shape while the page moved to a bounded
    window, so the two described different sets of rows — and the
    unbounded count was also the endpoint's dominant cost: the
    decision trail's ``inputs_snapshot`` is a 2.4 KB JSON document per
    row, so scanning all 61,951 of them to count the matches took
    **2,171 ms**, while the windowed page took **28 ms**.

    The window is now the contract for both. The summary says so
    explicitly in its ``scope`` field, because a count of "3" that
    means "3 in the newest 5,000" must not be read as "3 in the whole
    trail" — that is the difference between a measurement and a claim.

    A search **without** a needle still counts the whole table. "How
    many critical alerts are there" genuinely means all of them, and
    that count is cheap: the facet columns are narrow and indexed.
    """
    wanted = set(sources) if sources else set(TRAILS)

    by_source: dict[str, int] = {}
    by_kind: dict[str, int] = {}
    by_status: dict[str, int] = {}
    total = 0
    oldest: datetime | None = None
    newest: datetime | None = None
    window = scan_window(limit)

    for source in wanted:
        trail = TRAILS.get(source)
        if trail is None:
            continue
        if text:
            # The count has to look at the same rows the page looks at,
            # or the summary and the page describe different sets.
            rows = await _count_by_scan(
                session,
                trail,
                kinds=kinds,
                statuses=statuses,
                entity_id=entity_id,
                text=text,
                since=since,
                until=until,
                limit=limit,
            )
        else:
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
        # What the count covers. Without this the count is a number the
        # operator cannot interpret: the same field means "the whole
        # trail" for a facet query and "the newest N rows" for a
        # full-text search.
        "scope": "window" if text else "table",
        "window_rows": window if text else None,
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
