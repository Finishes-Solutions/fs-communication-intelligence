"""SENSITIVE_DATA_POLICY=mask: sensitive numbers and credentials are masked and the
message is published flagged for review; health information stays restricted; a
masking outage holds instead of publishing unmasked. Also: messages with several
readable attachments publish (chunk numbering) and remask requeues everything.
"""

from collections.abc import Iterator, Sequence
from datetime import UTC, datetime
from typing import Any

import pytest

from ci_cli.seed import seed
from ci_core.db import AppRole, Connection, connect
from ci_email.capture import capture_window
from ci_email.fixture import FixtureGraphClient
from ci_email.process import Backends, process_all, requeue_for_remask
from ci_processing.classification import FakeClassifier
from ci_processing.embedding import FakeEmbedder
from ci_processing.redaction import FakeRedactor, Redaction, RedactionUnavailable
from ci_processing.screening import FakeScreener

SSN = "900-00-0000"
KEY = "sk-synthetic0000000000000000FAKE"


def backends(redactor: Any = None) -> Backends:
    return Backends(
        FakeScreener(check_pii=False),
        FakeClassifier(),
        FakeEmbedder(),
        graph=FixtureGraphClient.from_directory(),
        extraction_enabled=True,
        redactor=redactor or FakeRedactor(),
    )


class Outage:
    def redact(self, texts: Sequence[str]) -> Redaction:
        raise RedactionUnavailable("test outage")


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
    process_all(lambda: connector("ci_ingest"), backends(), workers=1)
    yield database


def message(conn: Connection, subject_part: str) -> dict[str, Any]:
    row = conn.execute(
        "SELECT c.id::text AS communication_id, v.id::text AS version_id, v.processing_state,"
        " v.hold_reason_code, r.disposition, r.reason_codes"
        " FROM communications c JOIN communication_versions v ON v.communication_id = c.id"
        " LEFT JOIN classification_runs r ON r.version_id = v.id"
        " WHERE c.title LIKE %s ORDER BY v.version_no DESC LIMIT 1",
        (f"%{subject_part}%",),
    ).fetchone()
    assert row is not None, subject_part
    return row


def stored_text(conn: Connection, version_id: str) -> str:
    rows = conn.execute(
        "SELECT content FROM version_texts WHERE version_id = %s"
        " UNION ALL SELECT display_text FROM chunks WHERE version_id = %s",
        (version_id, version_id),
    ).fetchall()
    return "\n".join(r["content"] for r in rows)


def test_sensitive_numbers_and_credentials_are_masked_and_published(db: str) -> None:
    with connect(dbname=db) as conn:
        for subject, secret, reason in (
            ("New hire paperwork", SSN, "USSocialSecurityNumber"),
            ("Integration settings", KEY, "credential"),
        ):
            m = message(conn, subject)
            assert m["processing_state"] == "published", m
            text = stored_text(conn, m["version_id"])
            assert secret not in text and text
            published = conn.execute(
                "SELECT content FROM published_texts WHERE communication_id = %s",
                (m["communication_id"],),
            ).fetchall()
            assert published and all(secret not in r["content"] for r in published)
            decision = conn.execute(
                "SELECT reason_code FROM policy_decisions"
                " WHERE version_id = %s AND check_name = 'redaction'",
                (m["version_id"],),
            ).fetchone()
            assert decision is not None and reason in decision["reason_code"]
        # The fake classifier still sees "api_key:" and flags credentials -> review, not hidden.
        key = message(conn, "Integration settings")
        assert key["disposition"] == "review" and "credentials" in key["reason_codes"]


def test_masking_outage_holds_then_remask_publishes(db: str) -> None:
    def ingest() -> Connection:
        return connect(dbname=db, role="ci_ingest")

    with connect(dbname=db) as conn:  # a message that failed earlier is requeued too
        failed = message(conn, "Change Order 7 - Building C")["version_id"]
        conn.execute(
            "UPDATE communication_versions SET processing_state = 'failed',"
            " last_error_code = 'UniqueViolation' WHERE id = %s",
            (failed,),
        )
        conn.commit()
    with ingest() as conn:
        requeued = requeue_for_remask(conn)
        state = conn.execute(
            "SELECT processing_state FROM communication_versions WHERE id = %s", (failed,)
        ).fetchone()
        conn.commit()
    assert requeued >= 20 and state == {"processing_state": "fetched"}
    counts = process_all(ingest, backends(Outage()), workers=1)
    assert counts.get("published", 0) == 0 and counts.get("index", 0) == 0
    with connect(dbname=db) as conn:
        held = message(conn, "Integration settings")
        assert held["processing_state"] == "held"
        assert held["hold_reason_code"] == "held:redaction_pending"
        assert (
            conn.execute(
                "SELECT 1 FROM published_texts WHERE communication_id = %s",
                (held["communication_id"],),
            ).fetchone()
            is None
        )
    with ingest() as conn:
        requeue_for_remask(conn)
        conn.commit()
    counts = process_all(ingest, backends(), workers=1)
    assert counts.get("failed", 0) == 0
    with connect(dbname=db) as conn:
        assert message(conn, "Integration settings")["processing_state"] == "published"


def test_several_readable_attachments_publish_with_chunks_from_each(db: str) -> None:
    def ingest() -> Connection:
        return connect(dbname=db, role="ci_ingest")

    with connect(dbname=db) as conn:
        # Several messages share the subject (replies, mailbox copies); take the one with the PDF.
        m = conn.execute(
            "SELECT a.version_id::text FROM email_attachments a"
            " WHERE a.native_attachment_id = 'att-0201' ORDER BY a.version_id LIMIT 1"
        ).fetchone()
        assert m is not None
        conn.execute(
            "INSERT INTO email_attachments (version_id, native_attachment_id, name, content_type,"
            " size_bytes, is_inline, extraction_status, extracted_chars)"
            " VALUES (%s, 'att-extra', 'Schedule.txt', 'text/plain', 40, false, 'extracted', 40)",
            (m["version_id"],),
        )
        conn.execute(
            "INSERT INTO version_texts (version_id, text_kind, ref, content, sha256)"
            " VALUES (%s, 'attachment', 'att-extra', 'Framing crew starts Monday at Building C.',"
            " 'x')",
            (m["version_id"],),
        )
        conn.commit()
    with ingest() as conn:
        requeue_for_remask(conn)
        conn.commit()
    counts = process_all(ingest, backends(), workers=1)
    assert counts.get("failed", 0) == 0, counts
    with connect(dbname=db) as conn:
        rows = conn.execute(
            "SELECT source_ref, seq FROM chunks WHERE version_id = %s"
            " AND chunk_kind = 'attachment_text' ORDER BY seq",
            (m["version_id"],),
        ).fetchall()
    assert {r["source_ref"] for r in rows} == {"att-0201", "att-extra"}
    assert [r["seq"] for r in rows] == list(range(len(rows)))
