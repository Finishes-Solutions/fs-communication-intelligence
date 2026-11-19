"""Email lists also carry the names as shown (migration 0034): ``properties_shown``
and ``entities_shown`` follow the operator's Knowledge merges and renames, while
``properties_ai`` and ``entities_ai`` keep the names as written (CLAUDE.md 3a).

Two synthetic emails are given the project name "3 Lakes" (one also names "Three
Lakes Estates") and two spellings of a fictional company. Merging "3 Lakes" into
"Three Lakes Estates" must show the merged name in ``properties_shown`` at once,
and unmerging, renaming and hiding must apply at once too."""

from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from typing import Any

import pytest

from ci_cli.seed import seed
from ci_core.db import AppRole, Connection, connect
from ci_core.operator import hide_knowledge, merge_knowledge, rename_knowledge, unmerge_knowledge
from ci_email.capture import capture_window
from ci_email.fixture import FixtureGraphClient
from ci_email.process import Backends, process_all
from ci_processing.classification import FakeClassifier
from ci_processing.embedding import FakeEmbedder
from ci_processing.knowledge import name_key
from ci_processing.screening import FakeScreener
from ci_retrieval import search
from ci_retrieval.identity import READER_ALL, Principal
from ci_retrieval.search import NO_FILTERS, authorize, list_email

NOW = datetime(2026, 10, 28, tzinfo=UTC)
READER = Principal("reader-oid", "reader@finishessolutions.example", (READER_ALL,))
Stack = Callable[[AppRole | None], Connection]

SHORT, FULL = "3 Lakes", "Three Lakes Estates"
ORG_SHORT, ORG_FULL = "Lakeside Example Builders", "Lakeside Example Builders Group"
# attachment that identifies the email -> (properties, entities) written in it
WRITTEN = {
    "Invoice-4471.pdf": ([SHORT], [ORG_SHORT]),
    "CO-007-BuildingC.pdf": ([SHORT, FULL], [ORG_SHORT, ORG_FULL]),
}
WATCHED = {SHORT, FULL}


@pytest.fixture(scope="module")
def stack(database: str) -> Iterator[Stack]:
    def connector(role: AppRole | None = None) -> Connection:
        return connect(dbname=database, role=role)

    with connector() as conn:
        seed(conn, "pytest")
        conn.commit()
    graph = FixtureGraphClient.from_directory()
    capture_window(connector, graph, hours=744, include_sent=True, profile={"p": 1}, now=NOW)
    process_all(
        lambda: connector("ci_ingest"),
        Backends(
            FakeScreener(), FakeClassifier(), FakeEmbedder(), graph=graph, extraction_enabled=True
        ),
        workers=1,
    )
    with connector() as conn:
        for attachment, (properties, entities) in WRITTEN.items():
            conn.execute(
                "UPDATE classification_runs r SET properties = %s, entities = %s"
                " FROM email_attachments a WHERE a.version_id = r.version_id AND a.name = %s",
                (properties, entities, attachment),
            )
        for kind, name in (
            ("property", SHORT),
            ("property", FULL),
            ("organization", ORG_SHORT),
            ("organization", ORG_FULL),
        ):
            conn.execute(
                "INSERT INTO kg_entities (kind, key, name) VALUES (%s, %s, %s)"
                " ON CONFLICT (kind, key) DO NOTHING",
                (kind, name_key(kind, name), name),
            )
        conn.commit()
    yield connector


def entity(stack: Stack, kind: str, name: str) -> int:
    with stack(None) as conn:
        row = conn.execute(
            "SELECT id FROM kg_entities WHERE kind = %s AND key = %s", (kind, name_key(kind, name))
        ).fetchone()
        assert row is not None
        return int(row["id"])


def correct(stack: Stack, change: Callable[[Connection], None]) -> None:
    with stack(None) as conn:
        change(conn)
        conn.commit()


def listed(stack: Stack) -> list[dict[str, Any]]:
    with stack("ci_search") as conn:
        return list(list_email(conn, READER, NO_FILTERS, 100)["results"])


def named(stack: Stack) -> dict[tuple[str, ...], dict[str, Any]]:
    """The two watched emails, keyed by the property names written in them."""
    rows = {
        tuple(r["properties_ai"]): r for r in listed(stack) if WATCHED & set(r["properties_ai"])
    }
    assert set(rows) == {(SHORT,), (SHORT, FULL)}
    return rows


@pytest.fixture
def merged(stack: Stack) -> Iterator[tuple[int, int]]:
    short, full = entity(stack, "property", SHORT), entity(stack, "property", FULL)
    correct(stack, lambda c: merge_knowledge(c, "pytest", short, full))
    try:
        yield short, full
    finally:
        correct(stack, lambda c: unmerge_knowledge(c, "pytest", short))


def test_before_any_merge_the_shown_names_equal_the_written_ones(stack: Stack) -> None:
    for r in listed(stack):
        assert r["properties_shown"] == r["properties_ai"]
        assert r["entities_shown"] == r["entities_ai"]


def test_preview_3_lakes_merged_into_three_lakes_estates_is_listed_by_the_merged_name(
    stack: Stack, merged: tuple[int, int]
) -> None:
    """AC1: the relay's demonstration. The email keeps "3 Lakes" as written."""
    row = named(stack)[(SHORT,)]
    assert row["properties_ai"] == ["3 Lakes"]
    assert row["properties_shown"] == ["Three Lakes Estates"]


def test_a_merged_name_and_its_target_in_one_email_are_shown_once(
    stack: Stack, merged: tuple[int, int]
) -> None:
    """AC3: an email naming both shows the merged name once, in the written order."""
    row = named(stack)[(SHORT, FULL)]
    assert row["properties_ai"] == [SHORT, FULL]
    assert row["properties_shown"] == [FULL]


def test_other_emails_are_unchanged_by_the_merge(stack: Stack, merged: tuple[int, int]) -> None:
    for r in listed(stack):
        if not WATCHED & set(r["properties_ai"]):
            assert r["properties_shown"] == r["properties_ai"]


def test_organizations_follow_merges_through_entities_shown(stack: Stack) -> None:
    """AC3: entities_shown follows organization merges the same way."""
    short, full = entity(stack, "organization", ORG_SHORT), entity(stack, "organization", ORG_FULL)
    correct(stack, lambda c: merge_knowledge(c, "pytest", short, full))
    try:
        rows = named(stack)
        assert rows[(SHORT,)]["entities_ai"] == [ORG_SHORT]
        assert rows[(SHORT,)]["entities_shown"] == [ORG_FULL]
        assert rows[(SHORT, FULL)]["entities_shown"] == [ORG_FULL]
        assert rows[(SHORT,)]["properties_shown"] == [SHORT]  # property names unmerged
    finally:
        correct(stack, lambda c: unmerge_knowledge(c, "pytest", short))


def test_unmerge_rename_and_hide_apply_at_once(stack: Stack) -> None:
    """AC2."""
    short, full = entity(stack, "property", SHORT), entity(stack, "property", FULL)
    correct(stack, lambda c: merge_knowledge(c, "pytest", short, full))
    try:
        assert named(stack)[(SHORT,)]["properties_shown"] == [FULL]

        correct(stack, lambda c: rename_knowledge(c, "pytest", full, "Three Lakes Estates Ph 2"))
        assert named(stack)[(SHORT,)]["properties_shown"] == ["Three Lakes Estates Ph 2"]
        correct(stack, lambda c: rename_knowledge(c, "pytest", full, None))

        correct(stack, lambda c: hide_knowledge(c, "pytest", full))  # hidden: as written
        assert named(stack)[(SHORT,)]["properties_shown"] == [SHORT]
        correct(stack, lambda c: hide_knowledge(c, "pytest", full, hidden=False))
        assert named(stack)[(SHORT,)]["properties_shown"] == [FULL]
    finally:
        correct(stack, lambda c: unmerge_knowledge(c, "pytest", short))
    for r in named(stack).values():
        assert r["properties_shown"] == r["properties_ai"]


class Spy:
    """A connection that counts the statements sent through it."""

    def __init__(self, conn: Connection | None) -> None:
        self.conn, self.sent = conn, 0

    def execute(self, *args: Any, **kwargs: Any) -> Any:
        self.sent += 1
        assert self.conn is not None, "no statement may be sent"
        return self.conn.execute(*args, **kwargs)


def test_before_migration_0034_the_copies_equal_the_written_names_and_nothing_is_asked(
    stack: Stack, merged: tuple[int, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC7: without 0034 (name_merges() false) no canonical_names query is sent."""
    monkeypatch.setattr(search, "_NAME_MERGES", False)
    monkeypatch.setattr(search, "_check_name_merges", lambda conn: None)
    rows = [{"properties_ai": ["3 Lakes", "3 LAKES"], "entities_ai": [ORG_SHORT]}]
    spy = Spy(None)
    search._shown_names(spy, rows)  # type: ignore[arg-type]
    assert spy.sent == 0
    assert rows[0]["properties_shown"] == ["3 Lakes", "3 LAKES"]
    assert rows[0]["entities_shown"] == [ORG_SHORT]
    for r in listed(stack):  # end to end, even though "3 Lakes" is merged
        assert r["properties_shown"] == r["properties_ai"]
        assert r["entities_shown"] == r["entities_ai"]


def test_a_full_page_is_mapped_with_one_query(stack: Stack, merged: tuple[int, int]) -> None:
    rows = [
        {
            "properties_ai": [SHORT, f"Lot {n}", f"Lot {n + 1}", FULL, "Elm Street"],
            "entities_ai": [ORG_SHORT, f"Vendor {n}", f"Vendor {n + 2}", ORG_FULL, "Example Co"],
        }
        for n in range(100)
    ]
    with stack("ci_search") as conn:
        authorize(conn, READER)  # notes migration 0034 for this process
        spy = Spy(conn)
        search._shown_names(spy, rows)  # type: ignore[arg-type]
    assert spy.sent == 1
    assert rows[7]["properties_shown"] == [FULL, "Lot 7", "Lot 8", "Elm Street"]
    assert rows[7]["entities_shown"] == rows[7]["entities_ai"]


def test_odd_names_do_not_break_the_list(stack: Stack, merged: tuple[int, int]) -> None:
    """Names come from email text: empty, very long, odd Unicode and SQL-looking
    names are bound as parameters and come back one for one (duplicates dropped)."""
    odd = [
        "",
        "x" * 500,
        "3 Lakes" + chr(0x200B),  # zero-width space
        chr(0xFF13) + " Lakes",  # full-width 3: NFKC makes it "3 Lakes"
        "Très Lacs \U0001f3e0",
        "'); DROP TABLE kg_entities; --",
        "Elm Street",
        "elm street",
    ]
    rows = [{"properties_ai": odd, "entities_ai": odd}]
    with stack("ci_search") as conn:
        authorize(conn, READER)
        search._shown_names(conn, rows)
    with stack(None) as conn:  # the SQL-looking name was data, not a statement
        count = conn.execute("SELECT count(*) AS n FROM kg_entities").fetchone()
    assert count is not None and count["n"] >= 4
    shown = rows[0]["properties_shown"]
    assert FULL in shown and "x" * 500 in shown and "'); DROP TABLE kg_entities; --" in shown
    assert len(shown) == len({s.casefold() for s in shown})
    assert rows[0]["properties_ai"] == odd  # the written names are untouched
    assert rows[0]["entities_shown"] == search.apply_shown_names(odd, "organization", {})
