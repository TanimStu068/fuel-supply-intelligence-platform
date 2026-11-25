"""Tests for the unified audit trail.

The audit trail is what an operator reads after an incident, so the tests
are about the questions an operator would ask: "what happened to this
station?", "what failed?", "give me the last ten things".

The session is a fake, like the rest of the suite's: the point being
tested is the *filtering and merging*, not SQLAlchemy.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar

import pytest

from app.audit import TRAILS, read_audit_entries, summarise_audit_entries
from app.models import Alert, DomainEventRow, Recommendation


class _FakeResult:
    """The result of a fake query: rows and scalars()."""

    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def scalars(self) -> _FakeResult:
        return self

    def all(self) -> list[Any]:
        return list(self._rows)


class _FakeSession:
    """A session that returns the rows belonging to whatever was queried.

    The audit module issues one query per source, and the fake works out
    which source a query is for by looking at the table it names. That
    way the fake stays honest when only one of the three sources is
    requested — the query order is then not enough to tell.

    The summary is a **database aggregate**, so the fake has to emulate
    one: a ``COUNT`` query grouped by kind and status comes back as
    ``(kind, status, count, oldest, newest)`` rows, not as the model's
    own objects. The fake counts the rows it was given, which is what
    Postgres would do.
    """

    #: Which table belongs to which row bucket.
    _TABLES: ClassVar[dict[str, str]] = {
        "recommendations": "decisions",
        "domain_events": "events",
        "alerts": "alerts",
    }

    #: Which row bucket belongs to which audit trail.
    _TRAILS: ClassVar[dict[str, str]] = {
        "decisions": "decision",
        "events": "event",
        "alerts": "alert",
    }

    def __init__(self) -> None:
        self._rows: dict[str, list[Any]] = {
            "decisions": [],
            "events": [],
            "alerts": [],
        }

    async def execute(self, query: Any) -> _FakeResult:
        """Return the rows for the table the query names."""
        text = str(query)
        for table, bucket in self._TABLES.items():
            if table not in text:
                continue
            rows = self._rows[bucket]
            if "count" in text.lower():
                return _FakeResult(self._aggregate(bucket, rows))
            return _FakeResult(rows)
        return _FakeResult([])

    @classmethod
    def _aggregate(cls, bucket: str, rows: list[Any]) -> list[tuple[Any, ...]]:
        """Emulate ``GROUP BY kind, status`` with a count and two moments.

        The grouping is done on the *audit entry's* kind and status, not
        on the model's columns, because the three trails name them
        differently — a decision's kind is its risk level, an event's is
        its type, and an alert's status is its severity.
        """
        trail = TRAILS[cls._TRAILS[bucket]]
        groups: dict[tuple[Any, Any], list[Any]] = {}
        for row in rows:
            entry = trail.to_entry(row)
            groups.setdefault((entry.kind, entry.status), []).append(entry)
        aggregated = []
        for (kind, status), group in groups.items():
            moments = [entry.at for entry in group if entry.at is not None]
            aggregated.append(
                (
                    kind,
                    status,
                    len(group),
                    min(moments) if moments else None,
                    max(moments) if moments else None,
                )
            )
        return aggregated

    def queue(self, decisions: list[Any], events: list[Any], alerts: list[Any]) -> None:
        """Set the rows each of the three queries will return."""
        self._rows["decisions"] = decisions
        self._rows["events"] = events
        self._rows["alerts"] = alerts


@pytest.fixture
def now() -> datetime:
    """A fixed "now" so the time windows are deterministic."""
    return datetime.now(UTC)


def now_ish() -> datetime:
    """The current time, for building a series of alerts."""
    return datetime.now(UTC)


@pytest.fixture
def rows(now: datetime) -> dict[str, list[Any]]:
    """One of everything in the trail."""
    return {
        "decisions": [
            Recommendation(
                id=1,
                created_at=now - timedelta(minutes=5),
                recommendation_id="platform-station-mirpur-DIESEL-100",
                station_id="station-mirpur",
                fuel_type="DIESEL",
                source_depot_id="depot-gazipur",
                route_id="route-gazipur-mirpur",
                quantity=3000.0,
                status="submitted",
                risk_level="CRITICAL",
                hours_to_stockout=0.0,
                confidence=0.9,
                needs_human_review=True,
                used_fallback_policy=False,
                idempotency_key="platform-station-mirpur-DIESEL-100",
                rationale="Dispatch diesel to Mirpur: about 0 hours of cover left.",
                sim_tick=100,
                inputs_snapshot=json.dumps({"signals": [{"name": "inventory_liters"}]}),
            ),
            Recommendation(
                id=2,
                created_at=now - timedelta(minutes=2),
                recommendation_id="platform-station-karnaphuli-DIESEL-120",
                station_id="station-karnaphuli",
                fuel_type="DIESEL",
                source_depot_id="depot-patiya",
                route_id="route-patiya-karnaphuli",
                quantity=1500.0,
                status="rejected",
                risk_level="WATCH",
                hours_to_stockout=3.0,
                confidence=0.7,
                needs_human_review=True,
                used_fallback_policy=False,
                idempotency_key="platform-station-karnaphuli-DIESEL-120",
                rationale="Rejected by the operator: the route is disrupted.",
                sim_tick=120,
                inputs_snapshot=json.dumps({"signals": []}),
            ),
        ],
        "events": [
            DomainEventRow(
                id=1,
                captured_at=now - timedelta(minutes=4),
                simulator_id=7,
                type="demand_spike",
                start_tick=90,
                end_tick=110,
                status="RESOLVED",
                parameters=json.dumps({"multiplier": 2.0}),
            )
        ],
        "alerts": [
            Alert(
                id=1,
                created_at=now - timedelta(minutes=1),
                kind="shortage_critical",
                severity="critical",
                entity_type="station",
                entity_id="station-mirpur",
                sim_tick=130,
                message="Dispatch diesel now: about 0 simulated hours of cover left.",
                payload=json.dumps({"fuel_type": "DIESEL"}),
                resolved=False,
            )
        ],
    }


async def _read(
    session: _FakeSession,
    rows: dict[str, list[Any]],
    **kwargs: Any,
) -> list[Any]:
    """Run a read against the fake session."""
    session.queue(rows["decisions"], rows["events"], rows["alerts"])
    return await read_audit_entries(session, **kwargs)


async def test_the_trail_holds_all_three_sources(rows) -> None:
    session = _FakeSession()
    entries = await _read(session, rows)

    assert {entry.source for entry in entries} == {"decision", "event", "alert"}
    assert len(entries) == 4


async def test_the_decision_trail_reads_the_table_that_is_actually_written(rows) -> None:
    """The decision trail must not be empty.

    The audit module originally read ``decision_records`` — a table that
    exists in the schema but that nothing ever writes to. The platform's
    decisions go to ``recommendations``, so the "decision" half of the
    searchable trail was permanently empty, and a search over it silently
    returned nothing.
    """
    session = _FakeSession()
    entries = await _read(session, rows)

    decisions = [entry for entry in entries if entry.source == "decision"]
    assert len(decisions) == 2
    assert all(entry.entity_id for entry in decisions)
    # The decision carries the fields an operator asks about.
    assert decisions[0].payload["quantity"] in (3000.0, 1500.0)
    assert decisions[0].payload["fuel_type"] == "DIESEL"


async def test_the_entries_are_newest_first(rows) -> None:
    session = _FakeSession()
    entries = await _read(session, rows)

    timestamps = [entry.at for entry in entries]
    assert timestamps == sorted(timestamps, reverse=True)


async def test_a_search_can_be_narrowed_to_one_source(rows) -> None:
    session = _FakeSession()
    entries = await _read(session, rows, sources=["alert"])

    assert len(entries) == 1
    assert entries[0].source == "alert"
    assert entries[0].kind == "shortage_critical"


async def test_a_search_can_be_narrowed_to_one_entity(rows) -> None:
    session = _FakeSession()
    entries = await _read(session, rows, entity_id="station-mirpur")

    assert entries
    assert all(entry.entity_id == "station-mirpur" for entry in entries)


async def test_a_full_text_search_finds_the_rationale(rows) -> None:
    session = _FakeSession()
    entries = await _read(session, rows, text="disrupted")

    assert len(entries) == 1
    assert entries[0].status == "rejected"
    assert "disrupted" in entries[0].message


async def test_a_kind_filter_works(rows) -> None:
    session = _FakeSession()
    entries = await _read(session, rows, kinds=["demand_spike"])

    assert len(entries) == 1
    assert entries[0].source == "event"


async def test_a_status_filter_works(rows) -> None:
    session = _FakeSession()
    entries = await _read(session, rows, statuses=["critical"])

    assert len(entries) == 1
    assert entries[0].source == "alert"


async def test_a_time_window_works(rows, now) -> None:
    session = _FakeSession()
    entries = await _read(session, rows, since=now - timedelta(minutes=90))
    assert len(entries) == 4

    entries = await _read(session, rows, since=now - timedelta(minutes=3))
    # Only the alert (1 minute old) and the rejected decision (2 minutes).
    assert len(entries) == 2


async def test_the_limit_is_applied_after_the_merge(rows) -> None:
    session = _FakeSession()
    entries = await _read(session, rows, limit=2)

    # The limit splits the page fairly between the trails, so a limit
    # of two still covers the newest of each of the three sources
    # rather than two of the busiest one. Each source's share is at
    # least one entry, so a small limit still shows every trail.
    assert len(entries) == 3
    assert {entry.source for entry in entries} == {"decision", "event", "alert"}


async def test_a_busy_trail_does_not_crowd_out_the_others(rows) -> None:
    """Every source gets a fair share of the page.

    There are roughly seventy alerts for every decision, so an
    un-capped merge was all alerts and the decision trail — thousands
    of rows — was invisible. The merge gives each source an equal
    share.
    """
    # Fifty alerts, one decision, one event.
    rows["alerts"] = [
        Alert(
            id=100 + index,
            created_at=now_ish() - timedelta(seconds=index),
            kind="shortage_critical",
            severity="critical",
            entity_type="station",
            entity_id="station-mirpur",
            sim_tick=1,
            message="shortage",
            payload="{}",
        )
        for index in range(50)
    ]
    session = _FakeSession()
    entries = await _read(session, rows, limit=90)

    sources = {entry.source for entry in entries}
    assert sources == {"decision", "event", "alert"}
    # Each source gets an equal share of the page: the limit is split
    # three ways, so the alerts take 30 of the 90 and the other two
    # take the one entry each that they have. The decision trail is
    # visible, which is the whole point.
    assert sum(1 for entry in entries if entry.source == "alert") == 30
    assert sum(1 for entry in entries if entry.source == "decision") == 2
    assert sum(1 for entry in entries if entry.source == "event") == 1
    assert len(entries) <= 90


async def test_the_summary_counts_the_facets(rows) -> None:
    session = _FakeSession()
    entries = await _read(session, rows)
    summary = await summarise_audit_entries(session, entries)

    assert summary["count"] == 4
    assert summary["by_source"] == {"alert": 1, "decision": 2, "event": 1}
    # A decision's kind is its risk level: "every critical decision" is
    # the question the audit trail is asked most often.
    assert summary["by_kind"]["CRITICAL"] == 1
    assert summary["by_kind"]["WATCH"] == 1
    assert summary["newest"] is not None
    assert summary["oldest"] is not None


async def test_the_summary_counts_every_row_and_not_only_the_page(rows) -> None:
    """The count describes the whole result set, not the page.

    A summary that reports the size of the page is worse than no summary
    at all: it tells the operator there are three things to read when
    there are three thousand. The counts are ``COUNT`` queries in the
    database for exactly this reason.
    """
    rows["alerts"] = [
        Alert(
            id=100 + index,
            created_at=now_ish() - timedelta(seconds=index),
            kind="shortage_critical",
            severity="critical",
            entity_type="station",
            entity_id="station-mirpur",
            sim_tick=1,
            message="shortage",
            payload="{}",
        )
        for index in range(50)
    ]
    session = _FakeSession()
    session.queue(rows["decisions"], rows["events"], rows["alerts"])
    summary = await summarise_audit_entries(session)

    assert summary["count"] == 53
    assert summary["by_source"]["alert"] == 50


async def test_an_empty_trail_returns_nothing() -> None:
    session = _FakeSession()
    entries = await _read(session, {"decisions": [], "events": [], "alerts": []})

    assert entries == []
    summary = await summarise_audit_entries(session, entries)
    assert summary["count"] == 0


async def test_a_malformed_payload_does_not_break_the_search(rows) -> None:
    """The simulator's data is untrusted; a broken payload must not 500."""
    rows["decisions"].append(
        Recommendation(
            id=3,
            created_at=datetime.now(UTC),
            recommendation_id="platform-broken-DIESEL-1",
            station_id="station-broken",
            fuel_type="DIESEL",
            source_depot_id="depot-gazipur",
            route_id="route-gazipur-mirpur",
            quantity=1.0,
            status="submitted",
            risk_level="CRITICAL",
            hours_to_stockout=0.0,
            confidence=0.5,
            needs_human_review=True,
            used_fallback_policy=False,
            idempotency_key="platform-broken-DIESEL-1",
            rationale="A decision with a broken payload.",
            sim_tick=1,
            inputs_snapshot="{not json at all",
        )
    )
    session = _FakeSession()
    entries = await _read(session, rows, text="broken payload")

    assert len(entries) == 1
    # The broken snapshot is kept as a raw string, and the decision's own
    # fields are still there next to it — a broken payload must not make
    # the entry unreadable.
    assert entries[0].payload["raw"] == "{not json at all"
    assert entries[0].payload["station_id"] == "station-broken"
    assert entries[0].status == "submitted"
