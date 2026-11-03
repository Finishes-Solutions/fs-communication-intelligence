"""An action item is marked resolved by a later reply in its conversation (migration
0026), copies share one call, the status reaches list_actionable under the reader's
own access, and resolved items leave the default list. Fake backends only."""

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest

from ci_cli.seed import seed
from ci_core.db import AppRole, Connection, connect
from ci_core.runtime import require_capture_scope
from ci_email.capture import capture_window, store_message
from ci_email.fixture import FixtureGraphClient
from ci_email.process import Backends, process_all
from ci_email.resolutions import resolve_pending_actions
from ci_processing.classification import FakeClassifier
from ci_processing.embedding import FakeEmbedder
from ci_processing.resolution import FakeActionResolver, Resolution, ResolutionInput
from ci_processing.screening import FakeScreener
from ci_retrieval import search as retrieval
from ci_retrieval.identity import READER_ALL, Principal

NOW = datetime(2026, 10, 28, tzinfo=UTC)
READER = Principal("reader-oid", None, (READER_ALL,))
PAT = "pat.example@finishessolutions.example"
CO8_SUBJECT = "Change Order 8 - Building D plumbing reroute"


class CountingResolver(FakeActionResolver):
    def __init__(self) -> None:
        self.calls: list[ResolutionInput] = []

    def resolve(self, item: ResolutionInput) -> Resolution:
        self.calls.append(item)
        return super().resolve(item)


def reply(received: str, body: str, suffix: str) -> dict[str, Any]:
    """A synthetic reply in the Change Order 8 conversation, as Graph JSON."""
    return {
        "id": f"AAkALgAAAAAAHYQDEapmEc2byACqAC-EWg0A-syn-co8-reply-{suffix}",
        "internetMessageId": f"<synthetic-co8-reply-{suffix}@finishessolutions.example>",
        "conversationId": "AAQkAGsynconvco8",
        "changeKey": f"CQAAABYAAAAsynreply{suffix}",
        "subject": "RE: " + CO8_SUBJECT,
        "from": {"emailAddress": {"name": "Pat Example", "address": PAT}},
        "sender": {"emailAddress": {"name": "Pat Example", "address": PAT}},
        "toRecipients": [
            {
                "emailAddress": {
                    "name": "Sam Ortiz",
                    "address": "sam.ortiz@northfield-framing.example",
                }
            }
        ],
        "ccRecipients": [],
        "bccRecipients": [],
        "receivedDateTime": received,
        "sentDateTime": received,
        "body": {"contentType": "text", "content": body},
        "uniqueBody": {"contentType": "text", "content": body},
        "hasAttachments": False,
        "isDraft": False,
        "webLink": "https://outlook.example/reply-" + suffix,
        "parentFolderId": "sentitems",
    }


@pytest.fixture(scope="module")
def stack(database: str) -> Iterator[dict[str, Any]]:
    def connector(role: AppRole | None = None) -> Connection:
        return connect(dbname=database, role=role)

    with connector() as conn:
        seed(conn, "pytest")
        conn.commit()
    graph = FixtureGraphClient.from_directory()
    capture_window(connector, graph, hours=744, include_sent=True, profile={"p": 1}, now=NOW)
    backends = Backends(FakeScreener(), FakeClassifier(), FakeEmbedder(1536), graph=graph)
    process_all(lambda: connector("ci_ingest"), backends, workers=1)
    yield {"connect": connector, "backends": backends}


def co8_items(conn: Connection, principal: Principal, status: str) -> list[dict[str, Any]]:
    found = retrieval.list_actionable(conn, principal, hours=744, limit=100, status=status)
    return [a for a in found["actions"] if a["subject"] == CO8_SUBJECT]


def store_reply(stack: dict[str, Any], message: dict[str, Any]) -> None:
    connector = stack["connect"]
    with connector("ci_ingest") as conn:
        scope = next(s for s in require_capture_scope(conn) if s.address == PAT)
        corpus = conn.execute(
            "SELECT corpus_id::text FROM source_scopes WHERE id = %s::uuid", (scope.scope_id,)
        ).fetchone()
        assert corpus is not None
        store_message(
            conn,
            dataset="test",
            scope=scope,
            corpus_id=corpus["corpus_id"],
            message=message,
            attachments=[],
            profile={"p": 1},
        )
        conn.commit()
    process_all(lambda: connector("ci_ingest"), stack["backends"], workers=1)


def test_nothing_is_checked_without_a_reply(stack: dict[str, Any]) -> None:
    connector = stack["connect"]
    resolver = CountingResolver()
    counts = resolve_pending_actions(lambda: connector("ci_ingest"), resolver)
    assert not counts and not resolver.calls
    with connector("ci_search") as conn:
        items = co8_items(conn, READER, "open")
    assert len(items) == 1  # Pat's and Morgan's copies collapse into one listed item
    assert items[0]["status"] == "open" and items[0]["resolution_ai"] is None


def test_a_reply_in_the_thread_is_read_once_per_copy_and_marks_progress(
    stack: dict[str, Any],
) -> None:
    connector = stack["connect"]
    store_reply(
        stack,
        reply("2026-10-13T09:00:00Z", "Got it, will review with the beam detail tomorrow.", "1"),
    )
    resolver = CountingResolver()
    counts = resolve_pending_actions(lambda: connector("ci_ingest"), resolver, workers=1)
    assert counts["actions_in_progress"] == 1 and counts["resolution_reused"] == 2
    assert len(resolver.calls) == 1  # the other two mailbox copies reused the answer by hash
    assert resolver.calls[0].replies[0].sender == PAT
    with connector("ci_search") as conn:
        items = co8_items(conn, READER, "open")
    assert {i["status"] for i in items} == {"in_progress"}
    # In progress names the reply that advanced it, when the reader may read it.
    assert all(i["resolution_ai"]["resolved_by"]["sender"] == PAT for i in items)
    assert not resolve_pending_actions(lambda: connector("ci_ingest"), resolver)  # nothing new


def test_a_settling_reply_resolves_it_and_names_the_reply_when_readable(
    stack: dict[str, Any],
) -> None:
    connector = stack["connect"]
    store_reply(
        stack, reply("2026-10-14T09:00:00Z", "Approved. Signed copy attached, please proceed.", "2")
    )
    counts = resolve_pending_actions(
        lambda: connector("ci_ingest"), FakeActionResolver(), workers=1
    )
    assert counts["actions_resolved"] + counts["resolution_reused"] == 3
    with connector("ci_search") as conn:
        assert co8_items(conn, READER, "open") == []
        resolved = co8_items(conn, READER, "resolved")
        everything = co8_items(conn, READER, "all")
        # A reader without a grant sees nothing at all, resolution included.
        nobody = retrieval.list_actionable(
            conn, Principal("nobody-oid", None, ()), hours=744, limit=100, status="all"
        )
    assert len(resolved) == 1 and len(everything) == 1
    for item in resolved:
        ai = item["resolution_ai"]
        assert ai["status"] == "resolved" and ai["reason"]
        assert ai["resolved_by"]["sender"] == PAT
        assert ai["resolved_by"]["subject"] == "RE: " + CO8_SUBJECT
    assert nobody["actions"] == []
    with connector("ci_ingest") as conn:
        row = conn.execute(
            "SELECT count(*) AS n FROM action_resolutions WHERE status = 'resolved'"
            " AND replies_seen = 2 AND resolver_communication_id IS NOT NULL"
        ).fetchone()
    assert row is not None and row["n"] == 3
    # Resolved items are never re-checked when more mail arrives. (The approval reply
    # itself reads as an approval request to the fake classifier, so it is a new action
    # item of its own and is checked against the later reply: open, nothing settles it.)
    store_reply(stack, reply("2026-10-15T09:00:00Z", "Thanks, invoice to follow.", "3"))
    again = resolve_pending_actions(lambda: connector("ci_ingest"), FakeActionResolver())
    assert dict(again) == {"actions_open": 1}
    with connector("ci_ingest") as conn:
        row = conn.execute(
            "SELECT count(*) AS n FROM action_resolutions WHERE status = 'resolved'"
            " AND checked_through = '2026-10-14T09:00:00Z'"
        ).fetchone()
    assert row is not None and row["n"] == 3
