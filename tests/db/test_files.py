"""The console's Files page (ci_retrieval.files, migration 0032) on the synthetic
fixtures: which files each reader sees, one row per file, folders, search by name
and by text inside the files, paging, and an audit trail without search words."""

from collections.abc import Callable, Iterator
from datetime import UTC, datetime
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
from ci_retrieval import files
from ci_retrieval.identity import READER_ALL, READER_OWN, READER_RISKS, Principal

NOW = datetime(2026, 10, 28, tzinfo=UTC)
PAT = "pat.example@finishessolutions.example"
MORGAN = "morgan.lee@finishessolutions.example"
READER = Principal("reader-oid", "reader@finishessolutions.example", (READER_ALL,))
OWN = Principal("pat-oid", PAT, (READER_OWN,))
LIMITED = Principal("morgan-oid", MORGAN, (READER_ALL,))
NOBODY = Principal("nobody-oid", "nobody@finishessolutions.example", ())
RISKY = Principal("agent-oid", "agent@finishessolutions.example", (READER_RISKS,))


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
            FakeScreener(), FakeClassifier(), FakeEmbedder(), graph=graph, extraction_enabled=True
        ),
        workers=1,
    )
    with connector() as conn:  # Pat's mailbox is hidden from Morgan, except mail Morgan is on
        limit_reader(conn, MORGAN, "mailbox", PAT, "pytest", reason="private_business")
        conn.commit()
    yield connector


def listed(
    stack: Callable[[AppRole | None], Connection], who: Principal, **kw: Any
) -> list[dict[str, Any]]:
    with stack("ci_search") as conn:
        found: list[dict[str, Any]] = files.list_files(conn, who, **kw)["files"]
        return found


def test_newest_lists_each_file_once_without_signature_images(
    stack: Callable[[AppRole | None], Connection],
) -> None:
    found = listed(stack, READER)
    names = [f["name"] for f in found]
    assert "Invoice-4471.pdf" in names
    assert not any(str(n).lower().startswith("image") for n in names)  # inline logos left out
    assert len({f["file_key"] for f in found}) == len(found)  # one row per file
    dates = [f["received_at"] for f in found]
    assert dates == sorted(dates, reverse=True)  # newest first
    invoice = next(f for f in found if f["name"] == "Invoice-4471.pdf")
    assert invoice["emails"] >= 1 and invoice["communication_id"]
    assert invoice["attachment_id"] and len(str(invoice["attachment_id"])) == 16


def test_files_follow_the_readers_grants_and_limits(
    stack: Callable[[AppRole | None], Connection],
) -> None:
    assert listed(stack, NOBODY) == []
    assert {f["mailbox"] for f in listed(stack, OWN)} <= {PAT}
    everyone = listed(stack, READER)
    limited = listed(stack, LIMITED)
    assert len(limited) <= len(everyone)
    # Pat's mailbox is hidden from Morgan except the emails Morgan is on (the limit's
    # exception); a file from Pat's mailbox must come from such an email.
    with stack(None) as conn:
        for f in limited:
            if f["mailbox"] == PAT:
                on = conn.execute(
                    "SELECT 1 FROM published_participants WHERE communication_id = %s"
                    " AND email_normalized = %s",
                    (f["communication_id"], MORGAN),
                ).fetchone()
                assert on is not None, f["name"]
    with stack("ci_search") as conn:  # the console refuses risk-only access; the SQL narrows too
        for f in files.list_files(conn, RISKY)["files"]:
            labels = conn.execute(
                "SELECT yes_labels FROM published_classifications WHERE communication_id = %s",
                (f["communication_id"],),
            ).fetchone()
            assert labels is not None and "project_risk" in labels["yes_labels"]


def test_folders_and_their_files(stack: Callable[[AppRole | None], Connection]) -> None:
    with stack("ci_search") as conn:
        for group_by in files.GROUPS:
            folders = files.file_folders(conn, READER, group_by)["folders"]
            assert folders, group_by
            for folder in folders:
                inside = files.list_files(conn, READER, group_by=group_by, group=folder["key"])
                assert len(inside["files"]) == folder["files"], (group_by, folder)
        types = {f["key"]: f for f in files.file_folders(conn, READER, "type")["folders"]}
        assert "pdf" in types and types["pdf"]["label"] == "PDF"
        projects = files.file_folders(conn, READER, "project")["folders"]
        named = next(f for f in projects if f["key"])
        inside = files.list_files(conn, READER, group_by="project", group=named["key"])["files"]
        assert inside and all(
            named["key"] in [p.strip().lower() for p in f["properties_ai"]] for f in inside
        )
        with pytest.raises(ValueError):
            files.file_folders(conn, READER, "everything")


def test_search_by_name_and_by_text_inside(stack: Callable[[AppRole | None], Connection]) -> None:
    by_name = listed(stack, READER, q="invoice")
    assert by_name and all(
        "invoice" in f"{f['name']} {f['subject']} {f['sender']}".lower() for f in by_name
    )
    by_text = listed(stack, READER, q="plumbing", search_in="text")
    assert [f["name"] for f in by_text] == ["CO-008-BuildingD.pdf"]
    assert listed(stack, READER, q="zzqxv-nothing") == []
    with stack("ci_search") as conn, pytest.raises(ValueError):
        files.list_files(conn, READER, q="x", search_in="everywhere")


def test_paging(stack: Callable[[AppRole | None], Connection]) -> None:
    with stack("ci_search") as conn:
        whole = files.list_files(conn, READER)
        pages = [
            files.list_files(conn, READER, limit=1, offset=i) for i in range(len(whole["files"]))
        ]
    assert [p["files"][0]["file_key"] for p in pages] == [f["file_key"] for f in whole["files"]]
    assert [p["has_more"] for p in pages][-1] is False
    assert all(p["has_more"] for p in pages[:-1]) and not whole["has_more"]


def test_audit_has_ids_and_counts_not_search_words(
    stack: Callable[[AppRole | None], Connection],
) -> None:
    listed(stack, READER, q="very-private-file-words")
    with stack("ci_search") as conn:
        files.file_folders(conn, READER, "company", q="very-private-file-words")
    with stack(None) as conn:
        rows = conn.execute(
            "SELECT action, details::text AS d FROM audit_events"
            " WHERE action IN ('list_files', 'file_folders')"
        ).fetchall()
    assert {r["action"] for r in rows} == {"list_files", "file_folders"}
    assert not any("very-private" in r["d"] for r in rows)


def test_without_the_migration_the_page_says_so(
    stack: Callable[[AppRole | None], Connection],
) -> None:
    with stack(None) as conn:
        conn.execute("DROP FUNCTION ci.published_files(timestamptz, timestamptz, text)")
        conn.execute("SET ROLE ci_search")
        with pytest.raises(files.FilesUnavailable):
            files.list_files(conn, READER)
        conn.rollback()


def test_the_migration_applies_without_superuser(
    stack: Callable[[AppRole | None], Connection],
) -> None:
    """Azure gives the migrating login no superuser: the function's settings must be
    ones an ordinary role may set (as for 0015 and 0031)."""
    from pathlib import Path

    sql = (Path(__file__).resolve().parents[2] / "db/migrations/0032_files.sql").read_text()
    with stack(None) as conn:
        conn.execute("DROP FUNCTION ci.published_files(timestamptz, timestamptz, text)")
        conn.execute("CREATE ROLE pytest_migrator_32 NOSUPERUSER")
        conn.execute("GRANT USAGE, CREATE ON SCHEMA ci TO pytest_migrator_32")
        conn.execute("GRANT SELECT ON ALL TABLES IN SCHEMA ci TO pytest_migrator_32")
        conn.execute("GRANT ci_search TO pytest_migrator_32 WITH ADMIN OPTION")
        conn.execute("SET ROLE pytest_migrator_32")
        conn.execute(sql)
        conn.execute("SET ROLE ci_search")
        found = conn.execute("SELECT count(*) AS n FROM ci.published_files()").fetchone()
        conn.rollback()
    assert found is not None and found["n"] > 0
