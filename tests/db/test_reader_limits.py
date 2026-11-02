"""Per-reader limits (migration 0024): one reader stops seeing some mail, in every
tool, except mail they are on; other readers are unaffected.

The synthetic tenant stands in for the real case (CLAUDE.md section 3a, 2026-11-02):
Morgan plays the limited reader (Will), Pat the person whose mail is kept from them
(Chuck), example-gc.example a whole company (PNS Capital), and a term the words
that name it.
"""

from typing import Any

import psycopg
import pytest
from test_pipeline import EMBEDDER, MORGAN, PAT, pipeline  # noqa: F401

from ci_core.operator import OperatorError, limit_reader, list_reader_limits
from ci_retrieval import knowledge, reports
from ci_retrieval import search as retrieval
from ci_retrieval.identity import READER_ALL, Principal

LIMITED = Principal("morgan-oid", MORGAN, (READER_ALL,))
READER = Principal("reader-oid", "reader@finishessolutions.example", (READER_ALL,))
NO_ADDRESS = Principal("someone-oid", None, (READER_ALL,))
SEEN_BY_MORGAN = {
    "Change Order 8 - Building D plumbing reroute",  # in Pat's mailbox, but Morgan is on it
    "Repeated delays and defects on punch list",  # from the limited domain, to Morgan
    "Site photos follow-up",
    "Suite 210 lease renewal terms",
}


def visible(p: dict[str, Any], who: Principal) -> set[str]:
    """Subjects this reader can list (copies of one email are listed once)."""
    with p["connect"]("ci_search") as conn:
        items = retrieval.list_email(conn, who, retrieval.NO_FILTERS, 100)["results"]
    return {i["subject"] for i in items}


def ids_of(p: dict[str, Any], title: str, mailbox: str | None = None) -> list[str]:
    with p["connect"]() as conn:
        return [
            r["id"]
            for r in conn.execute(
                "SELECT pc.id::text FROM published_communications pc"
                " JOIN published_email_messages em ON em.communication_id = pc.id"
                " WHERE pc.title = %s AND (%s::text IS NULL OR em.mailbox_address = %s)",
                (title, mailbox, mailbox),
            ).fetchall()
        ]


@pytest.fixture(scope="module")
def limited(pipeline: dict[str, Any]) -> dict[str, Any]:  # noqa: F811
    before = visible(pipeline, LIMITED)
    with pipeline["connect"]() as conn:
        for kind, value in (
            ("mailbox", PAT),  # Chuck's mailbox
            ("person", PAT),  # any email Chuck is on
            ("domain", "@example-gc.example"),  # PNS Capital's domain
            ("term", "Banking Details"),  # words naming PNS Capital
        ):
            limit_reader(conn, MORGAN.upper(), kind, value, "pytest", reason="private_business")
        conn.commit()
    return {**pipeline, "before": before}


def test_without_limits_the_reader_sees_everything(limited: dict[str, Any]) -> None:
    assert len(limited["before"]) == 13
    assert visible(limited, READER) == limited["before"]


def test_limits_hide_matching_mail_except_mail_the_reader_is_on(limited: dict[str, Any]) -> None:
    seen = visible(limited, LIMITED)
    assert seen == SEEN_BY_MORGAN
    # Other readers are unaffected.
    assert visible(limited, READER) == limited["before"]


def test_hidden_mail_is_hidden_in_every_tool(limited: dict[str, Any]) -> None:
    lien = ids_of(limited, "Notice of mechanic's lien - Example Ridge Phase 2")[0]
    banking = ids_of(limited, "Updated banking details for future payments")[0]
    kept = ids_of(limited, "Change Order 8 - Building D plumbing reroute", PAT)[0]
    with limited["connect"]("ci_search") as conn:
        for cid in (lien, banking):
            assert retrieval.get_email(conn, LIMITED, cid) is None
            assert retrieval.get_email(conn, READER, cid) is not None
            assert retrieval.get_thread(conn, LIMITED, cid) is None
        # Every copy of an email Morgan is on stays, even the one in Pat's mailbox.
        assert retrieval.get_email(conn, LIMITED, kept) is not None
        found = retrieval.search(
            conn, LIMITED, "lien banking invoice draw change order", EMBEDDER, retrieval.NO_FILTERS
        )
        assert {r["subject"] for r in found["results"]} <= SEEN_BY_MORGAN
        by_box = reports.report_groups(conn, LIMITED, "messages", "mailbox")["groups"]
        counts = {g["key"]: g["messages"] for g in by_box}
        assert counts.get(PAT) == 1 and "ap@finishessolutions.example" in counts
        assert counts.get("ap@finishessolutions.example") == 1  # banking details hidden
        links = retrieval.file_links(conn, LIMITED, [lien, banking, kept])
        assert [x["communication_id"] for x in links["emails"]] == [kept]
        mine = knowledge.find_knowledge(conn, LIMITED, "example ridge")["results"]
        everyone = knowledge.find_knowledge(conn, READER, "example ridge")["results"]
        assert sum(r["messages"] for r in mine) < sum(r["messages"] for r in everyone)


def test_a_caller_without_an_address_gets_nothing_while_limits_exist(
    limited: dict[str, Any],
) -> None:
    with limited["connect"]("ci_search") as conn:
        assert retrieval.authorize(conn, NO_ADDRESS).empty
        assert retrieval.list_email(conn, NO_ADDRESS)["results"] == []


def test_the_search_role_can_check_limits_but_not_read_them(limited: dict[str, Any]) -> None:
    with limited["connect"]("ci_search") as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("SELECT * FROM reader_restrictions").fetchall()
        conn.rollback()
        row = conn.execute("SELECT * FROM reader_restricted(%s)", (MORGAN,)).fetchone()
        assert row == {"mine": True, "anyone": True}


def test_limits_are_validated_audited_and_removable(limited: dict[str, Any]) -> None:
    with limited["connect"]() as conn:
        for reader, kind, value in (
            ("not-an-address", "mailbox", PAT),
            (MORGAN, "mailbox", "nobody"),
            (MORGAN, "domain", "no dot"),
            (MORGAN, "term", "ab"),
            (MORGAN, "everything", "x"),
        ):
            with pytest.raises(OperatorError):
                limit_reader(conn, reader, kind, value, "pytest", reason="private_business")
        with pytest.raises(OperatorError):
            limit_reader(conn, MORGAN, "mailbox", PAT, "pytest", reason="private_business")
        assert len(list_reader_limits(conn)) == 4
        for kind, value in (("mailbox", PAT), ("person", PAT), ("term", "banking details")):
            limit_reader(conn, MORGAN, kind, value, "pytest", revoke=True)
        actions = conn.execute(
            "SELECT action, count(*) AS n FROM audit_events"
            " WHERE action LIKE 'reader_limit_%' GROUP BY action ORDER BY action"
        ).fetchall()
        conn.commit()
    assert actions == [
        {"action": "reader_limit_add", "n": 4},
        {"action": "reader_limit_remove", "n": 3},
    ]
    seen = visible(limited, LIMITED)
    assert "Updated banking details for future payments" in seen
    assert "Example Ridge site meeting notes" not in seen  # the domain limit remains
    with limited["connect"]() as conn:
        limit_reader(conn, MORGAN, "domain", "example-gc.example", "pytest", revoke=True)
        conn.commit()
    assert visible(limited, LIMITED) == limited["before"]


def test_a_term_matches_body_text_as_a_phrase(limited: dict[str, Any]) -> None:
    other = Principal("other-oid", "other@finishessolutions.example", (READER_ALL,))
    lease = "Suite 210 lease renewal terms"  # the phrase is only in the body
    with limited["connect"]() as conn:
        limit_reader(conn, other.upn or "", "term", "annual escalations", "pytest", reason="test")
        conn.commit()
    assert lease not in visible(limited, other)
    assert lease in visible(limited, READER)
    with limited["connect"]() as conn:
        limit_reader(conn, other.upn or "", "term", "escalation annual", "pytest", reason="test")
        limit_reader(conn, other.upn or "", "term", "annual escalations", "pytest", revoke=True)
        conn.commit()
    assert lease in visible(limited, other)  # words out of order are not the phrase
