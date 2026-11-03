"""A backfill plan is captured window by window from the scheduler tick, resumes
after a stopped host, and can be cancelled."""

from datetime import UTC, datetime, timedelta

import pytest

from ci_cli.pipeline import include_all_enrolled, tenant_init
from ci_cli.scheduled import run_backfill, scheduler_tick
from ci_core import ops
from ci_core.db import connect
from ci_core.operator import OperatorError
from ci_core.settings import load_settings

# The synthetic fixtures are dated 2025-12-15 to 2026-10-16.
NOW = datetime(2026, 10, 28, 15, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def env(monkeypatch: pytest.MonkeyPatch, database: str) -> None:
    monkeypatch.setenv("PGDATABASE", database)
    monkeypatch.setenv("DEPLOYMENT_ENVIRONMENT", "local")
    monkeypatch.setenv("CAPTURE_ENABLED", "true")


def test_plan_is_captured_newest_first_and_a_rerun_stores_nothing_new() -> None:
    with connect() as conn:
        tenant_init(conn, "pytest")
        conn.commit()
    with connect(role="ci_operator") as conn:
        summary = ops.plan_backfill(
            conn, "pytest", since_day="2025-12-01", until_day=None, days=93, now=NOW
        )
        conn.commit()
    # Nothing is approved yet: the first window is refused and recorded, the rest wait.
    refused = scheduler_tick(load_settings(), NOW)
    assert refused.due and "1 window" in refused.reason
    with connect(role="ci_operator") as conn:
        status = ops.backfill_status(conn)
        assert status["states"] == {"failed": 1, "pending": 3}
        assert status["recent"][0]["error_code"] == "refused_EmptyScopeError"
        assert ops.retry_failed_windows(conn, "pytest") == 1
        conn.commit()
    with connect() as conn:  # the refused tick ran the inventory; now approve the mailboxes
        include_all_enrolled(conn, "pytest")
        conn.commit()
    with connect(role="ci_operator") as conn:
        with pytest.raises(OperatorError, match="unfinished window"):
            ops.plan_backfill(
                conn, "pytest", since_day="2025-01-01", until_day=None, days=7, now=NOW
            )
        conn.commit()
    assert summary["windows"] == 4  # 331 days and some hours in 93-day windows

    decision = scheduler_tick(load_settings(), NOW)  # the schedule itself is off
    assert decision.due and decision.trigger == "backfill" and "4 window" in decision.reason

    with connect(role="ci_operator") as conn:
        status = ops.backfill_status(conn)
        runs = [r for r in ops.recent_runs(conn) if r["kind"] == "capture"]
    assert status["states"] == {"done": 4} and status["pending_since"] is None
    assert [r["trigger"] for r in runs] == ["backfill"] * 5  # four windows and the refusal
    assert [r["status"] for r in runs] == ["succeeded"] * 4 + ["refused"]
    stored = sum((r["counts"] or {}).get("new_version", 0) for r in runs)
    assert stored >= 20  # every fixture message in the period, each mailbox copy once
    # Newest window first: it ran first (so it is the oldest of the four runs) and is
    # a full 93 days; the oldest window is the remainder of the period.
    assert runs[3]["window_hours"] == 93 * 24 and runs[0]["window_hours"] < 93 * 24

    with connect(role="ci_operator") as conn:
        ops.plan_backfill(conn, "pytest", since_day="2025-12-01", until_day=None, days=93, now=NOW)
        conn.commit()
    assert run_backfill(load_settings(), NOW).due
    with connect(role="ci_operator") as conn:
        again = [r for r in ops.recent_runs(conn) if r["kind"] == "capture"][:4]
    assert sum((r["counts"] or {}).get("new_version", 0) for r in again) == 0
    assert all(r["status"] == "succeeded" for r in again)


def test_a_window_left_running_by_a_stopped_host_is_rerun() -> None:
    with connect(role="ci_operator") as conn:
        ops.plan_backfill(conn, "pytest", since_day="2026-10-01", until_day=None, days=93, now=NOW)
        conn.commit()
    with connect(role="ci_ingest") as conn:
        window = ops.claim_backfill_window(conn, NOW - timedelta(hours=3))
        assert window is not None and window.state == "running"
        conn.execute(
            "UPDATE ops_runs SET started_at = %s WHERE id = %s",
            (NOW - timedelta(hours=3), window.run_id),
        )
        conn.commit()
    with connect(role="ci_ingest") as conn:
        assert ops.requeue_stale_windows(conn) == 0  # its run still looks alive
        assert ops.close_stale_runs(conn, "capture", ops.RUNNING_CAPTURE_GRACE, NOW) == 1
        assert ops.requeue_stale_windows(conn) == 1
        rerun = ops.next_backfill_window(conn)
        conn.commit()
    assert rerun is not None and rerun.id == window.id and rerun.state == "pending"
    assert rerun.error_code == "rerun_after_stale_run"
    with connect(role="ci_operator") as conn:
        assert ops.cancel_backfill(conn, "pytest") == 1
        assert ops.backfill_status(conn)["states"].get("pending") is None
        conn.commit()
    assert scheduler_tick(load_settings(), NOW).reason == "capture is turned off"
