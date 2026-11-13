"""Messages processed with attachment extraction off (a helper without Graph access)
are requeued by `ci process --extract-pending` and get their attachments read by a
worker that has extraction on."""

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest

from ci_cli.seed import seed
from ci_core.db import AppRole, Connection, connect
from ci_email.capture import capture_window
from ci_email.fixture import FixtureGraphClient
from ci_email.process import Backends, process_all, requeue_unextracted_attachments
from ci_processing.classification import FakeClassifier
from ci_processing.embedding import FakeEmbedder
from ci_processing.screening import FakeScreener

NOW = datetime(2026, 10, 28, tzinfo=UTC)


@pytest.fixture(scope="module")
def stack(database: str) -> Iterator[dict[str, Any]]:
    def connector(role: AppRole | None = None) -> Connection:
        return connect(dbname=database, role=role)

    with connector() as conn:
        seed(conn, "pytest")
        conn.commit()
    graph = FixtureGraphClient.from_directory()
    capture_window(connector, graph, hours=744, include_sent=True, profile={"p": 1}, now=NOW)
    yield {"connect": connector, "graph": graph}


def attachment_states(conn: Connection) -> dict[str, int]:
    return {
        r["extraction_status"]: int(r["n"])
        for r in conn.execute(
            "SELECT a.extraction_status, count(*) AS n FROM email_attachments a"
            " JOIN communication_versions v ON v.id = a.version_id"
            " WHERE v.processing_state = 'published' AND NOT a.is_inline GROUP BY 1"
        ).fetchall()
    }


def test_catch_up_reads_attachments_skipped_by_an_extraction_off_worker(
    stack: dict[str, Any],
) -> None:
    connector = stack["connect"]
    without = Backends(FakeScreener(), FakeClassifier(), FakeEmbedder(1536))  # no graph
    assert not without.extraction_enabled
    process_all(lambda: connector("ci_ingest"), without, workers=1)
    with connector("ci_ingest") as conn:
        before = attachment_states(conn)
        assert before.get("not_attempted", 0) > 0 and "extracted" not in before
        requeued = requeue_unextracted_attachments(conn)
        conn.commit()
    assert requeued > 0
    with_graph = Backends(
        FakeScreener(),
        FakeClassifier(),
        FakeEmbedder(1536),
        graph=stack["graph"],
        extraction_enabled=True,
    )
    counts = process_all(lambda: connector("ci_ingest"), with_graph, workers=1)
    assert counts["index"] >= requeued
    with connector("ci_ingest") as conn:
        after = attachment_states(conn)
        assert requeue_unextracted_attachments(conn) == 0  # nothing left to catch up
    assert after.get("not_attempted", 0) == 0 and after.get("extracted", 0) > 0
