"""The bulk pre-filter in processing: the synthetic newsletter is recorded as
not indexed with no screening, masking or classification call, everything else
is processed as before, and ``ci process --retry-skipped`` processes it in full.
"""

from collections.abc import Iterator, Sequence
from datetime import UTC, datetime
from typing import Any

import pytest

from ci_cli.seed import seed
from ci_core.db import AppRole, Connection, connect
from ci_email.capture import capture_window
from ci_email.fixture import FixtureGraphClient
from ci_email.process import Backends, process_all, requeue_skipped
from ci_processing.classification import ClassificationInput, ClassificationResult, FakeClassifier
from ci_processing.embedding import FakeEmbedder
from ci_processing.redaction import FakeRedactor, Redaction
from ci_processing.screening import FakeScreener, ScreeningResult

NEWSLETTER = "Regional construction weekly digest"


class Spy:
    """Records the subjects each paid step saw."""

    def __init__(self) -> None:
        self.screened: list[str] = []
        self.classified: list[str] = []
        self.masked = 0


def backends(spy: Spy, *, prefilter: bool = True) -> Backends:
    class Screener(FakeScreener):
        def screen(self, text: str) -> ScreeningResult:
            spy.screened.append(text.split("\n", 1)[0])
            return super().screen(text)

    class Classifier(FakeClassifier):
        def classify(self, item: ClassificationInput, known: Any = ()) -> ClassificationResult:
            spy.classified.append(item.subject)
            return super().classify(item, known)

    class Redactor(FakeRedactor):
        def redact(self, texts: Sequence[str]) -> Redaction:
            spy.masked += 1
            return super().redact(texts)

    return Backends(
        Screener(check_pii=False),
        Classifier(),
        FakeEmbedder(),
        graph=FixtureGraphClient.from_directory(),
        extraction_enabled=True,
        redactor=Redactor(),
        prefilter=prefilter,
    )


@pytest.fixture(scope="module")
def db(database: str) -> Iterator[tuple[str, Spy]]:
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
    spy = Spy()
    counts = process_all(lambda: connector("ci_ingest"), backends(spy), workers=1)
    assert counts["failed"] == 0
    yield database, spy


def state(conn: Connection, subject: str) -> dict[str, Any]:
    row = conn.execute(
        "SELECT v.id::text AS version_id, v.processing_state, v.hold_reason_code,"
        " (SELECT count(*) FROM classification_runs r WHERE r.version_id = v.id) AS runs,"
        " (SELECT count(*) FROM chunks k WHERE k.version_id = v.id) AS chunks"
        " FROM communications c JOIN communication_versions v ON v.communication_id = c.id"
        " WHERE c.title = %s ORDER BY v.version_no DESC LIMIT 1",
        (subject,),
    ).fetchone()
    assert row is not None
    return row


def test_newsletter_is_skipped_without_any_paid_call(db: tuple[str, Spy]) -> None:
    database, spy = db
    assert NEWSLETTER not in spy.screened
    assert NEWSLETTER not in spy.classified
    with connect(dbname=database) as conn:
        row = state(conn, NEWSLETTER)
        assert row["processing_state"] == "rejected"
        assert row["hold_reason_code"] == "not_indexed:bulk_unsubscribe"
        assert (row["runs"], row["chunks"]) == (0, 0)
        decision = conn.execute(
            "SELECT decision, reason_code, service_ref FROM policy_decisions"
            " WHERE version_id = %s AND check_name = 'prefilter'",
            (row["version_id"],),
        ).fetchone()
        assert decision == {
            "decision": "reject",
            "reason_code": "bulk_unsubscribe",
            "service_ref": "rules",
        }
        # Business mail is untouched.
        published = conn.execute(
            "SELECT count(*) AS n FROM communication_versions WHERE processing_state = 'published'"
        ).fetchone()
        assert published is not None and published["n"] > 5
        assert "Invoice" in " ".join(spy.classified)


def test_retry_skipped_processes_it_in_full(db: tuple[str, Spy]) -> None:
    database, _ = db
    with connect(dbname=database, role="ci_ingest") as conn:
        assert requeue_skipped(conn, "other.example") == 0
        assert requeue_skipped(conn, "@Builder-Digest.example") == 1
        conn.commit()
    spy = Spy()
    process_all(lambda: connect(dbname=database, role="ci_ingest"), backends(spy), workers=1)
    assert spy.screened == [NEWSLETTER] and spy.classified == [NEWSLETTER]
    with connect(dbname=database) as conn:
        row = state(conn, NEWSLETTER)
        # Now the classifier's own bulk rule decides (newsletters are not indexed).
        assert row["processing_state"] == "rejected" and row["runs"] == 1
        assert row["hold_reason_code"].startswith("not_indexed:")
        assert "bulk_unsubscribe" not in row["hold_reason_code"]
        assert conn.execute(
            "SELECT 1 FROM policy_decisions WHERE version_id = %s"
            " AND reason_code = 'operator_override'",
            (row["version_id"],),
        ).fetchone()
        assert requeue_skipped(conn) == 0  # nothing left that the pre-filter skipped


def test_the_per_version_lookup_reads_an_index(db: tuple[str, Spy]) -> None:
    """Migration 0035: processing asks policy_decisions about one version for
    every email. Without an index on version_id each lookup read the whole table
    (2.2 million rows on the dev server, 2026-11-18)."""
    import migrate
    from psycopg import sql

    database, _ = db
    # The statement in ci_email.process._prefilter.
    lookup = (
        "SELECT 1 FROM policy_decisions WHERE version_id = {} AND check_name = 'prefilter'"
        " AND reason_code = 'operator_override'"
    )

    def index_columns(conn: Connection) -> list[list[str]]:
        rows = conn.execute(
            "SELECT array_agg(a.attname::text ORDER BY k.ord) AS columns"
            " FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid"
            " CROSS JOIN LATERAL unnest(i.indkey) WITH ORDINALITY AS k(attnum, ord)"
            " JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = k.attnum"
            " WHERE i.indrelid = 'ci.policy_decisions'::regclass AND i.indisvalid"
            " AND c.relname = 'policy_decisions_version' GROUP BY c.oid"
        ).fetchall()
        return [row["columns"] for row in rows]

    def nodes(plan: dict[str, Any]) -> Iterator[dict[str, Any]]:
        yield plan
        for child in plan.get("Plans", []):
            yield from nodes(child)

    with connect(dbname=database) as conn:
        assert index_columns(conn) == [["version_id", "check_name"]]  # AC-2.1
        version_id = state(conn, NEWSLETTER)["version_id"]
        conn.execute("SET LOCAL enable_seqscan = off")
        explained = conn.execute(
            sql.SQL("EXPLAIN (FORMAT JSON) " + lookup).format(sql.Literal(version_id))
        ).fetchone()
        conn.rollback()
        assert explained is not None
        scans = [
            node
            for node in nodes(explained["QUERY PLAN"][0]["Plan"])
            if node.get("Index Name") == "policy_decisions_version"
        ]
        assert scans, explained  # AC-2.2
        assert scans[0]["Node Type"] in ("Index Scan", "Index Only Scan", "Bitmap Index Scan")
        assert "version_id" in scans[0]["Index Cond"]

    # AC-2.3: applying again does nothing, by the ledger and by the file itself.
    assert migrate.apply(database) == []
    (migration,) = [m for m in migrate.discover() if m.version == "0035"]
    migrate.psql(database, "--single-transaction", "-f", "-", stdin=migration.path.read_text())
    with connect(dbname=database) as conn:
        assert index_columns(conn) == [["version_id", "check_name"]]
