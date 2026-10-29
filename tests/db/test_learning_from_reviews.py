"""The classifier learns from Label Review (migration 0023): reviewed, published
emails become examples with the reviewer's answers, corrections first, and reach
the classifier on the next processing run; withdrawn mail never does."""

from collections.abc import Iterator, Sequence
from datetime import UTC, datetime
from typing import Any

import pytest

from ci_cli.backends import with_switches
from ci_cli.seed import seed
from ci_core import ops
from ci_core.db import AppRole, Connection, connect, required
from ci_core.operator import OperatorError
from ci_core.settings import Settings
from ci_email.capture import capture_window
from ci_email.fixture import FixtureGraphClient
from ci_email.process import Backends, _requeue, process_all
from ci_email.review_examples import load_pool
from ci_processing.classification import (
    MAX_EXAMPLE_EXCERPT,
    ClassificationInput,
    ClassificationResult,
    FakeClassifier,
    KnownName,
)
from ci_processing.embedding import FakeEmbedder
from ci_processing.screening import FakeScreener

INVOICE = "Invoice 4471 for Lot 12 grading - payment due Friday"
NEWSLETTER = "Regional construction weekly digest"
LEASE = "Suite 210 lease renewal terms"


class Spy(FakeClassifier):
    def __init__(self) -> None:
        self.seen: list[ClassificationInput] = []

    def classify(
        self, item: ClassificationInput, known: Sequence[KnownName] = ()
    ) -> ClassificationResult:
        self.seen.append(item)
        return super().classify(item, known)


@pytest.fixture(scope="module")
def db(database: str) -> Iterator[dict[str, Any]]:
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
    process_all(
        lambda: connector("ci_ingest"), Backends(FakeScreener(), FakeClassifier(), FakeEmbedder())
    )
    ids = {}
    with connector() as conn:
        for title in (INVOICE, NEWSLETTER, LEASE):
            ids[title] = str(
                required(
                    conn.execute(
                        "SELECT id FROM communications WHERE title = %s ORDER BY id LIMIT 1",
                        (title,),
                    ).fetchone()
                )["id"]
            )
    yield {"connect": connector, "ids": ids}


def answer(conn: Connection, cid: str, label: str, applies: bool) -> None:
    conn.execute(
        "INSERT INTO label_feedback (communication_id, subject_kind, subject, applies,"
        " reviewer) VALUES (%s, 'label', %s, %s, 'matthew@finishessolutions.example')"
        " ON CONFLICT (communication_id, subject_kind, subject, reviewer)"
        " DO UPDATE SET applies = EXCLUDED.applies, updated_at = now()",
        (cid, label, applies),
    )


def test_reviewed_mail_becomes_examples_and_reaches_the_classifier(db: dict[str, Any]) -> None:
    connector, ids = db["connect"], db["ids"]
    with connector("ci_operator") as conn:
        # The fake model says invoice_receipt yes and urgent no for the invoice:
        # the reviewer agrees on the first and corrects the second.
        answer(conn, ids[INVOICE], "invoice_receipt", True)
        answer(conn, ids[INVOICE], "urgent", True)
        answer(conn, ids[INVOICE], "spam", False)
        answer(conn, ids[LEASE], "cat_leasing_operations", True)
        answer(conn, ids[NEWSLETTER], "newsletter", True)
        conn.commit()
    with connector("ci_ingest") as conn:
        pool = {c.communication_id: c for c in load_pool(conn)}
    invoice = pool[ids[INVOICE]].example
    assert invoice.yes == ("invoice_receipt", "urgent") and invoice.no == ("spam",)
    assert invoice.ai_was_wrong_on == ("urgent",)
    assert invoice.subject == INVOICE and 0 < len(invoice.excerpt) <= MAX_EXAMPLE_EXCERPT
    assert invoice.sender_domain and "@" not in invoice.sender_domain
    assert not pool[ids[LEASE]].corrects or pool[ids[LEASE]].example.ai_was_wrong_on
    # A newsletter is recorded but not published (not indexed): never an example.
    assert ids[NEWSLETTER] not in pool

    # Reprocess the lease with examples on: the classifier sees the invoice's
    # correction, and the lease is never its own example.
    with connector("ci_ingest") as conn:
        version = required(
            conn.execute(
                "SELECT active_version_id::text AS v FROM communications WHERE id = %s",
                (ids[LEASE],),
            ).fetchone()
        )["v"]
        _requeue(conn, [version])
        conn.commit()
    spy = Spy()
    with connector() as conn:
        schedule = ops.read_schedule(conn)
    assert schedule.review_examples == 6  # the migration's default
    backends = with_switches(
        Backends(FakeScreener(), spy, FakeEmbedder()),
        Settings(deployment_environment="local"),
        schedule,
    )
    process_all(lambda: connector("ci_ingest"), backends, workers=1)
    [lease] = [i for i in spy.seen if i.subject == LEASE]
    assert lease.reviewed and lease.reviewed[0].subject == INVOICE  # the correction first
    assert all(x.subject != LEASE for x in lease.reviewed)


def test_withdrawn_mail_is_dropped_and_zero_turns_it_off(db: dict[str, Any]) -> None:
    connector, ids = db["connect"], db["ids"]
    with connector() as conn:
        c = required(
            conn.execute(
                "SELECT connection_id, native_id FROM communications WHERE id = %s",
                (ids[INVOICE],),
            ).fetchone()
        )
        conn.execute(
            "INSERT INTO exclusions (exclusion_kind, connection_id, native_id, reason_code,"
            " created_by) VALUES ('item', %s, %s, 'test', 'pytest')",
            (c["connection_id"], c["native_id"]),
        )
        with connector("ci_ingest") as ingest:
            assert ids[INVOICE] in {x.communication_id for x in load_pool(ingest)}
        conn.commit()
        with connector("ci_ingest") as ingest:
            assert ids[INVOICE] not in {x.communication_id for x in load_pool(ingest)}
        conn.execute("DELETE FROM exclusions WHERE reason_code = 'test'")
        conn.commit()
    with connector("ci_operator") as conn:
        assert ops.update_schedule(conn, "pytest", review_examples=0).review_examples == 0
        for bad in (13, -1, True, 2.5):
            with pytest.raises(OperatorError):
                ops.update_schedule(conn, "pytest", review_examples=bad)
        ops.update_schedule(conn, "pytest", review_examples=6)
        conn.commit()
