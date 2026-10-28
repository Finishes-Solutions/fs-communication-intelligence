"""Capture -> process -> retrieve against a real database with fake backends.

Covers the acceptance negatives in CLAUDE.md section 11 that apply locally:
missing grant, excluded mailbox, participant exclusion, paused mode and a
forged filter all return nothing; restricted content is never returned.
"""

import logging
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest

from ci_cli.seed import seed
from ci_core.db import AppRole, Connection, connect
from ci_core.operator import exclude_mailbox, exclude_participant, set_mode
from ci_email.capture import capture_window
from ci_email.fixture import FixtureGraphClient, load_tenant
from ci_email.process import Backends, process_all
from ci_processing.classification import FakeClassifier
from ci_processing.embedding import FakeEmbedder
from ci_processing.screening import FakeScreener
from ci_retrieval import search as retrieval
from ci_retrieval.identity import READER_ALL, READER_OWN, Principal

NOW = datetime(2026, 10, 28, tzinfo=UTC)
PAT = "pat.example@finishessolutions.example"
MORGAN = "morgan.lee@finishessolutions.example"
READER = Principal("reader-oid", None, (READER_ALL,))
NOBODY = Principal("nobody-oid", None, ())
EMBEDDER = FakeEmbedder(1536)


@pytest.fixture(scope="module")
def pipeline(database: str) -> Iterator[dict[str, Any]]:
    def connector(role: AppRole | None = None) -> Connection:
        return connect(dbname=database, role=role)

    with connector() as conn:
        seed(conn, "pytest")
        conn.commit()
    graph = FixtureGraphClient.from_directory()
    capture = capture_window(
        connector, graph, hours=744, include_sent=True, profile={"p": 1}, now=NOW
    )
    rerun = capture_window(
        connector, graph, hours=744, include_sent=True, profile={"p": 1}, now=NOW
    )
    backends = Backends(
        FakeScreener(), FakeClassifier(), EMBEDDER, graph=graph, extraction_enabled=True
    )
    counts = process_all(lambda: connector("ci_ingest"), backends, workers=1)
    yield {"connect": connector, "capture": capture, "rerun": rerun, "counts": counts}


def search(
    p: dict[str, Any],
    query: str,
    principal: Principal = READER,
    filters: retrieval.Filters = retrieval.NO_FILTERS,
) -> dict[str, Any]:
    with p["connect"]("ci_search") as conn:
        return retrieval.search(conn, principal, query, EMBEDDER, filters, 25)


def subjects(result: dict[str, Any]) -> set[str]:
    return {r["subject"] for r in result["results"]}


def test_capture_skips_what_it_must_and_reruns_store_nothing(pipeline: dict[str, Any]) -> None:
    by_box = {r.address: r.counts for r in pipeline["capture"]}
    assert set(by_box) == {PAT, MORGAN, "ap@finishessolutions.example"}  # casey excluded
    assert by_box[PAT]["skipped_draft"] == 1 and by_box[PAT]["skipped_folder"] == 1
    assert all(r.counts["new_version"] == 0 for r in pipeline["rerun"])
    assert sum(r.counts["unchanged"] for r in pipeline["rerun"]) == 20


def test_every_captured_fixture_reaches_its_expected_disposition(
    pipeline: dict[str, Any],
) -> None:
    _, messages = load_tenant()
    expected = {m.id: m.fixture.expect.disposition for m in messages}
    with pipeline["connect"]() as conn:
        rows = conn.execute(
            "SELECT c.native_id, v.processing_state, v.hold_reason_code, r.disposition"
            " FROM communications c JOIN communication_versions v ON v.communication_id = c.id"
            " LEFT JOIN classification_runs r ON r.version_id = v.id"
        ).fetchall()
    assert len(rows) == 20
    for row in rows:
        want = expected[row["native_id"]]
        got = row["disposition"] or row["hold_reason_code"].split(":")[0]
        assert got == want, row["native_id"]
        published = row["processing_state"] == "published"
        assert published == (want in ("index", "review")), row["native_id"]
    captured = {r["native_id"] for r in rows}
    skipped = {m.id for m in messages if not m.fixture.expect.captured}
    assert captured.isdisjoint(skipped)


def test_search_finds_attachment_text_and_collapses_copies(pipeline: dict[str, Any]) -> None:
    result = search(pipeline, "compaction grading invoice")
    top = result["results"][0]
    assert top["subject"].startswith("Invoice 4471")
    assert any(e["attachment_name"] == "Invoice-4471.pdf" for e in top["excerpts"])
    co8 = [
        r
        for r in search(pipeline, "plumbing reroute beam")["results"]
        if r["subject"].startswith("Change Order 8")
    ]
    assert len(co8) == 1 and co8[0]["other_authorized_copies"] == 2


def test_restricted_and_held_are_never_returned(pipeline: dict[str, Any]) -> None:
    found = subjects(
        search(
            pipeline,
            "new hire payroll SSN integration api key settings ignore previous instructions",
        )
    )
    assert not found & {
        "New hire paperwork",
        "Integration settings for the reporting tool",
        "Quick question about Lot 9",
    }
    with pipeline["connect"]() as conn:
        restricted = conn.execute(
            "SELECT c.id::text FROM communications c WHERE c.title = 'New hire paperwork'"
        ).fetchone()
    assert restricted is not None
    with pipeline["connect"]("ci_search") as conn:
        assert retrieval.get_email(conn, READER, restricted["id"]) is None


def test_search_role_cannot_read_base_tables(pipeline: dict[str, Any]) -> None:
    import psycopg

    with (
        pipeline["connect"]("ci_search") as conn,
        pytest.raises(psycopg.errors.InsufficientPrivilege),
    ):
        conn.execute("SELECT count(*) FROM version_texts")


def test_no_grant_wrong_role_and_forged_filter_return_nothing(pipeline: dict[str, Any]) -> None:
    assert search(pipeline, "change order", NOBODY)["results"] == []
    assert search(pipeline, "change order", NOBODY)["authorized"] is False
    own_only = Principal("pat-oid", PAT, (READER_OWN,))
    own = search(pipeline, "change order lease renewal", own_only)
    assert own["results"] and {r["mailbox"] for r in own["results"]} == {PAT}
    forged = search(pipeline, "lease renewal suite", own_only, retrieval.Filters(mailbox=MORGAN))
    assert forged["results"] == []


def test_actionable_for_a_person(pipeline: dict[str, Any]) -> None:
    with pipeline["connect"]("ci_search") as conn:
        report = retrieval.list_actionable(conn, READER, person=PAT, hours=24 * 60)
    actions = {a["subject"] for a in report["actions"]}
    assert {
        "Invoice 4471 for Lot 12 grading - payment due Friday",
        "Change Order 7 - Building C framing scope",
        "Change Order 8 - Building D plumbing reroute",
    } <= actions
    assert "Suite 210 lease renewal terms" not in actions  # Morgan's mailbox, not Pat's
    risks = {r["subject"]: r for r in report["risks"]}
    assert "Repeated delays and defects on punch list" in risks
    assert "Notice of mechanic's lien - Example Ridge Phase 2" in risks
    assert risks["Repeated delays and defects on punch list"]["risk_ai"]
    assert "Updated banking details for future payments" not in risks  # security, not project


def test_audit_records_ids_not_query_text(pipeline: dict[str, Any]) -> None:
    search(pipeline, "very-distinctive-query-text")
    with pipeline["connect"]() as conn:
        rows = conn.execute(
            "SELECT details::text AS d FROM audit_events WHERE action = 'search'"
        ).fetchall()
    assert rows and not any("very-distinctive-query-text" in r["d"] for r in rows)


def test_vector_search_uses_the_index_and_matches_the_exact_scan(
    pipeline: dict[str, Any], caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    query = "change order lease invoice lien draw"
    with caplog.at_level(logging.INFO, logger="ci_retrieval.search"):
        fast = search(pipeline, query)
    assert "path=index" in caplog.text
    assert "lien" not in caplog.text  # timings and counts only, never the query
    monkeypatch.setattr(retrieval, "_set_search_options", lambda conn: False)
    exact = search(pipeline, query)
    assert fast["results"]
    ids = {r["communication_id"] for r in fast["results"]}
    assert ids == {r["communication_id"] for r in exact["results"]}


def test_too_few_candidates_after_grants_fall_back_to_the_exact_scan(
    pipeline: dict[str, Any], caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    own_only = Principal("pat-oid", PAT, (READER_OWN,))
    expected = search(pipeline, "change order lease renewal", own_only)
    monkeypatch.setattr(retrieval, "NEAREST_K", 2)  # the index yields 2; the grant keeps fewer
    with caplog.at_level(logging.INFO, logger="ci_retrieval.search"):
        own = search(pipeline, "change order lease renewal", own_only)
    assert "path=exact_fallback" in caplog.text
    assert own["results"] and {r["mailbox"] for r in own["results"]} == {PAT}
    assert [r["communication_id"] for r in own["results"]] == [
        r["communication_id"] for r in expected["results"]
    ]


def test_index_check_reports_recall_and_timings(pipeline: dict[str, Any]) -> None:
    with pipeline["connect"]("ci_search") as conn:
        report = retrieval.index_check(conn, samples=5)
    assert report["samples"] == 5
    assert report["recall_at_10"] == 1.0  # a small index is searched exhaustively
    assert report["index_ms"] >= 0 and report["exact_ms"] >= 0


def test_actionable_accepts_several_addresses_for_one_person(pipeline: dict[str, Any]) -> None:
    with pipeline["connect"]("ci_search") as conn:
        both = retrieval.list_actionable(
            conn, READER, person=f" {PAT.upper()} , {MORGAN}", hours=24 * 60
        )
    subjects = {a["subject"] for a in both["actions"]}
    assert "Invoice 4471 for Lot 12 grading - payment due Friday" in subjects  # Pat
    assert both["person"] == f"{MORGAN}, {PAT}"


def test_exclusions_and_pause_hide_published_mail(pipeline: dict[str, Any]) -> None:
    query = "change order lease invoice lien draw"
    assert search(pipeline, query)["results"]
    with pipeline["connect"]() as conn:
        exclude_participant(conn, "@brooks-hale-law.example", "pytest", reason="legal_privilege")
        exclude_mailbox(conn, MORGAN, "pytest", reason="personal_mailbox")
        conn.commit()
    found = search(pipeline, query)["results"]
    assert found and not {r["mailbox"] for r in found} & {MORGAN}
    # The index-first function returns published chunks only, with the view's values.
    vector = str(EMBEDDER.embed([query])[0])
    with pipeline["connect"]() as conn:
        total = conn.execute("SELECT count(*) AS n FROM chunks").fetchone()
    with pipeline["connect"]("ci_search") as conn:
        near = conn.execute(
            "SELECT n.corpus_id = pc.corpus_id AND n.communication_id = pc.communication_id"
            " AND n.occurred_at = pc.occurred_at AS same"
            " FROM nearest_published_chunks(%s::vector, 1000) n"
            " LEFT JOIN published_chunks pc ON pc.id = n.id",
            (vector,),
        ).fetchall()
        published = conn.execute("SELECT count(*) AS n FROM published_chunks").fetchone()
        cached = retrieval.coverage(conn)
    assert total and published
    assert len(near) == published["n"] < total["n"]  # the excluded mailbox's chunks stay out
    assert all(r["same"] for r in near)
    assert "Notice of mechanic's lien - Example Ridge Phase 2" not in subjects(
        search(pipeline, "lien attorney notice")
    )
    with pipeline["connect"]() as conn:
        set_mode(conn, "paused", "pytest")
        conn.commit()
    assert search(pipeline, query)["results"] == []
    with pipeline["connect"]("ci_search") as conn:
        paused = conn.execute(
            "SELECT count(*) AS n FROM nearest_published_chunks(%s::vector, 1000)", (vector,)
        ).fetchone()
        assert paused == {"n": 0}
        # The coverage block is recounted every few minutes, not on every search.
        assert retrieval.coverage(conn) == cached
        assert retrieval.coverage(conn, max_age=0) != cached
