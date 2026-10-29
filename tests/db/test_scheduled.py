"""The ingestion Function App jobs: switches, empty-scope refusal, then a full run."""

from datetime import UTC, datetime, timedelta

import pytest

from ci_cli.pipeline import include_all_enrolled, tenant_init
from ci_cli.scheduled import run_capture, run_processing, scheduler_tick
from ci_core import ops
from ci_core.db import connect
from ci_core.operator import exclude_mailbox
from ci_core.settings import load_settings


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
    assert run_capture(load_settings()) is None  # inventory ran; nothing approved yet
    with connect() as conn:
        include_all_enrolled(conn, "pytest")
        exclude_mailbox(
            conn, "casey.private@finishessolutions.example", "pytest", reason="personal_mailbox"
        )
    captured = run_capture(load_settings())
    assert captured is not None and captured["new_version"] == 20
    processed = run_processing(load_settings())
    assert processed is not None
    rated = processed.pop("risks_rated", 0)  # the risk-matrix rating step, after processing
    assert sum(processed.values()) == 20 and rated > 0
    again = run_capture(load_settings())
    assert again is not None and again.get("new_version", 0) == 0


def test_scheduler_tick_follows_the_schedule_and_records_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CAPTURE_ENABLED", "true")
    now = datetime(2026, 10, 28, 15, 0, tzinfo=UTC)
    assert scheduler_tick(load_settings(), now).reason == "capture is turned off"
    with connect(role="ci_operator") as conn:
        ops.update_schedule(
            conn, "pytest", capture_enabled=True, capture_window_hours=744, capture_interval_min=60
        )
        conn.commit()
    first = scheduler_tick(load_settings(), now)
    assert first.due and first.trigger == "schedule"
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


def test_app_kill_switch_beats_the_schedule(monkeypatch: pytest.MonkeyPatch) -> None:
    with connect(role="ci_operator") as conn:
        ops.request_run(conn, "pytest", 1)
        conn.commit()
    decision = scheduler_tick(load_settings())  # CAPTURE_ENABLED is not set
    assert not decision.due and "CAPTURE_ENABLED" in decision.reason
    with connect() as conn:
        conn.execute("UPDATE ops_settings SET run_requested_at = NULL")
        conn.commit()
