"""The keyword table of migration 0040 (ci.chunk_terms): keyword search ranks
from it once it is ready and gives the same answers as from the chunks table;
new chunks reach it through the trigger, deleted ones leave it; the operator's
build (scripts/keyword_table.py) copies existing chunks and switches over; and a
term with more matches than score_limit is ranked among its newest matches only.
Synthetic fixtures only; the speed on the dev server is measured there.
"""

import importlib.util
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from ci_cli.seed import seed
from ci_core.db import AppRole, Connection, connect
from ci_email.capture import capture_window
from ci_email.fixture import FixtureGraphClient
from ci_email.process import Backends, process_all
from ci_processing.classification import FakeClassifier
from ci_processing.embedding import FakeEmbedder
from ci_processing.screening import FakeScreener

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "keyword_table.py"
_spec = importlib.util.spec_from_file_location("keyword_table", _SCRIPT)
assert _spec and _spec.loader
keyword_table = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(keyword_table)

NOW = datetime(2026, 10, 28, tzinfo=UTC)
QUERIES = ("invoice OR lease OR lien", "change order", "invoice", '"pay application"', "zzzz")
LEX = "SELECT id::text AS id, rank FROM published_lexical(%s, 400, %s) ORDER BY rank DESC, id"
LEX_IN = (
    "SELECT id::text AS id, rank FROM published_lexical_in(%s, %s::uuid[], 400, %s)"
    " ORDER BY rank DESC, id"
)


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
        Backends(
            FakeScreener(),
            FakeClassifier(),
            FakeEmbedder(1536),
            graph=graph,
            extraction_enabled=True,
        ),
        workers=1,
    )
    yield connector


def _answers(conn: Connection) -> dict[str, Any]:
    conn.execute("SET ROLE ci_search")
    ids = [r["id"] for r in conn.execute("SELECT id::text AS id FROM published_communications")]
    found: dict[str, Any] = {}
    for q in QUERIES:
        for docs in (None, True, False):
            found[f"lex {q} {docs}"] = [dict(r) for r in conn.execute(LEX, (q, docs))]
            found[f"in {q} {docs}"] = [dict(r) for r in conn.execute(LEX_IN, (q, ids, docs))]
    conn.execute("RESET ROLE")
    return found


def _ready(conn: Connection, on: bool) -> None:
    conn.execute("UPDATE ci.chunk_terms_state SET ready = %s", (on,))


def test_a_new_database_is_ready_and_the_trigger_copies_every_chunk(
    stack: Callable[[AppRole | None], Connection],
) -> None:
    with stack(None) as conn:
        state = conn.execute("SELECT ready FROM ci.chunk_terms_state").fetchone()
        mismatched = conn.execute(
            "SELECT count(*) AS n FROM ci.chunks ch"
            " LEFT JOIN ci.chunk_terms ct ON ct.chunk_id = ch.id"
            " JOIN ci.communications c ON c.id = ch.communication_id"
            " WHERE ct.chunk_id IS NULL OR ct.tsv IS DISTINCT FROM ch.tsv"
            " OR ct.version_id <> ch.version_id OR ct.communication_id <> ch.communication_id"
            " OR ct.is_doc <> (ch.chunk_kind = 'attachment_text')"
            " OR ct.occurred_at <> c.occurred_at"
        ).fetchone()
        counts = conn.execute(
            "SELECT (SELECT count(*) FROM ci.chunks) AS chunks,"
            " (SELECT count(*) FROM ci.chunk_terms) AS terms"
        ).fetchone()
        storage = conn.execute(
            "SELECT attstorage FROM pg_attribute"
            " WHERE attrelid = 'ci.chunk_terms'::regclass AND attname = 'tsv'"
        ).fetchone()
    assert state == {"ready": True}
    assert counts and counts["chunks"] > 0 and counts["chunks"] == counts["terms"]
    assert mismatched == {"n": 0}
    assert storage == {"attstorage": "m"}  # kept in the row, not in TOAST


def test_keyword_search_answers_the_same_from_either_table(
    stack: Callable[[AppRole | None], Connection],
) -> None:
    with stack(None) as conn:
        _ready(conn, True)
        new = _answers(conn)
        _ready(conn, False)
        old = _answers(conn)
        conn.rollback()
    assert any(new[k] for k in new), "the queries found nothing to compare"
    assert any(new[k] for k in new if k.startswith("in ") and "True" in k)  # attachment text
    for key in old:
        assert [r["id"] for r in new[key]] == [r["id"] for r in old[key]], key
        assert [r["rank"] for r in new[key]] == pytest.approx([r["rank"] for r in old[key]])


def test_deleted_chunks_leave_the_table(stack: Callable[[AppRole | None], Connection]) -> None:
    with stack(None) as conn:
        gone = conn.execute(
            "DELETE FROM ci.chunks WHERE id = (SELECT id FROM ci.chunks LIMIT 1) RETURNING id"
        ).fetchone()
        assert gone is not None
        left = conn.execute(
            "SELECT count(*) AS n FROM ci.chunk_terms WHERE chunk_id = %s", (gone["id"],)
        ).fetchone()
        conn.rollback()
    assert left == {"n": 0}


def test_a_term_with_more_matches_than_the_limit_ranks_its_newest(
    stack: Callable[[AppRole | None], Connection],
) -> None:
    q = "invoice OR lease OR lien OR order"
    with stack(None) as conn:
        newest = conn.execute(
            "SELECT ct.chunk_id::text AS id FROM ci.chunk_terms ct"
            " WHERE ct.tsv @@ websearch_to_tsquery('english', %s)"
            " ORDER BY ct.occurred_at DESC, ct.chunk_id LIMIT 2",
            (q,),
        ).fetchall()
        conn.execute("UPDATE ci.chunk_terms_state SET score_limit = 2")
        conn.execute("SET ROLE ci_search")
        ids = [r["id"] for r in conn.execute("SELECT id::text AS id FROM published_communications")]
        got = {r["id"] for r in conn.execute(LEX, (q, None))}
        within = {r["id"] for r in conn.execute(LEX_IN, (q, ids, None))}
        conn.rollback()
    assert got and got <= {r["id"] for r in newest}
    assert within and len(within) <= 2


def test_the_build_copies_existing_chunks_and_switches_over(
    stack: Callable[[AppRole | None], Connection],
    database: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """As on the dev server: the migration finds chunks, so the table starts empty
    with no index and keyword search stays on the chunks table until the build."""
    with stack(None) as conn:
        before = _answers(conn)
        conn.execute("DROP INDEX ci.chunk_terms_tsv")
        conn.execute("DELETE FROM ci.chunk_terms")
        conn.execute("UPDATE ci.chunk_terms_state SET ready = false, copied_through = -1")
        conn.commit()
        assert _answers(conn) == before  # not ready: the chunks table answers
        conn.commit()
    monkeypatch.setenv("PGDATABASE", database)
    with keyword_table.connect() as op:
        keyword_table.build(op, batch_pages=1)
        keyword_table.build(op)  # a second run changes nothing
        keyword_table.status(op)
    with stack(None) as conn:
        state = conn.execute("SELECT ready, copied_through FROM ci.chunk_terms_state").fetchone()
        counts = conn.execute(
            "SELECT (SELECT count(*) FROM ci.chunks) AS chunks,"
            " (SELECT count(*) FROM ci.chunk_terms) AS terms"
        ).fetchone()
        after = _answers(conn)
    assert state and state["ready"] and state["copied_through"] >= 0
    assert counts and counts["terms"] == counts["chunks"] > 0
    assert after == before
