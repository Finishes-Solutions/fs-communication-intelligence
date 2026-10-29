"""The admin console's switches for the paid checks (Prompt Shields, masking):
with both off no paid check runs, mail the classifier marks as sensitive is
restricted instead of masked, each message records what was skipped, and
``ci process --rescreen`` redoes it with the checks on.
"""

from collections.abc import Iterator, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import pytest

from ci_cli.backends import with_switches
from ci_cli.seed import seed
from ci_core import ops
from ci_core.db import AppRole, Connection, connect
from ci_core.settings import Settings
from ci_email.capture import capture_window
from ci_email.fixture import FixtureGraphClient
from ci_email.process import Backends, process_all, requeue_unscreened
from ci_processing.classification import FakeClassifier
from ci_processing.embedding import FakeEmbedder
from ci_processing.redaction import FakeRedactor, Redaction
from ci_processing.screening import DISABLED, FakeScreener, ScreeningResult

SETTINGS = Settings(deployment_environment="local")


class Calls:
    screened = 0
    masked = 0


class Screener(FakeScreener):
    def screen(self, text: str) -> ScreeningResult:
        Calls.screened += 1
        return super().screen(text)


class Redactor(FakeRedactor):
    def redact(self, texts: Sequence[str]) -> Redaction:
        Calls.masked += 1
        return super().redact(texts)


def configured() -> Backends:
    return Backends(
        Screener(check_pii=False),
        FakeClassifier(),
        FakeEmbedder(),
        graph=FixtureGraphClient.from_directory(),
        extraction_enabled=True,
        redactor=Redactor(),
    )


@pytest.fixture(scope="module")
def db(database: str) -> Iterator[str]:
    def connector(role: AppRole | None = None) -> Connection:
        return connect(dbname=database, role=role)

    with connector() as conn:
        seed(conn, "pytest")
        schedule = ops.update_schedule(
            conn, "pytest", prompt_shield_enabled=False, masking_enabled=False
        )
        conn.commit()
    capture_window(
        connector,
        FixtureGraphClient.from_directory(),
        hours=744,
        include_sent=True,
        profile={"p": 1},
        now=datetime(2026, 10, 28, tzinfo=UTC),
    )
    backends = with_switches(configured(), SETTINGS, schedule)
    counts = process_all(lambda: connector("ci_ingest"), backends, workers=1)
    assert counts["failed"] == 0 and counts["index"] > 0
    yield database


def outcome(conn: Connection, subject_part: str) -> dict[str, Any]:
    row = conn.execute(
        "SELECT v.id::text AS version_id, v.processing_state, v.hold_reason_code"
        " FROM communications c JOIN communication_versions v ON v.communication_id = c.id"
        " WHERE c.title LIKE %s ORDER BY v.version_no DESC LIMIT 1",
        (f"%{subject_part}%",),
    ).fetchone()
    assert row is not None, subject_part
    return row


def test_switches_are_saved_and_audited(db: str) -> None:
    with connect(dbname=db) as conn:
        schedule = ops.read_schedule(conn)
        assert (schedule.prompt_shield_enabled, schedule.masking_enabled) == (False, False)
        audit = conn.execute(
            "SELECT details FROM audit_events WHERE action = 'schedule_change'"
            " ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert audit is not None
        assert audit["details"] == {"prompt_shield_enabled": False, "masking_enabled": False}


def test_no_paid_check_runs_and_sensitive_mail_is_restricted(db: str) -> None:
    assert (Calls.screened, Calls.masked) == (0, 0)
    with connect(dbname=db) as conn:
        ssn = outcome(conn, "New hire paperwork")
        assert ssn["processing_state"] == "rejected"
        assert ssn["hold_reason_code"].startswith("restricted:")
        skipped = conn.execute(
            "SELECT check_name FROM policy_decisions WHERE version_id = %s"
            " AND service_ref = %s ORDER BY 1",
            (ssn["version_id"], DISABLED),
        ).fetchall()
        assert [r["check_name"] for r in skipped] == ["prompt_shield", "redaction"]
        invoice = outcome(conn, "Invoice")
        assert invoice["processing_state"] == "published"


def test_rescreen_redoes_it_with_the_checks_on(db: str) -> None:
    with connect(dbname=db) as conn:
        schedule = ops.update_schedule(
            conn, "pytest", prompt_shield_enabled=True, masking_enabled=True
        )
        conn.commit()
    with connect(dbname=db, role="ci_ingest") as conn:
        assert requeue_unscreened(conn) > 5
        conn.commit()
    backends = with_switches(configured(), SETTINGS, schedule)
    process_all(lambda: connect(dbname=db, role="ci_ingest"), backends, workers=1)
    assert Calls.screened > 5 and Calls.masked > 5
    with connect(dbname=db) as conn:
        # Masked this time, so published and flagged for review instead of restricted.
        ssn = outcome(conn, "New hire paperwork")
        assert ssn["processing_state"] == "published"
        assert requeue_unscreened(conn) == 0
        conn.rollback()


def test_restrict_policy_ignores_the_switches(db: str) -> None:
    restrict = Settings(deployment_environment="local", sensitive_data_policy="restrict")
    with connect(dbname=db) as conn:
        schedule = ops.read_schedule(conn)
    off = replace(schedule, prompt_shield_enabled=False, masking_enabled=False)
    base = configured()
    applied = with_switches(base, restrict, off)
    assert applied.screener is base.screener and applied.redactor is base.redactor
    assert not applied.masking_disabled
    assert applied.review_examples == schedule.review_examples  # not a paid check


def test_schedule_reads_with_unknown_columns(db: str) -> None:
    with connect(dbname=db) as conn:
        conn.execute("ALTER TABLE ops_settings ADD COLUMN some_future_setting integer")
        assert ops.read_schedule(conn).time_zone
        conn.rollback()


def test_console_saves_before_the_migration(db: str) -> None:
    """Code is deployed before migration 0012 is applied: reading defaults the
    switches to on, and saving with them unchanged does not touch their columns."""
    with connect(dbname=db) as conn:
        conn.execute(
            "ALTER TABLE ops_settings DROP COLUMN prompt_shield_enabled,"
            " DROP COLUMN masking_enabled"
        )
        schedule = ops.read_schedule(conn)
        assert (schedule.prompt_shield_enabled, schedule.masking_enabled) == (True, True)
        saved = ops.update_schedule(
            conn,
            "pytest",
            processing_enabled=not schedule.processing_enabled,
            prompt_shield_enabled=True,
            masking_enabled=True,
        )
        assert saved.processing_enabled is not schedule.processing_enabled
        conn.rollback()
