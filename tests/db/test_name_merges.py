"""Knowledge merges reach every page that groups or filters by name (migration 0034;
Matthew, 2026-11-18: "if I merge Stokes Rd into Gracious Gardens, the system should
know moving forward that Stokes Rd means Gracious Gardens").

Two synthetic emails with attachments are given the project names "Stokes Rd" and
"Gracious Gardens". Merging Stokes Rd into Gracious Gardens in the knowledge base
must put both in one Files folder, one report group and one property filter, at
once and without reprocessing; unmerging, hiding and renaming must apply at once
too."""

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
from ci_retrieval import files, reports
from ci_retrieval.identity import READER_ALL, Principal
from ci_retrieval.search import Filters, authorize, list_email

NOW = datetime(2026, 10, 28, tzinfo=UTC)
READER = Principal("reader-oid", "reader@finishessolutions.example", (READER_ALL,))
Stack = Callable[[AppRole | None], Connection]


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
        for attachment, project in (
            ("Invoice-4471.pdf", "Stokes Rd"),
            ("CO-007-BuildingC.pdf", "Gracious Gardens"),
        ):
            conn.execute(
                "UPDATE classification_runs r SET properties = ARRAY[%s]"
                " FROM email_attachments a WHERE a.version_id = r.version_id AND a.name = %s",
                (project, attachment),
            )
            conn.execute(
                "INSERT INTO kg_entities (kind, key, name) VALUES ('property', %s, %s)"
                " ON CONFLICT (kind, key) DO NOTHING",
                (name_key("property", project), project),
            )
        conn.commit()
    yield connector


def entity(stack: Stack, name: str) -> int:
    with stack(None) as conn:
        row = conn.execute(
            "SELECT id FROM kg_entities WHERE kind = 'property' AND key = %s",
            (name_key("property", name),),
        ).fetchone()
        assert row is not None
        return int(row["id"])


def correct(stack: Stack, change: Callable[[Connection], None]) -> None:
    with stack(None) as conn:
        change(conn)
        conn.commit()


def project_folders(stack: Stack) -> dict[str, int]:
    with stack("ci_search") as conn:
        found = files.file_folders(conn, READER, "project")["folders"]
    return {f["label"]: f["files"] for f in found if f["label"] in WATCHED}


def report_counts(stack: Stack) -> dict[str, int]:
    with stack("ci_search") as conn:
        groups = reports.report_groups(conn, READER, "messages", "property")["groups"]
    return {g["key"]: g["messages"] for g in groups if g["key"] in WATCHED}


def filtered(stack: Stack, term: str) -> set[str]:
    with stack("ci_search") as conn:
        found = list_email(conn, READER, Filters(property=term), 50)["results"]
    return {n for r in found for n in r["properties_ai"]}


WATCHED = {"Stokes Rd", "Gracious Gardens", "Gracious Gardens (Stokes Rd)"}


def test_name_key_matches_the_knowledge_base(stack: Stack) -> None:
    samples = [
        ("property", "Stokes Rd."),
        ("property", "  GRACIOUS   gardens  "),
        ("property", "Lot 12 & 13 / Phase-2"),
        ("property", "Willow_Creek #4"),
        ("organization", "The Acme Sitework, L.L.C."),
        ("organization", "Example Capital Company"),
        ("organization", "Example Holdings Incorporated"),
        ("organization", "Gray & Sons Limited"),
        ("organization", "Example L.P."),
        ("property", "\uff33\uff54\uff4f\uff4b\uff45\uff53\u3000\uff32\uff44"),  # full-width: NFKC
    ]
    with stack(None) as conn:
        for kind, name in samples:
            row = conn.execute("SELECT ci.kg_name_key(%s, %s) AS k", (kind, name)).fetchone()
            assert row is not None
            assert row["k"] == name_key(kind, name), (kind, name)


def test_a_merge_reaches_folders_reports_and_filters_and_can_be_undone(stack: Stack) -> None:
    before = project_folders(stack)
    assert before == {"Stokes Rd": 1, "Gracious Gardens": 1}
    assert report_counts(stack) == {"Stokes Rd": 1, "Gracious Gardens": 1}
    assert filtered(stack, "gracious") == {"Gracious Gardens"}

    stokes, gracious = entity(stack, "Stokes Rd"), entity(stack, "Gracious Gardens")
    correct(stack, lambda c: merge_knowledge(c, "pytest", stokes, gracious))
    assert project_folders(stack) == {"Gracious Gardens": 2}
    assert report_counts(stack) == {"Gracious Gardens": 2}
    # The filter finds both by the merged name, and Stokes Rd still by its own.
    assert filtered(stack, "gracious") == {"Stokes Rd", "Gracious Gardens"}
    assert filtered(stack, "stokes") == {"Stokes Rd"}
    with stack("ci_search") as conn:  # a later email saying "STOKES RD." is mapped too
        row = conn.execute(
            "SELECT canonical_names('property', ARRAY['STOKES RD.', 'Gracious Gardens',"
            " 'Elm Street']) AS n"
        ).fetchone()
    assert row is not None and row["n"] == ["Gracious Gardens", "Elm Street"]

    correct(
        stack, lambda c: rename_knowledge(c, "pytest", gracious, "Gracious Gardens (Stokes Rd)")
    )
    assert project_folders(stack) == {"Gracious Gardens (Stokes Rd)": 2}

    correct(stack, lambda c: hide_knowledge(c, "pytest", gracious))  # hidden: names as written
    assert project_folders(stack) == {"Stokes Rd": 1, "Gracious Gardens": 1}
    correct(stack, lambda c: hide_knowledge(c, "pytest", gracious, hidden=False))
    correct(stack, lambda c: rename_knowledge(c, "pytest", gracious, None))

    correct(stack, lambda c: unmerge_knowledge(c, "pytest", stokes))
    assert project_folders(stack) == {"Stokes Rd": 1, "Gracious Gardens": 1}
    assert report_counts(stack) == {"Stokes Rd": 1, "Gracious Gardens": 1}


def test_risk_page_names_follow_merges(stack: Stack) -> None:
    stokes, gracious = entity(stack, "Stokes Rd"), entity(stack, "Gracious Gardens")
    correct(stack, lambda c: merge_knowledge(c, "pytest", stokes, gracious))
    try:
        rows: list[dict[str, Any]] = [
            {"properties": ["Stokes Rd", "Gracious Gardens"]},
            {"properties": ["stokes rd", "Elm Street"]},
            {"properties": []},
        ]
        with stack("ci_search") as conn:
            authorize(conn, READER)  # notes migration 0034 for this process
            reports._shown_property_names(conn, rows)
        assert [r["properties"] for r in rows] == [
            ["Gracious Gardens"],
            ["Gracious Gardens", "Elm Street"],
            [],
        ]
        assert rows[1]["written_properties"] == ["stokes rd", "Elm Street"]
    finally:
        correct(stack, lambda c: unmerge_knowledge(c, "pytest", stokes))
