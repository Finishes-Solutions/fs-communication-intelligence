"""Property/entity names end to end, the canonical-name registry, and long backfills."""

from collections.abc import Iterator
from datetime import UTC, datetime

import psycopg
import pytest

from ci_cli.main import main
from ci_cli.seed import seed
from ci_core import ops
from ci_core.db import AppRole, Connection, connect
from ci_core.operator import OperatorError, add_name
from ci_email.capture import capture_window
from ci_email.fixture import FixtureGraphClient
from ci_email.process import Backends, process_all
from ci_processing.classification import FakeClassifier
from ci_processing.embedding import FakeEmbedder
from ci_processing.screening import FakeScreener
from ci_retrieval import search as retrieval
from ci_retrieval.identity import READER_ALL, Principal

READER = Principal("reader-oid", None, (READER_ALL,))
INVOICE = "Invoice 4471 for Lot 12 grading - payment due Friday"


@pytest.fixture(scope="module")
def ready(database: str) -> Iterator[str]:
    def connector(role: AppRole | None = None) -> Connection:
        return connect(dbname=database, role=role)

    with connector() as conn:
        seed(conn, "pytest")
        # A canonical property whose alias is how the fixtures write it.
        add_name(conn, "pytest", "property", "Example Ridge", ["Example Ridge Lot 12"])
        conn.commit()
    graph = FixtureGraphClient.from_directory()
    capture_window(
        connector,
        graph,
        hours=24 * 93,  # the longest backfill allowed
        include_sent=True,
        profile={"p": 1},
        now=datetime(2026, 10, 28, tzinfo=UTC),
    )
    process_all(
        lambda: connector("ci_ingest"),
        Backends(
            FakeScreener(), FakeClassifier(), FakeEmbedder(), graph=graph, extraction_enabled=True
        ),
        workers=1,
    )
    yield database


def search_conn(database: str) -> Connection:
    return connect(dbname=database, role="ci_search")


def test_names_are_stored_canonical_and_returned(ready: str) -> None:
    with search_conn(ready) as conn:
        items = retrieval.list_email(conn, READER, retrieval.NO_FILTERS, 100)["results"]
        invoice = next(i for i in items if i["subject"] == INVOICE)
        assert "Acme Sitework LLC" in invoice["entities_ai"]  # from the PDF attachment
        assert "Example Ridge" in invoice["properties_ai"]  # alias mapped to canonical
        assert "Example Ridge Lot 12" not in invoice["properties_ai"]
        detail = retrieval.get_email(conn, READER, invoice["communication_id"])
        assert detail is not None and detail["entities_ai"] == invoice["entities_ai"]
        found = retrieval.search(conn, READER, "grading invoice", FakeEmbedder())["results"]
        assert any("Acme Sitework LLC" in r["entities_ai"] for r in found)


def test_property_and_entity_filters_narrow_results(ready: str) -> None:
    with search_conn(ready) as conn:
        everything = retrieval.list_email(conn, READER, retrieval.NO_FILTERS, 100)["results"]
        ridge = retrieval.list_email(conn, READER, retrieval.Filters(property="ridge"), 100)
        acme = retrieval.list_email(conn, READER, retrieval.Filters(entity="ACME sitework"), 100)
        wildcard = retrieval.list_email(conn, READER, retrieval.Filters(entity="%"), 100)
    assert 0 < len(ridge["results"]) < len(everything)
    assert all(any("ridge" in p.lower() for p in r["properties_ai"]) for r in ridge["results"])
    assert INVOICE in {r["subject"] for r in acme["results"]}
    assert wildcard["results"] == []  # "%" is matched as text, not as a wildcard


def test_names_cli_add_list_remove_and_audit(
    ready: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("PGDATABASE", ready)
    monkeypatch.setenv("DEPLOYMENT_ENVIRONMENT", "local")
    assert (
        main(
            [
                "--actor",
                "pytest",
                "names",
                "add",
                "entity",
                "Capital Company LLC",
                "--alias",
                "CapCo",
            ]
        )
        == 0
    )
    assert (
        main(
            [
                "--actor",
                "pytest",
                "names",
                "add",
                "property",
                "Willow Creek Retail",
                "--alias",
                "WC Retail",
                "--alias",
                "Willow Creek",
            ]
        )
        == 0
    )
    out = capsys.readouterr().out
    assert "entity    Capital Company LLC  (also: CapCo)" in out
    assert "Willow Creek Retail  (also: WC Retail, Willow Creek)" in out
    assert main(["--actor", "pytest", "names", "remove", "entity", "capital company llc"]) == 0
    assert "Capital Company LLC" not in capsys.readouterr().out
    assert main(["--actor", "pytest", "names", "remove", "entity", "Nobody LLC"]) == 1
    with connect(dbname=ready) as conn:
        actions = [
            r["action"]
            for r in conn.execute(
                "SELECT action FROM audit_events WHERE action LIKE 'known_name%%' ORDER BY id"
            ).fetchall()
        ]
        with pytest.raises(OperatorError):
            add_name(conn, "pytest", "building", "X")
    assert actions[-3:] == ["known_name_set", "known_name_set", "known_name_removed"]


def test_a_backfill_request_may_reach_back_93_days(ready: str) -> None:
    with connect(dbname=ready, role="ci_operator") as conn:
        ops.request_run(conn, "pytest", 24 * 93)
        assert ops.read_schedule(conn).run_requested_hours == 2232
        with pytest.raises(OperatorError, match="2232"):
            ops.request_run(conn, "pytest", 2233)
        conn.rollback()
    with connect(dbname=ready) as conn, pytest.raises(psycopg.errors.CheckViolation):
        conn.execute("UPDATE ops_settings SET run_requested_hours = 2233")


class FrozenDatetime(datetime):
    @classmethod
    def now(cls, tz: object = None) -> FrozenDatetime:
        return cls(2026, 10, 28, 6, 0, tzinfo=UTC)


def test_run_capture_since_a_date(
    ready: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import ci_cli.main as cli

    monkeypatch.setenv("PGDATABASE", ready)
    monkeypatch.setenv("DEPLOYMENT_ENVIRONMENT", "local")
    monkeypatch.setattr(cli, "datetime", FrozenDatetime)
    assert main(["--actor", "pytest", "run-capture", "--since", "2026-09-01"]) == 0
    assert "1369 hours back" in capsys.readouterr().out
    with connect(dbname=ready) as conn:
        assert ops.read_schedule(conn).run_requested_hours == 1369
        conn.execute("UPDATE ops_settings SET run_requested_at = NULL, run_requested_hours = NULL")
        conn.commit()
    assert main(["--actor", "pytest", "run-capture", "--since", "2026-07-01"]) == 1
    assert "93 days" in capsys.readouterr().err
