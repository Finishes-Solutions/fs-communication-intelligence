"""Runs that cannot hang or linger: database sessions carry keepalives and per-role
time limits, runs stopped without an end are closed as stale, and a processing
run reports its counts while it works."""

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest

from ci_cli.seed import seed
from ci_core import ops
from ci_core.db import KEEPALIVE, LIMITS, AppRole, Connection, connect
from ci_email.capture import capture_window
from ci_email.fixture import FixtureGraphClient
from ci_email.process import Backends, process_all
from ci_processing.classification import FakeClassifier
from ci_processing.embedding import FakeEmbedder
from ci_processing.screening import FakeScreener


@pytest.fixture(scope="module")
def db(database: str) -> Iterator[str]:
    def connector(role: AppRole | None = None) -> Connection:
        return connect(dbname=database, role=role)

    with connector() as conn:
        seed(conn, "pytest")
        conn.commit()
    capture_window(
        connector,
        FixtureGraphClient.from_directory(),
        hours=744,
        include_sent=True,
        profile={"p": 1},
        now=datetime(2026, 10, 28, tzinfo=UTC),
    )
    yield database


@pytest.mark.parametrize("role", ["ci_ingest", "ci_search", "ci_operator"])
def test_each_role_session_has_time_limits_and_keepalives(db: str, role: AppRole) -> None:
    with connect(dbname=db, role=role) as conn:
        statement = conn.execute("SHOW statement_timeout").fetchone()
        idle = conn.execute("SHOW idle_in_transaction_session_timeout").fetchone()
        params = conn.info.get_parameters()
    assert statement is not None and idle is not None
    assert (statement["statement_timeout"], idle["idle_in_transaction_session_timeout"]) == (
        LIMITS[role]
    )
    assert params.get("keepalives") == str(KEEPALIVE["keepalives"])
    assert params.get("connect_timeout") == str(KEEPALIVE["connect_timeout"])


def test_runs_stopped_without_an_end_are_closed_as_stale(db: str) -> None:
    now = datetime.now(UTC)
    with connect(dbname=db, role="ci_ingest") as conn:
        old = ops.start_run(conn, "process", "schedule")
        recent = ops.start_run(conn, "process", "schedule")
        capture = ops.start_run(conn, "capture", "manual", 24)
        conn.execute(
            "UPDATE ops_runs SET started_at = %s WHERE id = ANY(%s)",
            (now - timedelta(hours=3), [old, capture]),
        )
        assert ops.close_stale_runs(conn, "process", timedelta(minutes=85), now) == 1
        rows = {
            r["id"]: r
            for r in conn.execute(
                "SELECT id, status, error_code, finished_at FROM ops_runs WHERE id = ANY(%s)",
                ([old, recent, capture],),
            ).fetchall()
        }
        conn.rollback()
    assert (rows[old]["status"], rows[old]["error_code"]) == ("failed", "stale_run")
    assert rows[old]["finished_at"] is not None
    assert rows[recent]["status"] == "running"  # within its budget
    assert rows[capture]["status"] == "running"  # another kind of run


def test_processing_reports_progress_and_survives_a_failing_report(db: str) -> None:
    reports: list[dict[str, int]] = []

    def progress(counts: dict[str, int]) -> None:
        reports.append(dict(counts))
        if len(reports) == 1:
            raise RuntimeError("database unavailable for a moment")

    with connect(dbname=db, role="ci_ingest") as conn:
        run_id = ops.start_run(conn, "process", "schedule")
        conn.commit()
    counts = process_all(
        lambda: connect(dbname=db, role="ci_ingest"),
        Backends(FakeScreener(), FakeClassifier(), FakeEmbedder()),
        workers=2,
        progress=progress,
        progress_seconds=0,
    )
    assert sum(counts.values()) > 5
    assert len(reports) == sum(counts.values())  # every message, with progress_seconds=0
    assert sum(reports[-1].values()) == sum(counts.values())
    with connect(dbname=db, role="ci_ingest") as conn:
        ops.update_run_counts(conn, run_id, reports[-1])
        conn.commit()
        row = conn.execute(
            "SELECT status, counts FROM ops_runs WHERE id = %s", (run_id,)
        ).fetchone()
        ops.finish_run(conn, run_id, "succeeded", dict(counts))
        ops.update_run_counts(conn, run_id, {"index": 0})  # a finished run is left alone
        done = conn.execute("SELECT counts FROM ops_runs WHERE id = %s", (run_id,)).fetchone()
        conn.commit()
    assert row is not None and row["status"] == "running" and row["counts"] == reports[-1]
    assert done is not None and done["counts"] == dict(counts)
