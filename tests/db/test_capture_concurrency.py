"""Concurrent capture must not deadlock on shared participant rows.

The first real 57-day backfill failed with DeadlockDetected: parallel mailbox
workers upserted the same people in different orders (an upsert takes a row
lock that conflicts with other workers' foreign-key locks). Six threads here
store messages naming the same people in different orders.
"""

import copy
import json
import threading
from pathlib import Path
from typing import Any

import psycopg
import pytest

from ci_cli.seed import seed
from ci_core.db import connect
from ci_core.operator import approve_all_enrolled
from ci_core.runtime import eligible_mailboxes
from ci_email.capture import _store_with_retry, store_message

TEMPLATE = (
    Path(__file__).resolve().parents[1] / "fixtures/email/messages/01_invoice_action_required.json"
)
PEOPLE = [f"person{i:02d}@vendor.example" for i in range(12)]


def _message(index: int, people: list[str]) -> dict[str, Any]:
    message: dict[str, Any] = copy.deepcopy(json.loads(TEMPLATE.read_text())["message"])
    message["id"] = f"concurrency-{index}"
    message["internetMessageId"] = f"<concurrency-{index}@vendor.example>"
    # A different sender per message, as in real mail (a shared sender would
    # make every worker queue on that one row and hide the deadlock).
    sender = {"emailAddress": {"address": f"sender{index}@elsewhere.example", "name": None}}
    message["from"] = message["sender"] = sender
    message["toRecipients"] = [{"emailAddress": {"address": a, "name": a}} for a in people[:6]]
    message["ccRecipients"] = [{"emailAddress": {"address": a, "name": None}} for a in people[6:]]
    return message


def test_parallel_capture_of_shared_people_does_not_deadlock(database: str) -> None:
    with connect(dbname=database) as conn:
        seed(conn, "pytest")
        approve_all_enrolled(conn, "test", "pytest")
        conn.commit()
        scope = eligible_mailboxes(conn)[0]
        corpus = conn.execute("SELECT id::text FROM corpora LIMIT 1").fetchone()
        # Azure's database is a network hop away, so each message's transaction
        # stays open for many milliseconds. Simulate that so workers overlap.
        conn.execute(
            "CREATE OR REPLACE FUNCTION ci.test_latency() RETURNS trigger LANGUAGE plpgsql"
            " AS $$ BEGIN PERFORM pg_sleep(0.002); RETURN NEW; END $$"
        )
        conn.execute(
            "CREATE TRIGGER test_latency BEFORE INSERT ON ci.communication_participants"
            " FOR EACH ROW EXECUTE FUNCTION ci.test_latency()"
        )
        conn.commit()
    assert corpus is not None
    errors: list[BaseException] = []

    def worker(offset: int, people: list[str]) -> None:
        try:
            with connect(dbname=database, role="ci_ingest") as conn:
                for i in range(60):
                    store_message(
                        conn,
                        dataset="test",
                        scope=scope,
                        corpus_id=corpus["id"],
                        message=_message(offset + i, people),
                        attachments=[],
                        profile={"p": 1},
                    )
                    conn.commit()
        except BaseException as exc:
            errors.append(exc)

    # Six workers, like capture_workers in Azure, each naming the same people
    # in a different order.
    orders = [PEOPLE[k:] + PEOPLE[:k] for k in range(0, 12, 2)]
    threads = [
        threading.Thread(target=worker, args=(1000 * k, list(reversed(o)) if k % 2 else o))
        for k, o in enumerate(orders)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    with connect(dbname=database) as conn:
        conn.execute("DROP TRIGGER test_latency ON ci.communication_participants")
        conn.execute("DROP FUNCTION ci.test_latency()")
        conn.commit()
    assert errors == []
    with connect(dbname=database) as conn:
        stored = conn.execute(
            "SELECT count(*) AS n FROM participants WHERE email_normalized LIKE 'person%%'"
        ).fetchone()
    assert stored is not None and stored["n"] == len(PEOPLE)


class FakeConn:
    def __init__(self) -> None:
        self.commits = 0
        self.rollbacks = 0

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


def test_lock_conflicts_are_retried_then_given_up() -> None:
    conn = FakeConn()
    failures = [psycopg.errors.DeadlockDetected("x")]

    def store() -> str:
        if failures:
            raise failures.pop()
        return "new_version"

    assert _store_with_retry(conn, store, sleep=lambda _: None) == "new_version"  # type: ignore[arg-type]
    assert (conn.rollbacks, conn.commits) == (1, 1)

    def always() -> str:
        raise psycopg.errors.SerializationFailure("x")

    with pytest.raises(psycopg.errors.SerializationFailure):
        _store_with_retry(FakeConn(), always, attempts=3, sleep=lambda _: None)  # type: ignore[arg-type]
