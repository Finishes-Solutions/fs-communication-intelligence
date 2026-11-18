"""Outlook links (web_link) are returned only when they open for the reader: the
copy in the reader's own mailbox, never another person's copy (2026-11-18).

Change Order 8 sits in Pat's, Morgan's and the AP mailbox (fixtures 14a-c)."""

from typing import Any

from test_pipeline import EMBEDDER, MORGAN, PAT, pipeline  # noqa: F401

from ci_retrieval import reports
from ci_retrieval import search as retrieval
from ci_retrieval.identity import READER_ALL, Principal

TITLE = "Change Order 8 - Building D plumbing reroute"
PAT_LINK = "syn-0014"
MORGAN_LINK = "syn-0015"
OUTSIDER = Principal("reader-oid", "reader@finishessolutions.example", (READER_ALL,))


def _copy(p: dict[str, Any], mailbox: str) -> str:
    with p["connect"]() as conn:
        row = conn.execute(
            "SELECT pc.id::text AS id FROM published_communications pc"
            " JOIN published_email_messages em ON em.communication_id = pc.id"
            " WHERE pc.title = %s AND em.mailbox_address = %s",
            (TITLE, mailbox),
        ).fetchone()
    assert row, mailbox
    return str(row["id"])


def _email(p: dict[str, Any], who: Principal, cid: str) -> dict[str, Any]:
    with p["connect"]("ci_search") as conn:
        found = retrieval.get_email(conn, who, cid, 1000)
    assert found is not None
    return found


def test_owner_gets_their_own_copy_link(pipeline: dict[str, Any]) -> None:  # noqa: F811
    pat = Principal("pat-oid", PAT, (READER_ALL,))
    assert PAT_LINK in (_email(pipeline, pat, _copy(pipeline, PAT))["web_link"] or "")


def test_reader_on_the_email_gets_the_link_to_their_own_copy(
    pipeline: dict[str, Any],  # noqa: F811
) -> None:
    morgan = Principal("morgan-oid", MORGAN, (READER_ALL,))
    link = _email(pipeline, morgan, _copy(pipeline, PAT))["web_link"]
    assert link and MORGAN_LINK in link


def test_reader_without_a_copy_gets_no_link_anywhere(
    pipeline: dict[str, Any],  # noqa: F811
) -> None:
    assert _email(pipeline, OUTSIDER, _copy(pipeline, PAT))["web_link"] is None
    with pipeline["connect"]("ci_search") as conn:
        listed = retrieval.list_email(conn, OUTSIDER, retrieval.NO_FILTERS, 100)["results"]
        found = retrieval.search(conn, OUTSIDER, "change order plumbing", EMBEDDER)["results"]
        rows = reports.report_rows(conn, OUTSIDER, "messages")["rows"]
        risks = reports.risk_overview(conn, OUTSIDER, days=366)["items"]
    assert listed and found and rows and risks
    assert all(i["web_link"] is None for i in listed + found + rows + risks)


def test_no_sign_in_address_means_no_link(pipeline: dict[str, Any]) -> None:  # noqa: F811
    anonymous = Principal("x-oid", None, (READER_ALL,))
    assert _email(pipeline, anonymous, _copy(pipeline, PAT))["web_link"] is None
