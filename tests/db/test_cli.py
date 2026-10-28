"""The ``ci`` command: guards, exit codes and committed changes."""

import pytest

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
