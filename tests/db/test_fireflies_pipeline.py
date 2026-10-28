"""Fireflies capture -> analysis -> publication against a real database, fake backends.

Covers: whole-workspace listing (per-user listing finds meetings the default
listing misses), participant and item exclusions before anything is stored,
not-yet-transcribed meetings left for later, re-runs storing nothing, a late
summary creating a new version that replaces the old one atomically, the
fail-closed cases (source disabled, paused), and email search never returning
meeting chunks.
"""

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from ci_cli.scheduled import fireflies_processing, fireflies_tick
from ci_cli.seed import seed
from ci_core.db import AppRole, Connection, connect
from ci_core.operator import exclude_participant, set_mode, set_provider
from ci_core.runtime import EmptyScopeError
from ci_core.settings import load_settings
from ci_email.capture import capture_window
from ci_email.fixture import FixtureGraphClient
from ci_email.process import Backends, process_all
from ci_fireflies import setup
from ci_fireflies.capture import capture_meetings
from ci_fireflies.fixture import FixtureFirefliesClient
from ci_fireflies.process import MeetingBackends, process_meetings
from ci_processing.classification import FakeClassifier
from ci_processing.embedding import FakeEmbedder
from ci_processing.meetings import FakeMeetingAnalyzer
from ci_processing.screening import FakeScreener
from ci_retrieval import search as retrieval
from ci_retrieval.identity import READER_ALL, Principal

NOW = datetime(2026, 10, 28, tzinfo=UTC)
EMBEDDER = FakeEmbedder(1536)
BACKENDS = MeetingBackends(FakeMeetingAnalyzer(), EMBEDDER)
PROFILE = {"p": "fireflies-test"}
READER = Principal("reader-oid", None, (READER_ALL,))


@pytest.fixture(scope="module")
def ff(database: str) -> Iterator[dict[str, Any]]:
    def connector(role: AppRole | None = None) -> Connection:
        return connect(dbname=database, role=role)

    with connector() as conn:
        seed(conn, "pytest")  # the email fixture tenant, mode test
        setup.init_workspace(conn, "fixture-workspace", "pytest")
        exclude_participant(
            conn, "excluded.person@finishessolutions.example", "pytest", reason="hr_private"
        )
        conn.commit()
    client = FixtureFirefliesClient.from_directory()
    first = capture_meetings(connector, client, hours=168, profile=PROFILE, now=NOW)
    rerun = capture_meetings(connector, client, hours=168, profile=PROFILE, now=NOW)
    counts = process_meetings(lambda: connector("ci_ingest"), BACKENDS, workers=1)
    yield {"connect": connector, "client": client, "first": first, "rerun": rerun, "counts": counts}


def published(ff: dict[str, Any]) -> dict[str, dict[str, Any]]:
    with ff["connect"]("ci_search") as conn:
        rows = conn.execute(
            "SELECT m.communication_id::text, m.title, m.organizer_email, a.summary,"
            " a.decisions, a.action_items, a.risks, a.yes_labels, a.properties"
            " FROM published_meetings m"
            " LEFT JOIN published_meeting_analyses a USING (communication_id)"
        ).fetchall()
    return {r["title"]: r for r in rows}


def test_capture_reads_the_whole_workspace_and_applies_exclusions(ff: dict[str, Any]) -> None:
    c = ff["first"].counts
    assert c["found_via_default"] == 2  # meetings pat (the key owner) organized: ff-001, ff-003
    assert c["found_via_user"] == 3  # morgan's ff-002 and ff-004, riley's ff-005; ff-006 is old
    assert c["listed"] == 5
    assert c["excluded_participant"] == 1  # ff-003: never fetched into the database
    assert c["not_ready"] == 1  # ff-004 has no sentences yet
    assert c["new_version"] == 3
    assert ff["rerun"].counts["new_version"] == 0 and ff["rerun"].counts["unchanged"] == 3
    with ff["connect"]() as conn:
        titles = {
            r["title"]
            for r in conn.execute("SELECT title FROM communications WHERE kind = 'meeting'")
        }
    assert titles == {
        "Example Ridge weekly OAC",
        "Lender draw call",
        "Vendor call about Example Commons",
    }


def test_processing_publishes_analysis_and_cited_chunks(ff: dict[str, Any]) -> None:
    assert ff["counts"] == {"published": 3}
    meetings = published(ff)
    oac = meetings["Example Ridge weekly OAC"]
    assert "project_risk" in oac["yes_labels"]
    assert oac["decisions"] and oac["risks"] and oac["action_items"]
    assert "Example Ridge Lot 12" in oac["properties"]
    with ff["connect"]("ci_search") as conn:
        chunks = conn.execute(
            "SELECT chunk_kind, segment_from, segment_to, start_ms, speakers, display_text"
            " FROM published_chunks WHERE communication_id = %s ORDER BY chunk_kind, seq",
            (oac["communication_id"],),
        ).fetchall()
    kinds = {c["chunk_kind"] for c in chunks}
    assert kinds == {"transcript_window", "provider_summary"}
    window = next(c for c in chunks if c["chunk_kind"] == "transcript_window")
    assert window["segment_from"] == 0 and window["start_ms"] == 0 and window["speakers"]
    assert window["display_text"].startswith("Pat Example: ")


def test_meetings_are_recorded_as_not_screened(ff: dict[str, Any]) -> None:
    with ff["connect"]() as conn:
        rows = conn.execute(
            "SELECT d.check_name, d.decision, d.reason_code FROM policy_decisions d"
            " JOIN communication_versions v ON v.id = d.version_id"
            " JOIN fireflies_meetings m ON m.communication_id = v.communication_id"
        ).fetchall()
    assert rows and all(
        (r["check_name"], r["decision"], r["reason_code"])
        == ("pii", "allow", "not_screened_by_policy")
        for r in rows
    )


def test_a_late_summary_replaces_the_published_version(ff: dict[str, Any]) -> None:
    client: FixtureFirefliesClient = ff["client"]
    draw = next(t for t in client.workspace["transcripts"] if t["id"] == "ff-002-draw")
    draw["summary"] = {
        "overview": "Draw 9 needs title update and lien waivers.",
        "action_items": None,
        "keywords": ["draw"],
    }
    again = capture_meetings(ff["connect"], client, hours=168, profile=PROFILE, now=NOW)
    assert again.counts["new_version"] == 1
    assert process_meetings(lambda: ff["connect"]("ci_ingest"), BACKENDS, workers=1) == {
        "published": 1
    }
    with ff["connect"]() as conn:
        states = [
            r["processing_state"]
            for r in conn.execute(
                "SELECT v.processing_state FROM communication_versions v"
                " JOIN communications c ON c.id = v.communication_id"
                " WHERE c.native_id = 'ff-002-draw' ORDER BY v.version_no"
            )
        ]
    assert states == ["superseded", "published"]
    with ff["connect"]("ci_search") as conn:
        kinds = [
            r["chunk_kind"]
            for r in conn.execute(
                "SELECT ch.chunk_kind FROM published_chunks ch JOIN published_meetings m"
                " USING (communication_id) WHERE m.title = 'Lender draw call'"
            )
        ]
    assert "provider_summary" in kinds  # only the new version's chunks are visible


def test_a_participant_exclusion_added_later_hides_published_meetings(
    ff: dict[str, Any],
) -> None:
    with ff["connect"]() as conn:
        before = {r["title"] for r in conn.execute("SELECT title FROM published_meetings")}
        exclude_participant(conn, "@supplier.example", "pytest", reason="vendor_hidden")
        after = {r["title"] for r in conn.execute("SELECT title FROM published_meetings")}
        conn.rollback()
    assert before - after == {"Vendor call about Example Commons"}


def test_capture_fails_closed(ff: dict[str, Any]) -> None:
    with ff["connect"]() as conn:
        set_provider(conn, "fireflies", False, "pytest")
        conn.commit()
    try:
        with pytest.raises(EmptyScopeError, match="not enabled"):
            capture_meetings(ff["connect"], ff["client"], hours=24, profile=PROFILE, now=NOW)
    finally:
        with ff["connect"]() as conn:
            set_provider(conn, "fireflies", True, "pytest")
            conn.commit()
    with ff["connect"]() as conn:
        set_mode(conn, "paused", "pytest")
        conn.commit()
    try:
        with pytest.raises(EmptyScopeError, match="paused"):
            capture_meetings(ff["connect"], ff["client"], hours=24, profile=PROFILE, now=NOW)
        assert published(ff) == {}  # paused: nothing is readable
    finally:
        with ff["connect"]() as conn:
            set_mode(conn, "test", "pytest")
            conn.commit()
    assert len(published(ff)) == 3


def test_email_search_never_returns_meetings(ff: dict[str, Any]) -> None:
    graph = FixtureGraphClient.from_directory()
    capture_window(ff["connect"], graph, hours=744, include_sent=True, profile={"p": 1}, now=NOW)
    process_all(
        lambda: ff["connect"]("ci_ingest"),
        Backends(FakeScreener(), FakeClassifier(), EMBEDDER),
        workers=1,
    )
    with ff["connect"]("ci_search") as conn:
        result = retrieval.search(conn, READER, "framing truss delivery schedule", EMBEDDER)
        meeting = published(ff)["Example Ridge weekly OAC"]["communication_id"]
        assert retrieval.get_email(conn, READER, meeting) is None
    assert result["results"]  # email results still come back
    assert all(r["mailbox"] for r in result["results"])
    assert meeting not in {r["communication_id"] for r in result["results"]}


def test_scheduled_timers(
    ff: dict[str, Any], monkeypatch: pytest.MonkeyPatch, database: str
) -> None:
    monkeypatch.setenv("PGDATABASE", database)
    monkeypatch.setenv("DEPLOYMENT_ENVIRONMENT", "local")
    assert fireflies_tick(load_settings(), NOW).reason == "FIREFLIES_ENABLED app setting is false"
    assert fireflies_processing(load_settings()) is None
    monkeypatch.setenv("FIREFLIES_ENABLED", "true")
    assert fireflies_tick(load_settings(), NOW).reason == "capture is turned off"
    with ff["connect"]() as conn:
        setup.request_run(conn, "pytest", 168)
        conn.commit()
    decision = fireflies_tick(load_settings(), NOW)
    assert decision.due and decision.trigger == "manual"
    with ff["connect"]() as conn:
        run = conn.execute(
            "SELECT status, counts FROM ops_runs WHERE kind = 'fireflies_capture'"
            " ORDER BY id DESC LIMIT 1"
        ).fetchone()
        schedule = setup.read_schedule(conn)
    assert run is not None and run["status"] == "succeeded" and run["counts"]["listed"] == 5
    assert schedule.run_requested_at is None and schedule.last_capture_started_at == NOW
    assert fireflies_tick(load_settings(), NOW + timedelta(minutes=5)).reason == (
        "capture is turned off"
    )
    with ff["connect"]() as conn:
        setup.update_schedule(conn, "pytest", capture_enabled=True, capture_interval_min=60)
        conn.commit()
    assert fireflies_tick(load_settings(), NOW + timedelta(minutes=5)).reason == "not due yet"
    # The app's processing profile differs from this test's, so the scheduled capture
    # stored new versions; processing publishes them, then finds nothing more.
    assert run["counts"]["new_version"] == 3
    assert fireflies_processing(load_settings()) == {"published": 3}
    assert fireflies_processing(load_settings()) == {}
