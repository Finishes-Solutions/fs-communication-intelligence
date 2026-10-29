"""The knowledge base end to end: what processing records, what each caller may see
of it, and the corrections that replace an approval step (migration 0016)."""

from collections.abc import Iterator, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import pytest

from ci_cli.main import main
from ci_cli.seed import seed
from ci_core.db import AppRole, Connection, connect
from ci_core.operator import (
    OperatorError,
    exclude_participant,
    hide_knowledge,
    merge_knowledge,
    merge_knowledge_many,
    reclassify_knowledge,
    rename_knowledge,
    set_knowledge_detail,
    unmerge_knowledge,
)
from ci_email import knowledge_store
from ci_email.capture import capture_window
from ci_email.fixture import FixtureGraphClient
from ci_email.process import Backends, _requeue, process_all
from ci_processing.classification import (
    ClassificationInput,
    ClassificationResult,
    FakeClassifier,
    KnownName,
)
from ci_processing.embedding import FakeEmbedder
from ci_processing.knowledge import Source, name_key, validate
from ci_processing.screening import FakeScreener
from ci_retrieval import knowledge as kb
from ci_retrieval.identity import READER_ALL, READER_OWN, Principal

PAT = "pat.example@finishessolutions.example"
MORGAN = "morgan.lee@finishessolutions.example"
READER = Principal("reader-oid", None, (READER_ALL,))
PAT_OWN = Principal("pat-oid", PAT, (READER_OWN,))
MORGAN_OWN = Principal("morgan-oid", MORGAN, (READER_OWN,))
NOBODY = Principal("nobody-oid", None, ())
DRAW = "Draw request 9 documentation - Example Commons loan"
LEASE = "Suite 210 lease renewal terms"


def fact(subject: str, relation: str, obj: str, role: str | None, evidence: str) -> dict[str, Any]:
    return {"subject": subject, "relation": relation, "object": obj, "role": role,
            "evidence": evidence}  # fmt: skip


def org(written: str) -> dict[str, Any]:
    return {"kind": "organization", "written": written, "known": None, "domain": None}


def prop(written: str) -> dict[str, Any]:
    return {"kind": "property", "written": written, "known": None, "domain": None}


# What a model might answer for some fixture emails, on top of the fake rules.
INJECTED: dict[str, dict[str, Any]] = {
    DRAW: {
        "names": [org("Example Community Bank"), prop("Example Commons")],
        "people": [],
        "relations": [
            fact(
                "Example Community Bank",
                "role_on",
                "Example Commons",
                "lender",
                "the Example Commons construction loan, the lender needs",
            )
        ],
        "terms": [{"term": "pay application", "meaning": "a contractor's request for payment"}],
    },
    "Invoice 4471 for Lot 12 grading - payment due Friday": {
        "names": [org("Acme Sitework LLC"), prop("Example Ridge")],
        "people": [],
        "relations": [
            fact("Acme Sitework LLC", "role_on", "Example Ridge", "general_contractor", "grading")
        ],
        "terms": [],
    },
    "Example Ridge site meeting notes": {
        "names": [prop("Example Ridge")],
        "people": [
            {
                "name": "Chris Patel",
                "email": "chris.patel@example-gc.example",
                "organization": None,
                "title": "Site Superintendent",
            }
        ],
        "relations": [
            fact(
                "chris.patel@example-gc.example",
                "role_on",
                "Example Ridge",
                "general_contractor",
                "site meeting",
            )
        ],
        "terms": [],
    },
    # Restricted (SSN-like value): must never reach the knowledge base.
    "New hire paperwork": {
        "names": [],
        "people": [
            {
                "name": "HR Desk",
                "email": "hr@finishessolutions.example",
                "organization": None,
                "title": "Payroll",
            }
        ],
        "relations": [],
        "terms": [{"term": "payroll", "meaning": "salary processing"}],
    },
}


class InjectingClassifier(FakeClassifier):
    """The fake rules, plus a model-like knowledge answer for chosen subjects; it
    goes through the same validation as a real answer."""

    def __init__(self) -> None:
        self.learned_seen: list[tuple[str, ...]] = []

    def classify(
        self, item: ClassificationInput, known: Sequence[KnownName] = ()
    ) -> ClassificationResult:
        self.learned_seen.append(tuple(n.name for n in item.learned))
        result = super().classify(item, known)
        extra = INJECTED.get(item.subject)
        if extra is None:
            return result
        injected = validate(extra, item.source(), item.learned)
        base = result.knowledge
        assert base is not None
        return replace(
            result,
            knowledge=replace(
                base,
                names=base.names + injected.names,
                people=base.people + injected.people,
                relations=base.relations + injected.relations,
                terms=base.terms + injected.terms,
            ),
        )


@pytest.fixture(scope="module")
def ready(database: str) -> Iterator[str]:
    def connector(role: AppRole | None = None) -> Connection:
        return connect(dbname=database, role=role)

    with connector() as conn:
        seed(conn, "pytest")
        conn.commit()
    graph = FixtureGraphClient.from_directory()
    capture_window(
        connector,
        graph,
        hours=744,
        include_sent=True,
        profile={"p": 1},
        now=datetime(2026, 10, 28, tzinfo=UTC),
    )
    process_all(
        lambda: connector("ci_ingest"),
        Backends(FakeScreener(), InjectingClassifier(), FakeEmbedder(), graph=graph),
        workers=2,
    )
    yield database


def find(db: str, principal: Principal, query: str | None = None, **kw: Any) -> list[Any]:
    with connect(dbname=db, role="ci_search") as conn:
        results: list[Any] = kb.find_knowledge(conn, principal, query, **kw)["results"]
        return results


def profile(db: str, principal: Principal, entity_id: int) -> dict[str, Any] | None:
    with connect(dbname=db, role="ci_search") as conn:
        return kb.get_profile(conn, principal, entity_id)


def entity_id(db: str, query: str, kind: str) -> int:
    match = next(e for e in find(db, READER, query, kind=kind) if e["kind"] == kind)
    return int(match["entity_id"])


def test_facts_terms_and_profiles_from_processed_mail(ready: str) -> None:
    bank = find(ready, READER, "community bank", kind="organization")
    assert [b["name"] for b in bank] == ["Example Community Bank"]
    found = profile(ready, READER, bank[0]["entity_id"])
    assert found is not None
    lender = next(f for f in found["facts"] if f["role"] == "lender")
    assert lender["direction"] == "out" and lender["other"]["name"] == "Example Commons"
    assert (lender["messages"], lender["senders"]) == (1, 1)  # one message: weak evidence
    assert "construction loan" in lender["evidence"]
    assert found["recent_emails"][0]["subject"] == DRAW
    assert lender["communication_id"] == found["recent_emails"][0]["communication_id"]
    assert "without human review" in found_note(ready)

    term = find(ready, READER, "pay application", kind="term")
    assert term and term[0]["meaning"] == "a contractor's request for payment"

    people = find(ready, READER, "northfield", kind="person")
    assert [(p["name"], p["email"]) for p in people] == [
        ("Sam Ortiz", "sam.ortiz@northfield-framing.example")
    ]
    sam = profile(ready, READER, people[0]["entity_id"])
    assert sam is not None
    works = next(f for f in sam["facts"] if f["relation"] == "works_for")
    assert works["other"]["name"] == "Northfield Framing Co."
    # CO 8 is in three mailboxes: one message, not three.
    assert works["messages"] == 2 and sam["entity"]["messages"] == 2


def found_note(db: str) -> str:
    with connect(dbname=db, role="ci_search") as conn:
        note: str = kb.find_knowledge(conn, READER)["note"]
        return note


def test_restricted_and_phishing_mail_teach_nothing(ready: str) -> None:
    with connect(dbname=ready) as conn:
        keys = {r["key"] for r in conn.execute("SELECT key FROM kg_entities").fetchall()}
    assert "hr@finishessolutions.example" not in keys  # restricted (SSN-like value)
    assert "payroll" not in keys
    # The look-alike sender of the phishing fixture, signed "Jordan Rivera / Acme Sitework LLC".
    assert "jordan.rivera@acme-sitework-billing.example" not in keys
    assert find(ready, READER, "acme-sitework-billing") == []
    assert {p["email"] for p in find(ready, READER, kind="person")} >= {
        "jordan.rivera@acme-sitework.example"  # the real one, from the real invoice
    }


def test_only_what_the_callers_own_mail_says(ready: str) -> None:
    assert find(ready, NOBODY) == []
    with connect(dbname=ready, role="ci_search") as conn:
        assert kb.find_knowledge(conn, NOBODY)["authorized"] is False
    # The lease renewal is only in Morgan's mailbox; the draw request only in Pat's.
    assert find(ready, PAT_OWN, "example leasing") == []
    assert find(ready, PAT_OWN, "alex.kim") == []
    assert [e["name"] for e in find(ready, PAT_OWN, "community bank")] == ["Example Community Bank"]
    assert find(ready, MORGAN_OWN, "community bank") == []
    bank = entity_id(ready, "community bank", "organization")
    assert profile(ready, MORGAN_OWN, bank) is None  # an id from elsewhere finds nothing
    assert profile(ready, NOBODY, bank) is None
    commons = entity_id(ready, "example commons", "property")
    morgan_view = profile(ready, MORGAN_OWN, commons)
    assert morgan_view is not None
    assert all(f["other"]["name"] != "Example Community Bank" for f in morgan_view["facts"])
    assert morgan_view["entity"]["messages"] == 1  # the draw request is not counted for Morgan


def test_single_roles_mark_the_latest_holder_current(ready: str) -> None:
    ridge = profile(ready, READER, entity_id(ready, "example ridge", "property"))
    assert ridge is not None
    gc = [f for f in ridge["facts"] if f["role"] == "general_contractor"]
    assert {f["other"]["name"] for f in gc} == {"Acme Sitework LLC", "Chris Patel"}
    assert [f["current"] for f in gc].count(True) == 1
    current = next(f for f in gc if f["current"])
    assert current["last_seen"] == max(f["last_seen"] for f in gc)


def test_a_person_excluded_as_participant_is_never_shown(ready: str) -> None:
    ghost = "ghost.person@excluded-co.example"
    with connect(dbname=ready) as conn:
        version = conn.execute(
            "SELECT c.active_version_id FROM communications c WHERE c.title = %s", (LEASE,)
        ).fetchone()
        assert version is not None
        ghost_id = conn.execute(
            "INSERT INTO kg_entities (kind, key, name) VALUES ('person', %s, 'Ghost Person')"
            " RETURNING id",
            (ghost,),
        ).fetchone()["id"]  # type: ignore[index]
        conn.execute(
            "INSERT INTO kg_mentions (version_id, entity_id, written) VALUES (%s, %s, 'Ghost')",
            (version["active_version_id"], ghost_id),
        )
        conn.commit()
    try:
        assert [p["email"] for p in find(ready, READER, "ghost")] == [ghost]
        with connect(dbname=ready) as conn:
            exclude_participant(conn, "@excluded-co.example", "pytest", reason="pytest_exclusion")
            conn.commit()
        assert find(ready, READER, "ghost") == []
        assert find(ready, READER, "example leasing")  # the message itself stays visible
    finally:
        with connect(dbname=ready) as conn:
            conn.execute("DELETE FROM kg_mentions WHERE entity_id = %s", (ghost_id,))
            conn.execute("DELETE FROM kg_entities WHERE id = %s", (ghost_id,))
            conn.execute("DELETE FROM exclusions WHERE reason_code = 'pytest_exclusion'")
            conn.commit()


def test_corrections_merge_hide_rename_and_undo(ready: str) -> None:
    acme = entity_id(ready, "acme sitework", "organization")
    northfield = entity_id(ready, "northfield framing", "organization")
    jordan = entity_id(ready, "jordan.rivera@acme-sitework.example", "person")

    def operator() -> Connection:
        return connect(dbname=ready, role="ci_operator")

    with operator() as conn:
        merge_knowledge(conn, "pytest", acme, northfield)
        conn.commit()
    merged = find(ready, READER, "acme sitework", kind="organization")
    assert [m["entity_id"] for m in merged] == [northfield]
    assert "Acme Sitework LLC" in merged[0]["also_written"]
    with operator() as conn, pytest.raises(OperatorError):
        merge_knowledge(conn, "pytest", jordan, northfield)  # a person is not an organization
    with operator() as conn:
        unmerge_knowledge(conn, "pytest", acme)
        hide_knowledge(conn, "pytest", acme)
        conn.commit()
    assert find(ready, READER, "acme sitework", kind="organization") == []
    jordan_view = profile(ready, READER, jordan)
    assert jordan_view is not None and jordan_view["facts"] == []  # facts naming it are gone
    assert profile(ready, READER, acme) is None
    with operator() as conn:
        hide_knowledge(conn, "pytest", acme, hidden=False)
        rename_knowledge(conn, "pytest", acme, "Acme Sitework (grading)")
        conn.commit()
    assert find(ready, READER, "grading", kind="organization")[0]["name"] == (
        "Acme Sitework (grading)"
    )
    with operator() as conn:
        rename_knowledge(conn, "pytest", acme, None)
        conn.commit()
    with connect(dbname=ready) as conn:
        actions = {
            r["action"]
            for r in conn.execute(
                "SELECT action FROM audit_events WHERE action LIKE 'knowledge_%%'"
            ).fetchall()
        }
    assert {
        "knowledge_merge",
        "knowledge_unmerge",
        "knowledge_hide",
        "knowledge_unhide",
        "knowledge_rename",
    } <= actions
    assert find(ready, READER, "acme sitework", kind="organization")[0]["name"] == (
        "Acme Sitework LLC"
    )
    with connect(dbname=ready, role="ci_search") as conn:
        audits = conn.execute("SELECT count(*) AS n FROM published_kg_mentions").fetchone()
        assert audits is not None and audits["n"] > 0


def test_reclassify_and_merge_across_kinds(ready: str) -> None:
    """A name the AI filed under the wrong kind: reclassify it, or merge it into the
    right entry of another kind; later mail repeating the mistake follows the fix."""
    ridge = entity_id(ready, "example ridge", "property")

    def operator() -> Connection:
        return connect(dbname=ready, role="ci_operator")

    with operator() as conn:
        org_id = reclassify_knowledge(conn, "pytest", ridge, "organization", name_key)
        conn.commit()
    assert find(ready, READER, "example ridge", kind="property") == []
    as_org = find(ready, READER, "example ridge", kind="organization")
    assert [e["entity_id"] for e in as_org] == [org_id] and as_org[0]["name"] == "Example Ridge"
    with operator() as conn, pytest.raises(OperatorError):
        reclassify_knowledge(conn, "pytest", ridge, "organization", name_key)  # already one

    # Processing the same email again records the AI's "property" on the old entry,
    # which still shows as the organization.
    with connect(dbname=ready) as conn:
        versions = [
            r["version_id"]
            for r in conn.execute(
                "SELECT version_id FROM kg_mentions WHERE entity_id = %s", (ridge,)
            ).fetchall()
        ]
        conn.execute("DELETE FROM kg_relations WHERE version_id = ANY(%s)", (versions,))
        conn.execute("DELETE FROM kg_mentions WHERE version_id = ANY(%s)", (versions,))
        assert knowledge_store.backfill(conn)["mentions"] > 0
        conn.commit()
        again = conn.execute(
            "SELECT count(*) AS n FROM kg_mentions WHERE entity_id = %s", (ridge,)
        ).fetchone()
    assert again is not None and again["n"] == len(versions)
    assert [e["entity_id"] for e in find(ready, READER, "example ridge")] == [org_id]

    # Undo, then merge it into a person instead: it shows as that person.
    jordan = entity_id(ready, "jordan.rivera@acme-sitework.example", "person")
    with operator() as conn:
        unmerge_knowledge(conn, "pytest", ridge)
        merge_knowledge(conn, "pytest", ridge, jordan)
        conn.commit()
    person = find(ready, READER, "example ridge")
    assert [(e["entity_id"], e["kind"]) for e in person] == [(jordan, "person")]
    assert person[0]["email"] == "jordan.rivera@acme-sitework.example"
    with operator() as conn:
        with pytest.raises(OperatorError):
            merge_knowledge(conn, "pytest", jordan, org_id)  # a person stays a person
        with pytest.raises(OperatorError):
            reclassify_knowledge(conn, "pytest", jordan, "property", name_key)
        conn.rollback()
        unmerge_knowledge(conn, "pytest", ridge)
        # No address known: a person found by name only.
        named = reclassify_knowledge(conn, "pytest", ridge, "person", name_key)
        conn.commit()
    by_name = find(ready, READER, "example ridge", kind="person")
    assert [e["entity_id"] for e in by_name] == [named] and by_name[0]["email"] is None
    with operator() as conn:
        unmerge_knowledge(conn, "pytest", ridge)
        conn.commit()
    assert [e["entity_id"] for e in find(ready, READER, "example ridge")] == [ridge]
    with connect(dbname=ready) as conn:
        row = conn.execute(
            "SELECT details FROM audit_events WHERE action = 'knowledge_reclassify'"
            " ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert row is not None and row["details"]["to_kind"] == "person"


def test_merge_many_spellings_into_one_all_or_nothing(ready: str) -> None:
    """Several entries for one property (spellings the AI kept apart) become one entry
    in a single change; one entry that can't be merged stops the whole change."""
    ridge = entity_id(ready, "example ridge", "property")
    commons = entity_id(ready, "example commons", "property")
    jordan = entity_id(ready, "jordan.rivera@acme-sitework.example", "person")
    with connect(dbname=ready) as conn:
        extra = [
            conn.execute(
                "INSERT INTO kg_entities (kind, key, name) VALUES ('property', %s, %s)"
                " RETURNING id",
                (name_key("property", n), n),
            ).fetchone()["id"]  # type: ignore[index]
            for n in ("Example Ridge Ph 2", "Ex. Ridge")
        ]
        conn.commit()

    def operator() -> Connection:
        return connect(dbname=ready, role="ci_operator")

    try:
        with operator() as conn:
            with pytest.raises(OperatorError, match=f"#{jordan} "):
                merge_knowledge_many(conn, "pytest", [*extra, jordan, commons], ridge)
            conn.rollback()
        with connect(dbname=ready) as conn:
            untouched = conn.execute(
                "SELECT count(*) AS n FROM kg_entities WHERE merged_into IS NOT NULL"
            ).fetchone()
        assert untouched is not None and untouched["n"] == 0  # nothing half-merged

        with operator() as conn:
            # The target itself and duplicates in the list are skipped.
            assert (
                merge_knowledge_many(conn, "pytest", [*extra, commons, ridge, commons], ridge) == 3
            )
            assert merge_knowledge_many(conn, "pytest", [commons], ridge) == 0  # already one
            with pytest.raises(OperatorError):
                merge_knowledge_many(conn, "pytest", [], ridge)
            conn.commit()
        merged = find(ready, READER, "example", kind="property")
        assert [e["entity_id"] for e in merged] == [ridge]
        assert "Example Commons" in merged[0]["also_written"]
    finally:
        with connect(dbname=ready) as conn:
            conn.execute(
                "UPDATE kg_entities SET merged_into = NULL WHERE merged_into = %s", (ridge,)
            )
            conn.execute("DELETE FROM kg_entities WHERE id = ANY(%s)", (extra,))
            conn.commit()
    assert {e["entity_id"] for e in find(ready, READER, "example", kind="property")} >= {
        ridge,
        commons,
    }


def test_meetings_are_stored_and_found(ready: str) -> None:
    """The fifth kind (migration 0019): a meeting, what it is about and who attends,
    from one email's knowledge; and an entry can be reclassified as a meeting."""
    with connect(dbname=ready) as conn:
        row = conn.execute(
            "SELECT active_version_id::text AS v FROM communications WHERE title = %s LIMIT 1",
            (DRAW,),
        ).fetchone()
        assert row is not None
        knowledge = validate(
            {
                "names": [
                    {"kind": "meeting", "written": "Draw review call", "known": None,
                     "domain": None},
                    prop("Example Commons"),
                ],
                "people": [],
                "relations": [
                    fact("Draw review call", "about", "Example Commons", None, "the draw"),
                ],
                "terms": [],
            },
            Source("Draw review call for Example Commons", frozenset()),
        )  # fmt: skip
        counts = knowledge_store.record(conn, row["v"], knowledge)
        conn.commit()
    try:
        assert counts == {"mentions": 2, "relations": 1}
        meeting = find(ready, READER, "draw review", kind="meeting")
        assert [m["name"] for m in meeting] == ["Draw review call"]
        found = profile(ready, READER, meeting[0]["entity_id"])
        assert found is not None
        assert [(f["relation"], f["other"]["name"]) for f in found["facts"]] == [
            ("about", "Example Commons")
        ]
        term = entity_id(ready, "pay application", "term")
        with connect(dbname=ready, role="ci_operator") as conn:
            as_meeting = reclassify_knowledge(conn, "pytest", term, "meeting", name_key)
            unmerge_knowledge(conn, "pytest", term)
            conn.commit()
        assert as_meeting != term
    finally:
        with connect(dbname=ready) as conn:
            ids = [
                r["id"]
                for r in conn.execute(
                    "SELECT id FROM kg_entities WHERE kind = 'meeting'"
                ).fetchall()
            ]
            conn.execute("DELETE FROM kg_relations WHERE subject_id = ANY(%s)", (ids,))
            conn.execute("DELETE FROM kg_mentions WHERE entity_id = ANY(%s)", (ids,))
            conn.execute("DELETE FROM kg_entities WHERE id = ANY(%s)", (ids,))
            conn.commit()


def test_title_is_the_most_stated_unless_an_admin_set_it(ready: str) -> None:
    """One email with the wrong title (another person's signature) must not replace
    the title most emails gave; an admin's title wins over both (migration 0020)."""
    with connect(dbname=ready) as conn:
        versions = [
            r["v"]
            for r in conn.execute(
                "SELECT c.active_version_id::text AS v FROM communications c"
                " JOIN communication_versions cv ON cv.id = c.active_version_id"
                " WHERE cv.processing_state = 'published' ORDER BY c.occurred_at LIMIT 3"
            ).fetchall()
        ]
        person = conn.execute(
            "INSERT INTO kg_entities (kind, key, name) VALUES"
            " ('person', 'pat.owner@example.com', 'Pat Owner') RETURNING id"
        ).fetchone()["id"]  # type: ignore[index]
        for version, title in zip(versions, ["Owner", "Owner", "Director of Sales"], strict=True):
            conn.execute(
                "INSERT INTO kg_mentions (version_id, entity_id, written, detail)"
                " VALUES (%s, %s, 'Pat Owner', %s)",
                (version, person, title),
            )
        conn.commit()
    try:
        # The latest email says "Director of Sales"; two earlier ones say "Owner".
        assert find(ready, READER, "pat.owner", kind="person")[0]["title"] == "Owner"
        with connect(dbname=ready, role="ci_operator") as conn:
            set_knowledge_detail(conn, "pytest", person, "  CEO /  Owner ")
            conn.commit()
        assert find(ready, READER, "pat.owner", kind="person")[0]["title"] == "CEO / Owner"
        with connect(dbname=ready, role="ci_operator") as conn:
            with pytest.raises(OperatorError):
                set_knowledge_detail(
                    conn, "pytest", entity_id(ready, "example ridge", "property"), "x"
                )
            conn.rollback()
            set_knowledge_detail(conn, "pytest", person, None)
            conn.commit()
        assert find(ready, READER, "pat.owner", kind="person")[0]["title"] == "Owner"
    finally:
        with connect(dbname=ready) as conn:
            conn.execute("DELETE FROM kg_mentions WHERE entity_id = %s", (person,))
            conn.execute("DELETE FROM kg_entities WHERE id = %s", (person,))
            conn.commit()


def test_knowledge_graph_nodes_links_and_connectors(ready: str) -> None:
    with connect(dbname=ready, role="ci_search") as conn:
        graph = kb.knowledge_graph(conn, READER, limit=40, min_shared=1)
        nobody = kb.knowledge_graph(conn, NOBODY, limit=40, min_shared=1)
        orgs = kb.knowledge_graph(conn, READER, kinds=["organization"], min_shared=1)
    ids = {n["entity_id"] for n in graph["nodes"]}
    assert graph["nodes"] and graph["links"]
    assert all(x["source"] in ids and x["target"] in ids for x in graph["links"])
    assert all(x["shared"] >= 1 for x in graph["links"])
    assert max(n["connector"] for n in graph["nodes"]) == 1.0
    assert all(0 <= n["connector"] <= 1 for n in graph["nodes"])
    degree = {i: 0 for i in ids}
    for x in graph["links"]:
        degree[x["source"]] += 1
        degree[x["target"]] += 1
    assert {n["entity_id"]: n["links"] for n in graph["nodes"]} == degree
    # A fact between two nodes rides on their link.
    assert any("lender" in x["facts"] for x in graph["links"])
    assert nobody["nodes"] == [] and nobody["links"] == []
    assert {n["kind"] for n in orgs["nodes"]} == {"organization"}

    bank = entity_id(ready, "community bank", "organization")
    with connect(dbname=ready, role="ci_search") as conn:
        around = kb.knowledge_graph(conn, READER, focus=bank, min_shared=1)
    focus = [n for n in around["nodes"] if n["focus"]]
    assert [n["entity_id"] for n in focus] == [bank]
    assert any(bank in (x["source"], x["target"]) for x in around["links"])


def test_betweenness_scores_the_bridge() -> None:
    # a - b - c, and b - d: every path between a, c and d runs through b.
    score = kb._betweenness([1, 2, 3, 4], [(1, 2), (2, 3), (2, 4)])
    assert score == {1: 0.0, 2: 1.0, 3: 0.0, 4: 0.0}
    assert kb._betweenness([1, 2], []) == {1: 0.0, 2: 0.0}


def test_search_role_cannot_read_the_base_tables(ready: str) -> None:
    import psycopg

    for table in ("kg_entities", "kg_mentions", "kg_relations"):
        with (
            connect(dbname=ready, role="ci_search") as conn,
            pytest.raises(psycopg.errors.InsufficientPrivilege),
        ):
            conn.execute(f"SELECT 1 FROM {table}")  # noqa: S608


def test_learned_names_reach_the_classifier_and_requeue_clears_knowledge(ready: str) -> None:
    with connect(dbname=ready, role="ci_ingest") as conn:
        learned = knowledge_store.learned_names(conn)
    by_name = {n.name: n for n in learned}
    assert "Example Commons" in by_name  # the lease and the draw request
    assert "Example Ridge" in by_name
    assert "Example Community Bank" not in by_name  # one message is not enough
    with connect(dbname=ready, role="ci_ingest") as conn:
        version = conn.execute(
            "SELECT active_version_id::text AS v FROM communications WHERE title = %s",
            ("Example Ridge site meeting notes",),
        ).fetchone()
        assert version is not None
        _requeue(conn, [version["v"]])
        left = conn.execute(
            "SELECT count(*) AS n FROM kg_mentions WHERE version_id = %s", (version["v"],)
        ).fetchone()
        assert left == {"n": 0}
        conn.commit()
    spy = InjectingClassifier()
    process_all(
        lambda: connect(dbname=ready, role="ci_ingest"),
        Backends(FakeScreener(), spy, FakeEmbedder()),
        workers=1,
    )
    assert spy.learned_seen and "Example Ridge" in spy.learned_seen[0]
    with connect(dbname=ready) as conn:
        linked = conn.execute(
            "SELECT m.entity_id FROM kg_mentions m JOIN kg_entities e ON e.id = m.entity_id"
            " WHERE m.version_id = %s AND e.kind = 'property'",
            (version["v"],),
        ).fetchone()
    assert linked == {"entity_id": by_name["Example Ridge"].entity_id}  # linked via `known`


def test_backfill_rebuilds_knowledge_without_a_model_call(
    ready: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    with connect(dbname=ready) as conn:
        version = conn.execute(
            "SELECT active_version_id::text AS v FROM communications WHERE title = %s", (DRAW,)
        ).fetchone()
        assert version is not None
        conn.execute("DELETE FROM kg_relations WHERE version_id = %s", (version["v"],))
        conn.execute("DELETE FROM kg_mentions WHERE version_id = %s", (version["v"],))
        conn.commit()
    assert find(ready, READER, "pay application", kind="term") == []
    monkeypatch.setenv("PGDATABASE", ready)
    monkeypatch.setenv("DEPLOYMENT_ENVIRONMENT", "local")
    assert main(["--actor", "pytest", "knowledge", "backfill"]) == 0
    out = capsys.readouterr().out
    assert "messages" in out and "skipped_phishing" in out
    assert find(ready, READER, "pay application", kind="term")  # from the stored answer
    bank = profile(ready, READER, entity_id(ready, "community bank", "organization"))
    assert bank is not None and any(f["role"] == "lender" for f in bank["facts"])
    with connect(dbname=ready) as conn:  # an older run without a knowledge answer
        conn.execute(
            "UPDATE classification_runs SET knowledge = NULL WHERE version_id = %s",
            (version["v"],),
        )
        conn.execute("DELETE FROM kg_relations WHERE version_id = %s", (version["v"],))
        conn.execute("DELETE FROM kg_mentions WHERE version_id = %s", (version["v"],))
        counts = knowledge_store.backfill(conn)
        conn.commit()
        kinds = {
            r["kind"]
            for r in conn.execute(
                "SELECT e.kind FROM kg_mentions m JOIN kg_entities e ON e.id = m.entity_id"
                " WHERE m.version_id = %s",
                (version["v"],),
            ).fetchall()
        }
    # Names only: the fake rules found the property but no legal entity in this email.
    assert counts["messages"] == 1 and kinds == {"property"}


def test_backfill_commits_in_batches(ready: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """A long backfill must not hold one transaction (processing would wait on the same
    names); each batch is committed and reported, so an interrupted run keeps its work."""
    monkeypatch.setattr(knowledge_store, "BACKFILL_BATCH", 1)
    with connect(dbname=ready) as conn:
        versions = [
            r["v"]
            for r in conn.execute(
                "SELECT DISTINCT version_id::text AS v FROM kg_mentions LIMIT 2"
            ).fetchall()
        ]
        assert len(versions) == 2
        conn.execute("DELETE FROM kg_relations WHERE version_id = ANY(%s::uuid[])", (versions,))
        conn.execute("DELETE FROM kg_mentions WHERE version_id = ANY(%s::uuid[])", (versions,))
        conn.commit()
    seen: list[int] = []

    def progress(counts: Any) -> None:
        seen.append(counts["messages"])
        with connect(dbname=ready) as other:  # visible to others: already committed
            row = other.execute(
                "SELECT count(DISTINCT version_id) AS n FROM kg_mentions"
                " WHERE version_id = ANY(%s::uuid[])",
                (versions,),
            ).fetchone()
            assert row is not None and row["n"] == counts["messages"]

    with connect(dbname=ready) as conn:
        counts = knowledge_store.backfill(conn, progress=progress)
    assert counts["messages"] == 2 and seen == [1, 2]


def test_cli_corrections(ready: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PGDATABASE", ready)
    monkeypatch.setenv("DEPLOYMENT_ENVIRONMENT", "local")
    firm = entity_id(ready, "northfield framing", "organization")
    assert main(["--actor", "pytest", "knowledge", "hide", str(firm)]) == 0
    assert find(ready, READER, "northfield framing", kind="organization") == []
    assert main(["--actor", "pytest", "knowledge", "unhide", str(firm)]) == 0
    assert find(ready, READER, "northfield framing", kind="organization")
    assert main(["--actor", "pytest", "knowledge", "unmerge", str(firm)]) == 1  # not merged
    assert main(["--actor", "pytest", "knowledge", "rename", str(firm)]) == 1  # no name given
