"""Fireflies capture against a real database with the fixture team: scope rules,
unchanged reruns, edits, deletions, exclusions, backfill windows and the rolling tick."""

from datetime import UTC, datetime, timedelta

import pytest

from ci_cli.pipeline import capture_profile
from ci_cli.scheduled import fireflies_tick, run_backfill
from ci_cli.seed import seed
from ci_core import ops
from ci_core.db import AppRole, Connection, connect
from ci_core.operator import set_mode
from ci_core.runtime import EmptyScopeError
from ci_core.settings import load_settings
from ci_fireflies import inventory as ff
from ci_fireflies.capture import capture_window, withdraw_missing
from ci_fireflies.fixture import FixtureFirefliesClient, fixture_now

NOW = fixture_now()
SINCE = datetime(2026, 10, 1, tzinfo=UTC)


@pytest.fixture(autouse=True)
def env(monkeypatch: pytest.MonkeyPatch, database: str) -> None:
    monkeypatch.setenv("PGDATABASE", database)
    monkeypatch.setenv("DEPLOYMENT_ENVIRONMENT", "local")
    monkeypatch.setenv("CAPTURE_ENABLED", "true")
    monkeypatch.setenv("FIREFLIES_CAPTURE_ENABLED", "true")
    monkeypatch.setenv("FIREFLIES_BACKFILL_SINCE", "2026-01-01T00:00:00Z")


def connector(role: AppRole | None = None) -> Connection:
    return connect(role=role)


def rows(sql: str, *params: object) -> list[dict[str, object]]:
    with connect() as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def test_capture_stores_meetings_under_the_scopes_of_approved_members() -> None:
    with connect() as conn:
        seed(conn, "pytest")
        conn.commit()
    client = FixtureFirefliesClient.from_directory()
    profile = capture_profile(load_settings())
    result = capture_window(connector, client, since=SINCE, until=NOW, profile=profile)
    assert result.error is None
    # 6 meetings in October: 5 with approved members, the phone upload by an unapproved one.
    assert result.counts == {"listed": 6, "new_version": 5, "no_scope": 1}
    meetings = rows(
        "SELECT c.native_id, c.kind, c.duration_seconds, fm.sentence_count, fm.has_summary,"
        " (SELECT count(*) FROM communication_scopes cs WHERE cs.communication_id = c.id) AS scopes"
        " FROM communications c JOIN fireflies_meetings fm ON fm.communication_id = c.id"
        " ORDER BY c.native_id"
    )
    by_id = {m["native_id"]: m for m in meetings}
    assert by_id["fft-0001-weekly-ops"]["scopes"] == 2  # Pat and Morgan
    assert by_id["fft-0001-weekly-ops"]["duration_seconds"] == 1890
    assert by_id["fft-0001-weekly-ops"]["has_summary"] is True
    assert by_id["fft-0004-phone-upload"]["scopes"] == 0  # River is not approved
    texts = rows(
        "SELECT text_kind, left(content, 24) AS head FROM version_texts v"
        " JOIN communication_versions cv ON cv.id = v.version_id"
        " JOIN communications c ON c.id = cv.communication_id"
        " WHERE c.native_id = 'fft-0001-weekly-ops' ORDER BY text_kind"
    )
    assert [t["text_kind"] for t in texts] == ["provider_summary", "transcript"]
    assert texts[1]["head"] == "[00:00] Pat Example: Let"
    segments = rows(
        "SELECT count(*) AS n, min(start_ms) AS first FROM transcript_segments s"
        " JOIN communication_versions cv ON cv.id = s.version_id"
        " JOIN communications c ON c.id = cv.communication_id"
        " WHERE c.native_id = 'fft-0001-weekly-ops'"
    )
    assert segments[0]["n"] == 6 and segments[0]["first"] == 0
    people = rows(
        "SELECT cp.role, p.email_normalized FROM communication_participants cp"
        " JOIN participants p ON p.id = cp.participant_id"
        " JOIN communications c ON c.id = cp.communication_id"
        " WHERE c.native_id = 'fft-0002-lender-call' ORDER BY 1, 2"
    )
    pairs = {(r["role"], r["email_normalized"]) for r in people}
    assert ("organizer", "lena.banker@harborbank.example") in pairs

    # A rerun of the same window stores nothing new.
    again = capture_window(connector, client, since=SINCE, until=NOW, profile=profile)
    assert again.counts == {"listed": 6, "unchanged": 5, "no_scope": 1}

    # An edited transcript gets a second version; a deleted one is withdrawn by the sweep.
    client.advance()
    edited = capture_window(connector, client, since=SINCE, until=NOW, profile=profile)
    assert edited.counts == {"listed": 5, "new_version": 1, "unchanged": 3, "no_scope": 1}
    versions = rows(
        "SELECT count(*) AS n FROM communication_versions cv JOIN communications c"
        " ON c.id = cv.communication_id WHERE c.native_id = 'fft-0003-vendor-dispute'"
    )
    assert versions[0]["n"] == 2
    assert withdraw_missing(connector, client, since=SINCE, until=NOW) == 1
    state = rows(
        "SELECT lifecycle_state FROM communications WHERE native_id = 'fft-0007-deleted-later'"
    )
    assert state[0]["lifecycle_state"] == "deleted_at_source"

    # Item exclusion wins at capture; the email processor leaves meetings alone.
    with connect(role="ci_operator") as conn:
        ff.exclude_meeting(conn, "fft-0005-ssn-mention", "pytest", reason="fixture")
        conn.commit()
    third = capture_window(connector, client, since=SINCE, until=NOW, profile=profile)
    assert third.counts["excluded"] == 1
    from ci_email.process import _claim

    with connect(role="ci_ingest") as conn:
        assert _claim(conn) is None  # only meetings are 'fetched' here; email ignores them

    # Paused mode and a disabled source both refuse.
    with connect(role="ci_operator") as conn:
        set_mode(conn, "paused", "pytest")
        conn.commit()
    with pytest.raises(EmptyScopeError):
        capture_window(connector, client, since=SINCE, until=NOW, profile=profile)


def test_backfill_plan_and_rolling_tick_run_fireflies_windows() -> None:
    with connect() as conn:
        seed(conn, "pytest")
        set_mode(conn, "test", "pytest")  # the previous test left the mode paused
        conn.commit()
    with connect(role="ci_operator") as conn:
        summary = ops.plan_backfill(
            conn,
            "pytest",
            since_day="2025-11-01",
            until_day=None,
            days=93,
            now=NOW,
            source="fireflies",
        )
        # The email plan is separate: it can coexist.
        ops.plan_backfill(conn, "pytest", since_day="2026-10-01", until_day=None, days=93, now=NOW)
        conn.commit()
    assert summary["source"] == "fireflies" and summary["windows"] == 4
    decision = run_backfill(load_settings(), NOW)
    # 4 fireflies windows plus the email one, which is refused (no mailbox approved)
    # without stopping the other source.
    assert decision.due and "5 window" in decision.reason
    with connect(role="ci_operator") as conn:
        status = ops.backfill_status(conn)
        runs = ops.recent_runs(conn, 20)
    assert status["by_source"]["fireflies"] == {"done": 4}
    kinds = {r["kind"] for r in runs}
    assert "fireflies_capture" in kinds
    stored = rows("SELECT count(*) AS n FROM communications WHERE kind = 'meeting'")
    assert stored[0]["n"] == 7  # the old November 2025 meeting is in the plan's range

    # The rolling tick: runs once, then waits for the interval.
    settings = load_settings()
    first = fireflies_tick(settings, NOW + timedelta(hours=1))
    assert first.due and "succeeded" in first.reason
    second = fireflies_tick(settings, NOW + timedelta(hours=1, minutes=5))
    assert not second.due and "recently" in second.reason
    third = fireflies_tick(settings, NOW + timedelta(hours=3))
    assert third.due


def test_release_after_restart_requeues_the_killed_window_at_once() -> None:
    with connect() as conn:
        seed(conn, "pytest")
        set_mode(conn, "test", "pytest")
        conn.commit()
    with connect(role="ci_operator") as conn:
        ops.cancel_backfill(conn, "pytest")
        ops.plan_backfill(conn, "pytest", since_day="2026-10-01", until_day=None, days=93, now=NOW)
        conn.commit()
    # A window is claimed, then the host restarts before the capture records its end.
    with connect(role="ci_ingest") as conn:
        window = ops.claim_backfill_window(conn, NOW)
        conn.commit()
    assert window is not None
    with connect(role="ci_ingest") as conn:
        assert ops.capture_in_progress(conn, NOW)  # without release: blocked for 130 min
    with connect(role="ci_operator") as conn:
        released = ops.release_running_captures(conn, "pytest")
        conn.commit()
    assert released == {"runs": 1, "windows": 1}
    with connect(role="ci_ingest") as conn:
        assert not ops.capture_in_progress(conn, NOW)
        pending = ops.next_backfill_window(conn)
    assert pending is not None and pending.id == window.id
    assert pending.error_code == "rerun_after_released_after_restart"
