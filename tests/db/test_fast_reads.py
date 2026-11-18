"""The set-based reads of migration 0031 (ci.published_window and the chunk
functions) against the published_* views they replace: every list, report and
search gives the same answer either way, for every kind of reader, and the lists
page. Lists also read the newest emails first (ci.published_page, migration
0039): those answers are compared with both. Synthetic fixtures only; how fast
each is on a large database is measured separately (docs/DATABASE.md).
"""

import json
import re
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import psycopg
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
MODES = ("paged", "set", "views")


def ways(
    stack: Callable[[AppRole | None], Connection],
    monkeypatch: pytest.MonkeyPatch,
    call: Call,
) -> dict[str, Any]:
    """``call`` reading the newest emails first (0039), set-based (0031, as before
    0039) and through the views (as before 0031)."""
    answers = {}
    for mode in MODES:
        with monkeypatch.context() as patch:
            if mode != "paged":
                patch.setattr(retrieval, "paged_reads", lambda conn: False)
                # No paged read either way; this keeps the spy below to the paged mode.
                patch.setattr(retrieval, "_paged_skipped", lambda auth, filters: True)
            if mode == "views":
                patch.setattr(retrieval, "fast_reads", lambda conn: False)
                patch.setattr(reports, "fast_reads", lambda conn: False)
            with stack("ci_search") as conn:
                answers[mode] = call(conn)
                conn.rollback()
    return answers


class _Rows:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows

    def fetchall(self) -> list[dict[str, Any]]:
        return self.rows


def spy_outcomes(
    monkeypatch: pytest.MonkeyPatch, reads: list[tuple[bool, int]] | None = None
) -> list[str]:
    """Records how each listing ended: ``skipped`` (no paged read: straight to the
    0031 read), ``filled`` (the newest rows held the page), ``read whole`` (fewer
    rows than asked for: the period was read whole) or ``fallback`` (None: the
    caller reads as before 0039). ``reads`` gets, for each paged listing, whether
    it had a mailbox filter and how many published_page reads it made."""
    seen: list[str] = []
    real = retrieval._listing_rows_paged
    real_skip = retrieval._paged_skipped

    def skip(auth: retrieval.Authorization, filters: retrieval.Filters) -> bool:
        skipped = real_skip(auth, filters)
        if skipped:
            seen.append("skipped")
        return skipped

    def spy(conn: Connection, *args: Any) -> list[dict[str, Any]] | None:
        groups: list[int] = []

        class Recording:
            def execute(self, sql: str, params: dict[str, Any]) -> _Rows:
                rows = conn.execute(sql, params).fetchall()
                if "published_page(" in sql:
                    kept = [r for r in rows if r["communication_id"] is not None]
                    groups.append(len({retrieval._copy_group(r) for r in kept}))
                return _Rows(rows)

        result = real(cast(Connection, Recording()), *args)
        if reads is not None:
            reads.append((bool(args[1].mailbox), len(groups)))
        need = args[4]
        if result is None:
            seen.append("fallback")
        else:
            seen.append("filled" if groups[-1] >= need else "read whole")
        return result

    monkeypatch.setattr(retrieval, "_listing_rows_paged", spy)
    monkeypatch.setattr(retrieval, "_paged_skipped", skip)
    return seen


def in_order(found: dict[str, Any], part: str = "results") -> list[str]:
    """The emails as listed: the paged and set-based reads sort the same way."""
    return [r["communication_id"] for r in found[part]]


def as_emails(found: dict[str, Any], part: str = "results") -> list[str]:
    # Copies of one message arrive at the same moment; which copy stands for the
    # group is not defined through the views, so emails are compared without order.
    return sorted(f"{r['subject']}|{r['received_at']}|{r['sender']}" for r in found[part])


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
    reads: list[tuple[bool, int]] = []
    outcomes = spy_outcomes(monkeypatch, reads)
    got = ways(
        stack, monkeypatch, lambda c: retrieval.list_email(c, READERS[reader], LISTS[case], 100)
    )
    fast = got["paged"]
    assert in_order(fast) == in_order(got["set"])
    assert as_emails(fast) == as_emails(got["set"]) == as_emails(got["views"])
    assert fast["has_more"] == got["set"]["has_more"] == got["views"]["has_more"]
    # Risk-only readers, own-mailbox readers, readers with reader limits and
    # property or entity filters go straight to the 0031 read; nobody is refused
    # before any read. Otherwise 13 emails: the newest 303 asked for are the whole
    # period, so the paged answer is the one compared (no fallback), from one read
    # (one per look-back period with a mailbox filter: 3, 30, 365 days, all).
    skipped = reader in ("own", "risk_only", "limited") or case in ("a property", "an entity")
    if reader == "nobody":
        assert outcomes == [] and reads == []
    elif skipped:
        assert outcomes == ["skipped"] and reads == []
    else:
        assert outcomes == ["read whole"]
        assert reads == ([(True, 4)] if case == "a mailbox" else [(False, 1)])
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


@pytest.mark.parametrize("bounds", [(2, 4), (2, 40)])
def test_lists_match_when_the_newest_rows_are_few(
    stack: Callable[[AppRole | None], Connection],
    monkeypatch: pytest.MonkeyPatch,
    bounds: tuple[int, int],
) -> None:
    """With reads of 2 to 4 emails (or 2 to 40) every outcome happens on 13 emails,
    each paged listing reads once (once per look-back period with a mailbox
    filter) and never asks again for more, and the answer is still the one without
    0039 and the views'."""
    monkeypatch.setattr(retrieval, "PAGE_ROWS_MIN", bounds[0])
    monkeypatch.setattr(retrieval, "PAGE_ROWS_MAX", bounds[1])
    reads: list[tuple[bool, int]] = []
    outcomes = spy_outcomes(monkeypatch, reads)
    for reader, who in READERS.items():
        for case, filters in LISTS.items():
            for limit in (1, 3, 100):

                def call(c: Connection, w: Principal = who, f: F = filters, n: int = limit) -> Any:
                    return retrieval.list_email(c, w, f, n)

                got = ways(stack, monkeypatch, call)
                where = (reader, case, limit)
                assert in_order(got["paged"]) == in_order(got["set"]), where
                assert as_emails(got["paged"]) == as_emails(got["views"]), where
                assert got["paged"]["has_more"] == got["set"]["has_more"], where
    # One try only: a read of 3 x need rows that is cut and does not fill the page
    # falls back at once, also with up to 40 rows.
    assert set(outcomes) == {"skipped", "filled", "read whole", "fallback"}, outcomes
    assert {n for mailbox, n in reads if not mailbox} == {1}, reads
    assert {n for mailbox, n in reads if mailbox} <= {1, 2, 3, 4}, reads


def test_lists_page_with_an_offset(
    stack: Callable[[AppRole | None], Connection], monkeypatch: pytest.MonkeyPatch
) -> None:
    orders = []
    for mode in (*MODES, "paged in pages of 2 to 4"):
        with monkeypatch.context() as patch:
            if mode in ("set", "views"):
                patch.setattr(retrieval, "paged_reads", lambda conn: False)
            if mode == "views":
                patch.setattr(retrieval, "fast_reads", lambda conn: False)
            if mode.startswith("paged in"):
                patch.setattr(retrieval, "PAGE_ROWS_MIN", 2)
                patch.setattr(retrieval, "PAGE_ROWS_MAX", 4)
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
        if mode != "views":
            orders.append(every)
    assert orders[0] == orders[1] == orders[2]  # paged (both sizes) and set-based


def test_threads_and_action_items_match_the_views(
    stack: Callable[[AppRole | None], Connection], monkeypatch: pytest.MonkeyPatch
) -> None:
    with stack("ci_search") as conn:
        emails = retrieval.list_email(conn, READERS["all"], F(), 100)["results"]
    for reader in ("all", "limited", "own"):
        for email in emails:
            got = ways(stack, monkeypatch, thread_of(READERS[reader], email["communication_id"]))
            fast, views = got["paged"], got["views"]
            # Mailbox copies of one message arrive at the same moment, and before 0031
            # either copy could stand for the group: compare the emails, not the copy.
            assert (fast is None) == (views is None) == (got["set"] is None)
            if fast is not None and views is not None:
                assert fast["count"] == views["count"] and fast["truncated"] == views["truncated"]
                assert in_order(fast, "messages") == in_order(got["set"], "messages")
                assert as_emails(fast, "messages") == as_emails(views, "messages")
        # Action items read the newest emails first (0039); also with pages of 2 to 4.
        for small in (False, True):
            with monkeypatch.context() as patch:
                if small:
                    patch.setattr(retrieval, "PAGE_ROWS_MIN", 2)
                    patch.setattr(retrieval, "PAGE_ROWS_MAX", 4)
                for person in (None, PAT, f"{PAT},{MORGAN}"):
                    got = ways(stack, monkeypatch, actionable_of(READERS[reader], person))
                    for part in ("actions", "possible", "risks"):
                        assert in_order(got["paged"], part) == in_order(got["set"], part)
                        assert sorted(i["subject"] for i in got["paged"][part]) == sorted(
                            i["subject"] for i in got["views"][part]
                        ), (reader, person, part, small)


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


def test_published_page_returns_only_the_newest_published_emails(
    stack: Callable[[AppRole | None], Connection],
) -> None:
    """As ci_search, whatever the limit: published emails only, newest first, with
    published_window's columns."""
    page = "SELECT communication_id::text AS id FROM published_page(NULL, NULL, %s, NULL, %s)"
    with stack(None) as conn:
        gone = conn.execute(
            "UPDATE communications SET lifecycle_state = 'withdrawn' WHERE id ="
            " (SELECT id FROM published_communications ORDER BY occurred_at DESC LIMIT 1)"
            " RETURNING id::text"
        ).fetchone()
        assert gone is not None
        conn.execute("SET ROLE ci_search")
        newest = [
            r["id"]
            for r in conn.execute(
                "SELECT id::text FROM published_communications ORDER BY occurred_at DESC, id"
            ).fetchall()
        ]
        every = [r["id"] for r in conn.execute(page, (None, 1000)).fetchall()]
        three = [r["id"] for r in conn.execute(page, (None, 3)).fetchall()]
        mine = {r["id"] for r in conn.execute(page, (PAT, 1000)).fetchall()}
        in_pat = {
            r["id"]
            for r in conn.execute(
                "SELECT communication_id::text AS id FROM published_email_messages"
                " WHERE mailbox_address = %s",
                (PAT,),
            ).fetchall()
        }
        window = conn.execute(
            "SELECT * FROM published_window(NULL, NULL, %s::uuid[]) ORDER BY communication_id",
            (newest,),
        ).fetchall()
        paged = conn.execute(
            "SELECT * FROM published_page(NULL, NULL, NULL, NULL, 1000) ORDER BY communication_id"
        ).fetchall()
        conn.rollback()
    assert gone["id"] not in newest and gone["id"] not in every
    assert sorted(every) == sorted(newest) and len(every) == len(newest) > 3
    assert sorted(three) == sorted(newest[:3])
    assert mine == in_pat and mine
    assert paged == window  # the same columns and values as published_window


def test_a_list_page_reads_one_page_of_emails(
    stack: Callable[[AppRole | None], Connection], monkeypatch: pytest.MonkeyPatch
) -> None:
    """list_email(limit=25) for an all-mailbox reader: one published_page statement
    for the newest 200 emails, and no read of the whole period."""
    sent: list[tuple[str, Any]] = []
    real = psycopg.Connection.execute

    def spy(self: Any, query: Any, params: Any = None, **kwargs: Any) -> Any:
        sent.append((str(query), params))
        return real(self, query, params, **kwargs)

    monkeypatch.setattr(psycopg.Connection, "execute", spy)
    with stack("ci_search") as conn:
        found = retrieval.list_email(conn, READERS["all"], F(), 25)
        conn.rollback()
    assert len(found["results"]) == 13
    pages = [params for sql, params in sent if "FROM published_page(" in sql]
    assert len(pages) == 1 and pages[0]["row_limit"] == 200
    whole = ("published_window(", "published_labelled(")
    assert not [sql for sql, _ in sent if any(f in sql for f in whole)]


def test_the_page_migration_applies_without_superuser(
    stack: Callable[[AppRole | None], Connection],
) -> None:
    """Azure gives the migrating login no superuser (as for 0031)."""
    path = Path(__file__).resolve().parents[2] / "db/migrations/0039_published_page.sql"
    with stack(None) as conn:
        conn.execute("DROP FUNCTION published_page(timestamptz, timestamptz, text, text, integer)")
        conn.execute("CREATE ROLE pytest_migrator_39 NOSUPERUSER")
        conn.execute("GRANT USAGE, CREATE ON SCHEMA ci TO pytest_migrator_39")
        conn.execute("GRANT SELECT ON ALL TABLES IN SCHEMA ci TO pytest_migrator_39")
        conn.execute("GRANT ci_search TO pytest_migrator_39 WITH ADMIN OPTION")
        conn.execute("SET ROLE pytest_migrator_39")
        conn.execute(path.read_text())
        conn.execute("SET ROLE ci_search")
        page = conn.execute(
            "SELECT count(*) AS n FROM published_page(NULL, NULL, NULL, NULL, 5)"
        ).fetchone()
        conn.rollback()
    assert page is not None and page["n"] == 5


def test_only_the_search_role_may_call_published_page(
    stack: Callable[[AppRole | None], Connection],
) -> None:
    """EXECUTE on published_page is granted to ci_search only, not to PUBLIC."""
    signature = "ci.published_page(timestamptz, timestamptz, text, text, integer)"
    with stack(None) as conn:
        allowed = {
            role: conn.execute(
                "SELECT has_function_privilege(%s, %s, 'EXECUTE') AS ok", (role, signature)
            ).fetchone()
            for role in ("ci_search", "ci_ingest", "ci_operator", "public")
        }
        conn.rollback()
    assert {role: bool(row and row["ok"]) for role, row in allowed.items()} == {
        "ci_search": True,
        "ci_ingest": False,
        "ci_operator": False,
        "public": False,
    }


def test_the_python_row_cap_equals_the_function_cap(
    stack: Callable[[AppRole | None], Connection],
) -> None:
    """PAGE_ROWS_MAX mirrors published_page's cap: a larger value would make a cut
    read look like a period read whole."""
    with stack(None) as conn:
        row = conn.execute(
            "SELECT prosrc FROM pg_proc WHERE oid ="
            " 'ci.published_page(timestamptz, timestamptz, text, text, integer)'::regprocedure"
        ).fetchone()
        conn.rollback()
    assert row is not None
    cap = re.search(r"least\(greatest\(coalesce\(row_limit, 1\), 1\), (\d+)\)", row["prosrc"])
    assert cap is not None and int(cap.group(1)) == retrieval.PAGE_ROWS_MAX
