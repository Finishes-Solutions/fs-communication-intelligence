"""Risk-only access (migration 0029, Matthew 2026-11-13): an account with only
Communications.Reader.Risks sees project-risk emails, through the risk tools only,
with the fields chosen for it and never an email link or email text.
Synthetic tenant, fake backends."""

import asyncio
import json
from contextlib import AbstractContextManager
from typing import Any

import pytest
from test_pipeline import EMBEDDER, pipeline  # noqa: F401

from ci_core.db import Connection
from ci_core.operator import OperatorError, list_risk_fields, set_risk_fields
from ci_retrieval import mcp_server, reports, risk_access
from ci_retrieval import search as retrieval
from ci_retrieval.identity import (
    CONSOLE_ADMIN,
    READER_ALL,
    READER_RISKS,
    Principal,
)

AGENT = "svc-agent@finishessolutions.example"
RISKY = Principal("agent-oid", AGENT, (READER_RISKS,))
COUNTS = Principal("counts-oid", "counts@finishessolutions.example", (READER_RISKS,))
READER = Principal("reader-oid", "reader@finishessolutions.example", (READER_ALL,))
BOTH = Principal("both-oid", "both@finishessolutions.example", (READER_RISKS, READER_ALL))
LINK_KEYS = {"view_url", "download_url", "web_link", "communication_id", "mailbox"}
DAYS = 366  # the fixtures are dated October 2026


def risk_ids(p: dict[str, Any]) -> set[str]:
    with p["connect"]() as conn:
        return {
            r["id"]
            for r in conn.execute(
                "SELECT communication_id::text AS id FROM published_classifications"
                " WHERE yes_labels && ARRAY['project_risk']"
            ).fetchall()
        }


def keys(data: Any) -> set[str]:
    """Every key anywhere in a JSON-like value."""
    if isinstance(data, dict):
        return set(data) | {k for v in data.values() for k in keys(v)}
    if isinstance(data, list):
        return {k for v in data for k in keys(v)}
    return set()


def tool(p: dict[str, Any], who: Principal, name: str, args: dict[str, Any]) -> dict[str, Any]:
    def connector() -> AbstractContextManager[Connection]:
        return p["connect"]("ci_search")  # type: ignore[no-any-return]

    server = mcp_server.build_server(
        lambda: who, EMBEDDER, connector=connector, view_base="https://console.example"
    )
    result = asyncio.run(server.call_tool(name, args))
    data: dict[str, Any] = result.structured_content or json.loads(result.content[0].text)  # type: ignore[union-attr]
    return data


@pytest.fixture(scope="module")
def risky(pipeline: dict[str, Any]) -> dict[str, Any]:  # noqa: F811
    with pipeline["connect"]() as conn:
        set_risk_fields(conn, AGENT.upper(), ["summary", "date", "documents"], "pytest")
        conn.commit()
    return pipeline


def test_the_role_is_risk_only_unless_a_broader_role_is_held() -> None:
    assert RISKY.risk_only and COUNTS.risk_only
    assert not BOTH.risk_only and not READER.risk_only
    assert not Principal("a", "a@x.example", (READER_RISKS, CONSOLE_ADMIN)).risk_only


def test_sql_authorization_keeps_only_risk_emails(risky: dict[str, Any]) -> None:
    risks = risk_ids(risky)
    assert risks, "the fixture tenant has project-risk mail"
    with risky["connect"]("ci_search") as conn:
        everything = retrieval.list_email(conn, READER, retrieval.NO_FILTERS, 100)["results"]
        mine = retrieval.list_email(conn, RISKY, retrieval.NO_FILTERS, 100)["results"]
        assert len(mine) < len(everything)
        assert {r["communication_id"] for r in mine} <= risks
        other = next(
            r["communication_id"] for r in everything if r["communication_id"] not in risks
        )
        assert retrieval.get_email(conn, RISKY, other) is None
        assert retrieval.get_email(conn, READER, other) is not None
        # A broader role wins: Reader.All plus Reader.Risks reads everything.
        both = retrieval.list_email(conn, BOTH, retrieval.NO_FILTERS, 100)["results"]
        assert len(both) == len(everything)


def test_other_tools_refuse_risk_only_callers(risky: dict[str, Any]) -> None:
    risk = sorted(risk_ids(risky))[0]
    for name, args in (
        ("search_email", {"query": "lien"}),
        ("get_email", {"communication_id": risk}),
        ("get_attachment", {"communication_id": risk, "attachment_id": "x"}),
        ("list_email", {}),
        ("get_thread", {"communication_id": risk}),
        ("email_stats", {"group_by": "mailbox"}),
        ("get_file_links", {"communication_ids": [risk]}),
        ("find_knowledge", {}),
        ("get_profile", {"name": "example"}),
    ):
        assert tool(risky, RISKY, name, args) == risk_access.REFUSAL, name


def test_risk_overview_shows_only_the_chosen_fields_and_no_links(risky: dict[str, Any]) -> None:
    full = tool(risky, READER, "risk_overview", {"days": DAYS})
    shown = tool(risky, RISKY, "risk_overview", {"days": DAYS})
    assert shown["total"] == full["total"] > 0
    assert shown["matrix"] == full["matrix"]
    assert shown["fields_shown"] == ["date", "summary", "documents"]
    assert "by_mailbox" not in shown and "by_company" not in shown
    assert not keys(shown) & LINK_KEYS
    assert "https://console.example" not in json.dumps(shown)
    assert len(shown["items"]) == len(full["items"])
    for item in shown["items"]:
        assert set(item) == {"received_at", "risk_ai", "documents"}
        assert all(set(d) == {"name", "content_type"} for d in item["documents"])
    # The full reader's answer still carries links.
    assert all("view_url" in i for i in full["items"])


def test_counts_only_is_the_default(risky: dict[str, Any]) -> None:
    shown = tool(risky, COUNTS, "risk_overview", {"days": DAYS})
    assert shown["fields_shown"] == [] and "items" not in shown
    assert shown["total"] > 0 and shown["top_properties"] is not None
    listed = tool(risky, COUNTS, "list_actionable", {"hours": DAYS * 24})
    assert listed["actions"] == [] and listed["possible"] == []
    assert "risks" not in listed and listed["risk_count"] == shown["total"]


def test_list_actionable_returns_risks_only_with_the_chosen_fields(risky: dict[str, Any]) -> None:
    listed = tool(risky, RISKY, "list_actionable", {"hours": DAYS * 24, "limit": 2})
    assert listed["actions"] == [] and listed["possible"] == []
    assert 0 < len(listed["risks"]) <= 2
    assert not keys(listed) & LINK_KEYS
    assert all(set(r) == {"received_at", "risk_ai", "documents"} for r in listed["risks"])


def test_subject_sender_and_domain_appear_only_when_chosen(risky: dict[str, Any]) -> None:
    who = Principal("s-oid", "subject@finishessolutions.example", (READER_RISKS,))
    with risky["connect"]() as conn:
        set_risk_fields(conn, who.upn or "", ["subject", "sender", "sender_domain"], "pytest")
        conn.commit()
    shown = tool(risky, who, "risk_overview", {"days": DAYS})
    assert "by_company" in shown
    assert all(set(i) == {"subject", "sender", "sender_domain"} for i in shown["items"])
    assert all(
        i["sender_domain"] == i["sender"].split("@")[1] for i in shown["items"] if i["sender"]
    )


def test_field_settings_are_validated_audited_and_private(risky: dict[str, Any]) -> None:
    with risky["connect"]() as conn:
        with pytest.raises(OperatorError):
            set_risk_fields(conn, AGENT, ["body"], "pytest")
        with pytest.raises(OperatorError):
            set_risk_fields(conn, "not-an-address", [], "pytest")
        conn.rollback()
        rows = {r["reader_address"]: r["fields"] for r in list_risk_fields(conn)}
        assert rows[AGENT] == ["date", "summary", "documents"]
        actions = {r["action"] for r in conn.execute("SELECT action FROM audit_events").fetchall()}
        assert "risk_fields_set" in actions
    with risky["connect"]("ci_search") as conn:
        assert risk_access.fields_for(conn, RISKY) == {"date", "summary", "documents"}
        assert risk_access.fields_for(conn, Principal("x", None, (READER_RISKS,))) == frozenset()
        with pytest.raises(Exception, match="permission denied"):
            conn.execute("SELECT * FROM reader_risk_fields").fetchall()


def test_reports_still_apply_to_full_readers(risky: dict[str, Any]) -> None:
    with risky["connect"]("ci_search") as conn:
        found = reports.risk_overview(conn, READER, days=DAYS)
    assert found["items"] and "web_link" in found["items"][0]
