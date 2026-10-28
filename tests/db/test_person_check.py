"""Step 2 of action items end to end: profiles, per-person routes, and the lists built from them."""

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest

from ci_cli.seed import seed
from ci_core.db import AppRole, Connection, connect
from ci_core.operator import OperatorError, set_person
from ci_email.capture import capture_window
from ci_email.fixture import FixtureGraphClient
from ci_email.process import Backends, process_all, requeue_for_remask
from ci_processing.classification import ClassificationInput, FakeClassifier
from ci_processing.embedding import FakeEmbedder
from ci_processing.routing import FakePersonRouter, PersonProfile, Route
from ci_processing.screening import FakeScreener
from ci_retrieval import search as retrieval
from ci_retrieval.identity import READER_ALL, Principal

PAT = "pat.example@finishessolutions.example"
MORGAN = "morgan.lee@finishessolutions.example"
READER = Principal("reader-oid", None, (READER_ALL,))
INVOICE = "Invoice 4471 for Lot 12 grading - payment due Friday"


class CountingRouter(FakePersonRouter):
    def __init__(self) -> None:
        self.calls = 0

    def route(self, item: ClassificationInput, person: PersonProfile) -> Route:
        self.calls += 1
        return super().route(item, person)


def backends(router: FakePersonRouter) -> Backends:
    return Backends(
        FakeScreener(),
        FakeClassifier(),
        FakeEmbedder(),
        graph=FixtureGraphClient.from_directory(),
        extraction_enabled=True,
        router=router,
    )


@pytest.fixture(scope="module")
def ready(database: str) -> Iterator[dict[str, Any]]:
    def connector(role: AppRole | None = None) -> Connection:
        return connect(dbname=database, role=role)

    with connector() as conn:
        seed(conn, "pytest")
        set_person(
            conn,
            "pytest",
            "pat",
            display_name="Pat Example",
            addresses=[PAT],
            role="Approves invoices",
            not_to_confuse="Pat Junior (pat.jr@elsewhere.example)",
        )
        set_person(conn, "pytest", "morgan", display_name="Morgan Lee", addresses=[MORGAN])
        conn.commit()
    capture_window(
        connector,
        FixtureGraphClient.from_directory(),
        hours=744,
        include_sent=True,
        profile={"p": 1},
        now=datetime(2026, 10, 28, tzinfo=UTC),
    )
    router = CountingRouter()
    counts = process_all(lambda: connector("ci_ingest"), backends(router), workers=1)
    assert counts.get("failed", 0) == 0, counts
    yield {"db": database, "router": router}


def routes(db: str, person: str) -> list[dict[str, Any]]:
    with connect(dbname=db) as conn:
        return conn.execute(
            "SELECT c.title, r.decision, r.reason_code, r.probability, r.model, r.policy_version"
            " FROM action_routes r JOIN communication_versions v ON v.id = r.version_id"
            " JOIN communications c ON c.id = v.communication_id WHERE r.person_key = %s",
            (person,),
        ).fetchall()


def test_every_published_version_gets_a_decision_per_person(ready: dict[str, Any]) -> None:
    db = ready["db"]
    with connect(dbname=db) as conn:
        published = conn.execute(
            "SELECT count(*) AS n FROM communication_versions WHERE processing_state = 'published'"
        ).fetchone()
    pat, morgan = routes(db, "pat"), routes(db, "morgan")
    assert len(pat) == len(morgan) == published["n"]  # type: ignore[index]
    invoice = next(r for r in pat if r["title"] == INVOICE)
    assert (invoice["decision"], invoice["reason_code"]) == ("yes", "named_directly")
    morgan_invoice = next(r for r in morgan if r["title"] == INVOICE)
    assert (morgan_invoice["decision"], morgan_invoice["reason_code"], morgan_invoice["model"]) == (
        "no",
        "not_a_recipient",
        "rules",
    )
    assert all(r["probability"] is None for r in pat + morgan if r["model"] == "rules")
    # The model was asked only for candidates, never for rule-based answers.
    asked = sum(1 for r in pat + morgan if r["model"] != "rules")
    assert 0 < ready["router"].calls <= asked


def test_person_list_comes_from_the_person_check(ready: dict[str, Any]) -> None:
    with connect(dbname=ready["db"], role="ci_search") as conn:
        report = retrieval.list_actionable(conn, READER, person=PAT, hours=24 * 60)
        by_key = retrieval.list_actionable(conn, READER, person="pat", hours=24 * 60)
        no_profile = retrieval.list_actionable(
            conn, READER, person="ap@finishessolutions.example", hours=24 * 60
        )
    assert report["person_check"] == {"key": "pat", "name": "Pat Example"}
    assert INVOICE in {a["subject"] for a in report["actions"]}
    invoice = next(a for a in report["actions"] if a["subject"] == INVOICE)
    assert invoice["person_check_ai"]["reason"] == "named_directly"
    assert isinstance(report["possible"], list)
    assert {a["communication_id"] for a in by_key["actions"]} == {
        a["communication_id"] for a in report["actions"]
    }
    # No profile: the rule decides, and there is no "possible" list.
    assert no_profile["person_check"] is None and no_profile["possible"] == []


def test_profile_changes_are_validated_and_recompute_routes(ready: dict[str, Any]) -> None:
    db = ready["db"]
    with connect(dbname=db) as conn, pytest.raises(OperatorError):
        set_person(conn, "pytest", "Bad Key", display_name="X", addresses=[PAT])
    with connect(dbname=db) as conn, pytest.raises(OperatorError):
        set_person(conn, "pytest", "pat", display_name="Pat", addresses=["not-an-address"])
    before = {r["policy_version"] for r in routes(db, "pat") if r["model"] != "rules"}
    with connect(dbname=db) as conn:
        set_person(
            conn,
            "pytest",
            "pat",
            display_name="Pat Example",
            addresses=[PAT],
            role="Approves invoices and change orders",
            not_to_confuse="Pat Junior",
        )
        assert conn.execute(
            "SELECT 1 FROM audit_events WHERE action = 'person_profile_set' AND target_id = 'pat'"
        ).fetchone()
        conn.commit()
    with connect(dbname=db, role="ci_ingest") as conn:
        assert requeue_for_remask(conn) > 0
        conn.commit()
    assert routes(db, "pat") == []  # cleared with the rest of the derived data
    counts = process_all(
        lambda: connect(dbname=db, role="ci_ingest"), backends(FakePersonRouter()), workers=1
    )
    assert counts.get("failed", 0) == 0
    after = {r["policy_version"] for r in routes(db, "pat") if r["model"] != "rules"}
    assert after and after.isdisjoint(before)
