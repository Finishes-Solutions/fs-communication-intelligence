"""The ingestion Function App jobs: switches, empty-scope refusal, then a full run."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest

from ci_cli import scheduled
from ci_cli.pipeline import include_all_enrolled, tenant_init
from ci_cli.scheduled import run_capture, run_processing, scheduler_tick
from ci_core import ops
from ci_core.db import connect
from ci_core.operator import exclude_mailbox
from ci_core.settings import load_settings

# The synthetic fixtures are dated up to 2026-10-28; captures end there, not today.
NOW = datetime(2026, 10, 28, 15, 0, tzinfo=UTC)


def stored_counts() -> int:
    """How many of the six stored counts (migration 0038) exist."""
    with connect() as conn:
        row = conn.execute("SELECT count(*) AS n FROM ops_counts").fetchone()
        conn.rollback()
    assert row is not None
    return int(row["n"])


def forget_stored_counts() -> None:
    with connect() as conn:
        conn.execute("DELETE FROM ops_counts")
        conn.commit()


@pytest.fixture(autouse=True)
def env(monkeypatch: pytest.MonkeyPatch, database: str) -> None:
    monkeypatch.setenv("PGDATABASE", database)
    monkeypatch.setenv("DEPLOYMENT_ENVIRONMENT", "local")
    monkeypatch.setenv("CAPTURE_HOURS", "744")
    monkeypatch.setenv("ATTACHMENT_EXTRACTION_ENABLED", "true")


def test_jobs_are_off_until_switched_on() -> None:
    settings = load_settings()
    assert run_capture(settings) is None
    assert run_processing(settings) is None


def test_first_run_inventories_then_refuses_until_mailboxes_are_approved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CAPTURE_ENABLED", "true")
    monkeypatch.setenv("PROCESSING_ENABLED", "true")
    with connect() as conn:
        tenant_init(conn, "pytest")
    assert run_capture(load_settings(), NOW) is None  # inventory ran; nothing approved yet
    with connect() as conn:
        include_all_enrolled(conn, "pytest")
        exclude_mailbox(
            conn, "casey.private@finishessolutions.example", "pytest", reason="personal_mailbox"
        )
    # Ticks with nothing to do may already have stored counts (every tick
    # refreshes them, and an empty table is always counted): start empty.
    forget_stored_counts()
    captured = run_capture(load_settings(), NOW)
    assert captured is not None and captured["new_version"] == 20
    assert stored_counts() == 0  # run_capture (tests only) is not a tick
    processed = run_processing(load_settings())
    assert processed is not None
    assert stored_counts() == 6  # AC-3.7: a processing run that did work recounts
    rated = processed.pop("risks_rated", 0)  # the risk-matrix rating step, after processing
    assert sum(processed.values()) == 20 and rated > 0
    again = run_capture(load_settings(), NOW)
    assert again is not None and again.get("new_version", 0) == 0


def test_scheduler_tick_follows_the_schedule_and_records_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CAPTURE_ENABLED", "true")
    now = NOW
    assert scheduler_tick(load_settings(), now).reason == "capture is turned off"
    with connect(role="ci_operator") as conn:
        ops.update_schedule(
            conn, "pytest", capture_enabled=True, capture_window_hours=744, capture_interval_min=60
        )
        conn.commit()
    forget_stored_counts()
    first = scheduler_tick(load_settings(), now)
    assert first.due and first.trigger == "schedule"
    assert stored_counts() == 6  # AC-3.7: a capture recounts
    assert scheduler_tick(load_settings(), now + timedelta(minutes=5)).reason == "not due yet"
    with connect(role="ci_operator") as conn:
        ops.request_run(conn, "pytest", 744)
        conn.commit()
    manual = scheduler_tick(load_settings(), now + timedelta(minutes=10))
    assert manual.due and manual.trigger == "manual"
    with connect(role="ci_operator") as conn:
        runs = ops.recent_runs(conn)
        settings_row = ops.read_schedule(conn)
        ops.update_schedule(conn, "pytest", capture_enabled=False)
        conn.commit()
    captures = [r for r in runs if r["kind"] == "capture"]
    assert [r["trigger"] for r in captures[:2]] == ["manual", "schedule"]
    assert all(r["status"] in ("succeeded", "partial") for r in captures[:2])
    assert all("mailboxes_enrolled" in r["counts"] for r in captures[:2])
    assert settings_row.run_requested_at is None


def test_runs_complete_on_a_database_without_the_stored_counts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-3.7: the code is deployed before migration 0038 is applied. A capture and
    a processing run that did work must end as before."""
    monkeypatch.setenv("CAPTURE_ENABLED", "true")
    monkeypatch.setenv("PROCESSING_ENABLED", "true")
    # Nothing is queued by now; a pass that reports work reaches the recount.
    monkeypatch.setattr(scheduled, "process_all", lambda *args, **kwargs: {"index": 1})
    migration = Path(__file__).resolve().parents[2] / "db/migrations/0038_ops_counts.sql"
    with connect() as conn:
        conn.execute("DROP VIEW ci.coverage_snapshot")
        conn.execute("DROP FUNCTION ci.refresh_ops_counts(interval, boolean)")
        conn.execute("DROP TABLE ci.ops_counts")
        conn.commit()
    try:
        with connect(role="ci_operator") as conn:
            ops.request_run(conn, "pytest", 744)
            conn.commit()
        tick = scheduler_tick(load_settings(), NOW + timedelta(minutes=20))
        assert tick.due and tick.trigger == "manual"
        processed = run_processing(load_settings())
        assert processed is not None and processed["index"] == 1
        with connect(role="ci_operator") as conn:
            runs = ops.recent_runs(conn, 2)
            assert ops.overview(conn)["counts_as_of"] is None
            conn.rollback()
        assert {r["kind"] for r in runs} == {"capture", "process"}
        assert all(r["status"] == "succeeded" for r in runs)
    finally:
        with connect() as conn:
            conn.execute(migration.read_text())
            conn.commit()
    assert stored_counts() == 0
    assert run_processing(load_settings()) is not None and stored_counts() == 6


def test_app_kill_switch_beats_the_schedule(monkeypatch: pytest.MonkeyPatch) -> None:
    with connect(role="ci_operator") as conn:
        ops.request_run(conn, "pytest", 1)
        conn.commit()
    decision = scheduler_tick(load_settings())  # CAPTURE_ENABLED is not set
    assert not decision.due and "CAPTURE_ENABLED" in decision.reason
    with connect() as conn:
        conn.execute("UPDATE ops_settings SET run_requested_at = NULL")
        conn.commit()


def counted_at() -> datetime:
    with connect() as conn:
        row = conn.execute("SELECT min(refreshed_at) AS t FROM ops_counts").fetchone()
        conn.rollback()
    assert row is not None and row["t"] is not None
    return cast(datetime, row["t"])


def test_idle_ticks_recount_after_the_last_run_of_a_busy_period() -> None:
    """Every scheduler and processing tick refreshes the stored counts, also when
    it does no work (both switches off here): a run that finished after the last
    recount is counted once that recount is 5 minutes old; with no run finished
    since, an idle tick counts nothing."""
    with connect() as conn:
        conn.execute("SELECT refresh_ops_counts(changed => true)")
        conn.execute(
            "UPDATE ops_runs SET started_at = started_at - interval '1 hour',"
            " finished_at = finished_at - interval '1 hour' WHERE finished_at IS NOT NULL"
        )
        # The last run of a busy period ended a minute after a recount 3 minutes ago.
        conn.execute("UPDATE ops_counts SET refreshed_at = now() - interval '3 minutes'")
        conn.execute(
            "INSERT INTO ops_runs (kind, trigger, started_at, finished_at, status)"
            " VALUES ('process', 'schedule', now() - interval '150 seconds',"
            " now() - interval '2 minutes', 'succeeded')"
        )
        conn.commit()
    young = counted_at()
    assert run_processing(load_settings()) is None  # PROCESSING_ENABLED is not set
    assert counted_at() == young  # under 5 minutes old: not yet
    with connect() as conn:
        conn.execute("UPDATE ops_counts SET refreshed_at = refreshed_at - interval '5 minutes'")
        conn.commit()
    assert not scheduler_tick(load_settings(), NOW).due  # CAPTURE_ENABLED is not set
    recounted = counted_at()
    assert recounted > young  # the idle tick recounted
    with connect() as conn:  # 10 minutes later, nothing has run since
        conn.execute("UPDATE ops_counts SET refreshed_at = refreshed_at - interval '10 minutes'")
        conn.execute(
            "UPDATE ops_runs SET started_at = started_at - interval '1 hour',"
            " finished_at = finished_at - interval '1 hour' WHERE finished_at IS NOT NULL"
        )
        conn.commit()
    stale = counted_at()
    assert run_processing(load_settings()) is None
    assert not scheduler_tick(load_settings(), NOW).due
    assert counted_at() == stale  # nothing changed: nothing counted
