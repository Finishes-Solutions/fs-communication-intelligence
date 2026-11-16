"""The set-based reads of migration 0031 (ci.published_window and the chunk
functions) against the published_* views they replace: every list, report and
search gives the same answer either way, for every kind of reader, and the lists
page. Synthetic fixtures only; how fast each is on a large database is measured
separately (docs/DATABASE.md).
"""

import json
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from ci_cli.seed import seed
from ci_core.db import AppRole, Connection, connect
from ci_core.operator import limit_reader
from ci_email.capture import capture_window
from ci_email.fixture import FixtureGraphClient
from ci_email.process import Backends, process_all
from ci_processing.classification import FakeClassifier
from ci_processing.embedding import FakeEmbedder
from ci_processing.screening import FakeScreener
from ci_retrieval import reports
from ci_retrieval import search as retrieval
from ci_retrieval.identity import READER_ALL, READER_OWN, READER_RISKS, Principal

NOW = datetime(2026, 10, 28, tzinfo=UTC)
PAT = "pat.example@finishessolutions.example"
MORGAN = "morgan.lee@finishessolutions.example"
EMBEDDER = FakeEmbedder(1536)
READERS = {
    # A sign-in address: while any reader limit exists, a token without one gets nothing.
    "all": Principal("reader-oid", "reader@finishessolutions.example", (READER_ALL,)),
    "own": Principal("pat-oid", PAT, (READER_OWN,)),
    # Pat's mailbox is hidden from Morgan, except the emails Morgan is on.
    "limited": Principal("morgan-oid", MORGAN, (READER_ALL,)),
    "risk_only": Principal("agent-oid", "agent@finishessolutions.example", (READER_RISKS,)),
    "nobody": Principal("nobody-oid", None, ()),
}
F = retrieval.Filters


def days(n: int) -> datetime:
    return NOW - timedelta(days=n)


@pytest.fixture(scope="module")
def stack(database: str) -> Iterator[Callable[[AppRole | None], Connection]]:
    def connector(role: AppRole | None = None) -> Connection:
        return connect(dbname=database, role=role)

    with connector() as conn:
        seed(conn, "pytest")
        conn.commit()
    graph = FixtureGraphClient.from_directory()
    capture_window(connector, graph, hours=744, include_sent=True, profile={"p": 1}, now=NOW)
    process_all(
        lambda: connector("ci_ingest"),
        Backends(FakeScreener(), FakeClassifier(), EMBEDDER, graph=graph, extraction_enabled=True),
        workers=1,
    )
    with connector() as conn:
        limit_reader(conn, MORGAN, "mailbox", PAT, "pytest", reason="private_business")
        conn.commit()
    yield connector


@pytest.fixture(autouse=True)
def fixture_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(retrieval, "utcnow", lambda: NOW)


def both_ways(
    stack: Callable[[AppRole | None], Connection],
    monkeypatch: pytest.MonkeyPatch,
    call: Callable[[Connection], Any],
) -> tuple[Any, Any]:
    """``call`` on the set-based functions, then through the views (as before 0031)."""
    answers = []
    for fast in (True, False):
        with monkeypatch.context() as patch:
            if not fast:
                patch.setattr(retrieval, "fast_reads", lambda conn: False)
                patch.setattr(reports, "fast_reads", lambda conn: False)
            with stack("ci_search") as conn:
                answers.append(call(conn))
                conn.rollback()
    return answers[0], answers[1]


Call = Callable[[Connection], Any]


def thread_of(who: Principal, communication_id: str) -> Call:
    return lambda c: retrieval.get_thread(c, who, communication_id)


def actionable_of(who: Principal, person: str | None) -> Call:
    return lambda c: retrieval.list_actionable(
        c, who, person=person, hours=24 * 60, limit=100, status="all"
    )


def groups_of(
    who: Principal, dataset: str, group: str, filters: retrieval.Filters, person: str | None
) -> Call:
    return lambda c: reports.report_groups(c, who, dataset, group, filters, person=person)


def rows_of(who: Principal, dataset: str, filters: retrieval.Filters, person: str | None) -> Call:
    return lambda c: reports.report_rows(c, who, dataset, filters, person=person, limit=500)


def search_of(who: Principal, query: str, filters: retrieval.Filters) -> Call:
    return lambda c: retrieval.search(c, who, query, EMBEDDER, filters, 25)


def plain(value: Any) -> str:
    """An answer without the parts that are not about the emails found."""
    if isinstance(value, dict):
        value = {k: v for k, v in value.items() if k != "coverage"}
    return json.dumps(value, sort_keys=True, default=str)


LISTS: dict[str, retrieval.Filters] = {
    "everything": F(),
    "a period": F(since=days(20), until=days(2)),
    "a label": F(label="project_risk"),
    "a label in a period": F(label="action_required", since=days(400)),
    "a mailbox": F(mailbox=PAT),
    "a sender": F(sender="jordan.rivera@acme-sitework-billing.example"),
    "a property": F(property="lot 12"),
    "an entity": F(entity="llc"),
    "nothing": F(sender="nobody@nowhere.example"),
}


@pytest.mark.parametrize("reader", list(READERS))
@pytest.mark.parametrize("case", list(LISTS))
def test_lists_match_the_views(
    stack: Callable[[AppRole | None], Connection],
    monkeypatch: pytest.MonkeyPatch,
    reader: str,
    case: str,
) -> None:
    fast, views = both_ways(
        stack, monkeypatch, lambda c: retrieval.list_email(c, READERS[reader], LISTS[case], 100)
    )

    # Copies of one message arrive at the same moment; which copy stands for the
    # group is not defined, so the emails are compared without their order.
    def ids(found: dict[str, Any]) -> list[str]:
        return sorted(f"{r['subject']}|{r['received_at']}|{r['sender']}" for r in found["results"])

    assert ids(fast) == ids(views)
    if reader == "nobody":
        assert fast["results"] == []
    if reader == "limited" and case == "everything":
        assert len(fast["results"]) < len(everything(stack))  # the limit hides some
    if reader == "risk_only":
        assert all("project_risk" in r["labels"] for r in fast["results"])
    if reader == "own":
        assert {r["mailbox"] for r in fast["results"]} <= {PAT}


def everything(stack: Callable[[AppRole | None], Connection]) -> list[dict[str, Any]]:
    with stack("ci_search") as conn:
        found: list[dict[str, Any]] = retrieval.list_email(conn, READERS["all"], F(), 100)[
            "results"
        ]
        conn.rollback()
    return found


def test_a_list_has_results_for_the_filters_used_above(
    stack: Callable[[AppRole | None], Connection],
) -> None:
    """Guards the comparison above against comparing two empty answers."""
    with stack("ci_search") as conn:
        for case, filters in LISTS.items():
            found = retrieval.list_email(conn, READERS["all"], filters, 100)["results"]
            assert bool(found) == (case != "nothing"), case


def test_lists_page_with_an_offset(
    stack: Callable[[AppRole | None], Connection], monkeypatch: pytest.MonkeyPatch
) -> None:
    for fast in (True, False):
        with monkeypatch.context() as patch:
            if not fast:
                patch.setattr(retrieval, "fast_reads", lambda conn: False)
            with stack("ci_search") as conn:
                whole = retrieval.list_email(conn, READERS["all"], F(), 100)
                pages = [
                    retrieval.list_email(conn, READERS["all"], F(), 4, offset)
                    for offset in range(0, 16, 4)
                ]
        every = [r["communication_id"] for r in whole["results"]]
        assert len(every) == 13 and not whole["has_more"]
        assert [r["communication_id"] for p in pages for r in p["results"]] == every
        assert [p["has_more"] for p in pages] == [True, True, True, False]
        assert [p["offset"] for p in pages] == [0, 4, 8, 12]


def test_threads_and_action_items_match_the_views(
    stack: Callable[[AppRole | None], Connection], monkeypatch: pytest.MonkeyPatch
) -> None:
    with stack("ci_search") as conn:
        emails = retrieval.list_email(conn, READERS["all"], F(), 100)["results"]
    for reader in ("all", "limited", "own"):
        for email in emails:
            fast, views = both_ways(
                stack, monkeypatch, thread_of(READERS[reader], email["communication_id"])
            )
            assert plain(fast) == plain(views)
        for person in (None, PAT, f"{PAT},{MORGAN}"):
            fast, views = both_ways(stack, monkeypatch, actionable_of(READERS[reader], person))
            for part in ("actions", "possible", "risks"):
                assert sorted(i["subject"] for i in fast[part]) == sorted(
                    i["subject"] for i in views[part]
                )


@pytest.mark.parametrize("reader", ["all", "limited", "own", "risk_only"])
def test_reports_match_the_views(
    stack: Callable[[AppRole | None], Connection],
    monkeypatch: pytest.MonkeyPatch,
    reader: str,
) -> None:
    who = READERS[reader]
    filters = [F(), F(since=days(30)), F(label="invoice_receipt"), F(mailbox=PAT, since=days(400))]
    seen = 0
    for dataset in reports.DATASETS:
        for f in filters:
            for person in (None, PAT) if dataset == "actions" else (None,):
                for group in reports.GROUPS:
                    fast, views = both_ways(
                        stack, monkeypatch, groups_of(who, dataset, group, f, person)
                    )
                    assert plain(fast) == plain(views), (dataset, group, f)
                    seen += len(fast["groups"])
                fast, views = both_ways(stack, monkeypatch, rows_of(who, dataset, f, person))
                assert fast["total"] == views["total"]
                assert sorted(plain({**r, "communication_id": ""}) for r in fast["rows"]) == sorted(
                    plain({**r, "communication_id": ""}) for r in views["rows"]
                )
    assert seen  # the reports were not all empty


def test_filtered_searches_find_the_same_emails(
    stack: Callable[[AppRole | None], Connection], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Two candidates from each index, so every search below needs the emails in scope.
    monkeypatch.setattr(retrieval, "NEAREST_K", 2)
    cases = [
        ("change order lease renewal invoice", F()),
        ("invoice payment due", F(label="invoice_receipt")),
        ("invoice payment due", F(mailbox=PAT)),
        ("lien notice attorney", F(since=days(30))),
        ("grading compaction", F(source="documents")),
        ("grading compaction", F(source="email", property="lot 12")),
    ]
    found = 0
    for reader in ("all", "limited", "own"):
        for query, filters in cases:
            fast, views = both_ways(stack, monkeypatch, search_of(READERS[reader], query, filters))
            assert {r["subject"] for r in fast["results"]} == {
                r["subject"] for r in views["results"]
            }, (reader, query, filters)
            found += len(fast["results"])
    assert found


def test_the_chunk_functions_return_only_published_chunks(
    stack: Callable[[AppRole | None], Connection],
) -> None:
    """Whatever ids they are given: a caller cannot use them to read a chunk or an
    email the published_* views do not show."""
    with stack(None) as conn:
        every = [r["id"] for r in conn.execute("SELECT id::text FROM communications").fetchall()]
        chunks = [r["id"] for r in conn.execute("SELECT id::text FROM chunks").fetchall()]
        # Withdraw one email that has chunks: it must disappear from every function.
        gone = conn.execute(
            "UPDATE communications SET lifecycle_state = 'withdrawn' WHERE id ="
            " (SELECT communication_id FROM chunks LIMIT 1) RETURNING id::text"
        ).fetchone()
        assert gone is not None
        vector = str(EMBEDDER.embed(["invoice"])[0])
        conn.execute("SET ROLE ci_search")
        visible = {
            r["id"] for r in conn.execute("SELECT id::text FROM published_chunks").fetchall()
        }
        emails = {
            r["id"]
            for r in conn.execute("SELECT id::text FROM published_communications").fetchall()
        }
        assert gone["id"] not in emails and visible and len(visible) < len(chunks)
        for sql, args in (
            ("SELECT id::text FROM published_chunk_rows(%s::uuid[])", (chunks,)),
            ("SELECT id::text FROM published_nearest(%s::vector, 400)", (vector,)),
            (
                "SELECT id::text FROM published_nearest_in(%s::vector, %s::uuid[], 400)",
                (vector, every),
            ),
            ("SELECT id::text FROM published_lexical('invoice OR lease OR lien', 400)", ()),
            (
                "SELECT id::text FROM published_lexical_in('invoice OR lease OR lien',"
                " %s::uuid[], 400)",
                (every,),
            ),
        ):
            got = {r["id"] for r in conn.execute(sql, args).fetchall()}
            assert got and got <= visible, sql
        for sql, args in (
            ("SELECT communication_id::text AS id FROM published_window()", ()),
            ("SELECT communication_id::text AS id FROM published_ids()", ()),
            (
                "SELECT communication_id::text AS id FROM published_window(NULL, NULL, %s::uuid[])",
                (every,),
            ),
        ):
            assert {r["id"] for r in conn.execute(sql, args).fetchall()} == emails, sql
        conn.rollback()


def test_the_migration_applies_without_superuser(
    stack: Callable[[AppRole | None], Connection],
) -> None:
    """Azure gives the migrating login no superuser (as for 0015): the functions'
    planner settings must be ones an ordinary role may set."""
    path = Path(__file__).resolve().parents[2] / "db/migrations/0031_fast_windows.sql"
    with stack(None) as conn:
        for row in conn.execute(
            "SELECT oid::regprocedure::text AS f FROM pg_proc"
            " WHERE pronamespace = 'ci'::regnamespace AND proname IN ('published_window',"
            " 'published_ids', 'published_nearest', 'published_lexical',"
            " 'published_nearest_in', 'published_lexical_in', 'published_chunk_rows')"
        ).fetchall():
            conn.execute(f"DROP FUNCTION {row['f']}")
        conn.execute("CREATE ROLE pytest_migrator_31 NOSUPERUSER")
        conn.execute("GRANT USAGE, CREATE ON SCHEMA ci TO pytest_migrator_31")
        conn.execute("GRANT SELECT ON ALL TABLES IN SCHEMA ci TO pytest_migrator_31")
        conn.execute("GRANT ci_search TO pytest_migrator_31 WITH ADMIN OPTION")
        conn.execute("SET ROLE pytest_migrator_31")
        conn.execute(path.read_text())
        conn.execute("SET ROLE ci_search")
        vector = str(EMBEDDER.embed(["change order"])[0])
        near = conn.execute(
            "SELECT count(*) AS n FROM published_nearest(%s::vector, 200)", (vector,)
        ).fetchone()
        window = conn.execute("SELECT count(*) AS n FROM published_window()").fetchone()
        conn.rollback()
    assert near is not None and near["n"] > 0
    assert window is not None and window["n"] > 0
