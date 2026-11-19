"""The ``ci`` command: guards, exit codes and committed changes."""

from types import SimpleNamespace
from typing import Any

import pytest

from ci_cli import backends, pipeline
from ci_cli.main import main
from ci_core.db import connect


@pytest.fixture(autouse=True)
def env(monkeypatch: pytest.MonkeyPatch, database: str) -> None:
    monkeypatch.setenv("PGDATABASE", database)
    monkeypatch.setenv("DEPLOYMENT_ENVIRONMENT", "local")
    monkeypatch.setenv("CI_OPERATOR", "pytest-cli")


def test_seed_refuses_outside_local(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("DEPLOYMENT_ENVIRONMENT", "development")
    assert main(["seed"]) == 1
    assert "DEPLOYMENT_ENVIRONMENT=local" in capsys.readouterr().err


def test_seed_then_scope_and_mode(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["seed"]) == 0
    assert main(["scope", "list"]) == 0
    listing = capsys.readouterr().out
    assert "casey.private@finishessolutions.example" in listing
    assert main(["mode", "set", "production"]) == 1
    assert "--confirm-production" in capsys.readouterr().err
    assert (
        main(
            ["scope", "include", "pat.example@finishessolutions.example", "--dataset", "production"]
        )
        == 1
    )
    assert main(["mode", "set", "paused"]) == 0
    assert main(["mode", "show"]) == 0
    assert "mode: paused" in capsys.readouterr().out
    with connect() as conn:
        row = conn.execute(
            "SELECT actor_id FROM audit_events WHERE action = 'mode_change'"
            " ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert row == {"actor_id": "pytest-cli"}


def test_failed_command_commits_nothing(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["seed"]) == 0
    with connect() as conn:
        before = conn.execute("SELECT count(*) AS n FROM audit_events").fetchone()
    assert main(["scope", "exclude", "pat.example@finishessolutions.example"]) == 1
    assert "--reason" in capsys.readouterr().err
    with connect() as conn:
        assert conn.execute("SELECT count(*) AS n FROM audit_events").fetchone() == before


def test_runs_lists_recent_runs_backlog_and_failures(capsys: pytest.CaptureFixture[str]) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM ops_runs")
        conn.execute(
            "INSERT INTO ops_runs (kind, trigger, window_hours, started_at, finished_at, status,"
            " counts, error_code) VALUES"
            " ('capture', 'schedule', 2, '2026-10-29 13:00Z', '2026-10-29 13:01:30Z',"
            "  'succeeded', '{\"stored\": 12, \"mailboxes\": 40}', NULL),"
            " ('process', 'schedule', NULL, '2026-10-29 13:05Z', '2026-10-29 13:05:20Z',"
            "  'failed', '{}', 'AzureServiceError'),"
            " ('process', 'manual', NULL, '2026-10-29 13:10Z', NULL, 'running', '{}', NULL)"
        )
        conn.commit()
    assert main(["runs"]) == 0
    out = capsys.readouterr().out
    lines = out.splitlines()
    assert lines[0].startswith("STARTED (") and "STATUS" in lines[0]
    assert "running" in lines[1]  # newest first
    assert "error=AzureServiceError" in lines[2]
    assert "mailboxes=40, stored=12" in lines[3] and "1.5 min" in lines[3]
    assert lines[3].startswith("2026-10-29 08:00:00")  # shown in the schedule's time zone
    assert "waiting to be processed:" in out
    assert main(["runs", "--failed", "--limit", "5"]) == 0
    failed = capsys.readouterr().out
    assert "AzureServiceError" in failed and "succeeded" not in failed
    with connect() as conn:
        conn.execute("DELETE FROM ops_runs")
        conn.commit()


@pytest.mark.parametrize("command", [["capture", "--hours", "1"], ["process", "--limit", "1"]])
def test_capture_and_process_recount_the_stored_counts(
    monkeypatch: pytest.MonkeyPatch, command: list[str]
) -> None:
    """``ci capture`` and ``ci process`` record no run in ops_runs, so they ask for a
    recount themselves (migration 0038): counts an hour old are recounted even
    though no run finished since. Mail and the AI steps are stubbed out."""
    monkeypatch.setattr(pipeline, "capture_window", lambda *a, **k: [])
    monkeypatch.setattr(backends, "graph_client", lambda settings: None)
    monkeypatch.setattr(backends, "with_model", lambda settings, schedule: settings)
    monkeypatch.setattr(backends, "processing_backends", lambda settings: None)
    monkeypatch.setattr(
        backends,
        "with_switches",
        lambda backends, settings, schedule: SimpleNamespace(meter=None),
    )
    monkeypatch.setattr(backends, "risk_rater", lambda *a: None)
    monkeypatch.setattr(backends, "action_resolver", lambda *a: None)
    monkeypatch.setattr(pipeline, "process_all", lambda *a, **k: {})
    monkeypatch.setattr(pipeline, "rate_pending_risks", lambda *a, **k: {})
    monkeypatch.setattr(pipeline, "resolve_pending_actions", lambda *a, **k: {})

    def counted_at() -> Any:
        with connect() as conn:
            row = conn.execute("SELECT min(refreshed_at) AS t FROM ops_counts").fetchone()
            conn.rollback()
        return row and row["t"]

    with connect() as conn:
        conn.execute("SELECT refresh_ops_counts(changed => true)")
        conn.execute("UPDATE ops_counts SET refreshed_at = refreshed_at - interval '1 hour'")
        conn.execute("UPDATE ops_runs SET finished_at = finished_at - interval '1 day'")
        conn.commit()
    before = counted_at()
    assert before is not None
    assert main(command) == 0
    assert counted_at() > before
